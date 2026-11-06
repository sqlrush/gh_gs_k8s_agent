"""EXPLAIN (FORMAT JSON) → 节点树。

校准闸要做的事是「用公式复算每个节点的 cost，跟数据库自己报的比」，比对的
前提是拿到**每个节点**的实测值。cost.py 只取根节点的 Total Cost，够 hypopg
用（它只关心一个总数），不够校准用。

**本模块一律 fail closed：键取不到、值不是数、树是空的，全部抛异常。**

不给默认值是有意的。默认成 0.0 之后，校准闸拿 0.0 和 0.0 比会判定「完全
吻合」，于是「解析失败」伪装成「模型已校准」，推演照常出结论且看不出破绽。
这是本模块唯一真正危险的坏法，其余的坏法都会当场炸。

JSON 而不是 TEXT：TEXT 要从 `(cost=0.00..263123.45 rows=100 width=64)` 里
用正则抠数，缩进层级、节点名里的括号、长行折叠都能让它悄悄抠错一个数，
而抠错的结果依然是个合法浮点数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator


class PlanParseError(Exception):
    """计划解析失败。调用方必须当作「拿不到实测值」处理，不能继续推演。"""


@dataclass(frozen=True)
class PlanNode:
    node_type: str
    startup_cost: float
    total_cost: float
    plan_rows: float
    plan_width: int
    relation: str
    alias: str
    index_name: str
    join_type: str
    children: tuple
    # 原始节点。推演层要读别的键（Filter / Index Cond / Hash Cond …）时直接
    # 从这里取，不必回头给每个键加一个字段。
    raw: dict = field(compare=False, repr=False, default_factory=dict)


def parse(raw: Any) -> PlanNode:
    """把 EXPLAIN (FORMAT JSON) 的输出解析成根节点。

    raw 可以是 JSON 文本，也可以是已解码的 list/dict —— psycopg2 会把 json 列
    自动解码成 Python 对象，而中间件路径拿回来的是字符串。两条路径共用这一份
    解析，正是为了让「本地跑通、换条路径就错」无处可藏。
    """
    doc = _as_document(raw)
    if isinstance(doc, dict):
        doc = [doc]
    if not isinstance(doc, list):
        raise PlanParseError(
            "EXPLAIN JSON 顶层应是数组，实际是 %s" % type(doc).__name__)
    if not doc:
        raise PlanParseError(
            "EXPLAIN JSON 顶层数组为空 —— 没有计划可比对。"
            "空树会让校准闸「零个节点全部吻合」，所以这里当失败处理。")
    first = doc[0]
    if not isinstance(first, dict) or "Plan" not in first:
        raise PlanParseError("EXPLAIN JSON 第一个元素里没有 'Plan' 键")
    return _node(first["Plan"], "Plan")


def walk(node: PlanNode) -> Iterator[PlanNode]:
    """深度优先，先根后子。校准闸按这个顺序逐节点比对。"""
    yield node
    for child in node.children:
        yield from walk(child)


def node_count(node: PlanNode) -> int:
    return sum(1 for _ in walk(node))


# --- 内部 --------------------------------------------------------------------

def _as_document(raw: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    if raw is None:
        raise PlanParseError("EXPLAIN 没有返回内容")
    text = str(raw).strip()
    if not text:
        raise PlanParseError(
            "EXPLAIN 返回空串 —— 协议下 NULL 也渲染成空串，两者不可区分，"
            "一律当作取数失败。")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise PlanParseError(
            "EXPLAIN 输出不是合法 JSON：%s。开头 120 字符：%r"
            % (exc, text[:120])) from exc


def _node(raw: Any, path: str) -> PlanNode:
    if not isinstance(raw, dict):
        raise PlanParseError("%s 不是对象，而是 %s" % (path, type(raw).__name__))

    sub = raw.get("Plans", [])
    if sub is None:
        sub = []
    if not isinstance(sub, list):
        raise PlanParseError("%s.Plans 应是数组，实际是 %s"
                             % (path, type(sub).__name__))

    return PlanNode(
        node_type=_require_str(raw, "Node Type", path),
        startup_cost=_require_num(raw, "Startup Cost", path),
        total_cost=_require_num(raw, "Total Cost", path),
        plan_rows=_require_num(raw, "Plan Rows", path),
        plan_width=int(_require_num(raw, "Plan Width", path)),
        relation=_opt_str(raw, "Relation Name"),
        alias=_opt_str(raw, "Alias"),
        index_name=_opt_str(raw, "Index Name"),
        join_type=_opt_str(raw, "Join Type"),
        children=tuple(_node(c, "%s.Plans[%d]" % (path, i))
                       for i, c in enumerate(sub)),
        raw=raw,
    )


def _require_str(raw: dict, key: str, path: str) -> str:
    if key not in raw:
        raise PlanParseError(
            "%s 缺少 %r。openGauss 的 EXPLAIN JSON 键名与 PostgreSQL 不一致时"
            "会走到这里 —— 补映射，别给默认值。" % (path, key))
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise PlanParseError("%s.%s 不是非空字符串：%r" % (path, key, value))
    return value


def _require_num(raw: dict, key: str, path: str) -> float:
    if key not in raw:
        raise PlanParseError(
            "%s 缺少 %r —— 少了它就没有可比对的实测值。"
            "默认成 0 会让校准闸拿 0 和 0 比，判定「完全吻合」。" % (path, key))
    value = raw[key]
    # bool 是 int 的子类：True 会静默变成 1.0
    if isinstance(value, bool):
        raise PlanParseError("%s.%s 是布尔值 %r，不是数值" % (path, key, value))
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise PlanParseError(
            "%s.%s 不是数值：%r" % (path, key, value)) from exc


def _opt_str(raw: dict, key: str) -> str:
    """可选的字符串键。缺了就是空串 —— 这些键**本来就**只在特定节点上出现
    （Relation Name 只有扫描节点有，Join Type 只有 join 有），缺席是正常的，
    不是取数失败。与上面 fail closed 的那几个键区别就在这里。
    """
    value = raw.get(key, "")
    return value if isinstance(value, str) else ""
