"""Streaming(LOCAL GATHER) 复算的单测。

十组期望值全部来自 og5 的 EXPLAIN —— 五张表 × dop∈{2,4}。这个算子 PostgreSQL
没有、openGauss 文档也没写公式，所以除了实测没有别的校验手段：一旦这些数字
红了，就是内核版本换了或者公式反解错了，两种都必须当场知道。
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


costconst = _load("sqltune_costconst_for_stream_test", "costconst.py")
cm = _load("sqltune_costmodel_for_stream_test", "costmodel.py")

COST = costconst.CostConstants(
    seq_page_cost=1.0, random_page_cost=1.1,
    cpu_tuple_cost=0.01, cpu_index_tuple_cost=0.005, cpu_operator_cost=0.0025,
    block_size=8192, effective_cache_size=1572864, work_mem=16 * 1024 * 1024,
    query_dop=2)

# (表, dop, 子节点 total, 行数, 行宽, Streaming 实测 total)
_OG5 = [
    ("loadtest.big", 2, 343747.27, 9959954, 208, 836881.71),
    ("snapshot.snap_summary_statement", 2, 29454.07, 554914, 659, 116501.46),
    ("demo_mem.big_orders", 2, 33755.19, 2000938, 148, 104247.22),
    ("gaussdb.bench_order_items", 2, 47008.46, 4999991, 33, 86284.51),
    ("gaussdb.bench_reviews", 2, 28605.50, 3000000, 36, 54313.51),
    ("loadtest.big", 4, 174373.64, 9959954, 208, 585319.00),
    ("snapshot.snap_summary_statement", 4, 17227.03, 554914, 659, 89766.53),
    ("demo_mem.big_orders", 4, 19377.60, 2000938, 148, 78120.95),
    ("gaussdb.bench_order_items", 4, 26004.23, 4999991, 33, 58734.27),
    ("gaussdb.bench_reviews", 4, 16802.75, 3000000, 36, 38226.09),
]


@pytest.mark.parametrize("name,dop,child,rows,width,expected", _OG5,
                         ids=["%s-dop%d" % (r[0], r[1]) for r in _OG5])
def test_matches_og5_measurements(name, dop, child, rows, width, expected):
    est = cm.streaming_gather(child, 0.0, float(rows), width, COST, dop)
    assert est.total_cost == pytest.approx(expected, abs=0.01)


def test_transfer_scales_with_bytes_not_rows():
    """代价看的是**传输字节数**，不是行数 —— 窄表多行可以比宽表少行便宜。

    bench_order_items（500 万行 × 33 宽）的传输量小于
    snap_summary_statement（55 万行 × 659 宽），尽管行数多九倍。
    """
    wide = cm.streaming_gather(0.0, 0.0, 554914.0, 659, COST, 2).total_cost
    narrow = cm.streaming_gather(0.0, 0.0, 4999991.0, 33, COST, 2).total_cost
    assert narrow < wide


def test_higher_dop_costs_less_per_block():
    """C = 1.3 × (1 + 1/dop)：dop 越高，单块传输越便宜（1.95 → 1.625）。"""
    two = cm.streaming_gather(0.0, 0.0, 1000000.0, 100, COST, 2).total_cost
    four = cm.streaming_gather(0.0, 0.0, 1000000.0, 100, COST, 4).total_cost
    assert two / four == pytest.approx(1.95 / 1.625)


def test_startup_follows_child():
    est = cm.streaming_gather(100.0, 7.5, 1000.0, 10, COST, 2)
    assert est.startup_cost == pytest.approx(7.5)


def test_terms_sum_to_total():
    est = cm.streaming_gather(343747.27, 0.0, 9959954.0, 208, COST, 2)
    assert sum(t.value for t in est.terms) == pytest.approx(est.total_cost)


@pytest.mark.parametrize("bad", [{"dop": 0}, {"rows": -1.0}, {"width": -1}])
def test_bad_inputs_refuse(bad):
    kwargs = dict(child_total=100.0, child_startup=0.0, rows=1000.0,
                  width=10, cost=COST, dop=2)
    kwargs.update(bad)
    with pytest.raises(cm.ModelError):
        cm.streaming_gather(**kwargs)
