"""慢 SQL(S1–S7):七条写法不同、慢法不同的 SQL,SQL 原文在 sql/slow/*.sql。

**两类分工,缺一不可:**
  · S1 / S2 **一直跑不完** → 造的是「现在有一条 SQL 已经跑了十几分钟」。
    看它的是 health / lockwait / waitevent —— 它们读 pg_stat_activity,只看得到正在执行的。
  · S3–S6 **在窗口内跑完** → 造的是「执行历史里有几分钟的 SQL」。
    看它的是 topsql / slowsql / wdr —— 它们读 dbe_perf 的统计,**只统计已完成的执行**,
    一直没跑完的 S1 / S2 在那里一行都没有。
  · S7 高频 → 让「总耗时榜首」和「单条最慢」不是同一条。

**规模(cap)为什么要校准:** 这几条 SQL 的代价是「外层行数 × 内层全表扫描」,机器快一倍
耗时就减一半。写死一个数,换台机器就变成「两秒跑完」或者「一天都跑不完」。所以 cap 由
`calibrate` 实测后写进 ~/.faultlab/caps.json;没校准过就用下面的默认值(og5 上实测出来的)。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, NamedTuple

from .. import config as C
from ..db import Conn, LabError
from ..session import until
from .base import Base, register


class Spec(NamedTuple):
    sql: str            # sql/slow/ 下的文件名
    cap: int            # 默认规模(外层行数上限)
    work_mem: str       # 会话级 work_mem
    loop: bool          # 跑完接着再跑(让历史里多几条),还是只跑一次
    finishes: bool      # 设计上会不会在窗口内跑完 —— probe 据此判断是否正常
    target_s: int       # 设计目标耗时,calibrate 按它调 cap


SPECS: Dict[str, Spec] = {
    # S1 的规模故意给大。实测(2026-10-17,og5 空闲时)每行约 25 毫秒:
    # 4000 行 102 秒就跑完了 —— 窗口中途跑完,「已经跑了十几分钟的 SQL」就没了。
    # 10 万行约 42 分钟,窗口内铁定跑不完,这才是它要造的现象。
    "S1": Spec("s1.sql", 100_000, "1MB", False, False, 0),
    # S2 设计成「跑不完」。第一版取编码前 8 位只分出约 2000 组,LIKE 嵌套循环才 2000 × 20 万,
    # 一次 1~2 分钟就跑完了(2026-10-24 全量跑跑完 13 次)—— 手册、expected.yaml 却都按「一直 active」
    # 写的。现在按完整编码归集(20 万组),嵌套循环 20 万 × 20 万,窗口里铁定跑不完;
    # 落盘照旧发生在前半段的哈希聚合里,之后外层物化结果放不进 4MB work_mem,一直在读临时文件。
    "S2": Spec("s2.sql", 1_000_000, "4MB", False, False, 0),
    "S3": Spec("s3.sql", 24_000, "16MB", True, True, 480),
    "S4": Spec("s4.sql", 9_000, "16MB", True, True, 180),
    "S5": Spec("s5.sql", 60_000, "16MB", True, True, 480),
    # S6 的目标不是「单条跑很久」(S1/S2/S3/S5 已经覆盖),而是**落盘**。
    #
    # **work_mem 从 64kB 调到 2MB,是为了保护宿主机,不是为了跑快。** 64kB 时每个排序要
    # 切成极多的临时文件反复归并,四个排序叠起来文件数非常可观;2026-10-17 第三次全量跑时
    # 宿主机(macOS)的文件表被打满 —— 先是沙箱里的命令报 "Too many open files in system",
    # 紧接着 **og5 容器自己重启了**(postmaster 启动时间变新,好在只有它重启,
    # k8s 那些容器都没事)。2MB 下照样是 external merge、照样落几百 MB,
    # 但临时文件数量低一个量级。宁可少几个文件,也不能把别人的测试环境搞挂。
    "S6": Spec("s6.sql", 1_000_000, "2MB", True, True, 30),
    # S7 的 cap 不是行数上限,而是用来算查哪个行方编码(见 sql_for)。给 0 会被规模校验
    # 挡住,场景直接起不来(2026-10-17 全量跑时就是这样,S7 一次都没跑上)。
    "S7": Spec("s7.sql", 123_457, "16MB", True, True, 2),
}

CAPS_FILE = C.STATE_DIR / "caps.json"


def load_caps() -> Dict[str, int]:
    """校准过的规模。文件坏了不算致命 —— 用默认值接着跑,但要说一声。"""
    caps = {code: spec.cap for code, spec in SPECS.items()}
    if not CAPS_FILE.exists():
        return caps
    try:
        saved = json.loads(CAPS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("[faultlab] %s 读不出来(%s),慢 SQL 用默认规模" % (CAPS_FILE, exc))
        return caps
    for code, value in (saved or {}).items():
        if code in caps and isinstance(value, int) and value > 0:
            caps[code] = value
    return caps


def save_caps(caps: Dict[str, int]) -> None:
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    CAPS_FILE.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_comments(text: str) -> str:
    """去掉整行的 `--` 注释再发给库。

    **注释写的是成因**(「没索引」「故意不建」……),原样发过去就进了语句统计:
    dbe_perf.statement / WDR 的 SQL 原文、大盘与报告里的 SQL 全文都带着它 —— 2026-10-18 那轮测试里,
    模型直接照抄注释里的成因当诊断结论,测的就不是诊断能力了。注释只留在仓库的 .sql 文件里给人看。
    只认整行注释:这几个文件里没有行内注释和块注释(有的话照样发过去,宁可泄漏也不把 SQL 剥坏)。
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--")).strip() + "\n"


