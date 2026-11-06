"""把计划节点接到目录和代价模型上 —— 校准闸要的那个 resolver。

职责很窄：拿到一个 PlanNode，凑齐它那条公式需要的输入，调 costmodel，返回
Estimate。凑不齐就抛 ModelError（校准闸会计为未建模并留下原因），算子没实现
就返回 None。

**这里不做任何「估个差不多的值」。** 缺 correlation、表名有歧义、索引没
ANALYZE —— 全部抛错。推演的可信度全靠校准闸，而校准闸只能校验真的算过的
东西；用估计值填补空缺，等于把没验过的东西混进已验证的结论里。
"""
from __future__ import annotations

import re
from typing import List, Optional

import costmodel
import selectivity
from costmodel import ModelError
from plantree import PlanNode

# 扫描节点的 Filter 里有几个操作符 —— cost_qual_eval 按操作符个数收费。
# 这是**数出来的**，不是解析出来的：真解析要一个表达式 parser。数错一个，
# 亿行表上就是 25 万的偏差，所以用它的节点会被置 approximate，由校准闸裁决。
_OPERATOR_RE = re.compile(r"(?:>=|<=|<>|!=|=|>|<|~~\*?|!~~\*?|~\*?|!~\*?)")


def count_qual_operators(filter_text: str) -> int:
    if not filter_text:
        return 0
    # 去掉字符串字面量，避免把 'a=b' 里的等号数进去
    cleaned = re.sub(r"'[^']*'", "''", filter_text)
    return len(_OPERATOR_RE.findall(cleaned))


def indexed_columns(index_def: str) -> List[str]:
    """从 CREATE INDEX ... (a, b) 里取列名。

    只认最外层那对括号里的逗号分隔项，表达式索引（含函数调用）会被识别成
    非标识符 —— 调用方应当把它当作「拿不到列」处理，而不是猜第一个词。
    """
    match = re.search(r"\((.*)\)", index_def or "", re.S)
    if not match:
        return []
    out = []
    for part in match.group(1).split(","):
        name = part.strip().strip('"').split()[0] if part.strip() else ""
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            out.append(name.lower())
        else:
            return []          # 表达式索引，整体放弃
    return out


def make_resolver(catalog, cost, variant: Optional[costmodel.Variant] = None):
    """返回给 calibrate.calibrate() 用的 resolver。"""
    variant = variant or costmodel.Variant()
    dop = getattr(cost, "query_dop", 1)

    def resolve(node: PlanNode, children: List[PlanNode]):
        kind = node.node_type

        if kind.startswith("Streaming"):
            if not children:
                raise ModelError("Streaming 节点没有子节点")
            child = children[0]
            return costmodel.streaming_gather(
                child.total_cost, child.startup_cost,
                node.plan_rows, node.plan_width, cost, dop)

        if kind == "Seq Scan":
            table = catalog.table(_relation_of(node))
            return costmodel.seq_scan(
                float(table.cur_pages), table.planner_tuples, cost,
                qual_operators=count_qual_operators(node.raw.get("Filter", "")),
                has_filter=bool(node.raw.get("Filter")),
                dop=dop)

        if kind in ("Index Scan", "Index Scan Backward"):
            return _index_scan(node, catalog, cost, variant, dop)

        if kind == "Index Only Scan":
            # 堆访问由可见性图决定，跳过的比例目录里查不到 —— 没有它就
            # 算不出回表 IO。不建模，不拿 Index Scan 的公式硬套。
            return None

        if kind == "Hash":
            if not children:
                raise ModelError("Hash 节点没有子节点")
            child = children[0]
            # Hash 节点自身不加代价，就是子节点的代价 —— 这是个很好的锚点：
            # 它必须**逐位**相等，不等就说明计划树接错了。
            return costmodel.Estimate(
                node_type="Hash", startup_cost=child.total_cost,
                total_cost=child.total_cost,
                terms=[costmodel.Term("子节点", "%g" % child.total_cost,
                                      child.total_cost)])

        if kind == "Nested Loop":
            outer, inner = _two_children(node, children)
            return costmodel.nested_loop(
                outer.total_cost, outer.startup_cost,
                inner.total_cost, inner.startup_cost,
                outer.plan_rows, inner.plan_rows, cost,
                join_quals=count_qual_operators(node.raw.get("Join Filter", "")))

        if kind == "Hash Join":
            outer, hash_node = _two_children(node, children)
            return costmodel.hash_join(
                outer_total=outer.total_cost, outer_startup=outer.startup_cost,
                inner_total=hash_node.total_cost,
                inner_rows=hash_node.plan_rows,
                inner_width=hash_node.plan_width,
                outer_rows=outer.plan_rows, output_rows=node.plan_rows,
                cost=cost,
                num_hashclauses=_count_clauses(node.raw.get("Hash Cond", "")),
                join_quals=count_qual_operators(node.raw.get("Join Filter", "")))

        if kind == "Merge Join":
            outer, inner = _two_children(node, children)
            return costmodel.merge_join(
                outer_total=outer.total_cost, outer_startup=outer.startup_cost,
                inner_total=inner.total_cost, inner_startup=inner.startup_cost,
                outer_rows=outer.plan_rows, inner_rows=inner.plan_rows,
                output_rows=node.plan_rows,
                fractions=_merge_fractions(node, outer, inner, catalog),
                cost=cost,
                num_merge_clauses=_count_clauses(node.raw.get("Merge Cond", "")),
                join_quals=count_qual_operators(node.raw.get("Join Filter", "")))

        return None      # 未建模

    return resolve


