"""EXPLAIN JSON → 节点树的单测（不连库）。

重点全在**失败路径**。解析成功的那条只要键名对上就没什么可错的；真正
危险的是解析失败却没炸 —— 校准闸拿到一棵空树或一堆 0.0，会判定「复算与
实测完全吻合」，于是推演在模型根本没校准的情况下照常出结论。

所以这里每一条坏输入都断言抛 PlanParseError，而不是断言返回了什么。
"""
import importlib.util
import json
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


plantree = _load("sqltune_plantree_for_test", "plantree.py")


# 一棵真实形状的计划：Hash Join 上挂 Seq Scan（外）+ Hash → Seq Scan（内）
_PLAN = [{
    "Plan": {
        "Node Type": "Hash Join",
        "Join Type": "Inner",
        "Startup Cost": 3457.00,
        "Total Cost": 263123.45,
        "Plan Rows": 100.0,
        "Plan Width": 64,
        "Hash Cond": "(f.customer_id = c.id)",
        "Plans": [
            {
                "Node Type": "Seq Scan",
                "Relation Name": "fact_sales",
                "Alias": "f",
                "Startup Cost": 0.00,
                "Total Cost": 208333.00,
                "Plan Rows": 100.0,
                "Plan Width": 32,
            },
            {
                "Node Type": "Hash",
                "Startup Cost": 3457.00,
                "Total Cost": 3457.00,
                "Plan Rows": 100000.0,
                "Plan Width": 36,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "customers",
                        "Alias": "c",
                        "Startup Cost": 0.00,
                        "Total Cost": 3457.00,
                        "Plan Rows": 100000.0,
                        "Plan Width": 36,
                    },
                ],
            },
        ],
    }
}]


# --- 正常解析 ----------------------------------------------------------------

def test_parse_from_json_text():
    root = plantree.parse(json.dumps(_PLAN))
    assert root.node_type == "Hash Join"
    assert root.total_cost == pytest.approx(263123.45)
    assert root.startup_cost == pytest.approx(3457.00)
    assert root.join_type == "Inner"
    assert len(root.children) == 2


def test_parse_from_decoded_object():
    """psycopg2 会把 json 列自动解码 —— 两条路径喂进来的形态不同，结果必须一致。"""
    assert plantree.parse(_PLAN) == plantree.parse(json.dumps(_PLAN))


def test_parse_accepts_bare_object():
    assert plantree.parse(_PLAN[0]).node_type == "Hash Join"


def test_walk_is_depth_first_root_first():
    root = plantree.parse(_PLAN)
    assert [n.node_type for n in plantree.walk(root)] == [
        "Hash Join", "Seq Scan", "Hash", "Seq Scan"]
    assert plantree.node_count(root) == 4


def test_scan_nodes_carry_relation_and_alias():
    root = plantree.parse(_PLAN)
    scans = [n for n in plantree.walk(root) if n.node_type == "Seq Scan"]
    assert [s.relation for s in scans] == ["fact_sales", "customers"]
    assert [s.alias for s in scans] == ["f", "c"]


def test_optional_keys_absent_are_empty_not_errors():
    """Relation Name/Join Type 本来就只在特定节点上有，缺席是正常的。"""
    root = plantree.parse(_PLAN)
    assert root.relation == ""      # Hash Join 没有 Relation Name
    assert root.index_name == ""
    hash_node = root.children[1]
    assert hash_node.join_type == ""


def test_raw_keeps_untouched_keys():
    """推演层要读 Hash Cond / Index Cond，这些不该逼着这里加字段。"""
    root = plantree.parse(_PLAN)
    assert root.raw["Hash Cond"] == "(f.customer_id = c.id)"


# --- 失败路径：这些必须炸，不能给默认值 --------------------------------------

def test_empty_string_raises():
    """协议下 NULL 也渲染成空串，与真空串不可区分 —— 一律当取数失败。"""
    with pytest.raises(plantree.PlanParseError):
        plantree.parse("")


def test_none_raises():
    with pytest.raises(plantree.PlanParseError):
        plantree.parse(None)


def test_non_json_text_raises():
    with pytest.raises(plantree.PlanParseError):
        plantree.parse("Hash Join  (cost=3457.00..263123.45 rows=100 width=64)")


def test_empty_array_raises():
    """空树最危险：零个节点会让「逐节点比对」全部通过。"""
    with pytest.raises(plantree.PlanParseError):
        plantree.parse("[]")


def test_missing_plan_key_raises():
    with pytest.raises(plantree.PlanParseError):
        plantree.parse('[{"Execution Time": 1.0}]')


@pytest.mark.parametrize("key", ["Node Type", "Startup Cost", "Total Cost",
                                 "Plan Rows", "Plan Width"])
def test_missing_required_key_raises(key):
    node = dict(_PLAN[0]["Plan"])
    node.pop("Plans", None)
    node.pop(key)
    with pytest.raises(plantree.PlanParseError) as ei:
        plantree.parse([{"Plan": node}])
    assert key in str(ei.value), "报错信息要点名是哪个键，否则排查得靠猜"


def test_bool_cost_raises():
    """bool 是 int 的子类：True 会静默变成 1.0。"""
    node = dict(_PLAN[0]["Plan"], **{"Total Cost": True})
    node.pop("Plans", None)
    with pytest.raises(plantree.PlanParseError):
        plantree.parse([{"Plan": node}])


def test_non_numeric_cost_raises():
    node = dict(_PLAN[0]["Plan"], **{"Total Cost": "n/a"})
    node.pop("Plans", None)
    with pytest.raises(plantree.PlanParseError):
        plantree.parse([{"Plan": node}])


def test_numeric_string_cost_is_accepted():
    """中间件把所有值字符串化 —— 若某天 JSON 整个以字符串形态过来也要能解。"""
    node = dict(_PLAN[0]["Plan"], **{"Total Cost": "263123.45"})
    node.pop("Plans", None)
    assert plantree.parse([{"Plan": node}]).total_cost == pytest.approx(263123.45)


def test_bad_child_raises_with_path():
    """报错要指到具体是哪个子节点，深树里没有路径就只能一个个试。"""
    node = json.loads(json.dumps(_PLAN))
    del node[0]["Plan"]["Plans"][1]["Plans"][0]["Total Cost"]
    with pytest.raises(plantree.PlanParseError) as ei:
        plantree.parse(node)
    assert "Plans[1].Plans[0]" in str(ei.value)


def test_plans_not_a_list_raises():
    node = dict(_PLAN[0]["Plan"], Plans={"Node Type": "Seq Scan"})
    with pytest.raises(plantree.PlanParseError):
        plantree.parse([{"Plan": node}])
