"""目录与两道前置门的单测（不连库）。

这两道门的共同点：不满足时**整段作废**，不是打个折扣继续算。所以测试全在
断言「该拒的拒了」，以及「拒绝理由里带着可复核的数字」——只说「统计陈旧」
而不说凭哪两个数、差多少、阈值是多少，读的人没法判断这个结论可不可信。
"""
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))

import catalog  # noqa: E402


def _table(name, pages=1000, tuples=100000, schema="public", cur_pages=None):
    return types.SimpleNamespace(schema=schema, name=name, pages=pages,
                                 tuples=tuples,
                                 cur_pages=pages if cur_pages is None else cur_pages,
                                 kind="r", size_mb=1.0)


def _index(name, table="customers", pages=274, tuples=100000):
    return types.SimpleNamespace(table=table, name=name, is_unique=True,
                                 is_primary=True, pages=pages, tuples=tuples,
                                 definition="CREATE UNIQUE INDEX %s ON %s (id)"
                                            % (name, table))


def _column(table, column, correlation=0.9):
    return types.SimpleNamespace(table=table, column=column, n_distinct=-1.0,
                                 null_frac=0.0, avg_width=8,
                                 correlation=correlation)


def _fresh(table, live=100000, last="2026-08-01 03:12:44", auto="never"):
    return types.SimpleNamespace(schema="public", table=table, live_tuples=live,
                                 dead_tuples=0, last_analyze=last,
                                 last_autoanalyze=auto, analyze_count=1,
                                 autoanalyze_count=0)


def _catalog(**kw):
    kwargs = dict(
        tables=[_table("customers"), _table("orders", pages=500, tuples=50000)],
        indexes=[_index("customers_pkey")],
        columns=[_column("customers", "id")],
        freshness=[_fresh("customers"), _fresh("orders", live=50000)],
    )
    kwargs.update(kw)
    return catalog.Catalog(**kwargs)


# --- 查询 --------------------------------------------------------------------

def test_lookup_is_case_insensitive():
    cat = _catalog()
    assert cat.table("CUSTOMERS").name == "customers"
    assert cat.index("Customers_PKey").name == "customers_pkey"
    assert cat.column("Customers", "ID").correlation == 0.9


def test_total_table_pages_sums_all_tables():
    """Mackert-Lohman 的缓存摊分基数是本查询涉及的**所有**表的页数。"""
    assert _catalog().total_table_pages() == 1500.0


def test_missing_table_explains_the_likely_causes():
    with pytest.raises(catalog.CatalogError) as ei:
        _catalog().table("nope")
    assert "视图" in str(ei.value) or "CTE" in str(ei.value)


def test_missing_column_stats_refuses():
    """缺 correlation 就算不了回表 IO —— 不能拿 0 顶。"""
    with pytest.raises(catalog.CatalogError) as ei:
        _catalog().column("customers", "email")
    assert "correlation" in str(ei.value)


# --- 门一：名字歧义 ----------------------------------------------------------

def test_same_table_name_in_two_schemas_refuses():
    """采集脚本按 relname 匹配、不带 schema，同名表会返回两行。

    取第一行会拿另一张表的页数算出一个精确的错数，不报错也不告警。
    """
    with pytest.raises(catalog.CatalogError) as ei:
        _catalog(tables=[_table("customers", schema="app"),
                         _table("customers", schema="archive", pages=999999)])
    assert "歧义" in str(ei.value)
    assert "customers" in str(ei.value)


def test_duplicate_index_name_refuses():
    with pytest.raises(catalog.CatalogError):
        _catalog(indexes=[_index("idx_a"), _index("idx_a", table="orders")])


# --- 门二：统计新鲜度 --------------------------------------------------------

def test_fresh_when_page_drift_within_threshold():
    v = _catalog().freshness("customers")
    assert v.fresh is True
    assert v.drift == pytest.approx(0.0)


def test_stale_when_page_drift_exceeds_threshold():
    """冻结 1000 页 vs 实时 2000 页 = 偏离 100%。"""
    cat = _catalog(tables=[_table("customers", cur_pages=2000), _table("orders")])
    v = cat.freshness("customers")
    assert v.fresh is False
    assert v.drift == pytest.approx(1.0)


def test_small_growth_stays_fresh():
    """4.9% 的自然增长不该把整个推演拦掉 —— og5 上 orders 就是这个量级。"""
    cat = _catalog(tables=[_table("customers", cur_pages=1049), _table("orders")])
    assert cat.freshness("customers").fresh is True


def test_stale_reason_carries_both_numbers_and_the_threshold():
    """只说「统计陈旧」没用 —— 要能看出凭哪两个数、差多少、阈值多少。"""
    cat = _catalog(tables=[_table("customers", cur_pages=2000), _table("orders")])
    reason = cat.freshness("customers").reason
    assert "1000" in reason and "2000" in reason
    assert "100.0%" in reason
    assert "10%" in reason


def test_reset_stat_counters_do_not_make_a_good_table_stale():
    """**这条是回归测试。**

    og5 上 gsbench.fact_sales 报 last_analyze=never、n_live_tup=0，但 pg_stats
    里实实在在有 8 列统计信息 —— 计数器被 pg_stat_reset() 清过。原先拿
    last_analyze 当门，把统计完好的表判成「从未分析」，整个推演白做。
    现在计数器只作参考，判据用不会被重置的信号。
    """
    cat = _catalog(freshness=[_fresh("customers", live=0, last="never",
                                     auto="never"),
                              _fresh("orders", live=50000)])
    v = cat.freshness("customers")
    assert v.fresh is True
    assert v.stat_columns == 1        # pg_stats 里确实有行
    assert "仅供参考" in v.reason


def test_no_pg_stats_rows_is_genuinely_never_analyzed():
    """pg_stats 一列都没有 —— 这才是真的从未分析，n_distinct 全取不到。"""
    cat = _catalog(columns=[])
    v = cat.freshness("customers")
    assert v.fresh is False
    assert v.stat_columns == 0
    assert "一列统计信息都没有" in v.reason


def test_missing_stat_collector_row_does_not_block_when_pages_agree():
    """统计收集器没这张表的行，不影响判定 —— 它本来就只是参考信息。"""
    cat = _catalog(freshness=[_fresh("orders", live=50000)])
    assert cat.freshness("customers").fresh is True


def test_zero_relpages_is_not_fresh():
    cat = _catalog(tables=[_table("customers", pages=0, cur_pages=0),
                           _table("orders")])
    v = cat.freshness("customers")
    assert v.fresh is False
    assert v.drift is None
    assert "没有冻结基准" in v.reason


def test_freshness_report_covers_every_named_table():
    cat = _catalog(columns=[_column("customers", "id"), _column("orders", "id")])
    verdicts = cat.freshness_report(["customers", "orders"])
    assert [v.table for v in verdicts] == ["customers", "orders"]
    assert all(v.fresh for v in verdicts)


def test_freshness_is_per_table_not_global():
    """一张表没统计信息，不该把另一张也拖下水。"""
    cat = _catalog(columns=[_column("customers", "id")])
    by_table = {v.table: v.fresh for v in
                cat.freshness_report(["customers", "orders"])}
    assert by_table == {"customers": True, "orders": False}
