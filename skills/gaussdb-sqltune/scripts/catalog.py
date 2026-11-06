"""把证据包里的采集结果整理成可按名字查的目录，并守住两道前置门。

两道门都是「不满足就整段作废」，不是「打个折扣继续算」：

**门一：名字歧义。** evidence.extract_tables() 给的是**不带 schema** 的表名，
采集脚本也是 `WHERE relname IN (...)`。两个 schema 下有同名表时会返回两行，
而代码无从知道 SQL 用的是哪一张。此时若「取第一行」，推演会拿另一张表的
页数和统计信息算出一个精确的错数 —— 不报错、不告警。所以歧义即拒绝。

**门二：统计信息陈旧。** 推演的全部输入都是上次 ANALYZE 时冻结的快照。
表在那之后翻了十倍，这些数不会报错，只会让推演算出一个精确的错数。

门二的判据**只用不会被独立重置的信号**（relpages vs curpages，以及 pg_stats
里有没有行），不用 pg_stat_user_tables 的 last_analyze / n_live_tup ——
后两者会被 pg_stat_reset() 清掉，而 ANALYZE 的成果存在 pg_statistic 里不受影响。
用它们当门会把统计完好的表判成「从未分析」。og5 上实测过，见 freshness()。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence


class CatalogError(Exception):
    """目录不足以支撑推演。调用方必须停止，不能退化成「尽量算」。"""


# 冻结页数与实时页数偏离多少算陈旧。10% 是个工程判断：再小会被日常写入的
# 正常增长频繁触发，再大就失去意义。这个阈值必须**写进报告**，否则读的人
# 无从判断「陈旧」是按什么标准说的。
STALE_DRIFT_THRESHOLD = 0.10


@dataclass(frozen=True)
class FreshnessVerdict:
    table: str
    fresh: bool
    reason: str
    relpages: float           # pg_class 冻结的页数
    cur_pages: float          # 存储层的实时页数
    drift: Optional[float]    # 页数相对偏离，无法计算时 None
    stat_columns: int         # pg_stats 里这张表有几列有统计信息
    reltuples: float
    live_tuples: float        # 统计收集器的值，**仅供参考**
    last_analyze: str         # 同上
    last_autoanalyze: str


class Catalog:
    """按名字查表/索引/列统计。构造时就把歧义挡掉。"""

    def __init__(self, tables, indexes, columns, freshness):
        self._tables = _unique_by(tables, lambda t: t.name, "表")
        self._indexes = _unique_by(indexes, lambda i: i.name, "索引")
        self._columns: Dict[tuple, object] = {}
        for col in columns:
            self._columns[(col.table, col.column)] = col
        self._freshness = _unique_by(freshness, lambda f: f.table, "统计新鲜度行")

    # --- 查 ---------------------------------------------------------------

    def table(self, name: str):
        found = self._tables.get(_norm(name))
        if found is None:
            raise CatalogError(
                "表 %r 不在证据包里 —— 它可能是视图、CTE 或别名，"
                "也可能是表名提取漏了。无论哪种，都拿不到页数/行数，不能复算。"
                % name)
        return found

    def index(self, name: str):
        found = self._indexes.get(_norm(name))
        if found is None:
            raise CatalogError("索引 %r 不在证据包里" % name)
        return found

    def column(self, table: str, column: str):
        found = self._columns.get((_norm(table), _norm(column)))
        if found is None:
            raise CatalogError(
                "列 %s.%s 没有统计信息（pg_stats 里没有这一行）。"
                "该列可能从未被 ANALYZE 覆盖 —— 缺 correlation 就没法算回表 IO。"
                % (table, column))
        return found

    def total_table_pages(self) -> float:
        """本查询涉及的所有表的页数之和 —— Mackert-Lohman 的缓存摊分基数。

        用**实时**块数：规划器算缓存摊分时用的就是它，与 cost_index 里传的
        baserel->pages 是同一个来源。混用冻结值会让摊分基数偏小，b 偏大，
        于是「缓存装得下」被判成立，索引扫描的 IO 被低估。
        """
        return float(sum(t.cur_pages for t in self._tables.values()))

    # --- 门二 -------------------------------------------------------------

    def freshness(self, name: str) -> FreshnessVerdict:
        """判据只用**不会被独立重置**的信号。

        原先拿 pg_stat_user_tables 的 last_analyze / n_live_tup 当门，og5 上
        实测发现它会误判：gsbench.fact_sales 报 last_analyze=never、n_live_tup=0，
        但 pg_stats 里实实在在有 8 列统计信息 —— 计数器被 pg_stat_reset() 清过，
        或统计收集器的数据在重启时没落盘。**统计完好的表被判成从未分析，
        整个推演白做。**

        改用 relpages（pg_class 冻结值）与 curpages（存储层实时值）比：两者都
        不受统计收集器影响，而且这正是规划器换算行数用的那个比值 ——
        页数没变，规划器用的行数就等于 reltuples，冻结快照就是当前现实。

        统计收集器那两个值降级为**参考信息**，照样呈现，但不参与判定。
        """
        table = self.table(name)
        row = self._freshness.get(_norm(name))
        stat_columns = sum(1 for (t, _c) in self._columns if t == _norm(name))

        def verdict(fresh, reason, drift=None):
            return FreshnessVerdict(
                table=name, fresh=fresh, reason=reason,
                relpages=float(table.pages), cur_pages=float(table.cur_pages),
                drift=drift, stat_columns=stat_columns,
                reltuples=float(table.tuples),
                live_tuples=float(getattr(row, "live_tuples", 0) or 0),
                last_analyze=getattr(row, "last_analyze", "") or "",
                last_autoanalyze=getattr(row, "last_autoanalyze", "") or "")

        if stat_columns == 0:
            return verdict(False,
                           "pg_stats 里这张表一列统计信息都没有 —— 它确实从未被 "
                           "ANALYZE 覆盖（或统计被删过）。此时 n_distinct、"
                           "correlation 全都取不到，推演无从做起。")

        frozen_pages = float(table.pages)
        if frozen_pages <= 0:
            return verdict(False,
                           "pg_class.relpages 为 0 —— 没有冻结基准可比，"
                           "无法判断快照有多旧。")

        drift = abs(float(table.cur_pages) - frozen_pages) / frozen_pages
        aged = ("（统计收集器记录的上次 ANALYZE：%s；该计数器可被 pg_stat_reset "
                "清除，仅供参考，不参与判定）"
                % (getattr(row, "last_analyze", "") or "无记录"))

        if drift > STALE_DRIFT_THRESHOLD:
            return verdict(False,
                           "冻结页数 %.0f 与实时页数 %.0f 相差 %.1f%%，超过阈值 "
                           "%.0f%% —— 表在上次 ANALYZE 之后长大了，规划器会按 "
                           "reltuples/relpages 的密度把行数放大，而 n_distinct 与 "
                           "correlation 不会跟着更新。%s"
                           % (frozen_pages, table.cur_pages, drift * 100.0,
                              STALE_DRIFT_THRESHOLD * 100.0, aged),
                           drift)

        return verdict(True,
                       "冻结页数 %.0f 与实时页数 %.0f 相差 %.1f%%，在阈值 %.0f%% "
                       "之内；pg_stats 有 %d 列统计信息。%s"
                       % (frozen_pages, table.cur_pages, drift * 100.0,
                          STALE_DRIFT_THRESHOLD * 100.0, stat_columns, aged),
                       drift)

    def freshness_report(self, names: Sequence[str]) -> List[FreshnessVerdict]:
        return [self.freshness(n) for n in names]


def from_evidence(ev) -> Catalog:
    return Catalog(ev.tables, ev.indexes, ev.columns,
                   getattr(ev, "freshness", []))


# --- 内部 --------------------------------------------------------------------

def _norm(name: str) -> str:
    return str(name or "").strip().lower()


def _unique_by(rows, key, what: str) -> Dict[str, object]:
    """同名多行即拒绝 —— 见模块头「门一」。"""
    out: Dict[str, object] = {}
    dupes: Dict[str, int] = {}
    for row in rows or []:
        name = _norm(key(row))
        if name in out:
            dupes[name] = dupes.get(name, 1) + 1
            continue
        out[name] = row
    if dupes:
        listed = "、".join("%s(%d 份)" % (n, c) for n, c in sorted(dupes.items()))
        raise CatalogError(
            "%s名字有歧义：%s。采集脚本按 relname 匹配、不带 schema，"
            "多个 schema 下有同名对象时无从知道 SQL 用的是哪一个。"
            "此时取第一个会拿另一张表的统计信息算出一个精确的错数 —— "
            "不报错也不告警，所以这里直接拒绝。" % (what, listed))
    return out
