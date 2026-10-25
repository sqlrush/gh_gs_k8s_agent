"""假设路径的单测。

这一层与别处有个本质区别：它算的是「如果……会怎样」，**没有答案可对**。
所以测试重点不是数值精度，而是：估算成分有没有如实标出、偏差方向说没说清、
重算不了的祖先有没有被当成 0。最后一条最危险 —— 那会让假设路径凭空便宜。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-sqltune" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import costconst  # noqa: E402
import costmodel  # noqa: E402
import plantree  # noqa: E402
import whatif  # noqa: E402

COST = costconst.CostConstants(
    seq_page_cost=1.0, random_page_cost=1.1, cpu_tuple_cost=0.01,
    cpu_index_tuple_cost=0.005, cpu_operator_cost=0.0025, block_size=8192,
    effective_cache_size=1572864, work_mem=16 * 1024 * 1024, query_dop=2)


# --- 索引大小估算 ------------------------------------------------------------

def test_index_size_for_an_int_column():
    """8 字节列：每条 MAXALIGN(8+8)+4 = 20 字节；
    每页 (8192−40)×0.9 ÷ 20 = 366 条。"""
    size = whatif.estimate_index_size(1000000, 8, 8192)
    assert size.entry_bytes == 20
    assert size.entries_per_page == 366
    leaf = -(-1000000 // 366)
    assert size.pages == leaf + -(-leaf // 365)


def test_wider_column_needs_more_pages():
    narrow = whatif.estimate_index_size(1000000, 8, 8192)
    wide = whatif.estimate_index_size(1000000, 200, 8192)
    assert wide.pages > narrow.pages * 5


def test_page_overhead_is_counted():
    """页头 24 + special 16 = 40 字节。漏掉会让每页多塞几条，索引估小 ——
    而估小的方向正好让「加索引」显得更划算。"""
    size = whatif.estimate_index_size(1, 8, 8192)
    assert size.entries_per_page == int((8192 - 40) * 0.9 // 20)


def test_missing_avg_width_refuses():
    with pytest.raises(costmodel.ModelError) as ei:
        whatif.estimate_index_size(1000, 0, 8192)
    assert "ANALYZE" in str(ei.value)


def test_absurdly_wide_column_refuses():
    with pytest.raises(costmodel.ModelError):
        whatif.estimate_index_size(1000, 100000, 8192)


# --- 代进树里重算 ------------------------------------------------------------

_PLAN = [{"Plan": {
    "Node Type": "Streaming(type: LOCAL GATHER dop: 1/2)",
    "Startup Cost": 0.0, "Total Cost": 1000.0,
    "Plan Rows": 100.0, "Plan Width": 32,
    "Plans": [{"Node Type": "Seq Scan", "Relation Name": "big", "Alias": "b",
               "Startup Cost": 0.0, "Total Cost": 800.0,
               "Plan Rows": 100.0, "Plan Width": 32}]}}]
ROOT = plantree.parse(_PLAN)
SCAN = ROOT.children[0]


def _est(total, node_type="Index Scan（假设）"):
    return costmodel.Estimate(node_type=node_type, startup_cost=0.0,
                              total_cost=total, terms=[])


def test_ancestors_are_recomputed_with_the_new_child_cost():
    """把 800 的扫描换成 50，上层 Streaming 必须跟着**用公式重算**。

    不是「按实测差值平移」（1000 − 800 + 50）：祖先的代价是子节点代价的
    函数，换了子节点就该整条重算。平移法在祖先代价里含有与子节点无关的项
    时会算错，而这里的 Streaming 传输量只跟行数×行宽有关，与子节点代价无关
    —— 平移会把那一项算两遍。
    """
    def resolver(node, children):
        if node.node_type.startswith("Streaming"):
            return costmodel.streaming_gather(
                children[0].total_cost, children[0].startup_cost,
                node.plan_rows, node.plan_width, COST, 2)
        return None

    out = whatif.recompute_with_override(ROOT, resolver, SCAN, _est(50.0))
    transfer = 100.0 * 32 / 8192 * 1.3 * 1.5
    assert out.root_total == pytest.approx(50.0 + transfer)


def test_unmodeled_ancestor_keeps_measured_cost_and_is_flagged():
    """**最危险的一条。** 重算不了的祖先若当成 0，假设路径会凭空便宜。

    这里沿用实测值，总数偏保守，并把这件事记进 unmodeled 和 notes。
    """
    out = whatif.recompute_with_override(ROOT, lambda n, c: None, SCAN,
                                         _est(50.0))
    assert out.root_total == 1000.0            # 沿用实测，不是 50
    assert len(out.unmodeled) == 1
    root_est = [e for n, e in out.estimates if n is ROOT][0]
    assert root_est.approximate is True
    assert "偏保守" in root_est.notes[0]


def test_untouched_branches_keep_measured_values():
    """换一条访问路径不影响别的分支。"""
    plan = [{"Plan": {
        "Node Type": "Hash Join", "Startup Cost": 0.0, "Total Cost": 500.0,
        "Plan Rows": 10.0, "Plan Width": 8,
        "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "a", "Startup Cost": 0.0,
             "Total Cost": 300.0, "Plan Rows": 10.0, "Plan Width": 8},
            {"Node Type": "Seq Scan", "Relation Name": "b", "Startup Cost": 0.0,
             "Total Cost": 100.0, "Plan Rows": 10.0, "Plan Width": 8}]}}]
    root = plantree.parse(plan)
    seen = []

    def resolver(node, children):
        seen.append([c.total_cost for c in children])
        return _est(999.0, node.node_type)

    whatif.recompute_with_override(root, resolver, root.children[0], _est(7.0))
    # 未被替换的那一支仍带着实测的 100
    assert seen[0] == [7.0, 100.0]


def test_row_counts_are_not_changed_by_the_override():
    """换访问路径只改「花多少代价」，不改「产出多少行」。

    两件事一起改的话，对不上的时候分不清是哪一件造成的。
    """
    captured = {}

    def resolver(node, children):
        captured["rows"] = children[0].plan_rows
        captured["width"] = children[0].plan_width
        return _est(1.0)

    whatif.recompute_with_override(ROOT, resolver, SCAN, _est(50.0))
    assert captured["rows"] == 100.0
    assert captured["width"] == 32


# --- 假设索引扫描 ------------------------------------------------------------

class _Table:
    cur_pages = 45501
    planner_tuples = 2000938.0


class _Stat:
    column = "id"
    correlation = 0.9


def test_hypothetical_scan_is_always_approximate():
    est = whatif.hypothetical_index_scan(_Table(), _Stat(), 1e-6, COST,
                                         200000.0, 8)
    assert est.approximate is True
    assert est.node_type.endswith("（假设）")


def test_hypothetical_scan_declares_both_estimated_inputs():
    """索引大小和选择率两处都是估的，都得写进 notes 并说明偏差方向。"""
    est = whatif.hypothetical_index_scan(_Table(), _Stat(), 1e-6, COST,
                                         200000.0, 8)
    joined = "\n".join(est.notes)
    assert "索引尚不存在" in joined and "估小了" in joined
    assert "选择率" in joined and "不是实测" in joined


def test_selectivity_from_plan_rows_is_not_called_an_estimate():
    """**别把已有的严谨性说没了。**

    索引建在已经在过滤的那一列上时，选择率是从实测 Plan Rows 反推的，不是估的。
    把它标成「估算」会让读的人以为这里也有不确定性，而实际上没有 —— 反过来
    夸大不确定性同样是不准确，只是方向安全而已。
    """
    est = whatif.hypothetical_index_scan(_Table(), _Stat(), 1e-6, COST,
                                         200000.0, 8,
                                         selectivity_from_plan=True)
    joined = "\n".join(est.notes)
    assert "从**实测**计划里反推" in joined
    assert "不是实测" not in joined


# --- 从计划里提候选 ----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("(id > 1000)", ["id"]),
    ("(b.product_id = 4447)", ["product_id"]),
    ("((a > 1) AND (b = 2))", ["a", "b"]),
    ("((a > 1) AND (a < 9))", ["a"]),            # 去重
    ("", []),
])
def test_filter_columns(text, expected):
    assert whatif.filter_columns(text) == expected


def test_expression_filter_yields_no_candidate():
    """lower(name) = 'x' 要建的是表达式索引 —— 与本模块能估的普通 btree 不是
    一回事。猜一个列名出来会给出一条建了也不会被用的索引建议。"""
    assert whatif.filter_columns("(lower(name) = 'x')") == []


def test_missing_correlation_refuses():
    class _NoCorr:
        column = "id"
        correlation = None

    with pytest.raises(costmodel.ModelError) as ei:
        whatif.hypothetical_index_scan(_Table(), _NoCorr(), 1e-6, COST,
                                       200000.0, 8)
    assert "correlation" in str(ei.value)
