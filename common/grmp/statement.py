"""语句形态判定：这条 SQL 是只读的吗。

放在 common/grmp/ 而不是 tools/：注册期的硬拦截（script.py）与运行期的
会话模式（executor / runner）都要用它，tools 里的风险标注也复用同一份。
两处各判各的话，会出现「注册时认为只读、执行时开了写会话」这种错位。

判定是词法层面的启发式，不是 SQL 解析器。宁可把不确定的判成"写"——
判错成只读会让写操作拿到写会话之外的默许，判错成写只是多要一次显式声明。
"""
from __future__ import annotations

import re
from typing import List, Optional

# 只读语句的起始关键字。不在此列的一律当作写操作。
READ_ONLY_STARTERS = frozenset(
    {"select", "with", "explain", "show", "values", "table", "fetch"}
)

# 前导注释与空白
_LEADING_NOISE_RE = re.compile(r"^(?:\s|--[^\n]*\n?|/\*.*?\*/)+", re.DOTALL)
# M 兼容库(MySQL 词法)另有 # 注释;/*! … */ 在 MySQL 里是「会执行的注释」,不当注释跳
_LEADING_NOISE_M_RE = re.compile(r"^(?:\s|#[^\n]*\n?|--[^\n]*\n?|/\*(?!!).*?\*/)+", re.DOTALL)

# ---- 方言(v1.5.1,业务库可能是 M 兼容)-------------------------------------------------------------
# A:PostgreSQL 词法(A / PG / C 兼容库,也是 1.5.0 及以前的唯一规则)。
# BOTH:M 兼容库、以及兼容模式未知时 —— A、M 两套词法各切一遍,任一套判成多条 / 写操作即拒。
#   M 库不只按 M 词法切:M 的 sql_mode 可以关掉反斜杠转义(NO_BACKSLASH_ESCAPES),那时 'a\' 就在第二个引号处结束,
#   只按「反斜杠转义」切会把后面的 ; DELETE … 当成字符串内容放过去。两套都切,哪种设置都漏不了;代价是极少数
#   写法(# 注释里带分号、字符串以反斜杠结尾后面还有分号)会被多拒,方向是安全的。
DIALECT_A, DIALECT_BOTH = "A", "both"
_dialect: Optional[str] = None          # set_dialect() 设的本进程方言;None = 没设过,按 A(与 1.5.0 相同)


def dialect_for(mode: str) -> str:
    """业务库兼容模式 → 切词方言:A / PG / C → A;M / B / 未知('')→ 两套都切。"""
    return DIALECT_A if (mode or "").strip().upper() in ("A", "PG", "C") else DIALECT_BOTH


def set_dialect(mode: str) -> None:
    """按业务库兼容模式设本进程的切词方言。access.runner_for 造取数器时调;技能进程一次只连一个库。"""
    global _dialect
    _dialect = dialect_for(mode)


def _resolve(dialect: Optional[str]) -> str:
    if dialect is not None:
        return dialect if dialect in (DIALECT_A, DIALECT_BOTH) else dialect_for(dialect)
    return _dialect or DIALECT_A


def leading_keyword(sql: str, dialect: Optional[str] = None) -> str:
    """跳过前导空白与注释，取第一个关键字（小写）。两套都切时 # 注释也算前导注释。"""
    noise = _LEADING_NOISE_RE if _resolve(dialect) == DIALECT_A else _LEADING_NOISE_M_RE
    stripped = noise.sub("", sql or "").lstrip()
    match = re.match(r"[A-Za-z_]+", stripped)
    return match.group(0).lower() if match else ""


