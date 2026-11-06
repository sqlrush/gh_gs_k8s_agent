"""假设路径：把一条**还不存在**的索引代进计划，重算代价。

这一层与前面所有层有个本质区别：前面算的都是「已经发生的事」，能拿 EXPLAIN
对答案；这里算的是「如果……会怎样」，**没有答案可对**。所以它的可信度完全
寄生在校准闸上 —— 只有当同一套公式复现了基线的每一个节点，用它去算假设路径
才有意义。校准没过就不该走到这里。

两处必须自报的估算：

  1. **索引大小**。索引还不存在，页数只能按行宽和填充率估。估小了会低估
     索引扫描的 IO，让建议显得更划算 —— 偏差方向对结论不利。
  2. **选择率**。基线的选择率能从实测 Plan Rows 反推，假设路径不能，只能
     按统计信息算（见 selectivity.py）。

两处都会让结果偏，所以假设路径的代价**永远标成估算**，不与基线的复算值
同等呈现。把估算值和已校准的值并排放而不加区分，是这类报告最容易骗人的
地方 —— 读的人会以为两个数一样可靠。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Optional

import costmodel
from costmodel import Estimate, ModelError, Term

# btree 页里除去页头与 special space 之后能用的字节数。
# 页头 24 + special 16 = 40 —— 漏掉会让每页多塞几条，索引估小。
_PAGE_OVERHEAD = 40
# 每条索引项：IndexTupleData 头 8 字节（对齐到 8）+ 行指针 4 字节
_INDEX_TUPLE_HEADER = 8
_LINE_POINTER = 4
# btree 叶子页的默认填充率
_DEFAULT_FILLFACTOR = 0.9


@dataclass(frozen=True)
class IndexEstimate:
    pages: float
    tuples: float
    entries_per_page: float
    entry_bytes: int


def estimate_index_size(ntuples: float, avg_width: int, block_size: int,
                        fillfactor: float = _DEFAULT_FILLFACTOR) -> IndexEstimate:
    """估算一条还不存在的 btree 索引有多大。

        每条 = MAXALIGN(列宽 + 8) + 4
        每页 = (block_size − 40) × fillfactor ÷ 每条
        叶子页 = ceil(行数 ÷ 每页)
        总页数 = 叶子页 × fanout/(fanout−1)      ← 加上内部层

    **这是估算，不是查出来的。** 真索引建出来会因为对齐、NULL 位图、
    重复值前缀等因素有出入。估小了会低估索引扫描的 IO，让建议显得更划算，
    所以调用方必须把它标成估算值。
    """
    if ntuples < 0:
        raise ModelError("行数不能为负：%r" % ntuples)
    if avg_width <= 0:
        raise ModelError(
            "列宽 %r 不是正数 —— pg_stats.avg_width 没有值，多半是该列从未被 "
            "ANALYZE 覆盖。索引大小估不出来。" % avg_width)
    if block_size <= _PAGE_OVERHEAD:
        raise ModelError("block_size %r 太小" % block_size)
    if not (0.0 < fillfactor <= 1.0):
        raise ModelError("fillfactor 应在 (0,1]，取到 %r" % fillfactor)

    entry = _maxalign(avg_width + _INDEX_TUPLE_HEADER) + _LINE_POINTER
    usable = (block_size - _PAGE_OVERHEAD) * fillfactor
    per_page = math.floor(usable / entry)
    if per_page < 1:
        raise ModelError(
            "单条索引项 %d 字节，一页放不下一条（可用 %.0f 字节）—— "
            "这个列宽不适合建 btree 索引。" % (entry, usable))

    leaf = math.ceil(max(ntuples, 1.0) / per_page)
    # 内部层：每层是下一层的 1/per_page，等比级数求和 ≈ leaf/(per_page−1)
    internal = math.ceil(leaf / max(per_page - 1, 1)) if leaf > 1 else 0
    return IndexEstimate(pages=float(leaf + internal), tuples=float(ntuples),
                         entries_per_page=float(per_page), entry_bytes=entry)


def _maxalign(width: int) -> int:
    return (int(width) + 7) & ~7


# --- 把假设节点代进树里重算 --------------------------------------------------

class _Shim:
    """冒充 PlanNode 交给 resolver —— 只带它会读的那几个字段。

    行数和行宽沿用实测值：换了访问路径不改变**这一步产出多少行**，
    只改变产出它们要花多少代价。改了行数等于同时改了两件事，
    对不上的时候分不清是哪一件。
    """

    __slots__ = ("node_type", "startup_cost", "total_cost", "plan_rows",
                 "plan_width", "relation", "alias", "index_name", "join_type",
                 "children", "raw")

    def __init__(self, node, estimate):
        self.node_type = estimate.node_type
        self.startup_cost = estimate.startup_cost
        self.total_cost = estimate.total_cost
        self.plan_rows = node.plan_rows
        self.plan_width = node.plan_width
        self.relation = node.relation
        self.alias = node.alias
        self.index_name = node.index_name
        self.join_type = node.join_type
        self.children = node.children
        self.raw = node.raw


@dataclass(frozen=True)
class Proposal:
    """一条待评估的索引建议，连同它的推演结果。

    baseline_total 取的是**实测**根节点代价，hypothetical_total 是重算值 ——
    两者不同源，这一点必须在报告里说清楚：一个是数据库自己报的，一个是我们
    算的。拿它们相除得到的「快多少倍」因此也是估算，不是测量。
    """
    ddl: str
    table: str
    column: str
    baseline_total: float
    hypothetical_total: float
    scan_estimate: object            # costmodel.Estimate，假设的索引扫描
    recomputed: object               # Recomputed，祖先重算结果

    @property
    def ratio(self) -> Optional[float]:
        if self.hypothetical_total <= 0:
            return None
        return self.baseline_total / self.hypothetical_total


@dataclass(frozen=True)
class Recomputed:
    root_total: float
    estimates: list          # [(节点, Estimate)]，自底向上
    unmodeled: list          # 重算不了的节点，代价沿用实测值


def recompute_with_override(root, resolver, target, replacement: Estimate
                            ) -> Recomputed:
    """把 target 节点换成 replacement，自底向上重算它的每一级祖先。

    不在祖先链上的节点保持实测值不变 —— 换一条访问路径不影响别的分支。

    重算不了的祖先（未建模的算子）**沿用实测代价**，并记进 unmodeled。
    这会让总数偏保守（那一级的增量没算进去），报告必须说明；假装那一级
    代价为 0 则会让假设路径显得凭空便宜。
    """
    estimates: List[tuple] = []
    unmodeled: List[object] = []

    def walk(node):
        """返回这个节点重算后的 (总代价, 启动代价)。"""
        if node is target:
            estimates.append((node, replacement))
            return replacement

        child_shims = []
        changed = False
        for child in node.children:
            child_est = walk(child)
            if child_est is None:
                child_shims.append(child)
            else:
                child_shims.append(_Shim(child, child_est))
                changed = True

        if not changed:
            return None          # 这条分支没被影响，保持实测值

        try:
            est = resolver(node, child_shims)
        except ModelError:
            est = None
        if est is None:
            unmodeled.append(node)
            # 沿用实测：这一级的增量没算进去，总数偏保守
            est = Estimate(node_type=node.node_type,
                           startup_cost=node.startup_cost,
                           total_cost=node.total_cost,
                           terms=[Term("实测（该算子未建模，未重算）",
                                       "%.15g" % node.total_cost,
                                       node.total_cost)],
                           approximate=True,
                           notes=["这一级未建模，代价沿用实测值 —— "
                                  "换了下层路径之后它本该变化，这里没算。"
                                  "总数因此偏保守。"])
        estimates.append((node, est))
        return est

    root_est = walk(root)
    total = root_est.total_cost if root_est else root.total_cost
    return Recomputed(root_total=total, estimates=estimates,
                      unmodeled=unmodeled)


_FILTER_COLUMN_RE = re.compile(
    r"\(?\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:>=|<=|<>|!=|=|>|<)")


def filter_columns(filter_text: str) -> List[str]:
    """从 Filter 文本里取出被比较的列名，保持出现顺序、去重。

    只认「标识符 紧跟 比较运算符」这一种形态。函数调用（lower(name) = 'x'）、
    表达式（a + b > 1）都取不到 —— **这是有意的**：那些情况要建的是表达式
    索引，与本模块能估算的普通 btree 不是一回事，猜一个列名出来会给出一条
    建了也不会被用的索引建议。
    """
    if not filter_text:
        return []
    cleaned = re.sub(r"'[^']*'", "''", filter_text)
    out, seen = [], set()
    for name in _FILTER_COLUMN_RE.findall(cleaned):
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(low)
    return out


def propose_from_plan(root, catalog, cost, walk) -> List[dict]:
    """从基线计划里找出「顺序扫描 + 有过滤条件」的地方，提出候选索引。

    **选择率不用估。** 索引建在已经在过滤的那一列上时，选择率就是规划器
    自己对这个过滤条件的估算 —— 它已经体现在该节点的 Plan Rows 里：

        选择率 = Plan Rows ÷ 规划器用的表行数

    所以这一类建议只剩「索引大小」一处估算。这是刻意选的切入点：能拿实测
    值的地方绝不自己估。

    返回的是待评估项（还没算代价），由调用方决定要不要算 —— 前置门没过时
    根本不该算。
    """
    out = []
    for node in walk(root):
        if node.node_type != "Seq Scan" or not node.relation:
            continue
        filter_text = node.raw.get("Filter", "")
        columns = filter_columns(filter_text)
        if not columns:
            continue
        try:
            table = catalog.table(node.relation)
        except Exception:
            continue
        if table.planner_tuples <= 0:
            continue
        selectivity = node.plan_rows / table.planner_tuples
        if selectivity >= 1.0:
            # 过滤没滤掉什么 —— 建索引不会让它变快，规划器也不会用
            continue
        for column in columns[:1]:      # 先只提单列，多列组合是另一个问题
            try:
                stat = catalog.column(table.name, column)
            except Exception:
                continue
            out.append({"table": table, "column": column, "stat": stat,
                        "selectivity": min(1.0, max(0.0, selectivity)),
                        "node": node,
                        "ddl": "CREATE INDEX ON %s.%s (%s)"
                               % (table.schema, table.name, column)})
    return out


SEL_FROM_PLAN_ROWS = (
    "选择率 %.6g 是从**实测**计划里反推的（该扫描节点的 Plan Rows ÷ 规划器"
    "用的表行数）—— 索引建在已经在过滤的那一列上，规划器对这个条件的选择率"
    "估算已经体现在 Plan Rows 里，不需要我们再估一次。")
SEL_ESTIMATED = (
    "选择率 %.6g 来自统计信息估算，不是实测 —— 这一步没有可反推的实测值，"
    "统计信息偏了它就跟着偏。")


def hypothetical_index_scan(table, column_stat, selectivity: float,
                            cost, total_table_pages: float,
                            avg_width: int,
                            selectivity_from_plan: bool = False) -> Estimate:
    """假设在某列上建了索引之后，这一步的索引扫描代价。

    索引大小是估的（estimate_index_size），选择率也是估的 —— 两者都会让
    结果偏，所以返回的 Estimate 一定 approximate=True 并带上说明。
    """
    size = estimate_index_size(table.planner_tuples, avg_width, cost.block_size)
    if column_stat.correlation is None:
        raise ModelError(
            "列 %s 的 correlation 未知 —— 回表 IO 在「完全有序」和「完全无序」"
            "之间按 correlation² 插值，实测两端能差两个数量级，不能猜。"
            % getattr(column_stat, "column", "?"))

    inp = costmodel.IndexScanInput(
        index_pages=size.pages, index_tuples=size.tuples,
        table_pages=float(table.cur_pages), table_tuples=table.planner_tuples,
        selectivity=selectivity, correlation=float(column_stat.correlation),
        total_table_pages=total_table_pages + size.pages,
        num_index_quals=1)
    est = costmodel.index_scan(inp, cost)

    notes = list(est.notes) + [
        "索引尚不存在，页数 %.0f 是按列宽 %d 字节、每页 %.0f 条估算的"
        "（每条 %d 字节，含 8 字节索引项头与 4 字节行指针）。真建出来会因为"
        "对齐、NULL 位图、重复值前缀有出入 —— 估小了会低估索引扫描 IO，"
        "让建议显得更划算。"
        % (size.pages, avg_width, size.entries_per_page, size.entry_bytes),
        (SEL_FROM_PLAN_ROWS if selectivity_from_plan else SEL_ESTIMATED)
        % selectivity,
    ]
    return Estimate(node_type="Index Scan（假设）",
                    startup_cost=est.startup_cost, total_cost=est.total_cost,
                    terms=est.terms, approximate=True, notes=notes)
