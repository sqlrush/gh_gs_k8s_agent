"""归并连接与直方图扫描比例的单测。

期望值来自 og5 实测的三组 EXPLAIN。这个算子有个反直觉的性质，也是它最容易
被写错的地方：**总代价可以小于两个子节点代价之和** —— 归并一边耗尽就停，
两侧都不一定扫完。不折算就会高估，而高估多少取决于两侧键值范围重合多少。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-sqltune" / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


costconst = _load("cc_for_mj_test", "costconst.py")
cm = _load("cm_for_mj_test", "costmodel.py")
sel = _load("sel_for_mj_test", "selectivity.py")

COST = costconst.CostConstants(
    seq_page_cost=1.0, random_page_cost=1.1, cpu_tuple_cost=0.01,
    cpu_index_tuple_cost=0.005, cpu_operator_cost=0.0025, block_size=8192,
    effective_cache_size=1572864, work_mem=16 * 1024 * 1024, query_dop=2)


# --- 直方图插值 --------------------------------------------------------------

def test_fraction_le_is_positional_because_buckets_are_equal_frequency():
    """等频直方图：位置就是比例。4 个桶，落在第 2 个边界上就是 0.5。"""
    hist = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert sel.fraction_le(hist, 20.0) == pytest.approx(0.5)
    assert sel.fraction_le(hist, 10.0) == pytest.approx(0.25)


def test_fraction_le_interpolates_inside_a_bucket():
    hist = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert sel.fraction_le(hist, 15.0) == pytest.approx(0.375)


@pytest.mark.parametrize("value,expected", [(-5.0, 0.0), (0.0, 0.0),
                                            (40.0, 1.0), (99.0, 1.0)])
def test_fraction_le_clamps_outside_the_range(value, expected):
    assert sel.fraction_le([0.0, 10.0, 20.0, 30.0, 40.0], value) == expected


def test_single_bound_histogram_is_unusable():
    """一个点画不出分布 —— 不能当成「全在这一边」。"""
    assert sel.fraction_le([5.0], 3.0) is None
    assert sel.fraction_le([], 3.0) is None


def test_non_numeric_histogram_is_refused():
    """字符串/日期的比较要走各自的排序规则，float() 硬转会悄悄得到错的顺序。"""
    stat = sel.ColumnStats(table="t", column="c", n_distinct=10.0,
                           null_frac=0.0, correlation=0.5, mcv=[], mcv_freqs=[],
                           histogram=["apple", "banana"])
    assert sel.numeric_histogram(stat) == []


# --- 扫描比例 ----------------------------------------------------------------

def _stat(hist):
    return sel.ColumnStats(table="t", column="id", n_distinct=-1.0,
                           null_frac=0.0, correlation=1.0, mcv=[], mcv_freqs=[],
                           histogram=[str(v) for v in hist])


def test_merge_fractions_when_ranges_coincide():
    """两侧范围一样 —— 都扫完。"""
    f = sel.merge_scan_fractions(_stat([1, 50, 100]), _stat([1, 50, 100]))
    assert (f.outer_end, f.inner_end) == (1.0, 1.0)
    assert (f.outer_start, f.inner_start) == (0.0, 0.0)


def test_merge_fractions_when_outer_extends_beyond_inner():
    """外层 0..100、内层 0..50：外层只需扫一半，内层扫完。

    **这就是父节点可以比子节点之和便宜的原因。**
    """
    f = sel.merge_scan_fractions(_stat([0, 50, 100]), _stat([0, 25, 50]))
    assert f.outer_end == pytest.approx(0.5)
    assert f.inner_end == pytest.approx(1.0)


def test_merge_fractions_none_when_histogram_missing():
    assert sel.merge_scan_fractions(_stat([1]), _stat([1, 2, 3])) is None


# --- 与 og5 实测对齐 ---------------------------------------------------------
#
# 三组数据来自 og5，enable_hashjoin/enable_nestloop 关掉后强制走归并。
# 扫描比例由两侧 id 列的直方图算出：bench_order_items 的键域到 4.99936e6，
# bench_reviews 只到 2.99978e6，所以外层只扫 60.13%。

def test_matches_og5_two_table_merge_join():
    est = cm.merge_join(
        outer_total=132100.42, outer_startup=0.0,
        inner_total=79262.15, inner_startup=0.0,
        outer_rows=4999991, inner_rows=3000000, output_rows=3000000,
        fractions=sel.MergeScanFractions(outer_start=0.0, outer_end=0.601274,
                                         inner_start=0.000108, inner_end=1.0),
        cost=COST)
    assert est.total_cost == pytest.approx(203706.53, abs=0.5)


def test_matches_og5_self_join():
    """两侧同一列 —— 都扫完，总代价 = 子和 + CPU。

    CPU = 0.0025×(300万+300万) + 0.01×300万 = 15000 + 30000 = 45000
    """
    est = cm.merge_join(
        outer_total=79262.15, outer_startup=0.0,
        inner_total=79262.15, inner_startup=0.0,
        outer_rows=3000000, inner_rows=3000000, output_rows=3000000,
        fractions=sel.MergeScanFractions(0.0, 1.0, 0.0, 1.0), cost=COST)
    assert est.total_cost == pytest.approx(203524.30, abs=0.01)
    assert est.total_cost - (79262.15 * 2) == pytest.approx(45000.0, abs=0.01)


def test_matches_og5_filtered_merge_join():
    est = cm.merge_join(
        outer_total=28885.65, outer_startup=0.0,
        inner_total=29166.14, inner_startup=0.0,
        outer_rows=998760, inner_rows=1008468, output_rows=998760,
        fractions=sel.MergeScanFractions(outer_start=0.0, outer_end=0.601274,
                                         inner_start=0.000108, inner_end=1.0),
        cost=COST)
    assert est.total_cost == pytest.approx(60544.41, abs=1.0)


def test_total_can_be_below_the_sum_of_children():
    """反直觉但正确 —— 写这个算子最容易错的地方就是没折算。"""
    est = cm.merge_join(
        outer_total=132100.42, outer_startup=0.0,
        inner_total=79262.15, inner_startup=0.0,
        outer_rows=4999991, inner_rows=3000000, output_rows=3000000,
        fractions=sel.MergeScanFractions(0.0, 0.601274, 0.000108, 1.0),
        cost=COST)
    assert est.total_cost < 132100.42 + 79262.15


def test_missing_fractions_refuses_instead_of_scanning_everything():
    """退化成「两边全扫」会在键域不重合时高估，且高估多少取决于数据。"""
    with pytest.raises(cm.ModelError) as ei:
        cm.merge_join(100.0, 0.0, 100.0, 0.0, 10, 10, 10, None, COST)
    assert "未建模" in str(ei.value)


def test_terms_sum_to_total():
    est = cm.merge_join(
        132100.42, 0.0, 79262.15, 0.0, 4999991, 3000000, 3000000,
        sel.MergeScanFractions(0.0, 0.601274, 0.000108, 1.0), COST)
    assert sum(t.value for t in est.terms) == pytest.approx(est.total_cost,
                                                            abs=10.0)
