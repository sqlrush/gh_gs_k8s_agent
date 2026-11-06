"""skill 传原生取值 → 协议要求的字符串取值。

协议规定所有类型的 param_value 都以字符串承载（整数写成 "10"）。
skill 里自然拿到的是 int/bool，转换放在入口做一次，两条路径共用。
"""
from __future__ import annotations

from typing import Any


class ParamValueError(TypeError):
    """取值类型不受支持。"""


def to_param_value(value: Any) -> str:
    """把原生取值转成协议的字符串形式。

    刻意**不用 str() 兜底**：str(None) 会得到 "None"，作为 String 参数
    是合法字符串，会被原样贴进 SQL；str(1.5) 虽然会被 INTEGER 校验拒掉，
    但错误信息指向「取值不是十进制串」，而真正的问题是 skill 传了浮点数。
    在入口拒绝，错误信息才指得准。

    bool 必须在 int 之前判断：Python 的 bool 是 int 的子类，
    顺序反了 True 会变成 "1"，协议只认 "true"/"false"。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    raise ParamValueError(
        "参数取值类型 %s 不受支持（只接受 str/int/bool）。"
        "时间类参数请自行格式化成 'yyyy-MM-dd HH:mm:ss' 字符串。"
        % type(value).__name__
    )
