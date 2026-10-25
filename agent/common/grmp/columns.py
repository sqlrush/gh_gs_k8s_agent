"""结果列名的唯一性检查。

协议的结果集是按列名做键的对象数组，JSON 对象的键唯一 —— 两个同名列只会
剩一个，后面的覆盖前面的，不报错、不告警，只是少了几列数据。

这在原来的位置访问（r[3]、r[4]）下完全不是问题，一迁到中间件就开始丢数据。
所以检查放在注册期：把「SELECT 列表没起别名」从一个风格问题，变成一个
注册不进去的硬错误。
"""
from __future__ import annotations

from typing import List, Sequence

# 数据库对表达式列给的占位名。openGauss/PostgreSQL 用 ?column?
ANONYMOUS_NAMES = frozenset({"?column?", ""})


class ColumnError(Exception):
    """结果列名不适合作为 JSON 键。"""


def duplicate_columns(cols: Sequence[str]) -> List[str]:
    """返回出现多次的列名，按首次出现顺序。大小写不同视为不同的键。"""
    seen, dup = set(), []
    for c in cols:
        if c in seen and c not in dup:
            dup.append(c)
        seen.add(c)
    return dup


def anonymous_columns(cols: Sequence[str]) -> List[str]:
    """返回没有可用名字的列。"""
    return [c for c in cols if c in ANONYMOUS_NAMES]


def check_columns(cols: Sequence[str], script_name: str) -> None:
    """列名不适合做 JSON 键时抛 ColumnError。"""
    dup = duplicate_columns(cols)
    anon = anonymous_columns(cols)
    if not dup and not anon:
        return

    problems = []
    if dup:
        problems.append("重名列 %s" % ", ".join(dup))
    if anon:
        problems.append("无名列 %s" % ", ".join(repr(c) for c in anon))
    raise ColumnError(
        "脚本 %s 的结果列名不可用：%s。\n"
        "协议按列名做键返回对象数组，重名列会被后一个覆盖、无名列拿不到键 ——"
        "结果是数据静默丢失，不报错也不告警。\n"
        "修法：给 SELECT 列表里的每个表达式加别名，如 "
        "ROUND(x,2) AS total_ms。"
        % (script_name, "；".join(problems))
    )
