"""表膨胀的**估算** —— 纯函数，不连库。

**这是估算，不是测量。** `pgstattuple` 在这个内核上没装（实测
`pg_available_extensions` 里查不到），所以只能按「行数 × 平均行宽」推算应占多少页，
再和真实页数比。误差来源：fillfactor、已删列、TOAST 外联、变长字段的实际分布。
所以阈值取得宽，呈现时一律写明"估算"，并且**不给回收多少空间的承诺**。

**必须先做陈旧校验，否则会输出一个很严重的假数字。** `relpages` / `reltuples` 来自
最后一次 ANALYZE/VACUUM。og5 实测：`statement_history` 的 `relpages=428232`，而
真实只有 14 页（表被清空过但没重新 analyze）。拿这个 reltuples 去估算，会算出天文数字
的「膨胀率」—— 它看起来是最该处理的一张表，而真相是统计陈旧、表其实是空的。
所以 `estimate()` 先比 relpages 与 real_pages：差得过多就**拒绝估算**，返回
原因而不是数字。这一条是本模块存在的主要理由。

**死元组比例 ≠ 膨胀。** 既有的 `VACUUM_DEAD_RATIO` 回答「现在有多少可回收的死元组」，
这里回答「这张表占的空间比它该占的多多少」—— 一张刚 VACUUM 过的表死元组是 0，
但空间已经膨出去了（VACUUM 不还盘，只标记可重用）。两条都要有，处置也不同：
死元组高 → VACUUM；估算膨胀高而死元组低 → 要 `VACUUM FULL` / 重建（都要锁表）。
"""
from __future__ import annotations

import pathlib
import sys
from typing import NamedTuple, Optional

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "common" / "finding.py").exists():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from common.finding import Finding, Severity  # noqa: E402
from common.grmp.values import as_float, is_null  # noqa: E402

DIMENSION = "Bloat (estimated)"
CODE = "VACUUM_BLOAT_ESTIMATED"

PAGE_BYTES = 8192
# 每页的页头 + 行指针开销。24 字节页头，行指针 4 字节/行已算进每行开销里。
PAGE_OVERHEAD = 24
# 每行的元组头开销（HeapTupleHeader 23 字节 + 对齐 + 行指针 4）。
ROW_OVERHEAD = 28

# relpages 与真实页数相差超过这个倍数 → 统计陈旧，拒绝估算。
# 1.5 倍：正常的增删改不会让统计偏到这个程度；偏到了就是很久没 ANALYZE。
STALE_RATIO = 1.5

# 膨胀率阈值。比死元组那套宽得多 —— 这是估算，窄阈值只会制造假告警。
BLOAT_PCT_NOTICE = 50.0
BLOAT_PCT_WARN = 100.0      # 占了应占空间的两倍以上

# 小表不判：几十 MB 的表膨胀一倍也只是几十 MB，而重建要锁表。
MIN_BYTES = 256 * 1024 * 1024

MAX_FINDINGS = 5


class Est(NamedTuple):
    """一张表的估算结果。pct 为 None 表示估不了，reason 说明为什么。"""
    schema: str
    table: str
    real_bytes: int
    expect_bytes: Optional[int]
    pct: Optional[float]
    reason: str

    @property
    def name(self) -> str:
        return "%s.%s" % (self.schema, self.table)


def estimate(row: dict) -> Est:
    """一行输入 → 估算。估不了时 pct=None 并给出中文原因（绝不编一个数字）。"""
    schema = str(row.get("schema") or "")
    table = str(row.get("table") or "")
    real_bytes = int(as_float(row.get("real_bytes")) or 0)
    real_pages = as_float(row.get("real_pages"))
    relpages = as_float(row.get("relpages"))
    reltuples = as_float(row.get("reltuples"))
    width = None if is_null(row.get("avg_row_width")) else as_float(row.get("avg_row_width"))

    def no(reason):
        return Est(schema, table, real_bytes, None, None, reason)

    if width is None or width <= 0:
        return no("这张表没有列统计（从没 ANALYZE 过），估不出每行多宽")
    if reltuples is None or reltuples < 0:
        return no("取不到行数估计")
    if real_pages is None or real_pages <= 0:
        return no("取不到真实页数")
    if relpages is None or relpages <= 0:
        return no("统计里的页数为 0（从没 ANALYZE 过），不能用来做估算")
    # **陈旧校验**：统计里的页数与真实页数差得过多 → reltuples 也不可信。
    hi, lo = max(relpages, real_pages), min(relpages, real_pages)
    if hi > lo * STALE_RATIO:
        return no("统计已陈旧（统计里 %d 页、实际 %d 页），reltuples 不可信，"
                  "估算会给出假数字；先 ANALYZE 这张表再看"
                  % (int(relpages), int(real_pages)))

    per_page = max(1.0, (PAGE_BYTES - PAGE_OVERHEAD) / (width + ROW_OVERHEAD))
    expect_pages = reltuples / per_page
    if expect_pages <= 0:
        return no("按行数算出的应占页数为 0（表可能是空的）")
    expect_bytes = int(expect_pages * PAGE_BYTES)
    pct = 100.0 * (real_pages - expect_pages) / expect_pages
    return Est(schema, table, real_bytes, expect_bytes, pct, "")


def _sev(pct: float):
    if pct >= BLOAT_PCT_WARN:
        return Severity.WARN, BLOAT_PCT_WARN
    if pct >= BLOAT_PCT_NOTICE:
        return Severity.NOTICE, BLOAT_PCT_NOTICE
    return None, 0.0


def judge(rows: list) -> list:
    """findings。只报大表、只报估得出来的，按膨胀率倒序，最多 MAX_FINDINGS 条。"""
    ests = [estimate(r) for r in rows or []]
    hits = []
    for e in ests:
        if e.pct is None or e.real_bytes < MIN_BYTES:
            continue
        sev, thr = _sev(e.pct)
        if sev is None:
            continue
        hits.append((e.pct, e, sev, thr))
    hits.sort(key=lambda t: t[0], reverse=True)
    out = []
    for pct, e, sev, thr in hits[:MAX_FINDINGS]:
        out.append(Finding(
            DIMENSION, CODE, sev, "%s 估算膨胀" % e.name,
            "%.0f%%" % pct, ">%.0f%%" % thr,
            "真实 %s、按行数×平均行宽估算应占 %s —— **这是估算不是测量**"
            "（本内核没装 pgstattuple，误差来自 fillfactor / 已删列 / TOAST）。"
            "死元组比例低而这一项高，说明空间已经膨出去、VACUUM 不还盘："
            "要 VACUUM FULL 或重建，两者都锁表，须安排窗口。"
            % (_mb(e.real_bytes), _mb(e.expect_bytes or 0))))
    return out


def _mb(n: int) -> str:
    f = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if f < 1024 or unit == "T":
            return "%.1f%s" % (f, unit)
        f /= 1024
    return "%.1fT" % f


def skipped(rows: list) -> list:
    """估不了的那些（带原因），报告里要如实列出 —— 不列等于让人以为都估过了。"""
    return [e for e in (estimate(r) for r in rows or []) if e.pct is None]
