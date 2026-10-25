"""索引扫描复算的单测（不连库）。

期望值全部手算。最重要的一条是 test_single_row_probe_matches_canonical_plan：
它算出来的 0.435..8.4525 正是真实 PostgreSQL 计划里随处可见的
`(cost=0.43..8.45 rows=1 ...)` —— 一个独立于本实现的旁证。手抄公式抄错一项，
这个数就对不上了。
"""
import importlib.util
import math
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


costconst = _load("sqltune_costconst_for_idx_test", "costconst.py")
cm = _load("sqltune_costmodel_for_idx_test", "costmodel.py")

COST = costconst.CostConstants(
    seq_page_cost=1.0, random_page_cost=4.0,
    cpu_tuple_cost=0.01, cpu_index_tuple_cost=0.005, cpu_operator_cost=0.0025,
    block_size=8192, effective_cache_size=524288, work_mem=64 * 1024 * 1024,
)

# 一张 1000 万行、10 万页的表，上面一条 27400 页的唯一索引；
# 本查询还涉及另一张表，两表合计 20 万页。
_TABLE_PAGES = 100000.0
_TABLE_TUPLES = 10000000.0
_INDEX_PAGES = 27400.0
_INDEX_TUPLES = 10000000.0
_TOTAL_PAGES = 200000.0


def _input(selectivity, correlation, **kw):
    kwargs = dict(
        index_pages=_INDEX_PAGES, index_tuples=_INDEX_TUPLES,
        table_pages=_TABLE_PAGES, table_tuples=_TABLE_TUPLES,
        selectivity=selectivity, correlation=correlation,
        total_table_pages=_TOTAL_PAGES,
    )
    kwargs.update(kw)
    return cm.IndexScanInput(**kwargs)


# --- 与真实计划对齐的锚点 ----------------------------------------------------

def test_single_row_probe_matches_canonical_plan():
    """唯一索引单行探测，逐项手算：

        索引读页        1 页 × 4                    = 4.0
        索引条目 CPU    1 × (0.005 + 0.0025)        = 0.0075
        btree 下降比较  ceil(log2(1e7))=24 × 0.0025 = 0.06
        索引层下降 CPU  (2+1) × 50 × 0.0025         = 0.375
        回表 IO         1 页 × 4                    = 4.0
        回表每行 CPU    0.01 × 1                    = 0.01
        ────────────────────────────────────────────────
        startup = 0.06 + 0.375                      = 0.435
        total                                       = 8.4525

    0.43..8.45 正是真实计划里单行索引探测的典型数字。
    """
    est = cm.index_scan(_input(1e-7, 1.0), COST)
    assert est.startup_cost == pytest.approx(0.435)
    assert est.total_cost == pytest.approx(8.4525)


def test_terms_sum_to_total():
    """逐项之和必须等于总数 —— 报告要让人逐项复核，对不上就是渲染在骗人。"""
    est = cm.index_scan(_input(1e-7, 1.0), COST)
    assert sum(t.value for t in est.terms) == pytest.approx(est.total_cost)


def test_probe_is_marked_approximate_because_tree_height_is_estimated():
    est = cm.index_scan(_input(1e-7, 1.0), COST)
    assert est.approximate is True
    assert est.notes and "树高" in est.notes[0]


def test_known_tree_height_is_not_approximate():
    est = cm.index_scan(_input(1e-7, 1.0, tree_height=2), COST)
    assert est.approximate is False
    assert est.total_cost == pytest.approx(8.4525)   # 估出来的就是 2，值不变


# --- correlation 是胜负手 ----------------------------------------------------

def test_correlation_dominates_range_scan_cost():
    """选择率 1%、取 10 万行：

        完全无序 correlation=0 → 回表 66667 页 × 4 = 266668
        完全有序 correlation=1 → 4 + 999 × 1      = 1003

    差 266 倍。凭感觉给 correlation 就是在这个量级上编数。
    """
    hot = cm.index_scan(_input(0.01, 1.0), COST)
    cold = cm.index_scan(_input(0.01, 0.0), COST)
    assert cold.total_cost - hot.total_cost == pytest.approx(266668 - 1003)


