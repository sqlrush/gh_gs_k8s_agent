"""代价常数解析的单测（不连库）。

这一层错了的表现是「每个复算值按同一比例偏」——看起来完全正常的数。
所以单位换算逐个钉死，缺项/怪单位一律断言抛错而不是断言取到了默认值。
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


costconst = _load("sqltune_costconst_for_test", "costconst.py")


# pg_settings 的真实形态：值和单位都是字符串，内存类带 unit
_ROWS = [
    {"name": "seq_page_cost", "setting": "1", "unit": ""},
    {"name": "random_page_cost", "setting": "4", "unit": ""},
    {"name": "cpu_tuple_cost", "setting": "0.01", "unit": ""},
    {"name": "cpu_index_tuple_cost", "setting": "0.005", "unit": ""},
    {"name": "cpu_operator_cost", "setting": "0.0025", "unit": ""},
    {"name": "block_size", "setting": "8192", "unit": ""},
    {"name": "effective_cache_size", "setting": "524288", "unit": "8kB"},
    {"name": "work_mem", "setting": "65536", "unit": "kB"},
    {"name": "query_dop", "setting": "2", "unit": ""},
]


def _rows_without(name):
    return [r for r in _ROWS if r["name"] != name]


def _rows_with(name, **changes):
    return [dict(r, **changes) if r["name"] == name else r for r in _ROWS]


# --- 正常换算 ----------------------------------------------------------------

def test_plain_cost_constants():
    c = costconst.from_gucs(_ROWS)
    assert c.seq_page_cost == 1.0
    assert c.random_page_cost == 4.0
    assert c.cpu_tuple_cost == 0.01
    assert c.cpu_index_tuple_cost == 0.005
    assert c.cpu_operator_cost == 0.0025


def test_effective_cache_size_is_pages_not_bytes():
    """setting=524288 unit=8kB 是 4GiB；除以 8192 得 524288 页。

    这里数值上凑巧与 setting 相同（因为 block_size 就是 8kB）——正因为凑巧，
    单位漏换算时本地测不出来。下面那条用非默认 block_size 才是真正的闸。
    """
    assert costconst.from_gucs(_ROWS).effective_cache_size == 524288


def test_effective_cache_size_follows_block_size():
    """block_size 不是 8kB 时，页数必须跟着变。

    默认当成 8kB 是个静默假设：实例上是 16kB 的话，页数会大一倍，
    而 Mackert-Lohman 的缓存项整体偏，算出来仍是个像样的数。
    """
    rows = _rows_with("block_size", setting="16384")
    assert costconst.from_gucs(rows).effective_cache_size == 262144


def test_work_mem_in_bytes():
    assert costconst.from_gucs(_ROWS).work_mem == 65536 * 1024


def test_block_size_in_bytes():
    assert costconst.from_gucs(_ROWS).block_size == 8192


@pytest.mark.parametrize("unit,setting,expected", [
    ("B", "1024", 1024),
    ("kB", "1024", 1024 * 1024),
    ("MB", "64", 64 * 1024 * 1024),
    ("GB", "1", 1024 ** 3),
    ("8kB", "2", 2 * 8 * 1024),
    ("16kB", "2", 2 * 16 * 1024),
])
def test_memory_unit_forms(unit, setting, expected):
    rows = _rows_with("work_mem", setting=setting, unit=unit)
    assert costconst.from_gucs(rows).work_mem == expected


def test_accepts_objects_not_only_dicts():
    """evidence.GUC 是 dataclass，不是 dict。"""
    class _G:
        def __init__(self, name, setting, unit):
            self.name, self.setting, self.unit = name, setting, unit

    rows = [_G(r["name"], r["setting"], r["unit"]) for r in _ROWS]
    assert costconst.from_gucs(rows).work_mem == 65536 * 1024


def test_describe_lists_every_constant():
    """报告要把本实例实际值原样列出来，少一个都不行。"""
    names = [n for n, _ in costconst.from_gucs(_ROWS).describe()]
    assert set(names) == {
        "seq_page_cost", "random_page_cost", "cpu_tuple_cost",
        "cpu_index_tuple_cost", "cpu_operator_cost", "block_size",
        "effective_cache_size", "work_mem", "query_dop"}


def test_query_dop_is_parsed():
    """openGauss 特有。缺了它当 1，实例上是 2 时每个复算值差一倍。"""
    assert costconst.from_gucs(_ROWS).query_dop == 2


def test_zero_query_dop_raises():
    with pytest.raises(costconst.MissingConstant):
        costconst.from_gucs(_rows_with("query_dop", setting="0"))


# --- 失败路径 ----------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "seq_page_cost", "random_page_cost", "cpu_tuple_cost",
    "cpu_index_tuple_cost", "cpu_operator_cost", "block_size",
    "effective_cache_size", "work_mem", "query_dop",
])
def test_missing_constant_raises_and_names_it(name):
    with pytest.raises(costconst.MissingConstant) as ei:
        costconst.from_gucs(_rows_without(name))
    assert name in str(ei.value), "报错要点名缺的是哪个 GUC"


def test_empty_setting_raises():
    """协议把 NULL 渲染成空串 —— 不能当成 0。"""
    with pytest.raises(costconst.MissingConstant):
        costconst.from_gucs(_rows_with("work_mem", setting=""))


def test_unknown_unit_raises_instead_of_assuming_bytes():
    """认不出的单位当字节处理，会让 effective_cache_size 小 8192 倍。"""
    with pytest.raises(costconst.MissingConstant) as ei:
        costconst.from_gucs(_rows_with("effective_cache_size", unit="pages"))
    assert "pages" in str(ei.value)


def test_non_numeric_setting_raises():
    with pytest.raises(costconst.MissingConstant):
        costconst.from_gucs(_rows_with("random_page_cost", setting="on"))


def test_zero_block_size_raises():
    with pytest.raises(costconst.MissingConstant):
        costconst.from_gucs(_rows_with("block_size", setting="0"))
