"""代价模型的单测（不连库）。

Mackert-Lohman 那几条是手算出来的期望值，逐个对到小数 —— 这一项是索引扫描
与顺序扫描之间的胜负手，错了的方向是高估索引代价，于是本该推荐的索引被判成
没收益。那是个不会报错、也不会有人质疑的结论。
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


costconst = _load("sqltune_costconst_for_model_test", "costconst.py")
costmodel = _load("sqltune_costmodel_for_test", "costmodel.py")

COST = costconst.CostConstants(
    seq_page_cost=1.0, random_page_cost=4.0,
    cpu_tuple_cost=0.01, cpu_index_tuple_cost=0.005, cpu_operator_cost=0.0025,
    block_size=8192, effective_cache_size=524288, work_mem=64 * 1024 * 1024,
)


# --- Mackert-Lohman ----------------------------------------------------------
#
# 两个分支各自手算：
#   T <= b（缓存装得下整表）  T=100, total=100, cache=1000 -> b=1000
#   T >  b（装不下）          T=1000, total=1000, cache=100 -> b=100

def _fetch(tuples, pages, total, cache, index_pages=0.0):
    return costmodel.index_pages_fetched(tuples, pages, index_pages, total, cache)


def test_cached_branch_small_fetch():
    """(2·T·N)/(2·T+N) = (2·100·10)/(200+10) = 9.52 -> 向上取整 10"""
    assert _fetch(10, 100, 100, 1000) == 10


def test_cached_branch_caps_at_table_pages():
    """取得足够多时，读的页数封顶在整表页数 —— 不会比全表扫还多。"""
    assert _fetch(10000, 100, 100, 1000) == 100


def test_uncached_branch_below_limit():
    """T=1000 > b=100，N=50 <= lim=105.26：仍走 (2TN)/(2T+N)=48.78 -> 49"""
    assert _fetch(50, 1000, 1000, 100) == 49


def test_uncached_branch_above_limit():
    """N=1000 > lim：b + (N-lim)·(T-b)/T = 100 + 894.74·0.9 = 905.26 -> 906"""
    assert _fetch(1000, 1000, 1000, 100) == 906


def test_single_page_table_treated_as_one():
    assert _fetch(5, 0, 1, 1000) == 1


def test_monotonic_in_tuples_fetched():
    """取的行越多，读的页不能变少。"""
    prev = 0
    for n in (1, 10, 100, 1000, 10000, 100000):
        cur = _fetch(n, 1000, 1000, 100)
        assert cur >= prev, "N=%d 时读的页数反而变少了" % n
        prev = cur


def test_cached_branch_never_exceeds_table_pages():
    """缓存装得下时封顶在整表页数。"""
    for n in (1, 10, 1000, 10 ** 7):
        assert _fetch(n, 100, 100, 1000) <= 100


def test_uncached_branch_may_exceed_table_pages_on_purpose():
    """装不下时读的页数**可以**超过表页数，这不是 bug。

    取数足够多时同一页会被挤出缓存再读一次，物理读页数确实会超。顺手加个
    min(…, T) 会让「反复重读」从代价里消失，嵌套循环的内层扫描被系统性低估
    —— 而低估的方向正好是让「改成嵌套循环」这个建议显得更划算。
    """
    assert _fetch(10 ** 7, 1000, 1000, 100) > 1000


def test_bad_total_pages_raises():
    """表页数比「参与竞争的总页数」还大 —— 调用方传错了，不能算下去。"""
    with pytest.raises(costmodel.ModelError):
        _fetch(10, 5000, 100, 1000)


def test_zero_cache_raises():
    with pytest.raises(costmodel.ModelError):
        _fetch(10, 100, 100, 0)


# --- clamp_row_est -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (0.0, 1.0), (0.0001, 1.0), (1.0, 1.0), (1.4, 1.0), (1.6, 2.0), (100.0, 100.0),
])
def test_clamp_row_est(raw, expected):
    assert costmodel.clamp_row_est(raw) == expected


def test_clamp_row_est_rejects_nan():
    with pytest.raises(costmodel.ModelError):
        costmodel.clamp_row_est(float("nan"))


# --- Seq Scan ----------------------------------------------------------------

def test_seq_scan_without_filter_is_exact():
    """1.0×1234568 + 0.01×1e8 = 1234568 + 1000000"""
    est = costmodel.seq_scan(1234568, 100000000, COST)
    assert est.total_cost == pytest.approx(2234568.0)
    assert est.startup_cost == 0.0
    assert est.approximate is False
    assert [t.label for t in est.terms] == ["顺序读页", "每行 CPU"]


def test_seq_scan_terms_carry_the_arithmetic():
    """报告要能逐项复核，所以算式里得有代入后的实际数值。"""
    est = costmodel.seq_scan(100, 1000, COST)
    assert est.terms[0].formula == "1 × 100"
    assert est.terms[0].value == pytest.approx(100.0)
    assert est.terms[1].formula == "0.01 × 1000"


def test_seq_scan_with_qual_adds_operator_cost():
    """+ 0.0025 × 1 × 1e8 = 250000"""
    est = costmodel.seq_scan(1234568, 100000000, COST,
                             qual_operators=1, has_filter=True)
    assert est.total_cost == pytest.approx(2484568.0)
    assert est.approximate is True


def test_seq_scan_filter_without_operator_count_says_so():
    """数不出操作符个数时按 0 计，但必须留话 —— 否则「偏低」看不出来。"""
    est = costmodel.seq_scan(100, 1000, COST, qual_operators=0, has_filter=True)
    assert est.approximate is True
    assert est.notes and "没数出" in est.notes[0]


def test_seq_scan_rejects_negative_inputs():
    with pytest.raises(costmodel.ModelError):
        costmodel.seq_scan(-1, 1000, COST)


def test_seq_scan_rejects_dop_below_one():
    with pytest.raises(costmodel.ModelError):
        costmodel.seq_scan(100, 1000, COST, dop=0)


# --- 与 og5 实测对齐 ---------------------------------------------------------
#
# 下面九组数字全部来自 og5 上的 EXPLAIN (FORMAT JSON)，三张表 × dop∈{1,2,4}。
# 它们是这份代价模型唯一的外部锚点 —— 公式抄错、并行公式反解错、页数取错
# 来源，任何一样都会让这九条里的某几条红。
#
# 注意传进去的是**实时块数**和**密度换算后的行数**，不是 pg_class 的冻结值：
# snap_summary_statement 冻结 49596 页 / 535865 行，实时 51359 页 / 554914 行。
_OG5 = [
    # (表, 实时块数, 换算行数, {dop: 实测 total})
    ("loadtest.big", 585895, 9959954,
     {1: 685494.54, 2: 343747.27, 4: 174373.64}),
    ("snapshot.snap_summary_statement", 51359, 554914,
     {1: 56908.14, 2: 29454.07, 4: 17227.03}),
    ("demo_mem.big_orders", 45501, 2000938,
     {1: 65510.38, 2: 33755.19, 4: 19377.60}),
]


@pytest.mark.parametrize("name,pages,tuples,by_dop", _OG5,
                         ids=[r[0] for r in _OG5])
@pytest.mark.parametrize("dop", [1, 2, 4])
def test_matches_og5_measurements(name, pages, tuples, by_dop, dop):
    est = costmodel.seq_scan(float(pages), float(tuples), COST, dop=dop)
    # EXPLAIN 只给两位小数，所以按绝对值 0.01 比
    assert est.total_cost == pytest.approx(by_dop[dop], abs=0.01)


def test_dop_one_is_the_plain_postgresql_formula():
    """dop=1 时不该有任何并行项 —— 退化成标准公式。"""
    est = costmodel.seq_scan(585895.0, 9959954.0, COST, dop=1)
    assert [t.label for t in est.terms] == ["顺序读页", "每行 CPU"]
    assert est.total_cost == pytest.approx(685494.54, abs=0.01)


def test_parallel_setup_is_charged_per_extra_thread():
    """dop=2 收 1000，dop=4 收 3000 —— 是 1000×(dop−1)，不是 1000×dop。"""
    base = costmodel.seq_scan(585895.0, 9959954.0, COST, dop=1).total_cost
    two = costmodel.seq_scan(585895.0, 9959954.0, COST, dop=2).total_cost
    four = costmodel.seq_scan(585895.0, 9959954.0, COST, dop=4).total_cost
    assert two - base / 2 == pytest.approx(1000.0)
    assert four - base / 4 == pytest.approx(3000.0)
