"""结果集序列化。

一条贯穿始终的规则：**所有列值都渲染成 JSON 字符串**，数值型也不例外
（客户响应里 "datdba":"10"、"datconnlimit":"-1"、"encoding":"7"）。
调用方必须自行按业务语义转换类型，不能依赖 JSON 类型做判断。

这是信息损失，但它是客户中间件的真实行为，必须复刻。若我们「改良」成
保留原生类型，本地写出来的解析代码到客户环境就会全部失效。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Sequence

from .settings import Settings

TYPE_ARRAY = "array"
TYPE_TEXT = "Text"  # 大小写风格与 array 不统一，原文如此


def render_cell(value: Any, settings: Settings) -> str:
    """把单个列值渲染成字符串。

    bool 必须在 int 之前判断 —— Python 的 bool 是 int 的子类，
    顺序反了会让 True 渲染成 "1"，与两套已知渲染都不符。
    """
    if value is None:
        return settings.null_text
    if isinstance(value, bool):
        true_text, false_text = settings.bool_pair
        return true_text if value else false_text
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        # **驱动已经把 json/jsonb 列解码成了 Python 对象。**
        # 这里若走下面的 str()，产出的是 Python repr（单引号），
        # 任何消费方都解析不了：
        #     str([{"Plan": {...}}])  ->  "[{'Plan': {...}}]"
        # 实测踩到过：sqltune 走中间件取 EXPLAIN (FORMAT JSON) 时，
        # json.loads 在第 3 个字符就失败。数据库那边本来就是 JSON 文本，
        # 中间件不该把它变成一个别的语言的字面量。
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def result_array(
    cols: Sequence[str],
    rows: Iterable[Sequence[Any]],
    settings: Settings,
) -> Dict[str, Any]:
    """有结果集：type=array，data 是对象数组，每个对象一行，键为列名。"""
    data: List[Dict[str, str]] = [
        {col: render_cell(val, settings) for col, val in zip(cols, row)}
        for row in rows
    ]
    return {"type": TYPE_ARRAY, "data": data}


def result_text(text: str) -> Dict[str, Any]:
    """无结果集：type=Text，data 是字符串（DDL/DML 的命令标签等）。"""
    return {"type": TYPE_TEXT, "data": text}
