"""根因规则:计划节点 + SQL 正文都能确定的,才报。

evidence.scan_plan 只看算子名 —— 有 Seq Scan 报 seq_scan、有 Sort 报 sort。2026-10-24 faultlab(og5)
S1–S4 四条慢 SQL 拿到的全是这类通用警告:「相关子查询逐行执行」「NOT IN 退化成子计划」
「索引列上套了类型转换」「LIKE 的模式取自另一张表」这些真正的根因一条都没点到,读报告的人
还会被「consider an index on the Filter columns」带去建一个根本用不上的索引。

每条规则都要同时满足两件事才报:
  ① 计划里出现了能定性的节点形态 —— 非 hashed 的 SubPlan 里套着 Seq Scan、`NOT (SubPlan n)`、
     Filter 里列上的 `::类型` / 函数、`~~` 的右侧不是常量;
  ② 证据包能核实相关前提 —— 那一列上确实有以它开头的索引、SQL 正文里确实写的是 NOT IN、
     LIKE 的模式不是 sqltune 自己合成的占位符值。
缺一样就不报:根因说错比不说更糟,通用警告照旧在后面兜着。

读的是 TEXT 计划(证据包里就是它),只取算子名、表名 / 别名、条件文本这些结构信息,
不从文本里抠代价数字(那种抠法的风险见 plantree.py)。认不出的计划格式(如 GaussDB 的
pretty 表格)一条也不报。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from evidence import Finding

# ---------------------------------------------------------------- TEXT 计划解析

# 子计划头:SubPlan 1 / hashed SubPlan 1 / InitPlan 1 (returns $0) / CTE lvl1
_HEADER_RE = re.compile(r"^(?:(?:hashed )?SubPlan \d+|InitPlan \d+.*|CTE \S+)$")
_COND_RE = re.compile(r"^((?:Join |Index |Hash |Merge |Recheck |One-Time )?(?:Filter|Cond)):\s*(.*)$")
_SCAN_RE = re.compile(r"^(?P<op>[A-Za-z][A-Za-z ]*?Scan)(?: Backward)?(?: using (?P<index>\S+))?"
                      r" on (?P<table>\S+)(?: (?P<alias>[^\s(]+))?\s+\(")
_OP_RE = re.compile(r"^(?P<op>[A-Za-z][A-Za-z -]*?)\s+\(cost=")
_STRING_LIT_RE = re.compile(r"'(?:''|[^'])*'")
_QUALIFIED_RE = re.compile(r"\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\b(?!\s*\()")


@dataclass(frozen=True)
class Node:
    op: str                 # Seq Scan / Index Scan / Nested Loop …
    table: str              # 扫描的表(去掉 schema);非扫描节点为空
    alias: str
    index: str              # Index Scan using <index>
    subplan: str            # 所在最内层子计划头:"SubPlan 1" / "InitPlan 1 (returns $0)" / "CTE x" / ""
    conds: tuple            # (("Filter", "(order_id = orders.id)"), …)
    parent: int = -1        # 父节点在列表里的下标;根为 -1

    def cond(self, name: str) -> str:
        return next((text for key, text in self.conds if key == name), "")

    @property
    def names(self) -> frozenset:
        return frozenset(n for n in (self.table, self.alias) if n)


def parse_text_plan(plan: str) -> list:
    """TEXT 计划 → 节点列表(按出现顺序)。认不出的行(Sort Method、Buffers、actual 统计等)忽略。

    缩进决定归属:子计划头(SubPlan / InitPlan / CTE)管住它下面缩进更深的节点;
    父节点取缩进更浅的最近一个节点 —— 子计划头出现时,与它同级或更深的节点都已结束。
    """
    nodes, headers, stack = [], [], []      # headers / stack: [(缩进, 头 | 节点下标)]
    for line in (plan or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped.startswith("->") or (not nodes and "(cost=" in stripped):
            headers = [h for h in headers if h[0] < indent]
            stack = [s for s in stack if s[0] < indent]
            parent = stack[-1][1] if stack else -1
            stack.append((indent, len(nodes)))
            nodes.append(_node(stripped.lstrip("->").strip(), headers[-1][1] if headers else "", parent))
        elif _HEADER_RE.match(stripped):
            headers = [h for h in headers if h[0] < indent] + [(indent, stripped)]
            stack = [s for s in stack if s[0] < indent]
        elif nodes and _COND_RE.match(stripped):
            m = _COND_RE.match(stripped)
            last = nodes[-1]
            nodes[-1] = Node(last.op, last.table, last.alias, last.index, last.subplan,
                             last.conds + ((m.group(1), m.group(2)),), last.parent)
    return nodes


def _node(title: str, subplan: str, parent: int) -> Node:
    m = _SCAN_RE.match(title)
    if m:
        return Node(m.group("op"), m.group("table").rsplit(".", 1)[-1], m.group("alias") or "",
                    m.group("index") or "", subplan, (), parent)
    m = _OP_RE.match(title)
    return Node(m.group("op") if m else title.split("  ")[0], "", "", "", subplan, (), parent)


def children(nodes: list, i: int) -> list:
    """常规子节点(同一个子计划上下文里的),按出现顺序。嵌套循环的第二个是内层。"""
    return [j for j, n in enumerate(nodes) if n.parent == i and n.subplan == nodes[i].subplan]


def subtree(nodes: list, i: int) -> list:
    out, todo = [], [i]
    while todo:
        j = todo.pop()
        out.append(j)
        todo.extend(children(nodes, j))
    return out


# ---------------------------------------------------------------- 证据包里的前提

_INDEX_COLS_RE = re.compile(r"\bUSING \w+ \((.*)\)", re.I)
_INDEX_ON_RE = re.compile(r"\bON (?:ONLY )?(?:(\w+)\.)?(\w+) USING", re.I)


def _leading_columns(indexes: Iterable) -> dict:
    """{表: [(索引名, 首列)]}。首列是表达式(以括号开头)的不算 —— 那正是要建议的东西。"""
    out: dict = {}
    for ix in indexes or ():
        m = _INDEX_COLS_RE.search(getattr(ix, "definition", "") or "")
        if not m:
            continue
        first = m.group(1).split(",")[0].strip()
        if not first or first.startswith("("):
            continue
        col = first.split()[0].strip('"').lower()       # 去掉 opclass / ASC / NULLS 之类
        out.setdefault(ix.table.lower(), []).append((ix.name, col))
    return out


def _schema_of(table: str, indexes: Iterable, default: str) -> str:
    """建议 DDL 用的 schema:先看同表别的索引定义里写的,再看本次 SQL 所在的 schema。"""
    for ix in indexes or ():
        m = _INDEX_ON_RE.search(getattr(ix, "definition", "") or "")
        if m and m.group(2).lower() == table and m.group(1):
            return m.group(1)
    return default


def _null_frac(columns: Iterable, table: str, column: str) -> Optional[float]:
    for c in columns or ():
        if c.table.lower() == table and c.column.lower() == column:
            return c.null_frac
    return None


@dataclass(frozen=True)
class _Ctx:
    nodes: list
    sql: str                # 比对用:去掉注释与字符串字面量、小写
    raw_sql: str            # 原样(找显式转换)
    leading: dict
    indexes: tuple
    columns: tuple
    schema: str
    synthetic_like: bool    # 原 SQL 里 LIKE 的模式是占位符 —— 计划里那个常量是 sqltune 合成的

    def index_on(self, table: str, column: str, node: Optional[Node] = None) -> str:
        """table 上以 column 开头、且这个节点**没在用**的索引名;没有返回空串。"""
        for name, col in self.leading.get(table, ()):
            if col == column and not (node is not None and node.index == name):
                return name
        return ""

    def table_of(self, alias: str) -> str:
        for n in self.nodes:
            if n.table and alias in n.names:
                return n.table
        return ""


_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_LIKE_PLACEHOLDER_RE = re.compile(r"\bi?like\s+(?:\?|\$\d+|:\d+)", re.I)


def _context(ev, original_sql: str) -> _Ctx:
    raw = original_sql or getattr(ev, "sql", "") or ""
    sql = _STRING_LIT_RE.sub("''", _COMMENT_RE.sub(" ", raw)).lower()
    indexes = tuple(getattr(ev, "indexes", ()) or ())
    return _Ctx(nodes=parse_text_plan(getattr(ev, "plan", "")), sql=sql, raw_sql=raw,
                leading=_leading_columns(indexes), indexes=indexes,
                columns=tuple(getattr(ev, "columns", ()) or ()),
                schema=(getattr(ev, "schema", "") or "").split(",")[0].strip(),
                synthetic_like=bool(_LIKE_PLACEHOLDER_RE.search(original_sql or "")))


# ---------------------------------------------------------------- 入口

def root_causes(ev, original_sql: str = "") -> list:
    """证据包(Evidence 或同形对象)→ 根因发现列表。original_sql 是替换占位符之前的原文(没有就用 ev.sql)。"""
    ctx = _context(ev, original_sql)
    if not ctx.nodes:
        return []
    not_in = _not_in_subplans(ctx)
    skip = {name for name in _subplan_refs(ctx, r"NOT \((?:hashed )?(SubPlan \d+)\)")} if not_in else set()
    return (not_in + _correlated_subplans(ctx, skip) + _wrapped_index_columns(ctx)
            + _like_join_patterns(ctx) + _like_leading_wildcards(ctx))


def _all_conds(ctx: _Ctx):
    """(节点下标, 节点, 条件名, 条件文本),按计划里出现的顺序。"""
    for i, n in enumerate(ctx.nodes):
        for key, text in n.conds:
            yield i, n, key, text


def _subplan_refs(ctx: _Ctx, pattern: str) -> list:
    rx = re.compile(pattern)
    return [m.group(1) for _, _, _, text in _all_conds(ctx) for m in rx.finditer(text)]


# ---------------------------------------------------------------- NOT IN

_NOT_IN_SQL_RE = re.compile(
    r"\bnot\s+in\s*\(\s*select\s+(?:distinct\s+)?(?:(\w+)\.)?(\w+)\s+from\s+(?:\w+\.)?(\w+)"
    r"(?:\s+(?:as\s+)?(\w+))?")
_NOT_IN_PLAN_RE = re.compile(r"NOT \((hashed )?(SubPlan \d+)\)")


def _not_in_subplans(ctx: _Ctx) -> list:
    """`NOT (SubPlan n)`:NOT IN 没能变成反连接。SQL 里没写 NOT IN 的(NOT (x = ANY …) 等)不认。"""
    sql_m = _NOT_IN_SQL_RE.search(ctx.sql)
    if not sql_m:
        return []
    out, seen = [], set()
    for _, _, key, text in _all_conds(ctx):
        for m in _NOT_IN_PLAN_RE.finditer(text):
            if m.group(2) in seen:
                continue
            seen.add(m.group(2))
            out.append(_not_in_finding(ctx, sql_m, "%s: %s" % (key, text), bool(m.group(1))))
    return out


def _not_in_finding(ctx: _Ctx, sql_m, cond: str, hashed: bool) -> Finding:
    qualifier, column, table, alias = sql_m.groups()
    # 输出列的前缀既不是 FROM 第一张表也不是它的别名(子查询里有 JOIN):认不准是哪张表的列,不说可空性
    known_table = not qualifier or qualifier in (table, alias)
    if hashed:
        how = ("NOT IN 用的是 hashed SubPlan:子查询结果整体装进内存哈希表,不是反连接;"
               "结果集超过 work_mem 时规划器只能退回逐行子计划")
    else:
        how = "NOT IN 子查询没有变成反连接(Anti Join),退化成逐行子计划:外层每一行都把子查询重跑一遍"
    nf = _null_frac(ctx.columns, table, column) if known_table else None
    nulls = ""
    if nf is not None and nf > 0:
        nulls = ("。子查询输出列 %s.%s 有 NULL(null_frac=%.3f):NOT IN 碰上 NULL 时结果是 NULL 而不是 true,"
                 "所以优化器不能把它当反连接做 —— 而且只要子查询结果里有一个 NULL,这一行的 NOT IN 就不会为真"
                 % (table, column, nf))
    advice = ("改写方向:NOT EXISTS (SELECT 1 FROM %s x WHERE x.%s = <外层列> …),优化器能做成 Hash Anti Join"
              "(反连接),子查询只扫一遍。两者语义不同,改写前要和业务确认:子查询列有 NULL 时 NOT IN 不返回行、"
              "NOT EXISTS 照常返回;外层列为 NULL 的行 NOT IN 不返回、NOT EXISTS 返回。要与原 SQL 结果完全一致,"
              "就在改写里显式处理 NULL;改写后用 verify.py 验证等价与收益。" % (table, column))
    return Finding("not_in_subplan", "warn", "%s —— %s%s" % (cond, how, nulls), advice)


# ---------------------------------------------------------------- 相关子查询逐行执行

def _outer_refs(text: str, node: Node) -> list:
    """扫描节点条件里引用的**别的**关系的列。EXPLAIN 里本表的列不带前缀,外层的带。"""
    bare = _STRING_LIT_RE.sub("''", text)
    return ["%s.%s" % (q, c) for q, c in _QUALIFIED_RE.findall(bare) if q not in node.names]


_IDENT_RE = re.compile(r"^[a-z_][\w$]*$")


def _inner_side(conjunct: str, node: Node) -> str:
    """`a = outer.b` 里属于本表的那一侧(去掉外层括号);两侧都不是 / 不是等值的返回空串。"""
    parts = _unwrap(conjunct).split(" = ")
    if len(parts) != 2:
        return ""
    sides = [_unwrap(p) for p in parts]
    for mine, other in (sides, sides[::-1]):
        if _outer_refs(other, node) and not _outer_refs(mine, node):
            return mine
    return ""


def _correlated_subplans(ctx: _Ctx, skip: set) -> list:
    """非 hashed 的 SubPlan 里有 Seq Scan、条件引用外层的列:外层每一行都整表扫一遍。

    同一张表同一组关联列的多个 SubPlan(S1 的 pay_cnt / max_pay)合成一条 —— 根因与改法是同一个。
    """
    hashed = set(_subplan_refs(ctx, r"hashed (SubPlan \d+)"))
    groups: dict = {}
    for n in ctx.nodes:
        if (not n.op.endswith("Seq Scan") or not re.fullmatch(r"SubPlan \d+", n.subplan)
                or n.subplan in hashed or n.subplan in skip):
            continue
        filt = n.cond("Filter")
        if not filt or not _outer_refs(filt, n):
            continue
        inner = tuple(s for s in (_inner_side(c, n) for c in _conjuncts(filt)) if s)
        groups.setdefault((n.table, inner), []).append((n, filt))
    return [_correlated_finding(ctx, table, inner, hits) for (table, inner), hits in groups.items()]


def _conjuncts(text: str) -> list:
    return [_unwrap(c) for c in _unwrap(text).split(" AND ")]


def _balanced(text: str) -> bool:
    depth = 0
    for ch in text:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth < 0:
            return False
    return depth == 0


def _correlated_finding(ctx: _Ctx, table: str, inner: tuple, hits: list) -> Finding:
    node, filt = hits[0]
    names = "、".join(dict.fromkeys(n.subplan for n, _ in hits))
    detail = ("%s 是相关子查询,外层每一行都执行一次;子查询里 Seq Scan on %s(Filter: %s)每次都把 %s 整表扫一遍,"
              "代价 ≈ 外层行数 × 内层全表扫描" % (names, table + (" " + node.alias if node.alias else ""), filt, table))
    bare = [c for c in inner if _IDENT_RE.match(c)]
    wrapped = [c for c in inner if not _IDENT_RE.match(c)]
    if wrapped:
        fix = ("内层关联列被 %s 包住,%s 上的索引用不上(见同一节的 cast_on_indexed_column / "
               "func_on_indexed_column),先把转换 / 函数挪走" % ("、".join(wrapped), table))
    elif bare and ctx.index_on(table, bare[0], node):
        fix = ("%s 上已有以 %s 开头的索引 %s,计划却没用上 —— 先查关联两侧类型是否一致、关联列上有没有隐式转换"
               % (table, bare[0], ctx.index_on(table, bare[0], node)))
    elif bare:
        ddl = "CREATE INDEX ON %s%s(%s)" % (_ddl_schema(ctx, table), table, ", ".join(bare))
        fix = ("内层关联列 %s.%s 没有可用索引。建议在 %s(%s) 上建索引,让每次子查询走索引(建议,未验证 —— "
               "用 verify.py --index '%s' 验证)" % (table, bare[0], table, ", ".join(bare), ddl))
    else:
        fix = "内层是全表扫描"
    advice = (fix + ";或把相关子查询改写成先按关联列 GROUP BY 预聚合、再 LEFT JOIN 回外层 —— 一次扫描代替逐行扫描,"
              "改写后用 verify.py 验证结果等价与收益。")
    return Finding("correlated_subplan", "warn", detail, advice)


def _ddl_schema(ctx: _Ctx, table: str) -> str:
    """建议 DDL 里表名前的 `schema.`;定不下来就不写。"""
    schema = _schema_of(table, ctx.indexes, ctx.schema)
    return schema + "." if schema else ""


# ---------------------------------------------------------------- 索引列上套了转换 / 函数

_TEXTLIKE = frozenset({"text", "varchar", "character", "bpchar", "name", "nvarchar2", "char", "clob"})
_CAST_RE = re.compile(r"\((?P<col>(?:[a-z_][\w$]*\.)?[a-z_][\w$]*)\)::(?P<type>[a-z][\w]*(?: [a-z]+)*?)(?=[\s),]|$)")
_FUNC_RE = re.compile(r"\b(?P<fn>[a-z_][\w$]*)\(\s*\(?(?P<col>(?:[a-z_][\w$]*\.)?[a-z_][\w$]*)\)?"
                      r"(?:::[a-z][\w ]*?)?\s*[,)]")
_ARITH_RE = re.compile(r"\((?P<col>(?:[a-z_][\w$]*\.)?[a-z_][\w$]*) (?P<op>[-+*/%]) ")


def _wrapped_index_columns(ctx: _Ctx) -> list:
    """Filter / Join Filter 里,有索引的列被类型转换 / 函数 / 算术包住 —— 那条索引因此用不上。

    只报「这张表上确实有以该列开头、而它的扫描没在用的索引」:没有索引时根因是缺索引,不是转换。
    """
    out, seen = [], set()
    for i, n, key, text in _all_conds(ctx):
        scans = _candidate_scans(ctx, i, n, key)
        if not scans:
            continue
        for kind, col, wrapper in _wrappers(_STRING_LIT_RE.sub("''", text)):
            scan = _scan_for(col, n, key, scans)
            column = col.rsplit(".", 1)[-1].lower()
            index = ctx.index_on(scan.table, column, scan) if scan is not None else ""
            if not index or (scan.table, column, wrapper) in seen:
                continue
            seen.add((scan.table, column, wrapper))
            out.append(_wrapped_finding(ctx, kind, scan.table, column, wrapper, index, "%s: %s" % (key, text),
                                        join=key == "Join Filter" or bool(_outer_refs(text, n))))
    return out


def _candidate_scans(ctx: _Ctx, i: int, n: Node, key: str) -> list:
    """哪些全表扫描上的列被包住才算数:

      Filter       节点自己就是 Seq Scan —— 本表的列在这里不带前缀;
      Join Filter  节点是嵌套循环,只看**内层**子树里的 Seq Scan:外层那一侧的表达式
                   不妨碍内层走索引(S1 里 Merge Semi Join 的 `orders.order_date + 3` 就不是根因)。
    """
    if key == "Filter":
        return [n] if n.op.endswith("Seq Scan") else []
    if key == "Join Filter" and n.op.endswith("Nested Loop"):
        kids = children(ctx.nodes, i)
        if len(kids) >= 2:
            return [ctx.nodes[j] for j in subtree(ctx.nodes, kids[1]) if ctx.nodes[j].op.endswith("Seq Scan")]
    return []


def _scan_for(col: str, node: Node, key: str, scans: list):
    """被包住的列属于哪个候选扫描:带前缀的按别名找,裸列只在扫描节点自己的 Filter 里算本表的。"""
    if "." in col:
        alias = col.split(".", 1)[0]
        return next((s for s in scans if alias in s.names), None)
    return node if key == "Filter" else None


def _wrappers(text: str):
    for m in _CAST_RE.finditer(text):
        if m.group("type").split()[0] not in _TEXTLIKE:     # varchar 列显示成 (col)::text 是二进制兼容标注,索引照用
            yield "cast", m.group("col"), "(%s)::%s" % (m.group("col"), m.group("type"))
    for m in _FUNC_RE.finditer(text):
        yield "func", m.group("col"), "%s(%s…)" % (m.group("fn"), m.group("col"))
    for m in _ARITH_RE.finditer(text):
        yield "func", m.group("col"), "(%s %s …)" % (m.group("col"), m.group("op"))


def _resolve(ctx: _Ctx, node: Node, col: str, key: str) -> tuple:
    """条件里的列 → (表, 列)。扫描节点 Filter 里的裸列属于该节点的表;带前缀的按别名找。"""
    if "." in col:
        alias, column = col.split(".", 1)
        return ctx.table_of(alias), column.lower()
    if key == "Filter" and node.table:
        return node.table, col.lower()
    return "", ""


def _unwrap(text: str) -> str:
    """去掉成对包住整段的外层括号:`((a = b))` → `a = b`;`(a) = (b)` 不动。"""
    body = text.strip()
    while body.startswith("(") and body.endswith(")") and _balanced(body[1:-1]):
        body = body[1:-1].strip()
    return body


def _wrapped_finding(ctx: _Ctx, kind: str, table: str, column: str, wrapper: str, index: str,
                     cond: str, join: bool) -> Finding:
    where = "关联条件上" if join else "过滤条件上"
    if kind == "cast":
        explicit = re.search(r"\b(?:\w+\.)?%s\s*::|cast\s*\(\s*(?:\w+\.)?%s\s+as\b" % (column, column),
                             ctx.raw_sql, re.I)
        how = ("SQL 正文里显式写了这个转换" if explicit
               else "SQL 正文里没写这个转换,是比较两侧类型不一致时数据库自动加的隐式转换")
        detail = ("%s —— %s类型转换 %s 套在索引列 %s.%s 上(%s),%s 用不上,只能逐行转换后比较"
                  % (cond, where, wrapper, table, column, how, index))
        advice = ("让比较两侧类型一致:把转换挪到非索引列那一侧(例如对另一侧的值转换成 %s 列的类型;"
                  "先确认编码格式一致 —— 前导零、空格会让两种写法结果不同),或参数 / 常量直接按列的类型给。"
                  "SQL 改不了时可考虑表达式索引 CREATE INDEX ON %s%s ((%s))(要求该列每个值都能转换,"
                  "否则建索引失败;建议,未验证)。改写后用 verify.py 验证等价与收益。"
                  % (column, _ddl_schema(ctx, table), table, wrapper.replace("(%s)" % column, column, 1)))
        return Finding("cast_on_indexed_column", "warn", detail, advice)
    detail = ("%s —— %s索引列 %s.%s 被函数 / 表达式 %s 包住,%s 用不上"
              % (cond, where, table, column, wrapper, index))
    advice = ("把函数 / 运算挪到非索引列那一侧,或改成等价的范围条件(如 to_char(d,'YYYY-MM-DD') = '…' 改成 "
              "d >= … AND d < …);SQL 改不了时可建同一表达式上的表达式索引(建议,未验证)。"
              "改写后用 verify.py 验证等价与收益。")
    return Finding("func_on_indexed_column", "warn", detail, advice)


# ---------------------------------------------------------------- LIKE

_LIKE_RE = re.compile(r"^(?P<lhs>\(?(?:[a-z_][\w$]*\.)?[a-z_][\w$]*\)?(?:::[a-z][\w ]*?)?) ~~\*? (?P<rhs>.+)$")
_LEADING_WILDCARD_RE = re.compile(r"(?P<lhs>\(?(?:[a-z_][\w$]*\.)?[a-z_][\w$]*\)?(?:::[a-z][\w ]*?)?) ~~\*? "
                                  r"(?P<pat>'[%_](?:''|[^'])*')")


def _like_column(lhs: str) -> str:
    return re.sub(r"::[a-z][\w ]*", "", lhs).strip("() ")


def _like_join_patterns(ctx: _Ctx) -> list:
    """`a.code ~~ (b.k || '%')`:模式取自另一侧的列,要到运行时逐行才知道,索引范围扫描无从谈起。"""
    out, seen = [], set()
    for _, n, key, text in _all_conds(ctx):
        if key not in ("Filter", "Join Filter"):
            continue
        # 逐个 AND 子句看:别的子句里的外层列不能算到 LIKE 的模式头上
        for m in filter(None, (_LIKE_RE.match(c) for c in _conjuncts(text))):
            rhs = _STRING_LIT_RE.sub("''", m.group("rhs"))
            lhs_col = _like_column(m.group("lhs"))
            if (_QUALIFIED_RE.search(rhs) or "SubPlan" in rhs) and lhs_col not in seen:
                seen.add(lhs_col)
                out.append(_like_join_finding(ctx, n, key, text, lhs_col))
    return out


def _like_join_finding(ctx: _Ctx, n: Node, key: str, text: str, lhs_col: str) -> Finding:
    table, column = _resolve(ctx, n, lhs_col, key)
    index = ctx.index_on(table, column) if table else ""
    idx_note = ",%s 上的 %s 用不上" % (table, index) if index else ""
    detail = ("%s: %s —— LIKE 的模式取自另一侧的列,要到运行时逐行才知道,规划器没法把它变成索引范围扫描%s,"
              "只能嵌套循环逐行比对" % (key, text, idx_note))
    advice = ("前缀长度固定时(如前缀来自 substr(x, 1, 8))改成等值关联:substr(%s, 1, <前缀长度>) = <另一侧前缀> —— "
              "等值条件能走 Hash Join,不必逐行比对;还要走索引就建同一表达式的表达式索引。"
              "前提是前缀里不含 %% 或 _(LIKE 的通配符),改写后用 verify.py 验证等价与收益。" % lhs_col)
    return Finding("like_join_pattern", "warn", detail, advice)


def _like_leading_wildcards(ctx: _Ctx) -> list:
    """`col ~~ '%abc'`:左模糊,B-tree 索引用不上 —— 有没有索引都说,免得被「Filter 列上建索引」带偏。

    原 SQL 里 LIKE 的模式是占位符时不报:计划里那个 '%test%' 是 sqltune 自己合成的。
    """
    if ctx.synthetic_like:
        return []
    out, seen = [], set()
    for _, n, key, text in _all_conds(ctx):
        if key not in ("Filter", "Join Filter"):
            continue
        for m in _LEADING_WILDCARD_RE.finditer(text):
            col = _like_column(m.group("lhs"))
            if col in seen:
                continue
            seen.add(col)
            detail = "%s: %s —— 左模糊匹配 %s(模式以 %% 或 _ 开头)" % (key, text, m.group("pat"))
            advice = ("左模糊没法用 B-tree 索引做范围扫描,给 %s 建普通索引也用不上。能改成前缀匹配('abc%%')就改;"
                      "必须任意位置匹配时考虑全文检索(tsvector + GIN 索引),或先用别的有索引的条件把范围缩小。" % col)
            out.append(Finding("like_leading_wildcard", "warn", detail, advice))
    return out
