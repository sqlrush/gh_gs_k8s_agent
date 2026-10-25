"""代价模型：按规划器的公式复算每个算子的 cost。

**这个模块的输出不是「我认为应该是多少」，是「按公式算出来是多少」。**
它存在的唯一理由是让校准闸有东西可比：先用它复算 EXPLAIN 已经给出的那个
计划，与数据库自己报的 cost 逐节点对；对上了，才有资格拿同一套公式去算
假设路径。对不上就是模型在这个实例上不适用，当场停，不出建议。

公式对应 PostgreSQL 的 src/backend/optimizer/path/costsize.c。openGauss 的
内核基线是 9.2.4，优化器有自己的改动 —— 所以这里**不假设**公式一定适用，
由校准闸实测判定。这也是为什么每一项都拆成 Term 单独留痕：对不上的时候，
能看出是哪一项偏了，而不是只知道总数不对。

术语对齐（与 costsize.c 一致，避免和「行数/页数」混淆）：
    T  表的页数（relpages），至少 1
    b  该表能分到的缓存页数（effective_cache_size 按页数占比摊）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

# btcostestimate 里每下降一层索引页要收的 CPU 费用倍数
# （PostgreSQL 的 DEFAULT_PAGE_CPU_MULTIPLIER）
_PAGE_CPU_MULTIPLIER = 50.0

# openGauss 每多一个并行线程收的固定启动费用。**这个数是实测反解出来的，
# 不是文档写的，也没有对应的 GUC**（pg_settings 里 parallel|dop|smp 只有
# query_dop / recovery_parallelism / ss_parallel_thread_count 三个）。
# og5 上三张表 × dop∈{1,2,4} 九组数据全部逐位吻合。
# 换实例、换版本要重验 —— 校准闸会在它变了的时候当场报出来。
PARALLEL_SETUP_COST = 1000.0

# Streaming(LOCAL GATHER) 每传输一个 block_size 的数据收的费用基数。
# **同样是实测反解的硬编码常数。** 实测它对 seq_page_cost / random_page_cost /
# cpu_tuple_cost / cpu_operator_cost 四个 GUC 全都不敏感（逐个改动，C 一动不动），
# 且 pg_settings 里没有任何 stream 相关的代价参数 —— 所以是内核写死的。
STREAM_TRANSFER_COST = 1.3


class ModelError(Exception):
    """输入不足以复算。调用方当作「这个节点没建模」处理，不能填 0 顶替。"""


@dataclass(frozen=True)
class Term:
    """代价里的一项。报告直接渲染这些，读的人能逐项复核。"""
    label: str
    formula: str     # 代入了实际数值的算式，如 "1 × 1234568"
    value: float


@dataclass(frozen=True)
class Variant:
    """两处已知的内核版本分歧点。

    openGauss 的内核基线是 PostgreSQL 9.2.4，但优化器改动很多，我**不知道**
    它的代价函数跟的是哪一版。与其挑一个然后当成事实，不如把分歧点摆出来，
    让校准闸拿真实计划去试：哪个变体能与 EXPLAIN 对上，就用哪个。
    这样「用了哪一版公式」从一个假设变成一次测量。

    min_io_split_seq
        单次索引扫描、完全相关情况下的最小 IO 代价。
        True （9.3+ 的写法）：第一页随机、其余顺序
            min_IO = random_page_cost + (pages-1) × seq_page_cost
        False（9.2 的写法）：全部按随机
            min_IO = pages × random_page_cost
        相关性高的索引上两者差得很明显 —— 恰恰是「加了索引很划算」那类场景。

    btree_page_cpu_cost
        btcostestimate 里 (tree_height+1) × 50 × cpu_operator_cost 这一项。
        9.3 才加的。绝对值很小（树高 3 时约 0.5），大扫描里可以忽略，但
        嵌套循环单行探测的总代价本身也才个位数，这一项就是百分之几。
    """
    min_io_split_seq: bool = True
    btree_page_cpu_cost: bool = True


@dataclass(frozen=True)
class IndexScanInput:
    """索引扫描复算所需的全部输入。

    刻意做成一个显式的结构而不是十个位置参数：这里面每一个数取错了，
    结果都还是一个像样的浮点数。取值处必须一眼看得出各是什么。
    """
    index_pages: float
    index_tuples: float
    table_pages: float
    table_tuples: float
    selectivity: float          # 索引条件的选择率
    correlation: float          # pg_stats.correlation
    total_table_pages: float    # 本查询涉及的所有表的页数之和
    num_index_quals: int = 1
    tree_height: Optional[int] = None   # 拿不到就估，见 estimate_tree_height


@dataclass(frozen=True)
class Estimate:
    node_type: str
    startup_cost: float
    total_cost: float
    terms: List[Term] = field(default_factory=list)
    # 公式里含估算成分（比如 Filter 的操作符个数是从文本数出来的）时置位。
    # 不降低校准标准 —— 只是让对不上的时候先怀疑这里。
    approximate: bool = False
    notes: List[str] = field(default_factory=list)


# --- 页面获取模型（Mackert-Lohman） -----------------------------------------

def index_pages_fetched(tuples_fetched: float, pages: float,
                        index_pages: float, total_table_pages: float,
                        effective_cache_size: int) -> float:
    """取 N 个元组要读多少页 —— Mackert-Lohman 公式，costsize.c 同名函数。

    直觉：随机取 N 行，落在同一页上的会被缓存命中，所以读的页数远少于 N。
    N 很大时趋近于「整表都读一遍」。

    这一项是索引扫描与顺序扫描之间的胜负手：没有它，「取 100 行」会被算成
    100 次随机 IO，而实际上重复页只读一次。算错的方向是**高估索引代价**，
    于是本该推荐的索引被判成没收益 —— 一个安静的错误结论。
    """
    if effective_cache_size <= 0:
        raise ModelError("effective_cache_size 必须为正，取到 %r" % effective_cache_size)

    T = float(pages) if pages > 1 else 1.0

    # 竞争缓存的总页数：查询涉及的所有表 + 本索引。costsize.c 用的是
    # root->total_table_pages（该查询涉及的表的总页数），不是全库。
    total = max(float(total_table_pages) + float(index_pages), 1.0)
    if T > total:
        # 单表页数不该超过总页数；出现说明调用方传错了 total_table_pages
        raise ModelError(
            "表页数 %.15g 超过参与竞争的总页数 %.15g —— total_table_pages "
            "应当是本查询涉及的所有表的页数之和，含本表。" % (T, total))

    b = float(effective_cache_size) * T / total
    b = 1.0 if b <= 1.0 else math.ceil(b)

    if T <= b:
        # 缓存装得下整表：读的页数封顶在整表页数，不会比全表扫还多
        pages_fetched = (2.0 * T * tuples_fetched) / (2.0 * T + tuples_fetched)
        if pages_fetched >= T:
            return T
        return math.ceil(pages_fetched)

    # 缓存装不下整表：前 b 页按上面的规律，之后每多取一个元组按比例多读页。
    #
    # **这个分支不封顶到 T，是有意的**（costsize.c 同样不封）。取数足够多时
    # 同一页会被挤出缓存再读一次，物理读页数确实可以超过表的总页数。
    # 顺手加个 min(…, T) 会让「反复重读」这件事从代价里消失，于是嵌套循环
    # 的内层扫描被系统性低估。
    lim = (2.0 * T * b) / (2.0 * T - b)
    if tuples_fetched <= lim:
        pages_fetched = (2.0 * T * tuples_fetched) / (2.0 * T + tuples_fetched)
    else:
        pages_fetched = b + (tuples_fetched - lim) * (T - b) / T
    return math.ceil(pages_fetched)


def clamp_row_est(rows: float) -> float:
    """行数估算的下限。costsize.c 的 clamp_row_est：不小于 1，且取整。

    没有这个下限，选择率极小时会算出 0.0001 行，后面一路乘下去让代价趋近 0
    —— 于是任何索引看起来都收益无穷大。
    """
    if math.isnan(rows):
        raise ModelError("行数估算得到 NaN")
    if rows <= 1.0:
        return 1.0
    # C 的 rint() 默认是 round-half-to-even，Python 的 round() 同规则
    return float(round(rows))


# --- Seq Scan ----------------------------------------------------------------

def seq_scan(cur_pages: float, cur_tuples: float, cost, qual_operators: int = 0,
             has_filter: bool = False, dop: int = 1) -> Estimate:
    """顺序扫描。costsize.c: cost_seqscan，加上 openGauss 的并行调整。

        base  = seq_page_cost × 块数
              + cpu_tuple_cost × 行数
              + cpu_operator_cost × 过滤条件里的操作符个数 × 行数
        total = base / dop + 1000 × (dop − 1)

    **参数是「实时块数」和「换算行数」，不是 pg_class.relpages/reltuples。**
    名字刻意起成 cur_*：规划器用的是 RelationGetNumberOfBlocks() 的实时块数，
    行数按 density = reltuples/relpages 再乘实时块数换算。传冻结值进来不会
    报错，只会让复算值差几个百分点，而校准闸报出来会像是「模型不适用」。

    并行那两项是在 og5 上**实测反解**出来的，openGauss 文档没写：三张表
    × dop∈{1,2,4} 九组数据逐位吻合。dop=1 时退化成标准的 PostgreSQL 公式。
    """
    if cur_pages < 0 or cur_tuples < 0:
        raise ModelError("块数/行数不能为负：%r / %r" % (cur_pages, cur_tuples))
    if dop < 1:
        raise ModelError("dop 必须 ≥ 1，取到 %r" % dop)

    io = cost.seq_page_cost * cur_pages
    cpu = cost.cpu_tuple_cost * cur_tuples
    terms = [
        Term("顺序读页", "%.15g × %.15g" % (cost.seq_page_cost, cur_pages), io),
        Term("每行 CPU", "%.15g × %.15g" % (cost.cpu_tuple_cost, cur_tuples), cpu),
    ]
    base = io + cpu

    if qual_operators:
        qual = cost.cpu_operator_cost * qual_operators * cur_tuples
        terms.append(Term(
            "过滤条件求值",
            "%.15g × %d × %.15g" % (cost.cpu_operator_cost, qual_operators, cur_tuples),
            qual))
        base += qual

    notes = []
    if has_filter and not qual_operators:
        notes.append(
            "计划里有 Filter 但没数出操作符个数 —— 该项按 0 计，"
            "复算值会偏低。这不是「没有过滤代价」，是「没数出来」。")
    elif qual_operators:
        notes.append(
            "过滤条件的操作符个数（%d）是从计划文本里**数**出来的，不是解析出来的。"
            "数错一个在这张表上就是 %.2f 的偏差。"
            % (qual_operators, cost.cpu_operator_cost * cur_tuples))

    total = base
    if dop > 1:
        setup = PARALLEL_SETUP_COST * (dop - 1)
        total = base / dop + setup
        # 记成「扣除」而不是「÷dop 后的值」：这样各项之和仍等于合计，
        # 报告里那张逐项表才能一路加下来对得上。
        terms.append(Term("并行摊分：扣除 (1−1/%d)" % dop,
                          "−(1−1/%d) × %.4f" % (dop, base),
                          base / dop - base))
        terms.append(Term("并行启动", "%.15g × (%d−1)" % (PARALLEL_SETUP_COST, dop),
                          setup))

    return Estimate(
        node_type="Seq Scan",
        startup_cost=0.0,
        total_cost=total,
        terms=terms,
        approximate=has_filter,
        notes=notes,
    )


# --- Streaming（openGauss 特有） ---------------------------------------------

def streaming_gather(child_total: float, child_startup: float,
                     rows: float, width: int, cost, dop: int) -> Estimate:
    """Streaming(type: LOCAL GATHER) —— 把各并行线程的结果汇总回来。

    PostgreSQL 没有这个算子（对应的是 Gather），公式也不一样，文档没写。
    实测反解：

        total = 子节点 total + (行数 × 行宽 ÷ block_size) × 1.3 × (1 + 1/dop)

    传输量按「行数×行宽 折算成多少个 block」算，每块收 1.3×(1+1/dop)。
    五张表 × dop∈{2,4} 十组数据逐位吻合；1.3 对四个代价 GUC 都不敏感，
    是内核硬编码。

    **这个算子必须建模**，不是可选项：og5 默认 query_dop=2，几乎每个计划顶上
    都顶着一个 Streaming。不建模的话校准覆盖率会一直上不去，而覆盖率低正是
    「大部分节点没验过」的委婉说法。
    """
    if dop < 1:
        raise ModelError("dop 必须 ≥ 1，取到 %r" % dop)
    if rows < 0 or width < 0:
        raise ModelError("行数/行宽不能为负：%r / %r" % (rows, width))
    if cost.block_size <= 0:
        raise ModelError("block_size 必须为正")

    blocks = rows * width / float(cost.block_size)
    per_block = STREAM_TRANSFER_COST * (1.0 + 1.0 / dop)
    transfer = blocks * per_block

    return Estimate(
        node_type="Streaming",
        startup_cost=child_startup,
        total_cost=child_total + transfer,
        terms=[
            Term("子节点", "%.15g" % child_total, child_total),
            Term("汇总传输",
                 "(%.15g×%d÷%d) × %.15g × (1+1/%d)"
                 % (rows, width, cost.block_size, STREAM_TRANSFER_COST, dop),
                 transfer),
        ],
    )


# --- Index Scan --------------------------------------------------------------

def estimate_tree_height(index_pages: float, index_tuples: float) -> int:
    """估算 btree 树高。

    **目录里拿不到真值** —— 规划器用的是 _bt_getrootheight()，那要读索引元页，
    SQL 取不到。这里拿平均每页条目数当扇出反推：

        fanout = index_tuples / index_pages
        height = ceil(log(index_tuples) / log(fanout)) - 1

    误差 ±1 层在大扫描里可以忽略（每层只值 50×cpu_operator_cost≈0.125），
    但嵌套循环单行探测的总代价本身就是个位数，那时它是百分之几。所以用了
    估算值的节点一律置 approximate。
    """
    if index_pages <= 1 or index_tuples <= 1:
        return 0
    fanout = index_tuples / index_pages
    if fanout <= 1.0:
        raise ModelError(
            "索引每页平均条目数 %.15g ≤ 1（%.15g 条目 / %.15g 页）—— 索引膨胀到这个程度，"
            "扇出模型不成立，树高估不出来。这本身就是个值得报出去的发现，"
            "不该在这里凑一个数糊过去。" % (fanout, index_tuples, index_pages))
    return max(0, math.ceil(math.log(index_tuples) / math.log(fanout)) - 1)


def index_scan(inp: IndexScanInput, cost, loop_count: float = 1.0,
               variant: Optional[Variant] = None) -> Estimate:
    """btree 索引扫描。costsize.c: cost_index + genericcostestimate + btcostestimate。

    分两块：**索引侧**（沿树下降、扫叶子页）与**堆侧**（按 tid 回表取行）。
    堆侧那块是整个模型里最容易被想当然的地方 —— 它不是「取 N 行就是 N 次
    随机 IO」，而是在「完全无序」和「完全有序」两个极端之间按 correlation²
    插值。correlation 实测 0.1 和 0.9 能差出几十倍，凭感觉写就是幻觉。
    """
    variant = variant or Variant()
    if inp.correlation is None:
        raise ModelError(
            "correlation 未知（pg_stats.correlation 为 NULL）。"
            "不能当 0 处理 —— 0 是「物理顺序与索引顺序完全无关」这个**结论**，"
            "不是「不知道」。该列没有统计信息时应当拒绝推演。")
    _require_range("selectivity", inp.selectivity, 0.0, 1.0)
    _require_range("correlation", inp.correlation, -1.0, 1.0)
    if inp.index_pages <= 0 or inp.index_tuples <= 0:
        raise ModelError(
            "索引页数/条目数必须为正，取到 %.15g / %.15g。为 0 通常意味着索引建好后"
            "还没 ANALYZE 过，此时任何复算都是无意义的。"
            % (inp.index_pages, inp.index_tuples))

    cache = cost.effective_cache_size
    terms: List[Term] = []
    notes: List[str] = []
    approximate = False

    # --- 索引侧 --------------------------------------------------------------
    num_index_tuples = inp.selectivity * inp.index_tuples
    num_index_pages = math.ceil(num_index_tuples * inp.index_pages / inp.index_tuples)

    if loop_count > 1:
        fetched = index_pages_fetched(num_index_pages * loop_count, inp.index_pages,
                                      inp.index_pages, inp.total_table_pages, cache)
        index_io = fetched * cost.random_page_cost / loop_count
        index_io_formula = "%.15g 页 × %.15g ÷ %.15g 次探测" % (
            fetched, cost.random_page_cost, loop_count)
    else:
        index_io = num_index_pages * cost.random_page_cost
        index_io_formula = "%.15g × %.15g" % (num_index_pages, cost.random_page_cost)
    terms.append(Term("索引读页", index_io_formula, index_io))

    qual_op_cost = cost.cpu_operator_cost * inp.num_index_quals
    index_cpu = num_index_tuples * (cost.cpu_index_tuple_cost + qual_op_cost)
    terms.append(Term(
        "索引条目 CPU",
        "%.15g × (%.15g + %.15g×%d)" % (num_index_tuples, cost.cpu_index_tuple_cost,
                               cost.cpu_operator_cost, inp.num_index_quals),
        index_cpu))

    index_startup = 0.0
    index_total = index_io + index_cpu

    # 沿树下降的比较次数：log2(条目数)，每次一个 cpu_operator_cost
    if inp.index_tuples > 1:
        descent = math.ceil(math.log(inp.index_tuples) / math.log(2.0)) \
            * cost.cpu_operator_cost
        index_startup += descent
        index_total += descent          # num_sa_scans = 1（简单等值/范围条件）
        terms.append(Term(
            "btree 下降比较",
            "ceil(log2(%.15g)) × %.15g" % (inp.index_tuples, cost.cpu_operator_cost),
            descent))

    if variant.btree_page_cpu_cost:
        height = inp.tree_height
        if height is None:
            height = estimate_tree_height(inp.index_pages, inp.index_tuples)
            approximate = True
            notes.append(
                "树高 %d 是估的 —— 真值要读索引元页（_bt_getrootheight），"
                "SQL 取不到。误差 ±1 层约 %.3f，大扫描里可忽略，"
                "单行探测里是百分之几。" % (height, _PAGE_CPU_MULTIPLIER
                                            * cost.cpu_operator_cost))
        descent = (height + 1) * _PAGE_CPU_MULTIPLIER * cost.cpu_operator_cost
        index_startup += descent
        index_total += descent
        terms.append(Term(
            "索引层下降 CPU",
            "(%d+1) × %.15g × %.15g" % (height, _PAGE_CPU_MULTIPLIER,
                                  cost.cpu_operator_cost),
            descent))

    # --- 堆侧 ----------------------------------------------------------------
    tuples_fetched = clamp_row_est(inp.selectivity * inp.table_tuples)
    correlated_pages = math.ceil(inp.selectivity * inp.table_pages)

    if loop_count > 1:
        fetched = index_pages_fetched(tuples_fetched * loop_count, inp.table_pages,
                                      inp.index_pages, inp.total_table_pages, cache)
        max_io = fetched * cost.random_page_cost / loop_count
        fetched_c = index_pages_fetched(correlated_pages * loop_count, inp.table_pages,
                                        inp.index_pages, inp.total_table_pages, cache)
        min_io = fetched_c * cost.random_page_cost / loop_count
    else:
        fetched = index_pages_fetched(tuples_fetched, inp.table_pages,
                                      inp.index_pages, inp.total_table_pages, cache)
        max_io = fetched * cost.random_page_cost
        if variant.min_io_split_seq:
            min_io = cost.random_page_cost
            if correlated_pages > 1:
                min_io += (correlated_pages - 1) * cost.seq_page_cost
        else:
            min_io = correlated_pages * cost.random_page_cost

    csquared = inp.correlation ** 2
    heap_io = max_io + csquared * (min_io - max_io)
    terms.append(Term(
        "回表 IO（按 correlation² 插值）",
        "%.2f + %.4f × (%.2f − %.2f)" % (max_io, csquared, min_io, max_io),
        heap_io))

    heap_cpu = cost.cpu_tuple_cost * tuples_fetched
    terms.append(Term(
        "回表每行 CPU", "%.15g × %.15g" % (cost.cpu_tuple_cost, tuples_fetched), heap_cpu))

    return Estimate(
        node_type="Index Scan",
        startup_cost=index_startup,
        total_cost=index_total + heap_io + heap_cpu,
        terms=terms,
        approximate=approximate,
        notes=notes,
    )


# --- Join 算子 ---------------------------------------------------------------
#
# **join 复算一律拿子节点的实测 cost 当输入**，不拿我自己复算的子节点值。
# 两个理由：
#   1. 误差不累积。用复算值的话，扫描层偏 1%，join 层跟着偏，再上一层再偏，
#      最后只知道「总数对不上」，不知道是哪一层的公式错了。
#   2. 这样每个节点检验的是**它自己那条公式**，正是逐节点校准的意义。

def nested_loop(outer_total: float, outer_startup: float,
                inner_total: float, inner_startup: float,
                outer_rows: float, inner_rows: float, cost,
                join_quals: int = 0) -> Estimate:
    """嵌套循环。costsize.c: cost_nestloop（内层无 Materialize 的常规情形）。

    展开后是很直观的一句话：**外层代价 + 外层行数 × 内层代价**。

        startup = 外.startup + 内.startup
        total   = 外.total + 外行数 × 内.total
                + (cpu_tuple_cost + cpu_operator_cost×连接条件数) × 外行数×内行数

    内层重扫的代价按「与首次相同」计（cost_rescan 对索引扫描就是这么算的）。
    内层若是 Materialize，重扫会便宜得多，此处未建模 —— 遇到那种计划应当
    判为未建模，而不是拿这条公式硬套。
    """
    if outer_rows < 0 or inner_rows < 0:
        raise ModelError("行数不能为负：%r / %r" % (outer_rows, inner_rows))

    startup = outer_startup + inner_startup
    source = outer_total + outer_rows * inner_total
    pairs = outer_rows * inner_rows
    cpu_per_pair = cost.cpu_tuple_cost + cost.cpu_operator_cost * join_quals
    cpu = cpu_per_pair * pairs

    return Estimate(
        node_type="Nested Loop",
        startup_cost=startup,
        total_cost=source + cpu,
        terms=[
            Term("外层扫描", "%.15g" % outer_total, outer_total),
            Term("内层重复 %.15g 次" % outer_rows,
                 "%.15g × %.15g" % (outer_rows, inner_total),
                 outer_rows * inner_total),
            Term("配对 CPU",
                 "(%.15g + %.15g×%d) × %.15g×%.15g" % (cost.cpu_tuple_cost,
                                           cost.cpu_operator_cost, join_quals,
                                           outer_rows, inner_rows),
                 cpu),
        ],
    )


def hash_join(outer_total: float, outer_startup: float,
              inner_total: float, inner_rows: float, inner_width: int,
              outer_rows: float, output_rows: float, cost,
              num_hashclauses: int = 1, join_quals: int = 0) -> Estimate:
    """哈希连接（**单批次**）。costsize.c: initial_cost_hashjoin + final_cost_hashjoin。

        startup = 外.startup + 内.total                      ← 内表必须先全建完
                + (cpu_operator_cost×哈希列数 + cpu_tuple_cost) × 内行数
        run     = (外.total − 外.startup)
                + cpu_operator_cost×哈希列数 × 外行数         ← 探测时算哈希
                + 桶内比较 + cpu_tuple_cost × 输出行数

    **内表放不下 work_mem 时直接拒绝建模，不猜批次数。** 多批次要按
    ExecChooseHashTableSize 的规则算批数，还要加内外表各自的落盘读写；
    批数猜错一档，代价差一个量级。宁可报「未建模」也不给一个像模像样的错数。

    桶内比较那一项按均匀分布近似（每桶约 1 条），真值要 MCV 分布才算得准，
    所以整个结果置 approximate。倾斜列上这一项会被低估。
    """
    if inner_rows < 0 or outer_rows < 0 or output_rows < 0:
        raise ModelError("行数不能为负")
    if inner_width < 0:
        raise ModelError("行宽不能为负：%r" % inner_width)

    inner_bytes = relation_byte_size(inner_rows, inner_width)
    if inner_bytes > cost.work_mem:
        raise ModelError(
            "内表约 %.1f MB，超过 work_mem %.1f MB —— 会走多批次哈希，"
            "批数与落盘 IO 本实现未建模。批数猜错一档代价差一个量级，"
            "所以判为未建模，不给近似值。"
            % (inner_bytes / 1048576.0, cost.work_mem / 1048576.0))

    hash_cost = cost.cpu_operator_cost * num_hashclauses
    build = (hash_cost + cost.cpu_tuple_cost) * inner_rows
    startup = outer_startup + inner_total + build
    probe_hash = hash_cost * outer_rows
    # 桶内比较：均匀分布下每桶约 1 条，costsize.c 的 ×0.5 是「平均比到一半」
    bucket = hash_cost * outer_rows * 1.0 * 0.5
    out_cpu = (cost.cpu_tuple_cost + cost.cpu_operator_cost * join_quals) * output_rows

    return Estimate(
        node_type="Hash Join",
        startup_cost=startup,
        total_cost=(outer_total - outer_startup) + startup
                   + probe_hash + bucket + out_cpu,
        terms=[
            Term("内表建哈希表（含其扫描）",
                 "%.15g + (%.15g+%.15g)×%.15g" % (inner_total, hash_cost,
                                      cost.cpu_tuple_cost, inner_rows),
                 inner_total + build),
            Term("外表扫描", "%.15g" % outer_total, outer_total),
            Term("探测算哈希", "%.15g × %.15g" % (hash_cost, outer_rows), probe_hash),
            Term("桶内比较（均匀分布近似）",
                 "%.15g × %.15g × 0.5" % (hash_cost, outer_rows), bucket),
            Term("输出行 CPU", "%.15g × %.15g" % (cost.cpu_tuple_cost, output_rows),
                 out_cpu),
        ],
        approximate=True,
        notes=["桶内比较按均匀分布近似（每桶约 1 条）。真值要 MCV 分布，"
               "倾斜列上这一项会被低估。"],
    )


def merge_join(outer_total: float, outer_startup: float,
               inner_total: float, inner_startup: float,
               outer_rows: float, inner_rows: float, output_rows: float,
               fractions, cost, num_merge_clauses: int = 1,
               join_quals: int = 0) -> Estimate:
    """归并连接。costsize.c: initial_cost_mergejoin + final_cost_mergejoin。

        startup = 外.startup + 外run×外起始比 + 内.startup + 内run×内起始比
        run     = 外run×(外结束比−外起始比) + 内run×(内结束比−内起始比)
        cpu     = cpu_operator_cost×归并列数 × (实扫外行数 + 实扫内行数)
                + (cpu_tuple_cost + cpu_operator_cost×附加条件数) × 输出行数

    **关键在那几个比例。** 归并一边耗尽就停，两侧都不一定扫完 —— 所以
    Merge Join 的总代价**可以小于两个子节点代价之和**。og5 上实测过：
    203706.53 < 132100.42 + 79262.15。不折算就会高估，而高估的幅度取决于
    两侧键值范围重合多少，猜不出来。比例由 selectivity.merge_scan_fractions
    从两侧直方图算，og5 上三个用例总代价 0.0000% 吻合。

    **startup 的拆分不精确**（实测 11.68，本式给 8.58），但 total 精确。
    校准闸比的是 total，所以够用；要拿 startup 做判断的话得先补齐
    outer_skip_rows 那一项。
    """
    if fractions is None:
        raise ModelError(
            "拿不到两侧连接键的数值直方图，算不出归并的扫描比例。"
            "退化成「两边都全扫」会在键值范围不重合时高估代价，"
            "而高估多少取决于数据 —— 所以判为未建模。")
    for name in ("outer_start", "outer_end", "inner_start", "inner_end"):
        _require_range(name, getattr(fractions, name), 0.0, 1.0)

    outer_run = outer_total - outer_startup
    inner_run = inner_total - inner_startup

    startup = (outer_startup + outer_run * fractions.outer_start
               + inner_startup + inner_run * fractions.inner_start)
    run = (outer_run * (fractions.outer_end - fractions.outer_start)
           + inner_run * (fractions.inner_end - fractions.inner_start))

    scanned_outer = clamp_row_est(outer_rows * fractions.outer_end)
    scanned_inner = clamp_row_est(inner_rows * fractions.inner_end)
    merge_qual = cost.cpu_operator_cost * num_merge_clauses
    compare = merge_qual * (scanned_outer + scanned_inner)
    emit = (cost.cpu_tuple_cost
            + cost.cpu_operator_cost * join_quals) * output_rows

    return Estimate(
        node_type="Merge Join",
        startup_cost=startup,
        total_cost=startup + run + compare + emit,
        terms=[
            Term("外层扫描（折算 %.4f）" % fractions.outer_end,
                 "%.4f × %.4f" % (outer_run, fractions.outer_end),
                 outer_startup + outer_run * fractions.outer_end),
            Term("内层扫描（折算 %.4f）" % fractions.inner_end,
                 "%.4f × %.4f" % (inner_run, fractions.inner_end),
                 inner_startup + inner_run * fractions.inner_end),
            Term("归并比较",
                 "%.15g × (%.15g + %.15g)" % (merge_qual, scanned_outer,
                                              scanned_inner),
                 compare),
            Term("输出行 CPU",
                 "%.15g × %.15g" % (cost.cpu_tuple_cost, output_rows), emit),
        ],
    )


def relation_byte_size(tuples: float, width: int) -> float:
    """关系在内存里占多少字节。costsize.c 同名函数。

    每行除了数据还有 24 字节的元组头（SizeofHeapTupleHeader 对齐到 8）。
    漏掉它会让「内表装不装得下 work_mem」在临界处判反 —— 而判反的后果是
    把多批次哈希当成单批次，少算掉全部落盘 IO。
    """
    return tuples * (_maxalign(width) + _HEAP_TUPLE_HEADER)


_HEAP_TUPLE_HEADER = 24


def _maxalign(width: int) -> int:
    return (int(width) + 7) & ~7


def _require_range(name: str, value: float, low: float, high: float) -> None:
    if value is None or math.isnan(value) or not (low <= value <= high):
        raise ModelError("%s 应在 [%.15g, %.15g] 内，取到 %r" % (name, low, high, value))