def sql_for(code: str, cap: int) -> str:
    """读 SQL 并把 {cap} 换成实际数字。

    **cap 必须是整数** —— 它直接拼进 SQL。这里是本工具唯一一处把外部数值拼进 SQL 的地方,
    所以校验放在这一个函数里,别处都不许自己拼。
    """
    spec = SPECS.get(code)
    if spec is None:
        raise LabError("没有 %s 的慢 SQL 定义" % code)
    if not isinstance(cap, int) or cap <= 0 or cap > 10_000_000:
        raise LabError("%s 的规模 cap 不合法:%r(要 1~1000 万的整数)" % (code, cap))
    path = C.SQL_DIR / "slow" / spec.sql
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LabError("读不到 %s:%s" % (path, exc)) from exc
    text = strip_comments(text)
    # S7 查的是编码值,不是行数上限 —— 它的 {code} 占位符另算
    text = text.replace("{cap}", str(cap))
    return text.replace("{code}", "'%010d'" % (cap % 200_000 + 1))


@register("S1", "S2", "S3", "S4", "S5", "S6", "S7")
class SlowQuery(Base):
    """一条慢 SQL = 一条会话,按 spec 决定跑一次还是循环跑。"""

    group = "slow"

    def start(self) -> None:
        spec = SPECS[self.code]
        cap = load_caps()[self.code]
        sql = sql_for(self.code, cap)
        self.log("规模 cap=%s,work_mem=%s,%s" % (
            cap, spec.work_mem, "循环跑" if spec.loop else "跑一次(预期跑不完)"))
        # 会跑完的那几条按排期停时**不打断最后一轮**(drain):打断了它就进不了执行历史,
        # 而 topsql / slowsql / wdr 要看的正是执行历史。窗口结束时一律**终止会话**而不是取消语句
        # (terminate):被取消的语句也会记进执行历史,跑不完的 S1/S2 就不再「历史里没有」。
        self.worker("q", lambda c, stop: self._run(c, stop, spec, sql), drain=spec.finishes,
                    terminate=True)

    def _run(self, c: Conn, stop: threading.Event, spec: Spec, sql: str) -> None:
        c.set("work_mem", spec.work_mem)
        # **一条慢 SQL 只用一个 CPU。** openGauss 默认会给这类查询开 SMP 并行
        # (计划里的 `Streaming(dop: 2/2)`),一条会话就吃掉两个核 —— 排期表里按
        # 「一个场景一个核」算的 CPU 预算就全错了,og5 只有 4 个核,很快变成全局 CPU 排队,
        # 那时每个场景都慢,反而分不出是谁造成的。
        c.set("query_dop", "1")
        if spec.loop:
            until(stop, lambda: self._once(c, sql), period_s=0.0)
        else:
            self._once(c, sql)

    # 循环跑的那几条不能每跑完一次就写一行时间线:S7 单次只要零点几秒,20 分钟能跑几千次,
    # 时间线会被它刷掉(实测 2 分钟就写了 300 多行,报告直接没法看)。
    # 所以只记「慢到值得一提的」和「每隔若干次的一条小结」,次数本身由计数器与 probe 负责。
    LOG_IF_SLOWER_S = 5.0
    LOG_EVERY = 1000

    def _once(self, c: Conn, sql: str) -> None:
        t0 = time.time()
        c.query(sql)
        took = time.time() - t0
        self.ctx.bump("%s_done" % self.code)
        self.ctx.bump("%s_seconds" % self.code, int(took))
        n = self.ctx.snapshot_counters().get("%s_done" % self.code, 0)
        if took >= self.LOG_IF_SLOWER_S:
            self.log("跑完一次,%.0f 秒" % took)
        elif n % self.LOG_EVERY == 0:
            self.log("已跑完 %d 次(单次约 %.1f 秒)" % (n, took))

    def probe(self, admin: Conn) -> str:
        spec = SPECS[self.code]
        # sessionid <> 0:线程池的空闲条目也带着 application_name,它们的 query_start 是空,
        # 不过滤的话会先撞上那种条目,算出来的「已跑多少秒」就成了 None。
        rows = admin.query(
            "SELECT state, extract(epoch FROM now() - query_start)::int "
            "  FROM pg_stat_activity WHERE application_name = %s AND sessionid <> 0",
            (C.APP_PREFIX + self.code + "/q",))
        done = self.ctx.snapshot_counters().get("%s_done" % self.code, 0)
        if not rows:
            return "没有会话(已结束;跑完 %d 次)" % done
        state, secs = rows[0][0], int(rows[0][1] or 0)
        if state != "active":
            return "会话在但没在跑(state=%s);跑完 %d 次" % (state, done)
        if spec.finishes:
            return "正在跑第 %d 次,已 %d 秒(目标约 %d 秒)" % (done + 1, secs, spec.target_s)
        return "已连续执行 %d 秒(设计上不会跑完)" % secs