_MERGE_COND_RE = re.compile(
    r"\(?\s*(?:([A-Za-z_][A-Za-z0-9_]*)\.)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?:([A-Za-z_][A-Za-z0-9_]*)\.)?([A-Za-z_][A-Za-z0-9_]*)")


def _merge_fractions(node, outer, inner, catalog):
    """从 Merge Cond 找出两侧的连接列，取直方图算扫描比例。

    按**别名**把条件的左右两半对到两个子节点上 —— 不能按出现顺序：
    `Merge Cond: (r.id = o.id)` 里左半可能对应计划里的内层。对反了会把两侧的
    比例互换，而互换后仍是两个合法的比例，代价照样算得出来，只是错的。
    """
    match = _MERGE_COND_RE.search(node.raw.get("Merge Cond", "") or "")
    if not match:
        raise ModelError("Merge Cond 解析不出连接列：%r"
                         % node.raw.get("Merge Cond", ""))
    left_alias, left_col, right_alias, right_col = match.groups()

    def side_for(child):
        alias = (child.alias or child.relation or "").lower()
        if left_alias and left_alias.lower() == alias:
            return left_col
        if right_alias and right_alias.lower() == alias:
            return right_col
        raise ModelError(
            "Merge Cond 里的别名（%s / %s）与子节点 %r 对不上 —— "
            "对错边会把两侧扫描比例互换，而互换后仍是两个合法的数字。"
            % (left_alias, right_alias, alias))

    stats = []
    for child in (outer, inner):
        if not child.relation:
            raise ModelError("归并的一侧不是基表扫描（%s），取不到连接列统计"
                             % child.node_type)
        stats.append(selectivity.from_row(
            catalog.column(child.relation, side_for(child))))
    return selectivity.merge_scan_fractions(stats[0], stats[1])


# --- 内部 --------------------------------------------------------------------

def _relation_of(node: PlanNode) -> str:
    if not node.relation:
        raise ModelError(
            "%s 节点没有 Relation Name —— 可能扫的是子查询、CTE 或函数结果，"
            "目录里查不到页数和行数。" % node.node_type)
    return node.relation


def _two_children(node: PlanNode, children: List[PlanNode]):
    if len(children) != 2:
        raise ModelError("%s 应有 2 个子节点，实际 %d 个"
                         % (node.node_type, len(children)))
    return children[0], children[1]


def _count_clauses(cond: str) -> int:
    """连接条件里有几个等值子句 —— 哈希函数按列数收费。"""
    if not cond:
        return 1
    return max(1, cond.count("=") - cond.count("=="))


def _index_scan(node: PlanNode, catalog, cost, variant, dop):
    index = catalog.index(node.index_name) if node.index_name else None
    if index is None:
        raise ModelError("索引扫描节点没有 Index Name")
    table = catalog.table(_relation_of(node))

    columns = indexed_columns(index.definition)
    if not columns:
        raise ModelError(
            "索引 %s 是表达式索引或定义解析不出列名 —— 拿不到 correlation，"
            "回表 IO 算不了。" % index.name)
    stat = catalog.column(table.name, columns[0])
    if stat.correlation is None:
        raise ModelError(
            "列 %s.%s 的 correlation 是 NULL —— 不能当 0 处理，"
            "0 是「完全无关」这个结论。" % (table.name, columns[0]))

    planner_tuples = table.planner_tuples
    if planner_tuples <= 0:
        raise ModelError("表 %s 的规划器行数为 0，选择率无从反推" % table.name)

    # **选择率从实测 Plan Rows 反推**，不自己估：校准检验的是代价公式，
    # 与选择率估算解耦。两处误差混在一起时，对不上就分不清是哪边错的。
    selectivity = min(1.0, max(0.0, node.plan_rows / planner_tuples))

    inp = costmodel.IndexScanInput(
        index_pages=float(index.pages), index_tuples=float(index.tuples),
        table_pages=float(table.cur_pages), table_tuples=planner_tuples,
        selectivity=selectivity, correlation=float(stat.correlation),
        total_table_pages=catalog.total_table_pages(),
        # **不要 max(1, …)。** 没有 Index Cond 时（merge join 底下的全索引
        # 扫描就是这样）条件数就是 0，凑成 1 会给每个索引条目多收一份
        # cpu_operator_cost。og5 实测:bench_order_items 多收 4999991×0.0025
        # = 12500，正好是当时 9.46% 偏差的全部来源。
        num_index_quals=count_qual_operators(node.raw.get("Index Cond", "")))
    return costmodel.index_scan(inp, cost, loop_count=1.0, variant=variant)