def _scan(sql: str):
    """逐字符扫描，标出每个字符处在 sql / quote / comment 哪一段。

    单遍扫描共享给 strip_noise 与 split_statements —— 两处各写一套状态机的话，
    迟早在某个转义细节上分叉，而分叉出来的差异是静默的。

    转义与引号的处理刻意保守：`E'\\''` 这类反斜杠转义会被当成字面量提前结束，
    `$$...$$` 完全不认。两者的错法都是**多切一刀**，也就是把一条语句判成多条 ——
    多判出来的语句会被上层拒掉，是"错杀"而不是"放过"。反过来漏切才危险。
    """
    i, n = 0, len(sql)
    quote = None
    comment = None
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if comment == "--":
            yield ch, "comment"
            if ch == "\n":
                comment = None
            i += 1
        elif comment == "/*":
            yield ch, "comment"
            if ch == "*" and nxt == "/":
                yield nxt, "comment"
                comment = None
                i += 2
            else:
                i += 1
        elif quote:
            yield ch, "quote"
            if ch == quote:
                if nxt == quote:      # 连续两个引号是转义，不结束字面量
                    yield nxt, "quote"
                    i += 2
                    continue
                quote = None
            i += 1
        elif ch == "-" and nxt == "-":
            comment = "--"
            yield ch, "comment"
            yield nxt, "comment"
            i += 2
        elif ch == "/" and nxt == "*":
            comment = "/*"
            yield ch, "comment"
            yield nxt, "comment"
            i += 2
        elif ch in ("'", '"'):
            quote = ch
            yield ch, "quote"
            i += 1
        else:
            yield ch, "sql"
            i += 1


_SPACE = " \t\r\n\f\v"


def _scan_m(sql: str):
    """MySQL 词法(M 兼容库):与 _scan 同样产出 (字符, sql / quote / comment)。

    与 PG 词法的差别:# 到行尾是注释;-- 后面必须跟空白才是注释(--1 是两个负号);/*! … */ 是会执行的内容,
    不当注释;单引号、双引号都是字符串(双引号默认不是标识符),里面反斜杠转义下一个字符;反引号是标识符,
    里面连续两个反引号是转义、反斜杠不转义。
    """
    i, n = 0, len(sql)
    quote = None
    comment = None
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if comment == "line":
            yield ch, "comment"
            if ch == "\n":
                comment = None
            i += 1
        elif comment == "block":
            yield ch, "comment"
            if ch == "*" and nxt == "/":
                yield nxt, "comment"
                comment = None
                i += 2
            else:
                i += 1
        elif quote:
            yield ch, "quote"
            if ch == "\\" and quote != "`" and nxt:
                yield nxt, "quote"
                i += 2
                continue
            if ch == quote:
                if nxt == quote:
                    yield nxt, "quote"
                    i += 2
                    continue
                quote = None
            i += 1
        elif ch == "#":
            comment = "line"
            yield ch, "comment"
            i += 1
        elif ch == "-" and nxt == "-" and (i + 2 >= n or sql[i + 2] in _SPACE):
            comment = "line"
            yield ch, "comment"
            yield nxt, "comment"
            i += 2
        elif ch == "/" and nxt == "*" and sql[i + 2:i + 3] != "!":
            comment = "block"
            yield ch, "comment"
            yield nxt, "comment"
            i += 2
        elif ch in ("'", '"', "`"):
            quote = ch
            yield ch, "quote"
            i += 1
        else:
            yield ch, "sql"
            i += 1


def strip_noise(sql: str, literals: bool = False, _scanner=_scan) -> str:
    """把注释（可选：连同字符串字面量）抹成等长空格。

    抹成空格而不是删掉：`select 1--c\\nfrom t` 直接删注释会粘成 `select 1from t`，
    再去数关键字就成了 `1from`。等长替换还顺带保住了字符偏移。

    literals=True 时连字面量一起抹 —— 扫关键字时要的是这个，否则
    `select 'delete' as a` 会被当成 DELETE。
    """
    out = []
    for ch, state in _scanner(sql):
        blank = state == "comment" or (literals and state == "quote")
        # 换行保留原样：`--` 注释靠它收尾，抹成空格会把下一行并进注释里
        out.append(("\n" if ch == "\n" else " ") if blank else ch)
    return "".join(out)


