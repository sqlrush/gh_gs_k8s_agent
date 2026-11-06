"""参数基线 —— 纯数据 + 单位归一，不连库、不判定。

**这份基线刻意不是一张「推荐值大全」。** 绝大多数性能参数的"合理值"取决于机器
规格、业务形态和数据量，而这个工具**看不到** CPU 核数、物理内存、磁盘类型。
对着一个看不到的事实给"建议值"，就是在编依据 —— 哪怕编得很像。

所以只收三类**客观上就是错的**情形：

  ① **自相矛盾**：两个参数放在一起必然有一个不生效，或互相打架。
     例：`archive_mode=on` 但归档目标空着 —— 归档必然失败；
         `max_connections` 超过线程池大小 —— 多出来的那部分永远用不上。
  ② **会静默丢数据/丢可观测性**：`fsync=off`、`zero_damaged_pages=on`、
     `logging_collector=off` 这类，与机器规格无关，开了就是错。
  ③ **按本实例自己的预算**算出来的超卖：用 `max_process_memory`(openGauss
     自己的内存预算，可读)做分母，不猜物理内存。

每条都带 `why`(为什么是问题)与 `how`(怎么处置,含要不要重启)。没有依据的
"建议调到 X" 一律不写 —— 这正是本仓库其他 skill 一直守的那条线。
"""
from __future__ import annotations

from typing import Dict, NamedTuple, Optional

# pg_settings.unit → 字节/毫秒的倍数。**不按 unit 归一会把 4GB 读成 524288。**
# openGauss 上实测出现过的单位:'' / kB / 8kB / ms / s / min。
_BYTE_UNITS = {"B": 1, "kB": 1024, "8kB": 8 * 1024, "16kB": 16 * 1024,
               "32kB": 32 * 1024, "MB": 1024 * 1024, "GB": 1024 ** 3}
_TIME_UNITS = {"ms": 1, "s": 1000, "min": 60 * 1000, "h": 3600 * 1000,
               "d": 86400 * 1000}

TRUE_WORDS = ("on", "true", "t", "yes", "1")


class Setting(NamedTuple):
    """一个参数的现值。raw 是原样字符串，num 是按单位归一后的数(字节 / 毫秒)。"""
    name: str
    raw: str
    unit: str
    num: Optional[float]
    boot: str
    reset: str
    context: str
    source: str
    category: str
    desc: str

    @property
    def on(self) -> bool:
        return self.raw.strip().lower() in TRUE_WORDS

    @property
    def changed_from_default(self) -> bool:
        return self.raw.strip() != self.boot.strip()

    @property
    def self_inflicted(self) -> bool:
        """这个值是不是**本次连接自己 SET 出来的**。

        `pg_settings.setting` 是**会话值**。取数层会给自己的会话设
        `statement_timeout`(保护超时),于是体检读到 30000 —— 而全局配置其实是 0。
        不区分这一点,「全局没有语句超时」这条就永远测不出来:工具观测到了自己。
        这和 vacuum 的 oldest_xmin 脚本里 `pid <> pg_backend_pid()` 那个
        已记录的坑是同一类错(工具自证)。
        """
        return self.source.strip().lower() in ("session", "client")

    @property
    def effective(self) -> str:
        """该拿来判定的值:自己 SET 过的话用 reset_val(= RESET 会恢复成的值,
        也就是全局配置值),否则用会话值。

        **只在 source='session' 时才信 reset_val。** 实测:`source='default'` 的
        参数里有 108 个 reset_val 没有有效值(见 audit.py 顶部那段说明),
        无条件用 reset_val 会引入一百多条噪音。
        """
        return self.reset if self.self_inflicted and self.reset else self.raw

    @property
    def effective_num(self) -> Optional[float]:
        return normalize(self.unit, self.effective)


def normalize(unit: str, raw: str) -> Optional[float]:
    """按 pg_settings.unit 把值归一成字节或毫秒。非数值返回 None。

    **返回 None 而不是 0**:`statement_timeout=0` 是「不限制」这个明确含义,
    而「值解析不出来」是另一回事,混成 0 会让判定把解析失败当成「已配置为无限制」。
    """
    text = (raw or "").strip()
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    u = (unit or "").strip()
    if not u:
        return v
    if u in _BYTE_UNITS:
        return v * _BYTE_UNITS[u]
    if u in _TIME_UNITS:
        return v * _TIME_UNITS[u]
    return v        # 未知单位:原样返回,判定侧只在明确知道单位时才用 num


def to_settings(rows) -> Dict[str, Setting]:
    """取数结果 → {参数名: Setting}。"""
    out: Dict[str, Setting] = {}
    for r in rows or []:
        name = str(r.get("name") or "")
        if not name:
            continue
        raw = str(r.get("setting") or "")
        unit = str(r.get("unit") or "")
        out[name] = Setting(
            name=name, raw=raw, unit=unit, num=normalize(unit, raw),
            boot=str(r.get("boot_val") or ""), reset=str(r.get("reset_val") or ""),
            context=str(r.get("context") or ""), source=str(r.get("source") or ""),
            category=str(r.get("category") or ""), desc=str(r.get("short_desc") or ""))
    return out


# 改这些参数需要重启实例(pg_settings.context='postmaster')。处置建议里必须说清 ——
# 不说,DBA 会以为 reload 就行,改完发现没生效。实际取值从 context 列读,
# 这里只是给说明用的中文。
CONTEXT_CN = {
    "postmaster": "需重启实例",
    "sighup": "reload 即可生效",
    "superuser": "会话级可改（超级用户）",
    "user": "会话级可改",
    "backend": "新连接生效",
    "internal": "编译期固定，改不了",
}


def how_to_apply(s: Setting) -> str:
    return CONTEXT_CN.get(s.context, s.context or "生效方式未知")