def test_correlation_sign_does_not_matter():
    """插值用的是 correlation²，反向相关与正向同样有效。"""
    a = cm.index_scan(_input(0.01, -1.0), COST)
    b = cm.index_scan(_input(0.01, 1.0), COST)
    assert a.total_cost == pytest.approx(b.total_cost)


def test_missing_correlation_refuses():
    """NULL 不能当 0 —— 0 是「完全无关」这个结论，不是「不知道」。"""
    with pytest.raises(cm.ModelError) as ei:
        cm.index_scan(_input(0.01, None), COST)
    assert "correlation" in str(ei.value)


# --- 两处版本分歧点 ----------------------------------------------------------

def test_min_io_variant_changes_correlated_cost():
    """9.2 写法（全随机 1000×4=4000）与 9.3+ 写法（4+999×1=1003）差 2997。"""
    modern = cm.index_scan(_input(0.01, 1.0), COST, variant=cm.Variant())
    legacy = cm.index_scan(_input(0.01, 1.0), COST,
                           variant=cm.Variant(min_io_split_seq=False))
    assert legacy.total_cost - modern.total_cost == pytest.approx(4000 - 1003)


def test_page_cpu_variant_removes_that_term():
    """关掉 9.3 才加的那一项，正好少 (2+1)×50×0.0025 = 0.375。"""
    on = cm.index_scan(_input(1e-7, 1.0, tree_height=2), COST)
    off = cm.index_scan(_input(1e-7, 1.0, tree_height=2), COST,
                        variant=cm.Variant(btree_page_cpu_cost=False))
    assert on.total_cost - off.total_cost == pytest.approx(0.375)
    assert on.startup_cost - off.startup_cost == pytest.approx(0.375)


# --- 重复探测 ----------------------------------------------------------------

def test_loop_count_amortises_shared_pages():
    """嵌套循环内层：100 次探测共享缓存，单次均摊代价不高于独立一次。"""
    once = cm.index_scan(_input(0.001, 0.5), COST, loop_count=1.0)
    many = cm.index_scan(_input(0.001, 0.5), COST, loop_count=100.0)
    assert many.total_cost <= once.total_cost
    assert many.total_cost > 0


# --- 树高估算 ----------------------------------------------------------------

def test_estimate_tree_height_typical():
    """扇出 1e7/27400 = 365，ceil(log(1e7)/log(365)) - 1 = 2"""
    assert cm.estimate_tree_height(27400, 10000000) == 2


@pytest.mark.parametrize("pages,tuples", [(0, 100), (1, 100), (10, 1), (10, 0)])
def test_estimate_tree_height_degenerate_is_zero(pages, tuples):
    assert cm.estimate_tree_height(pages, tuples) == 0


def test_estimate_tree_height_refuses_bloated_index():
    """每页平均不到 1 条 —— 扇出模型不成立，不能凑个数糊过去。"""
    with pytest.raises(cm.ModelError) as ei:
        cm.estimate_tree_height(1000, 500)
    assert "膨胀" in str(ei.value)


# --- 输入校验 ----------------------------------------------------------------

@pytest.mark.parametrize("sel", [-0.1, 1.5, float("nan")])
def test_bad_selectivity_refuses(sel):
    with pytest.raises(cm.ModelError):
        cm.index_scan(_input(sel, 0.5), COST)


@pytest.mark.parametrize("corr", [-1.5, 1.5])
def test_bad_correlation_refuses(corr):
    with pytest.raises(cm.ModelError):
        cm.index_scan(_input(0.01, corr), COST)


def test_unanalyzed_index_refuses():
    """索引建好没 ANALYZE 时 relpages/reltuples 是 0，此时复算无意义。"""
    with pytest.raises(cm.ModelError) as ei:
        cm.index_scan(_input(0.01, 0.5, index_pages=0, index_tuples=0), COST)
    assert "ANALYZE" in str(ei.value)
