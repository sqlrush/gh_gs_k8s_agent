"""把 SQL 里不带 schema 的表名补全成 schema.表 —— search_path 切不成时的替代路;也用来找出「哪些表名不带 schema」。

现场(客户 2026-09-09 早截图):「SET search_path; EXPLAIN」两语句模板在客户的执行器上没走通(探测脚本没灌 /
执行器不认两条语句 / JDBC 只取第一个结果集),退回提示里的备选②「把表名写全」是模型手工做的。这一步该由脚本做:
确定性、可测、报告注明。补全后的 SQL 解析到同一张表,EXPLAIN 的计划与原 SQL 等价。

只动**表位置**上的名字:FROM / JOIN / UPDATE / INSERT INTO / DELETE FROM 后面的标识符,以及 FROM 列表里逗号后的项
(任意嵌套深度,子查询里的 FROM 同样处理);不动列名、别名、函数调用、CTE 名、已带 schema 的名字、字符串与注释里的东西。
拿不准的一律不动——漏补的后果是 EXPLAIN 报对象不存在(看得见),补错的后果是分析了另一张表(看不见)。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from common.search_path import valid_schema

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<lcomment>--[^\n]*)
  | (?P<bcomment>/\*.*?\*/)
  | (?P<str>'(?:[^']|'')*')
  | (?P<qident>"(?:[^"]|"")+")
  | (?P<param>\{\{[^}]*\}\}|\$\d+|:[A-Za-z_]\w*|\?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<punct>[(),.;=<>+\-*/%|^&!~\[\]])
  | (?P<other>.)
    """,
    re.S | re.X,
)

# 引导词后面可能先出现的修饰词,越过它们再看名字
_SKIP_AFTER_LEADER = {"only", "lateral"}
# 引导词后跟这些就不是表(子查询 / 关键字)
_NOT_A_TABLE = {
    "select", "values", "lateral", "only", "with", "table", "as", "on", "using", "where", "set", "and", "or",
    "not", "in", "exists", "case", "when", "then", "else", "end", "null", "true", "false", "default",
    "unnest", "dual",
}
# 这些词出现在某一层,说明该层的 FROM 列表结束(逗号后不再当表名看)
_FROM_CLAUSE_ENDERS = {
    "where", "group", "order", "limit", "having", "window", "union", "except", "intersect", "minus", "set",
    "returning", "fetch", "offset", "for", "select", "values", "into", "update", "delete", "insert",
    "start", "connect", "using",
}   # 注意 "on" 不在里面:FROM a JOIN b ON … , c 里逗号后的 c 仍是表
# 表名后面不能当别名用的词
_NOT_AN_ALIAS = _FROM_CLAUSE_ENDERS | _NOT_A_TABLE | {
    "join", "inner", "left", "right", "full", "cross", "natural", "outer",
}


def _tokens(sql: str):
    pos = 0
    for m in _TOKEN_RE.finditer(sql):
        assert m.start() == pos, "tokenizer left a gap"
        pos = m.end()
        yield m.lastgroup, m.group(0)
    assert pos == len(sql), "tokenizer stopped early"


def _skip_parens(sig, j) -> int:
    """sig[j] 是 "(",返回配对的 ")" 之后的位置。"""
    depth = 0
    while j < len(sig):
        if sig[j] == ("punct", "("):
            depth += 1
        elif sig[j] == ("punct", ")"):
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def _cte_names(sig) -> set:
    """WITH a AS (...), b AS (...)  → {a, b}。只认最外层 WITH 列表的形状:名字 [ (列) ] AS (。"""
    names = set()
    i = 0
    while i < len(sig):
        k, v = sig[i]
        if k == "ident" and v.lower() == "with":
            j = i + 1
            if j < len(sig) and sig[j][0] == "ident" and sig[j][1].lower() == "recursive":
                j += 1
            while j < len(sig):
                if sig[j][0] not in ("ident", "qident"):
                    break
                name = sig[j][1]
                j += 1
                if j < len(sig) and sig[j] == ("punct", "("):          # 列清单
                    j = _skip_parens(sig, j)
                if not (j < len(sig) and sig[j][0] == "ident" and sig[j][1].lower() == "as"):
                    break
                j += 1
                if not (j < len(sig) and sig[j] == ("punct", "(")):
                    break
                names.add(name[1:-1].lower() if name.startswith('"') else name.lower())
                j = _skip_parens(sig, j)                               # 跳过 CTE 体
                if j < len(sig) and sig[j] == ("punct", ","):
                    j += 1
                    continue
                break
            i = j
        else:
            i += 1
    return names


