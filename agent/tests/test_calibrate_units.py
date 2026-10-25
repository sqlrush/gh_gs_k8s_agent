"""校准闸的单测（不连库）。

这道闸是整套推演唯一的可信度来源，所以测试重点全在**它会不会误判通过**：
零个节点建模、实测代价为 0、复算得到 NaN —— 每一条都能让「全部吻合」在
逻辑上成立而实际上什么都没验。

这里用普通 import（不是按路径唯一命名加载）：calibrate.py 内部 `from
costmodel import ModelError`，测试若加载出第二份 costmodel，抛出的 ModelError
与 calibrate 捕获的不是同一个类，except 抓不住 —— 测试会以一种极难看懂的
方式变绿或变红。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))

import calibrate  # noqa: E402
import costmodel  # noqa: E402
import plantree  # noqa: E402


_PLAN = [{
    "Plan": {
        "Node Type": "Hash Join", "Join Type": "Inner",
        "Startup Cost": 3457.0, "Total Cost": 213041.375,
        "Plan Rows": 100.0, "Plan Width": 64,
        "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "fact_sales", "Alias": "f",
             "Startup Cost": 0.0, "Total Cost": 208333.0,
             "Plan Rows": 100.0, "Plan Width": 32},
            {"Node Type": "Sort",
             "Startup Cost": 3457.0, "Total Cost": 3457.0,
             "Plan Rows": 100000.0, "Plan Width": 36},
        ],
    }
}]

ROOT = plantree.parse(_PLAN)


def _est(node_type, total, startup=0.0, approximate=False):
    return costmodel.Estimate(node_type=node_type, startup_cost=startup,
                              total_cost=total, approximate=approximate)


def _resolver(by_type):
    """by_type: {节点名: Estimate | None | ModelError 实例}"""
    def resolve(node, children):
        value = by_type.get(node.node_type, None)
        if isinstance(value, Exception):
            raise value
        return value
    return resolve


# --- 通过与不通过 ------------------------------------------------------------

def test_all_modeled_and_matching_passes():
    cal = calibrate.calibrate(ROOT, _resolver({
        "Hash Join": _est("Hash Join", 213041.375),
        "Seq Scan": _est("Seq Scan", 208333.0),
        "Sort": _est("Sort", 3457.0),
    }))
    assert cal.passed is True
    assert cal.coverage == 1.0
    assert cal.worst_deviation == pytest.approx(0.0)


def test_one_mismatch_fails_and_names_the_worst():
    cal = calibrate.calibrate(ROOT, _resolver({
        "Hash Join": _est("Hash Join", 213041.375),
        "Seq Scan": _est("Seq Scan", 150000.0),      # 差 28%
        "Sort": _est("Sort", 3457.0),
    }))
    assert cal.passed is False
    assert "Seq Scan" in cal.reason
    assert "150000" in cal.reason and "208333" in cal.reason


def test_zero_modeled_nodes_is_a_failure_not_a_pass():
    """空集上的「全部吻合」恒真 —— 这是这类闸门最经典的失效方式。"""
    cal = calibrate.calibrate(ROOT, _resolver({}))
    assert cal.passed is False
    assert cal.coverage == 0.0
    assert "一个节点都没能复算" in cal.reason


def test_unmodeled_nodes_do_not_fail_the_gate():
    """整棵树因为有个 Sort 就拒绝，功能在真实计划上永远用不了。"""
    cal = calibrate.calibrate(ROOT, _resolver({
        "Hash Join": _est("Hash Join", 213041.375),
        "Seq Scan": _est("Seq Scan", 208333.0),
    }))
    assert cal.passed is True
    assert cal.coverage == pytest.approx(2 / 3)


def test_tolerance_boundary():
    within = calibrate.calibrate(ROOT, _resolver({
        "Seq Scan": _est("Seq Scan", 208333.0 * 1.009)}))
    beyond = calibrate.calibrate(ROOT, _resolver({
        "Seq Scan": _est("Seq Scan", 208333.0 * 1.011)}))
    assert within.passed is True
    assert beyond.passed is False


# --- 「输入不足」与「没实现」要分得开 ----------------------------------------

def test_model_error_is_unmodeled_but_keeps_the_reason():
    cal = calibrate.calibrate(ROOT, _resolver({
        "Hash Join": _est("Hash Join", 213041.375),
        "Seq Scan": costmodel.ModelError("correlation 未知"),
    }))
    seq = [c for c in cal.checks if c.node_type == "Seq Scan"][0]
    assert seq.status == calibrate.UNMODELED
    assert "correlation 未知" in seq.reason


def test_not_implemented_says_so():
    cal = calibrate.calibrate(ROOT, _resolver({
        "Hash Join": _est("Hash Join", 213041.375)}))
    sort = [c for c in cal.checks if c.node_type == "Sort"][0]
    assert sort.reason == "该算子未建模"


def test_approximate_flag_is_carried_through():
    cal = calibrate.calibrate(ROOT, _resolver({
        "Seq Scan": _est("Seq Scan", 208333.0, approximate=True)}))
    seq = [c for c in cal.checks if c.node_type == "Seq Scan"][0]
    assert seq.approximate is True


# --- relative_deviation 的两个坑 ---------------------------------------------

def test_zero_measured_falls_back_to_absolute():
    """除以 0 会得到 inf/nan，而 nan <= tolerance 是 False…… 但 nan 参与
    任何比较都是 False，某些写法下反而会判成吻合。这里退化成绝对差。"""
    assert calibrate.relative_deviation(0.0, 0.0) == 0.0
    assert calibrate.relative_deviation(5.0, 0.0) == 5.0


def test_nan_is_infinite_deviation_not_a_pass():
    """nan 参与比较恒为 False —— 直接比大小会让算错的节点被判成吻合。"""
    nan = float("nan")
    assert calibrate.relative_deviation(nan, 100.0) == float("inf")
    assert calibrate.relative_deviation(100.0, nan) == float("inf")

    cal = calibrate.calibrate(ROOT, _resolver({
        "Seq Scan": _est("Seq Scan", float("nan"))}))
    assert cal.passed is False


def test_deviation_is_symmetric_in_sign():
    assert calibrate.relative_deviation(90.0, 100.0) == pytest.approx(0.1)
    assert calibrate.relative_deviation(110.0, 100.0) == pytest.approx(0.1)


# --- 变体选择 ----------------------------------------------------------------

def test_best_variant_finds_the_one_that_matches():
    """只有关掉 btree 页下降 CPU 那一版能对上 —— 闸门应当自己找到它。"""
    def factory(variant):
        total = 208333.0 if not variant.btree_page_cpu_cost else 999999.0
        return _resolver({"Seq Scan": _est("Seq Scan", total)})

    cal = calibrate.calibrate_best_variant(ROOT, factory)
    assert cal.passed is True
    assert cal.variant.btree_page_cpu_cost is False


def test_best_variant_reports_the_closest_attempt_when_none_match():
    def factory(variant):
        total = 208333.0 * (1.02 if variant.min_io_split_seq else 1.50)
        return _resolver({"Seq Scan": _est("Seq Scan", total)})

    cal = calibrate.calibrate_best_variant(ROOT, factory)
    assert cal.passed is False
    assert "四种内核版本变体都对不上" in cal.reason
    # 最接近的那次是 2% 那一版，偏差要能从 checks 里看出来
    assert cal.worst_deviation == pytest.approx(0.02)


# --- summary -----------------------------------------------------------------

def test_summary_counts_every_bucket():
    cal = calibrate.calibrate(ROOT, _resolver({
        "Hash Join": _est("Hash Join", 213041.375),
        "Seq Scan": _est("Seq Scan", 150000.0),
    }))
    text = cal.summary()
    assert "复算 2/3 个节点" in text
    assert "吻合 1" in text and "超差 1" in text and "未建模 1" in text