def _split(sql: str, scanner) -> List[str]:
    out, buf = [], []
    for ch, state in scanner(sql):
        if state == "sql" and ch == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [s for s in out if strip_noise(s, _scanner=scanner).strip()]


def split_statements(sql: str, dialect: Optional[str] = None) -> List[str]:
    """按分号拆成多条语句，跳过字符串字面量**与注释**里的分号。

    中间件允许一次调用发多条语句（PREPARE + EXPLAIN EXECUTE 就靠这个），
    所以判定必须逐条来。只看首个关键字的话，
    `select 1; drop table t;` 会被判成只读。

    注释里的分号原先是算数的，于是 `SELECT 1 AS x -- a;b;c` 被判成 3 条语句。
    实测后果：中间件模式下这条完全合法的单语句被 explain 拒掉，理由还是错的
    （"含多条语句（3 条）"）；直连模式靠回落到原始会话侥幸能跑 —— 同一条 SQL
    两条路两个结果。现场环境只有中间件，那就是硬失败。

    只由注释和空白构成的片段会被丢掉：`SELECT 1; -- 收尾注释` 是一条语句，
    不是两条。丢掉不含可执行内容的片段不可能凭空多出语句，方向上是安全的。

    两套都切时(M 库 / 兼容模式未知)返回切出语句更多的那一份:多出来的语句会被上层拒掉,方向是安全的。
    """
    a = _split(sql, _scan)
    if _resolve(dialect) == DIALECT_A:
        return a
    m = _split(sql, _scan_m)
    return m if len(m) > len(a) else a


def _parts_each(sql: str, dialect: Optional[str]):
    """按方言给出几份切法 (切出的语句, 用的词法, 取首关键字时的方言):A 库一份;两套都切时两份,
    首关键字都按 M 规则跳前导注释(# 注释也跳,否则以 # 注释开头的 M 语句会被误拒)。"""
    d = _resolve(dialect)
    yield _split(sql, _scan), _scan, d
    if d != DIALECT_A:
        yield _split(sql, _scan_m), _scan_m, d


def is_read_only(sql: str, dialect: Optional[str] = None) -> bool:
    """整条脚本（可能含多语句）是否全部只读。两套都切时两套都得判只读。"""
    for parts, _scanner, d in _parts_each(sql, dialect):
        if parts and not all(leading_keyword(p, d) in READ_ONLY_STARTERS for p in parts):
            return False
    return True


# 会改数据的起始关键字。DDL/DCL 不在这里 —— 它们由 READ_ONLY_STARTERS
# 的白名单兜住，不需要再列一份黑名单（列黑名单永远会漏，COPY/GRANT/VACUUM
# 就是原先那份 DDL 正则漏掉的）。
_DML_STARTERS = frozenset({"insert", "update", "delete", "merge"})
_DML_WORD_RE = re.compile(r"(?i)\b(insert|update|delete|merge)\b")


def is_dml(sql: str, dialect: Optional[str] = None) -> bool:
    """这条 SQL 会不会改数据。

    **按归一化后的首关键字判，不在原始文本上跑正则。** 原先三个 skill 各自
    抄了一份 `^\\s*(insert|update|delete|merge)\\b`：`^\\s*` 跳空白但不跳注释，
    于是 `/* c */ UPDATE t SET ...` 判成非 DML。

    实测后果（og5，gsql 与直连驱动两条各复现一次）：explain --analyze
    下这条载荷既没被拒绝（拒绝检查用的就是本函数），也没被包进回滚事务
    （包不包也用本函数），UPDATE/DELETE 直接落盘 —— 表被改、被清空，
    而 explain 退出码 0，报告里是一份看起来完全正常的执行计划。
    一个函数同时把着"要不要拒"和"要不要回滚"两道闸，判错一次就是双重失效。

    CTE 里藏写操作（`WITH x AS (DELETE ... RETURNING 1) SELECT * FROM x`）
    首关键字是 with，得往里再看一眼；扫之前先把注释和字符串字面量抹掉，
    否则 `WITH x AS (SELECT 'delete') ...` 会被误判。
    """
    for parts, scanner, d in _parts_each(sql, dialect):  # 两套都切时任一套判成 DML 就是 DML
        for part in parts:
            keyword = leading_keyword(part, d)
            if keyword in _DML_STARTERS:
                return True
            if keyword == "with" and _DML_WORD_RE.search(
                    strip_noise(part, literals=True, _scanner=scanner)):
                return True
    return False


