"""事务号冻结水位的判定 —— 纯函数，不连库。

**先纠正一个很容易搬错的前提。** PostgreSQL 上 frozenxid 追到 2^31 会让库
**强制停写**（"not accepting commands to avoid wraparound data loss"），抢救要停机。
这个结论**不能照搬到本内核**：实测 openGauss 5.0.3 上

  · `autovacuum_freeze_max_age` 默认 **4000000000**，`vacuum_freeze_min_age` 默认
    **2000000000** —— 两个都**超过 2^31**（2147483648）。PG 不允许这样的取值；
  · 没有 `xid_stop_limit` / `xid_warn_limit` 这两个 GUC；
  · `datfrozenxid64` 是 64 位列。

也就是说这个内核用 64 位事务号，不存在 PG 那种 2^31 的硬墙。所以本模块**不说
「库要停写」** —— 那会把一个 🟡 的事报成 🔴，而虚报一次之后，真正该紧张的时候
没人会再信。

**那这项还查什么（真实、可验证的后果）：**
  ① 水位接近 `autovacuum_freeze_max_age` 时，autovacuum 会对大表触发**全表冻结
     扫描**。它不看业务高峰、不可预测时点，在大表上就是一场 IO 风暴 —— 现场
     表现是"某天夜里库突然很慢，但没有慢 SQL、没有锁"。
  ② 冻结水位一直不推进，clog / csnlog 就一直不能截断，持续占盘。

阈值用**这个库自己配的** `autovacuum_freeze_max_age` 做分母，不是写死的常数：
客户把它调小（常见做法，为了让冻结更早更分散）时，占比才真正反映"离强制冻结
还有多远"。分母取不到或 <= 0 时**不判**（返回空），而不是退回一个猜的常数 ——
分母错了，占比就是个没有意义的数字。
"""
from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "common" / "finding.py").exists():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from common.finding import Finding, Severity  # noqa: E402
from common.grmp.values import as_float, is_null  # noqa: E402

DIMENSION = "Freeze Age"
CODE = "VACUUM_FREEZE_AGE"

# 占 autovacuum_freeze_max_age 的百分比。
#   60% —— 开始留意：离强制冻结还有距离，但趋势该看了
#   85% —— 告警：全表冻结扫描随时可能被触发，该主动在低峰期手工 VACUUM FREEZE
# 不设 CRITICAL 档：这个内核上它不会导致停写，报 🔴 是虚报（见模块说明）。
FREEZE_PCT_NOTICE = 60.0
FREEZE_PCT_WARN = 85.0

# 一次最多报几个库 —— 一个实例上几十个库同时偏高时，刷满报告反而看不见重点。
MAX_FINDINGS = 3


def freeze_pct(row: dict) -> float:
    """这个库的水位占 max_age 的百分比。算不出来返回 -1（调用方跳过，不当 0）。

    返回 0 会让"算不出来"和"刚冻结过、水位很低"这两个相反的事实混在一起。
    """
    age = as_float(row.get("freeze_age"))
    cap = as_float(row.get("max_age"))
    if is_null(row.get("freeze_age")) or is_null(row.get("max_age")):
        return -1.0
    if cap is None or cap <= 0 or age is None or age < 0:
        return -1.0
    return 100.0 * age / cap


def _severity(pct: float):
    if pct >= FREEZE_PCT_WARN:
        return Severity.WARN, FREEZE_PCT_WARN
    if pct >= FREEZE_PCT_NOTICE:
        return Severity.NOTICE, FREEZE_PCT_NOTICE
    return None, 0.0


_EVIDENCE = ("pg_database.datfrozenxid64 对当前快照 xmin 的差值 %d，"
             "占 autovacuum_freeze_max_age(%d) 的 %.1f%%。"
             "到 100%% 时 autovacuum 会对大表做全表冻结扫描（不择时的 IO 风暴）；"
             "本内核是 64 位事务号，**不会**像 PostgreSQL 那样到 2^31 就停写。")


def judge(rows: list) -> list:
    """每个库一行 → findings。按水位从高到低，最多 MAX_FINDINGS 条。"""
    scored = []
    for row in rows or []:
        pct = freeze_pct(row)
        if pct < 0:
            continue
        sev, thr = _severity(pct)
        if sev is None:
            continue
        scored.append((pct, row, sev, thr))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for pct, row, sev, thr in scored[:MAX_FINDINGS]:
        out.append(Finding(
            DIMENSION, CODE, sev,
            "库 %s 的冻结水位" % (row.get("datname") or "?"),
            "%.1f%%" % pct, ">%.0f%%" % thr,
            _EVIDENCE % (int(as_float(row.get("freeze_age")) or 0),
                         int(as_float(row.get("max_age")) or 0), pct)))
    return out


def worst_pct(rows: list) -> float:
    """最高的那个库的水位，给报告抬头用。一个都算不出来时返回 -1。"""
    vals = [freeze_pct(r) for r in rows or []]
    vals = [v for v in vals if v >= 0]
    return max(vals) if vals else -1.0