def _schema_prefix(schema: str) -> str:
    return (schema if schema == schema.lower() else '"%s"' % schema) + "."


def qualify_tables(sql: str, schema: str) -> Tuple[str, List[str]]:
    """返回 (补全后的 SQL, 被补全的表名列表——按出现顺序,原样大小写/引号)。没有可补的就原样返回。"""
    if not valid_schema(schema):
        raise ValueError("schema 名 %r 不是合法标识符" % (schema,))
    return _rewrite(sql, _schema_prefix(schema))


def unqualified_tables(sql: str) -> List[str]:
    """SQL 里表位置上不带 schema 的名字(按出现顺序,原样大小写/引号)——贴文本没带 --schema 时拿去目录里找 schema 用。"""
    return _rewrite(sql, None)[1]


def _rewrite(sql: str, prefix: Optional[str]) -> Tuple[str, List[str]]:
    """单趟扫描,按括号层级各自记住「这一层正处在 FROM 列表里」。prefix 为 None 时只收集不改写。"""
    toks = list(_tokens(sql))
    sig_idx = [i for i, (k, _) in enumerate(toks) if k not in ("ws", "lcomment", "bcomment")]
    sig = [toks[i] for i in sig_idx]
    ctes = _cte_names(sig)
    out = [v for _, v in toks]
    changed: List[str] = []

    def at(n):
        return sig[n] if 0 <= n < len(sig) else (None, None)

    def is_name(n):
        k, v = at(n)
        if k == "qident":
            return True
        return k == "ident" and v.lower() not in _NOT_A_TABLE and v.lower() not in _FROM_CLAUSE_ENDERS

    def qualify(n, allow_paren=False):
        k, v = at(n)
        nk, nv = at(n + 1)
        if (nk, nv) == ("punct", ".") or ((nk, nv) == ("punct", "(") and not allow_paren):   # 已带 schema / 函数调用
            return
        key = v[1:-1].lower() if k == "qident" else v.lower()
        if key in ctes:
            return
        if prefix is not None:
            out[sig_idx[n]] = prefix + v
        changed.append(v)

    def skip_alias(n):    # 表名后面的 [AS] alias,返回别名之后的位置
        k, v = at(n)
        if k == "ident" and v.lower() == "as":
            n += 1
            k, v = at(n)
        if k == "qident" or (k == "ident" and v.lower() not in _NOT_AN_ALIAS):
            n += 1
        return n

    def table_after(m, allow_paren=False):
        """引导词 / 逗号之后:越过修饰词,是名字就补全并返回别名之后的位置;不是名字(子查询、函数、关键字)返回 None。"""
        while at(m)[0] == "ident" and at(m)[1].lower() in _SKIP_AFTER_LEADER:
            m += 1
        if is_name(m):
            qualify(m, allow_paren)
            return skip_alias(m + 1)
        return None

    active: Dict[int, bool] = {}       # 层级 → 是否正处在 FROM 列表里(逗号后的项算表)
    depth = 0
    prev = ""
    n = 0
    while n < len(sig):
        k, v = at(n)
        low = v.lower() if k == "ident" else ""
        if k == "punct" and v == "(":
            depth += 1
            n += 1
            continue
        if k == "punct" and v == ")":
            active.pop(depth, None)
            depth = max(0, depth - 1)
            n += 1
            continue
        if low in ("from", "join", "update") or (low == "into" and prev == "insert"):
            nxt = table_after(n + 1, allow_paren=(low == "into"))
            if low == "from":
                active[depth] = True
            n = nxt if nxt is not None else n + 1
            continue
        if k == "punct" and v == "," and active.get(depth):
            nxt = table_after(n + 1)
            if nxt is None:
                active[depth] = False
                n += 1
            else:
                n = nxt
            continue
        if low in _FROM_CLAUSE_ENDERS:
            active[depth] = False
        if low in ("insert", "delete", "update", "select"):
            prev = low
        n += 1
    return "".join(out), changed


__all__ = ["qualify_tables", "unqualified_tables"]