class ExplainNotAllowed(Exception):
    """这条 SQL 不能递进 EXPLAIN 模板。"""


def ensure_explainable(sql: str, analyze: bool = False, dialect: Optional[str] = None) -> None:
    """把用户 SQL 递进 `EXPLAIN (...) {{sql}}` 模板前的守卫。

    白名单模型下，EXPLAIN 用户临时给的 SQL 只有这一条路:注册一条模板，
    用户 SQL 落进参数位。而中间件是**文本替换**不是绑定变量，参数位就是
    注入面 —— 本函数是第一道防线。

    三道防线缺一不可:
      1. 这里:单语句 + 只读
      2. 脚本标 readonly: true，只读会话里执行 —— DML/DDL 被数据库挡掉
      3. 模板里 ANALYZE 写死 —— 不 analyze 时用户 SQL 根本不被执行

    **为什么不放行 DML 的 EXPLAIN ANALYZE**:那要 `BEGIN; ...; ROLLBACK;`
    多语句模板加可写会话。实测载荷

        SELECT 1 AS n; COMMIT; CREATE TABLE x(i int); --

    用一个 `--` 注释掉模板末尾的 ROLLBACK，表真建出来了 —— 回滚包装拦不住。
    而那条脚本必须可写，逃出来就是带写权限的任意 SQL 通道。收益配不上代价。

    analyze 参数在这里只影响错误措辞:只读 SQL 两种都放行，非只读两种都拒。
    留着它是为了让调用方的意图出现在调用点上。
    """
    if not sql or not sql.strip():
        raise ExplainNotAllowed("SQL 为空。")

    statements = split_statements(sql, dialect)
    if not statements:
        raise ExplainNotAllowed("SQL 里没有可执行的语句（只有注释或空白）。")
    if len(statements) > 1:
        raise ExplainNotAllowed(
            "SQL 含多条语句（%d 条），不能递进 EXPLAIN 模板。\n"
            "模板是文本替换，多语句会整串拼进去 —— 实测能用一个 `--` "
            "注释掉模板尾部，绕过原本的限制。请一次只给一条语句。"
            % len(statements)
        )

    # 纯注释现在由 split_statements 直接丢掉，上面 `not statements` 那条就兜住了。
    # 这里再判一次是因为「片段非空但取不出首关键字」仍然可能（比如一段孤立的
    # 字符串字面量）—— 递进模板会拼出 EXPLAIN 后面空无一物，报的是看不出
    # 所以然的语法错。
    if not leading_keyword(statements[0], dialect):
        raise ExplainNotAllowed("SQL 里没有可执行的语句（只有注释或空白）。")

    # 只在 analyze 时才要求只读。EXPLAIN 不带 ANALYZE **根本不执行**语句 ——
    # 实测 `EXPLAIN UPDATE ...` 走只读模板能正常出计划(4 行)，拒掉它是
    # 平白砍能力。带 ANALYZE 就不一样了:语句会真跑，只读会话当场拦下，
    # 但要在这里先说清原因，而不是让用户看一句数据库的只读事务报错。
    if analyze and not is_read_only(statements[0], dialect):
        raise ExplainNotAllowed(
            "EXPLAIN ANALYZE 只受理只读语句，本次是 %s。\n"
            "带 ANALYZE 时语句会**真执行**。写语句要包在回滚事务里跑，"
            "而回滚包装实测可被一个 `--` 注释绕过，那条通道等于开放写权限。\n"
            "写语句的实际执行计划请走直连（driver: psycopg2）。"
            % (leading_keyword(statements[0], dialect).upper() or "非查询语句")
        )
