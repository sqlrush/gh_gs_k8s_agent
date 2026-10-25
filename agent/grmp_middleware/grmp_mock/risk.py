"""注册期风险标注 —— 标注，不拦截。

区分标准只有一条：**客户环境也会失败的才拦，客户环境能跑通的一律放行。**
占位符落在表名位或 ORDER BY 位，在客户的文本替换中间件上是能正常执行的，
所以我们不能拒绝它 —— 拒绝就等于本地比客户更严，一条客户那边天天在跑的
脚本在本地注册不进去，本地测试反而失去参照价值。

我们能做的是把它**变得可见**：出具一份「哪些脚本的参数存在注入面」的清单。
这不改变运行时行为，只是把本来看不见的差异摆到台面上。

检测是词法层面的启发式，不是 SQL 解析器。宁可漏报也尽量不误报 ——
误报会淹没真正需要人工判断的条目，让清单没人看。
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, List, Sequence, Tuple

# 只读判定统一由 common/grmp/statement 提供 —— 注册期硬拦截、运行期会话模式、
# 这里的风险标注三处必须用同一份判定。各判各的会出现「注册时判只读、
# 执行时却开了写会话」这种错位。
from common.grmp.statement import is_read_only

CODE_IDENT_POSITION = "IDENT_POSITION"
CODE_MULTI_VALUE = "MULTI_VALUE"
CODE_NON_READONLY = "NON_READONLY"
CODE_STRING_PARAM = "STRING_PARAM"

# 五种类型里只有 String 没有格式约束，类型校验对它等于不存在
_UNCONSTRAINED_TYPES = frozenset({"STRING"})


@dataclasses.dataclass(frozen=True)
class Risk:
    code: str
    detail: str


_PLACEHOLDER = r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}"
_PLACEHOLDER_RE = re.compile(_PLACEHOLDER)

# 表名位：FROM / JOIN / INTO / UPDATE 之后紧跟占位符
_AFTER_KEYWORD_RE = re.compile(
    r"\b(?:from|join|into|update)\s+" + _PLACEHOLDER, re.IGNORECASE
)

# ORDER BY / GROUP BY 列表：从关键字起，到下一个子句关键字或语句结束为止
_SORT_CLAUSE_RE = re.compile(
    r"\b(?:order|group)\s+by\b(?P<body>.*?)"
    r"(?=\b(?:order\s+by|group\s+by|having|limit|offset|fetch|for|union|"
    r"intersect|except)\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)

# SELECT 列表：SELECT 之后到第一个子句关键字为止。没有 FROM 的
# `select 1 where x > {{n}}` 必须靠 WHERE 收住，否则会把取值位误判成列名位。
_SELECT_LIST_RE = re.compile(
    r"\bselect\b(?:\s+distinct)?(?P<body>.*?)"
    r"(?=\b(?:from|where|group\s+by|order\s+by|limit|offset)\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)

# IN (...) 内的占位符
_IN_LIST_RE = re.compile(
    r"\bin\s*\((?P<body>[^()]*)\)", re.IGNORECASE | re.DOTALL
)

def _names_in(text: str) -> List[str]:
    return [m.group(1) for m in _PLACEHOLDER_RE.finditer(text)]


def _until_unbalanced_paren(text: str) -> str:
    """截到第一个「多出来的」右括号为止。

    ORDER BY / GROUP BY 的列表用正则找不到可靠的右边界：下一个子句关键字
    可能出现在括号外很远的地方。CTE 里就会踩到 ——
        WITH b AS (... GROUP BY a), e AS (... WHERE id={{e}} GROUP BY a)
    第一个 GROUP BY 的范围一路扫到第二个 group by，把 {{e}} 这个取值位
    占位符误判成排序位。

    按括号配平截断即可：遇到没有对应左括号的右括号，说明已经出了当前子句
    所在的那对括号。
    """
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return text[:i]
            depth -= 1
    return text


def assess(sql: str, params: Sequence[Any] = ()) -> Tuple[Risk, ...]:
    """返回该脚本的风险标注。无风险时返回空元组。

    params 是参数声明（有 .key/.type 的对象）。不传时只做语句形态检查。
    """
    risks: List[Risk] = []

    # String 参数在文本替换下就是注入向量。实测：
    #   where usename = '{{username}}'  传入  x' or '1'='1
    #   渲染成  where usename = 'x' or '1'='1'  → 返回全表
    # 这不是实现缺陷，是文本替换的固有结果 —— 客户中间件若同样用
    # String.replace 实现，客户环境存在同一个洞。所以标注而不拦截：
    # 拦了就拒掉一条客户能跑的脚本，而这个洞本来就该由客户知晓并决策。
    unconstrained = sorted(
        {
            p.key
            for p in params
            if str(getattr(p, "type", "")).upper() in _UNCONSTRAINED_TYPES
        }
    )
    if unconstrained:
        risks.append(
            Risk(
                CODE_STRING_PARAM,
                "参数 %s 为 String 类型：五种类型里只有它没有格式约束，"
                "类型校验挡不住注入载荷。取值须来自受控枚举或上游已校验的来源；"
                "此风险在客户环境同样存在（若其中间件也是文本替换）"
                % ", ".join(unconstrained),
            )
        )

    if not is_read_only(sql):
        risks.append(
            Risk(
                CODE_NON_READONLY,
                "语句不是只读查询，需单独审批后才能注册（执行器默认只读会话）",
            )
        )

    ident_names: List[str] = []
    for match in _AFTER_KEYWORD_RE.finditer(sql):
        ident_names.append(match.group(1))
    for match in _SORT_CLAUSE_RE.finditer(sql):
        ident_names.extend(_names_in(_until_unbalanced_paren(match.group("body"))))
    for match in _SELECT_LIST_RE.finditer(sql):
        ident_names.extend(_names_in(_until_unbalanced_paren(match.group("body"))))

    unique_idents = sorted(set(ident_names))
    if unique_idents:
        risks.append(
            Risk(
                CODE_IDENT_POSITION,
                "占位符 %s 位于表名/列名/排序位：类型校验对标识符位无效，"
                "该脚本在客户环境同样存在注入面"
                % ", ".join(unique_idents),
            )
        )

    multi_names: List[str] = []
    for match in _IN_LIST_RE.finditer(sql):
        multi_names.extend(_names_in(match.group("body")))
    unique_multi = sorted(set(multi_names))
    if unique_multi:
        risks.append(
            Risk(
                CODE_MULTI_VALUE,
                "占位符 %s 位于 IN (...) 内：多值展开语义客户中间件未定义，"
                "本实现按单值文本替换处理" % ", ".join(unique_multi),
            )
        )

    return tuple(risks)
