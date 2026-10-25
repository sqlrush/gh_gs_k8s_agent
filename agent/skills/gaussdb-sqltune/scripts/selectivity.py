"""选择率估算 —— 让推演从「解释现状」走到「预测改动」。

校准阶段的选择率是从实测 Plan Rows **反推**的，那时不需要估。但假设路径
（「加了这条索引会怎样」）没有实测值，必须自己算 —— 而这一步一旦估错，
后面的代价公式再准也没用：代价 = f(选择率)，输入错了输出必错。

公式对应 PostgreSQL 的 src/backend/utils/adt/selfuncs.c。

**三条纪律：**

  1. 统计信息缺失时**拒绝**，不套用 DEFAULT_EQ_SEL(0.005) 之类的兜底常数。
     规划器可以用默认值 —— 它猜错了顶多选错计划，代价由数据库承担；我们
     猜错了会给出一条「加这个索引能快 600 倍」的建议，代价由客户承担。
  2. n_distinct 的**负值是比例不是个数**（-1 表示唯一）。当成个数用会让
     唯一列的选择率算成 1/1 = 1，即「每次都全表」—— 方向完全反了。
  3. MCV 命中与未命中要分开算。倾斜列上只用 n_distinct 会把高频值的
     选择率低估几个数量级，而那正是最需要索引的场景。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional


class SelectivityError(Exception):
    """统计信息不足以估算。调用方必须拒绝出结论，不能退化成默认值。"""


@dataclass(frozen=True)
class ColumnStats:
    """pg_stats 的一行，已解析成可计算的形态。"""
    table: str
    column: str
    n_distinct: float          # 原始值：正数是个数，负数是占行数的比例
    null_frac: float
    correlation: Optional[float]
    mcv: List[str]
    mcv_freqs: List[float]
    histogram: List[str]

    def distinct_count(self, ntuples: float) -> float:
        """把 n_distinct 换算成**个数**。

        pg_stats 的约定：正数就是个数；**负数是占行数的比例**（-1 = 全唯一，
        -0.5 = 每两行一个不同值）。把 -1 当成个数用会算出选择率 1/1 = 1，
        意思是「每次查都命中全表」—— 与真相（每次只命中一行）正好相反，
        而结果依然是个合法的选择率，不会报错。
        """
        if self.n_distinct > 0:
            return self.n_distinct
        if self.n_distinct < 0:
            return max(1.0, abs(self.n_distinct) * ntuples)
        raise SelectivityError(
            "%s.%s 的 n_distinct 是 0 —— 该列没有统计信息（从未被 ANALYZE "
            "覆盖）。这不是「只有一个值」，是「不知道」，不能拿来算选择率。"
            % (self.table, self.column))


def parse_pg_array(text: str) -> List[str]:
    """解析 PostgreSQL 数组的文本形态：{a,b,"c,d"} → ['a','b','c,d']。

    引号内的逗号不是分隔符 —— 直接 split(',') 会把 {"a,b"} 拆成两个值，
    于是 MCV 的个数凭空多一个，频率与值对不上号，选择率整体偏。
    """
    if not text:
        return []
    body = text.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1]
    if not body:
        return []

    out, buf, in_quote, escaped = [], [], False, False
    for ch in body:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_quote = not in_quote
        elif ch == "," and not in_quote:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def parse_freqs(text: str) -> List[float]:
    """频率数组。openGauss 输出成 .000166667 这种前导点写法，float() 认得。"""
    out = []
    for item in parse_pg_array(text):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(float(item))
        except ValueError as exc:
            raise SelectivityError("频率值 %r 解析不了：%s" % (item, exc)) from exc
    return out


def from_row(row, table: str = "", column: str = "") -> ColumnStats:
    """从证据包的一行列统计构造。row 可以是对象也可以是映射。"""
    def get(name, default=None):
        if isinstance(row, dict):
            return row.get(name, default)
        return getattr(row, name, default)

    mcv = parse_pg_array(str(get("most_common_vals", "") or ""))
    freqs = parse_freqs(str(get("most_common_freqs", "") or ""))
    if len(mcv) != len(freqs):
        raise SelectivityError(
            "%s.%s 的 MCV 有 %d 个值但 %d 个频率 —— 对不上号时任何一边都不能用，"
            "按索引取值会静默错位。"
            % (table or get("table", ""), column or get("column", ""),
               len(mcv), len(freqs)))
    return ColumnStats(
        table=table or str(get("table", "") or get("tablename", "")),
        column=column or str(get("column", "") or get("attname", "")),
        n_distinct=float(get("n_distinct", 0.0) or 0.0),
        null_frac=float(get("null_frac", 0.0) or 0.0),
        correlation=(None if get("correlation") in (None, "")
                     else float(get("correlation"))),
        mcv=mcv, mcv_freqs=freqs,
        histogram=parse_pg_array(str(get("histogram_bounds", "") or "")),
    )


# --- 等值选择率 --------------------------------------------------------------

def eq_const(stat: ColumnStats, ntuples: float,
             value: Optional[str] = None) -> float:
    """`col = 常量` 的选择率。selfuncs.c: var_eq_const。

    value 给了就先查 MCV：命中的话选择率就是它自己的频率，与「平均一个值
    占多少」无关。倾斜列上两者能差几个数量级 —— 而高频值恰恰是最常被查、
    也最需要判断该不该建索引的那些。

    value 为 None（不知道要查什么值，比如参数化 SQL）时退回平均值。
    """
    if value is not None and stat.mcv:
        for candidate, freq in zip(stat.mcv, stat.mcv_freqs):
            if candidate == value:
                return _clamp(freq)

    nd = stat.distinct_count(ntuples)
    if stat.mcv:
        # 不在 MCV 里：从「非 MCV、非 NULL」的那部分里平摊
        sum_mcv = sum(stat.mcv_freqs)
        other_distinct = nd - len(stat.mcv)
        if other_distinct <= 0:
            raise SelectivityError(
                "%s.%s 的 MCV 覆盖了全部 %g 个不同值，但要查的值不在其中 —— "
                "统计信息与查询对不上，不猜。" % (stat.table, stat.column, nd))
        return _clamp((1.0 - sum_mcv - stat.null_frac) / other_distinct)

    return _clamp((1.0 - stat.null_frac) / nd)


def eq_join(left: ColumnStats, left_tuples: float,
            right: ColumnStats, right_tuples: float) -> float:
    """`a.x = b.y` 的连接选择率。selfuncs.c: eqjoinsel_inner 的无 MCV 分支。

        sel = (1−null_frac_a) × (1−null_frac_b) / max(nd_a, nd_b)

    取**较大**的那个 n_distinct：两边不同值个数不等时，多的那边决定了
    有多少值匹配不上。取较小的会高估匹配行数，从而高估 join 的收益。
    """
    nd_left = left.distinct_count(left_tuples)
    nd_right = right.distinct_count(right_tuples)
    denominator = max(nd_left, nd_right)
    if denominator <= 0:
        raise SelectivityError("两侧的不同值个数都非正，无法估算连接选择率")
    return _clamp((1.0 - left.null_frac) * (1.0 - right.null_frac) / denominator)


def index_probe(stat: ColumnStats, ntuples: float) -> float:
    """嵌套循环内层按索引单值探测时，一次探测命中多少比例的行。

    就是「平均一个值占多少」——不查 MCV：探测值来自外层的每一行，事先
    不知道是哪个，用某个高频值的频率去代表全部探测会系统性高估。
    """
    return _clamp((1.0 - stat.null_frac) / stat.distinct_count(ntuples))


# --- 直方图 ------------------------------------------------------------------

def numeric_histogram(stat: ColumnStats) -> List[float]:
    """把直方图边界解析成数值。非数值列返回空列表。

    只处理数值型：字符串、日期的比较要走各自的排序规则，拿 float() 硬转会
    悄悄得到一个错的顺序。返回空让调用方拒绝，比转出个错的强。
    """
    out = []
    for item in stat.histogram:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            return []
    return out


def fraction_le(histogram: List[float], value: float) -> Optional[float]:
    """直方图里取值 ≤ value 的行占多少。selfuncs.c: ineq_histogram_selectivity。

    直方图是**等频**的：N+1 个边界划出 N 个桶，每桶各占 1/N 的行。所以
    位置就是比例，桶内按线性插值。

    边界不足两个时返回 None —— 一个点画不出分布，不能当成「全在这一边」。
    """
    if len(histogram) < 2:
        return None
    buckets = len(histogram) - 1
    if value <= histogram[0]:
        return 0.0
    if value >= histogram[-1]:
        return 1.0
    for i in range(buckets):
        if value < histogram[i + 1]:
            span = histogram[i + 1] - histogram[i]
            within = (value - histogram[i]) / span if span else 0.0
            return _clamp((i + within) / buckets)
    return 1.0


@dataclass(frozen=True)
class MergeScanFractions:
    """归并连接两侧各自要扫到多少 —— selfuncs.c: mergejoinscansel。

    归并一边耗尽就停，所以两侧都不一定扫完。**这是父节点代价可能小于
    子节点代价之和的原因**，也是复现 Merge Join 代价绕不过去的一步。
    """
    outer_start: float
    outer_end: float
    inner_start: float
    inner_end: float


def merge_scan_fractions(outer: ColumnStats, inner: ColumnStats
                         ) -> Optional[MergeScanFractions]:
    """两侧连接键的直方图 → 各自的起止扫描比例。

        外层扫到  = 外层中 ≤ 内层最大值 的比例
        外层跳过  = 外层中 <  内层最小值 的比例
        内层同理，两边互换

    任一侧拿不到可用的数值直方图就返回 None —— 调用方应当判为未建模，
    而不是退化成「两边都全扫」：那会在键值范围不重合时高估代价，
    且高估的幅度取决于数据，无法预估。
    """
    ho = numeric_histogram(outer)
    hi = numeric_histogram(inner)
    if len(ho) < 2 or len(hi) < 2:
        return None
    outer_end = fraction_le(ho, hi[-1])
    inner_end = fraction_le(hi, ho[-1])
    outer_start = fraction_le(ho, hi[0])
    inner_start = fraction_le(hi, ho[0])
    if None in (outer_end, inner_end, outer_start, inner_start):
        return None
    return MergeScanFractions(outer_start=outer_start, outer_end=outer_end,
                              inner_start=inner_start, inner_end=inner_end)


def _clamp(value: float) -> float:
    if math.isnan(value):
        raise SelectivityError("选择率算出 NaN")
    return min(1.0, max(0.0, value))
