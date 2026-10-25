"""选择率估算的单测。

这一层错了，后面的代价公式再准也没用：代价 = f(选择率)。而它的错法全是
安静的 —— 算出来永远是个 0 到 1 之间的合法数字，不会有任何东西报错。
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


sel = _load("sqltune_selectivity_for_test", "selectivity.py")


def _stats(**kw):
    base = dict(table="t", column="c", n_distinct=100.0, null_frac=0.0,
                correlation=0.5, mcv=[], mcv_freqs=[], histogram=[])
    base.update(kw)
    return sel.ColumnStats(**base)


# --- n_distinct 的正负约定 ---------------------------------------------------

def test_positive_n_distinct_is_a_count():
    assert _stats(n_distinct=50.0).distinct_count(1000000) == 50.0


def test_negative_n_distinct_is_a_ratio_not_a_count():
    """**-1 表示全唯一。**

    当成个数用会算出选择率 1/1 = 1，意思是「每次查都命中全表」—— 与真相
    （每次只命中一行）正好相反，而 1.0 是个合法选择率，不会报错。
    """
    assert _stats(n_distinct=-1.0).distinct_count(1000000) == 1000000.0
    assert _stats(n_distinct=-0.5).distinct_count(1000000) == 500000.0


def test_zero_n_distinct_refuses():
    """0 是「不知道」，不是「只有一个值」。"""
    with pytest.raises(sel.SelectivityError) as ei:
        _stats(n_distinct=0.0).distinct_count(1000)
    assert "不知道" in str(ei.value)


def test_unique_column_probe_selectivity():
    """1000 万行的唯一列，单值探测命中 1/1000 万。"""
    assert sel.index_probe(_stats(n_distinct=-1.0), 1e7) == pytest.approx(1e-7)


# --- 数组解析 ----------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("", []),
    ("{}", []),
    ("{1,2,3}", ["1", "2", "3"]),
    ("{a,b}", ["a", "b"]),
    ('{"a,b",c}', ["a,b", "c"]),
    ('{"say \\"hi\\"",x}', ['say "hi"', "x"]),
])
def test_parse_pg_array(text, expected):
    assert sel.parse_pg_array(text) == expected


def test_quoted_comma_is_not_a_separator():
    """直接 split(',') 会让 MCV 个数凭空多一个，频率与值错位。"""
    assert len(sel.parse_pg_array('{"a,b,c"}')) == 1


def test_parse_freqs_accepts_leading_dot():
    """openGauss 输出 .000166667 这种写法。"""
    assert sel.parse_freqs("{.000166667,.5}") == [pytest.approx(0.000166667),
                                                  0.5]


def test_mcv_length_mismatch_refuses():
    """值和频率对不上号时任何一边都不能用 —— 按下标取会静默错位。"""
    with pytest.raises(sel.SelectivityError) as ei:
        sel.from_row({"tablename": "t", "attname": "c", "n_distinct": 10,
                      "null_frac": 0, "correlation": 0,
                      "most_common_vals": "{1,2,3}",
                      "most_common_freqs": "{0.1,0.2}",
                      "histogram_bounds": ""})
    assert "对不上号" in str(ei.value)


def test_from_row_parses_a_realistic_pg_stats_row():
    stat = sel.from_row({
        "tablename": "bench_reviews", "attname": "product_id",
        "n_distinct": 50263.0, "null_frac": 0.0, "correlation": 0.0110928,
        "most_common_vals": "{4447,6021,10188}",
        "most_common_freqs": "{.000166667,.000166667,.000166667}",
        "histogram_bounds": "{2,529,1014}"})
    assert stat.table == "bench_reviews" and stat.column == "product_id"
    assert stat.mcv == ["4447", "6021", "10188"]
    assert len(stat.mcv_freqs) == 3
    assert stat.histogram == ["2", "529", "1014"]


# --- 等值选择率 --------------------------------------------------------------

def test_eq_const_without_mcv_is_uniform():
    """(1 − null_frac) / n_distinct"""
    assert sel.eq_const(_stats(n_distinct=100.0, null_frac=0.1), 1000) \
        == pytest.approx(0.9 / 100)


def test_eq_const_hits_mcv_frequency_directly():
    """**倾斜列的关键。** 高频值的选择率是它自己的频率，与平均值无关。

    这里 'hot' 占 40%，而平均值只有 1%（100 个不同值）—— 差 40 倍。
    只用 n_distinct 会把最该建索引的场景判成不值得。
    """
    stat = _stats(n_distinct=100.0, mcv=["hot", "warm"], mcv_freqs=[0.4, 0.1])
    assert sel.eq_const(stat, 1000, "hot") == pytest.approx(0.4)


def test_eq_const_miss_mcv_uses_the_remainder():
    """不在 MCV 里：从剩下的比例里平摊，不是拿总量除以 n_distinct。"""
    stat = _stats(n_distinct=100.0, null_frac=0.05,
                  mcv=["hot", "warm"], mcv_freqs=[0.4, 0.1])
    expected = (1.0 - 0.5 - 0.05) / (100 - 2)
    assert sel.eq_const(stat, 1000, "cold") == pytest.approx(expected)


def test_eq_const_without_value_falls_back_to_average():
    """参数化 SQL 不知道要查什么值 —— 退回平均，不拿某个高频值代表全部。"""
    stat = _stats(n_distinct=100.0, mcv=["hot"], mcv_freqs=[0.4])
    assert sel.eq_const(stat, 1000, None) == pytest.approx((1 - 0.4) / 99)


def test_mcv_covering_everything_but_value_missing_refuses():
    stat = _stats(n_distinct=2.0, mcv=["a", "b"], mcv_freqs=[0.6, 0.4])
    with pytest.raises(sel.SelectivityError):
        sel.eq_const(stat, 1000, "c")


# --- 连接选择率 --------------------------------------------------------------

def test_eq_join_uses_the_larger_distinct_count():
    """取较大的那个：多的那边决定了有多少值匹配不上。

    取较小的会高估匹配行数，从而**高估 join 的收益** —— 恰恰是会让人
    多建一条没用索引的方向。
    """
    left = _stats(n_distinct=1000.0)
    right = _stats(n_distinct=10.0)
    assert sel.eq_join(left, 1e6, right, 1e6) == pytest.approx(1.0 / 1000)


def test_eq_join_accounts_for_nulls_on_both_sides():
    left = _stats(n_distinct=100.0, null_frac=0.1)
    right = _stats(n_distinct=100.0, null_frac=0.2)
    assert sel.eq_join(left, 1e6, right, 1e6) == pytest.approx(0.9 * 0.8 / 100)


def test_eq_join_of_unique_columns():
    """两侧都唯一的 1000 万行表：选择率 1e-7。"""
    u = _stats(n_distinct=-1.0)
    assert sel.eq_join(u, 1e7, u, 1e7) == pytest.approx(1e-7)


# --- 边界 --------------------------------------------------------------------

def test_selectivity_is_clamped_to_unit_interval():
    stat = _stats(n_distinct=0.5)      # 不同值个数不足 1
    assert 0.0 <= sel.eq_const(stat, 1000) <= 1.0
