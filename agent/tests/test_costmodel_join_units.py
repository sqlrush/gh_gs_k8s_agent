"""Join 算子复算的单测（不连库）。期望值手算。

join 复算拿的是子节点的**实测** cost，不是复算值 —— 这样每条测试检验的
是这一个算子自己的公式，而不是「扫描层 + join 层」的合成误差。
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


costconst = _load("sqltune_costconst_for_join_test", "costconst.py")
cm = _load("sqltune_costmodel_for_join_test", "costmodel.py")

COST = costconst.CostConstants(
    seq_page_cost=1.0, random_page_cost=4.0,
    cpu_tuple_cost=0.01, cpu_index_tuple_cost=0.005, cpu_operator_cost=0.0025,
    block_size=8192, effective_cache_size=524288, work_mem=64 * 1024 * 1024,
)


# --- Nested Loop -------------------------------------------------------------

def test_nested_loop_is_outer_plus_n_times_inner():
    """外层 Seq Scan 3457（100 行），内层索引探测 0.435..8.4525（1 行）：

        total = 3457 + 100 × 8.4525 + 0.01 × 100×1
              = 3457 + 845.25 + 1.0 = 4303.25
        startup = 0 + 0.435
    """
    est = cm.nested_loop(outer_total=3457.0, outer_startup=0.0,
                         inner_total=8.4525, inner_startup=0.435,
                         outer_rows=100.0, inner_rows=1.0, cost=COST)
    assert est.startup_cost == pytest.approx(0.435)
    assert est.total_cost == pytest.approx(4303.25)


def test_nested_loop_join_quals_charged_per_pair():
    """连接条件按**配对数**收费，不是按输出行数 —— 内层行数大时差很多。"""
    base = cm.nested_loop(100.0, 0.0, 10.0, 0.0, 100.0, 50.0, COST, join_quals=0)
    with_q = cm.nested_loop(100.0, 0.0, 10.0, 0.0, 100.0, 50.0, COST, join_quals=1)
    assert with_q.total_cost - base.total_cost == pytest.approx(0.0025 * 100 * 50)


def test_nested_loop_terms_sum_to_total():
    est = cm.nested_loop(3457.0, 0.0, 8.4525, 0.435, 100.0, 1.0, COST)
    assert sum(t.value for t in est.terms) == pytest.approx(est.total_cost)


def test_nested_loop_single_outer_row_degenerates_to_sum():
    """外层只有 1 行时就是「外 + 内」。"""
    est = cm.nested_loop(100.0, 5.0, 20.0, 2.0, 1.0, 1.0, COST)
    assert est.total_cost == pytest.approx(100.0 + 20.0 + 0.01)
    assert est.startup_cost == pytest.approx(7.0)


def test_nested_loop_rejects_negative_rows():
    with pytest.raises(cm.ModelError):
        cm.nested_loop(100.0, 0.0, 10.0, 0.0, -1.0, 1.0, COST)


# --- Hash Join ---------------------------------------------------------------

def _hash(**kw):
    kwargs = dict(outer_total=208333.0, outer_startup=0.0,
                  inner_total=3457.0, inner_rows=100000.0, inner_width=36,
                  outer_rows=100.0, output_rows=100.0, cost=COST)
    kwargs.update(kw)
    return cm.hash_join(**kwargs)


def test_hash_join_single_batch():
    """内表 10 万行 × 36 宽 = 6.4MB，装得下 64MB work_mem：

        build   = (0.0025 + 0.01) × 100000            = 1250
        startup = 0 + 3457 + 1250                     = 4707
        探测哈希 = 0.0025 × 100                        = 0.25
        桶内比较 = 0.0025 × 100 × 0.5                  = 0.125
        输出 CPU = 0.01 × 100                          = 1.0
        total   = 208333 + 3457 + 1250 + 0.25 + 0.125 + 1.0 = 213041.375
    """
    est = _hash()
    assert est.startup_cost == pytest.approx(4707.0)
    assert est.total_cost == pytest.approx(213041.375)


def test_hash_join_startup_includes_whole_inner_scan():
    """内表必须全建完才能开始探测 —— 这是 hash join 与 nested loop 的分水岭。"""
    est = _hash()
    assert est.startup_cost > 3457.0


def test_hash_join_terms_sum_to_total():
    est = _hash()
    assert sum(t.value for t in est.terms) == pytest.approx(est.total_cost)


def test_hash_join_is_marked_approximate():
    """桶内比较按均匀分布近似，倾斜列上会低估 —— 必须自报。"""
    est = _hash()
    assert est.approximate is True
    assert est.notes and "倾斜" in est.notes[0]


def test_multi_batch_refuses_instead_of_guessing():
    """内表放不下 work_mem 就走多批次，批数猜错一档代价差一个量级。"""
    with pytest.raises(cm.ModelError) as ei:
        _hash(inner_rows=2000000.0)
    assert "work_mem" in str(ei.value)


def test_batch_boundary_uses_tuple_header():
    """临界点必须算上 24 字节元组头，否则会把多批次判成单批次。

    work_mem 恰好 6400000 B 时，10 万行 × (40+24) = 6400000 B 正好装下；
    少算元组头会得到 4000000 B，于是把一个已经溢出的场景判成没溢出。
    """
    tight = costconst.CostConstants(
        seq_page_cost=1.0, random_page_cost=4.0, cpu_tuple_cost=0.01,
        cpu_index_tuple_cost=0.005, cpu_operator_cost=0.0025,
        block_size=8192, effective_cache_size=524288, work_mem=6400000)
    _hash(cost=tight)                       # 正好等于，不抛
    with pytest.raises(cm.ModelError):
        _hash(cost=tight, inner_rows=100001.0)


# --- relation_byte_size ------------------------------------------------------

@pytest.mark.parametrize("width,aligned", [(32, 32), (33, 40), (36, 40), (40, 40)])
def test_maxalign_rounds_up_to_eight(width, aligned):
    assert cm.relation_byte_size(1, width) == aligned + 24


def test_relation_byte_size_counts_header_per_row():
    assert cm.relation_byte_size(100, 36) == 100 * (40 + 24)
