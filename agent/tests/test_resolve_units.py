"""resolver 的单测：把计划节点接到目录和模型上的那一层。

重点在两个「看起来无关紧要、实际决定成败」的小函数：过滤条件里的操作符
计数，和索引定义里的列名解析。它们错了不会抛异常，只会让复算值偏几个
百分点 —— 而校准闸报出来会像是「模型不适用」，排查方向被带偏。
"""
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))

import calibrate  # noqa: E402
import catalog  # noqa: E402
import costconst  # noqa: E402
import costmodel  # noqa: E402
import plantree  # noqa: E402
import resolve  # noqa: E402

COST = costconst.CostConstants(
    seq_page_cost=1.0, random_page_cost=1.1, cpu_tuple_cost=0.01,
    cpu_index_tuple_cost=0.005, cpu_operator_cost=0.0025, block_size=8192,
    effective_cache_size=1572864, work_mem=16 * 1024 * 1024, query_dop=2)


# --- 过滤条件的操作符计数 ----------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("", 0),
    (None, 0),
    ("(id > 1000)", 1),
    ("((id > 1000) AND (name = 'x'))", 2),
    ("((a >= 1) AND (b <= 2) AND (c <> 3))", 3),
    ("(name ~~ 'abc%')", 1),
])
def test_count_qual_operators(text, expected):
    assert resolve.count_qual_operators(text) == expected


def test_operators_inside_string_literals_are_not_counted():
    """'a=b' 里的等号是数据不是条件 —— 数进去会多收一份 cpu_operator_cost。"""
    assert resolve.count_qual_operators("(tag = 'a=b>c')") == 1


def test_no_index_cond_means_zero_not_one():
    """**这条是回归测试。**

    merge join 底下的全索引扫描没有 Index Cond，条件数就是 0。原先写成
    max(1, …) 强行凑成 1，等于给每个索引条目多收一份 cpu_operator_cost：
    og5 实测 bench_order_items 多收 4999991 × 0.0025 = 12500，正是当时那
    9.46% 偏差的全部来源。而它既不报错也不崩，只是所有索引扫描一致偏高。
    """
    assert resolve.count_qual_operators("") == 0


# --- 索引定义里的列名 --------------------------------------------------------

@pytest.mark.parametrize("definition,expected", [
    ("CREATE UNIQUE INDEX pk ON t USING btree (id)", ["id"]),
    ("CREATE INDEX i ON t USING btree (a, b)", ["a", "b"]),
    ('CREATE INDEX i ON t USING btree ("Mixed")', ["mixed"]),
    ("CREATE INDEX i ON s.t USING btree (a) TABLESPACE ts", ["a"]),
])
def test_indexed_columns(definition, expected):
    assert resolve.indexed_columns(definition) == expected


def test_expression_index_yields_nothing_rather_than_guessing():
    """表达式索引取不到列 —— 猜第一个词会拿到函数名，然后查不到统计信息。

    返回空列表让调用方明确拒绝，比返回一个错列名强：后者会在 catalog.column()
    抛「列不存在」，报错指向的地方是错的。
    """
    assert resolve.indexed_columns(
        "CREATE INDEX i ON t USING btree (lower(name))") == []


# --- resolver 分派 -----------------------------------------------------------

def _catalog():
    table = types.SimpleNamespace(
        schema="public", name="big", pages=1000, tuples=100000,
        cur_pages=1000, kind="r", size_mb=1.0)
    table.planner_tuples = 100000.0
    return catalog.Catalog([table], [], [], [])


def _node(node_type, **kw):
    raw = {"Node Type": node_type, "Startup Cost": 0.0, "Total Cost": 1.0,
           "Plan Rows": 100.0, "Plan Width": 32}
    raw.update(kw)
    return plantree.parse([{"Plan": raw}])


def test_seq_scan_is_dispatched_with_dop():
    """dop 从代价常数来，不是每个调用点各写一份。"""
    node = _node("Seq Scan", **{"Relation Name": "big", "Alias": "b"})
    est = resolve.make_resolver(_catalog(), COST)(node, [])
    expected = costmodel.seq_scan(1000.0, 100000.0, COST, dop=2)
    assert est.total_cost == pytest.approx(expected.total_cost)


@pytest.mark.parametrize("kind", ["Bitmap Heap Scan", "Materialize",
                                  "WindowAgg", "CTE Scan"])
def test_unknown_operator_returns_none(kind):
    """未建模返回 None，由校准闸计入覆盖率 —— 不是抛错让整棵树作废。

    （原先这条拿 Merge Join 当例子，它现在建模了。挑几个确实还没做的。）
    """
    assert resolve.make_resolver(_catalog(), COST)(_node(kind), []) is None


def test_scan_without_relation_name_refuses():
    """扫的是子查询/CTE/函数结果时目录里查不到，必须说清楚而不是当 0 行。"""
    node = _node("Seq Scan")
    with pytest.raises(costmodel.ModelError) as ei:
        resolve.make_resolver(_catalog(), COST)(node, [])
    assert "Relation Name" in str(ei.value)


def test_hash_node_must_equal_its_child_exactly():
    """Hash 自身不加代价 —— 它是个好锚点：不逐位相等就说明计划树接错了。"""
    node = _node("Hash")
    child = _node("Seq Scan", **{"Relation Name": "big", "Total Cost": 3457.0})
    est = resolve.make_resolver(_catalog(), COST)(node, [child])
    assert est.total_cost == 3457.0
    assert est.startup_cost == 3457.0


def test_join_with_wrong_child_count_refuses():
    with pytest.raises(costmodel.ModelError):
        resolve.make_resolver(_catalog(), COST)(_node("Nested Loop"), [])


def test_index_only_scan_is_unmodeled_not_faked():
    """堆访问跳过多少由可见性图决定，目录里查不到 —— 不拿 Index Scan 硬套。"""
    node = _node("Index Only Scan", **{"Relation Name": "big",
                                       "Index Name": "big_pkey"})
    assert resolve.make_resolver(_catalog(), COST)(node, []) is None
