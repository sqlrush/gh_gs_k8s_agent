"""结构化列值的序列化。

驱动会把 json/jsonb 列自动解码成 Python 对象。中间件若对它做 str()，产出的是
**Python repr**（单引号）—— 一个别的语言的字面量，任何 JSON 消费方都解析不了。

实测踩到过：sqltune 走中间件取 EXPLAIN (FORMAT JSON)，json.loads 在第 3 个
字符就失败，报文开头是 `[{'Plan': {'Node Type': ...`。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import serialize  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402

S = Settings()


def test_decoded_json_column_stays_json():
    plan = [{"Plan": {"Node Type": "Seq Scan", "Total Cost": 3457.0}}]
    rendered = serialize.render_cell(plan, S)
    assert json.loads(rendered) == plan


def test_python_repr_never_leaks_out():
    """单引号是判据：Python repr 用单引号，JSON 只用双引号。"""
    rendered = serialize.render_cell({"a": "b"}, S)
    assert "'" not in rendered
    assert rendered == '{"a": "b"}'


def test_non_ascii_is_not_escaped():
    """ensure_ascii=False —— 转义成 \\uXXXX 不算错，但会让报文没法读。"""
    assert serialize.render_cell({"表": "值"}, S) == '{"表": "值"}'


def test_scalars_are_unaffected():
    """标量仍旧全部字符串化，这是客户中间件的行为，不能顺手「改良」。"""
    assert serialize.render_cell(10, S) == "10"
    assert serialize.render_cell(-1, S) == "-1"
    assert serialize.render_cell(3.5, S) == "3.5"


def test_bool_still_wins_over_int():
    """bool 是 int 的子类，判断顺序反了 True 会变成 "1"。"""
    true_text, false_text = S.bool_pair
    assert serialize.render_cell(True, S) == true_text
    assert serialize.render_cell(False, S) == false_text


def test_none_is_the_null_text():
    assert serialize.render_cell(None, S) == S.null_text


def test_array_result_carries_json_cells():
    out = serialize.result_array(["QUERY PLAN"],
                                 [[[{"Plan": {"Node Type": "Seq Scan"}}]]], S)
    cell = out["data"][0]["QUERY PLAN"]
    assert json.loads(cell)[0]["Plan"]["Node Type"] == "Seq Scan"
