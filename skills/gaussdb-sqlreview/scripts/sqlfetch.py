"""Resolve a unique_sql_id to SQL text (port of internal/probe/sqlfetch.go).

statement_history first (literal values), dbe_perf.statement as the normalized
fallback. The id is parsed as an integer and inlined (safe: int-validated).

openGauss caps the SQL text it retains in its stat views (track_activity_query_size,
default 1024 bytes). Long statements come back TRUNCATED — there is no way to
recover the missing tail from the database. looks_truncated() detects this so
callers degrade gracefully (ask for full text via --sql-stdin) instead of
EXPLAIN-ing a half statement and emitting a cryptic syntax error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PLACEHOLDER_RE = re.compile(r"\?|\$\d+|(?:^|[^:])(:[a-zA-Z_]\w*)")


import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/sqlreview/ —— 两条路径共用同一份定义

HISTORY_SCRIPT = "sqlreview.from_history"
STATEMENT_SCRIPT = "sqlreview.from_statement"


# Tokens that cannot legitimately end a complete statement — if the stored text
# stops here, openGauss cut it off mid-statement.
_INCOMPLETE_TAIL = frozenset({
    "select", "from", "where", "and", "or", "in", "not", "join", "on", "by",
    "group", "order", "having", "union", "as", "exists", "between", "like",
    "limit", "offset", "case", "when", "then", "else",
})


@dataclass(frozen=True)
class FetchResult:
    sql_id: str
    sql: str
    schema: str
    source: str  # statement_history | statement
    normalized: bool
    placeholders: int
    truncated: bool = False
    truncated_reason: str = ""
    degraded_reason: str = ""   # statement_history 不可用时退到 statement 的原因（备机 / 权限 …）


def count_placeholders(sql_text: str) -> int:
    n = 0
    for m in _PLACEHOLDER_RE.finditer(sql_text):
        if m.group(1) or m.group(0).startswith("?") or m.group(0).startswith("$"):
            n += 1
    return n


def looks_truncated(sql: str) -> tuple[bool, str]:
    """Detect SQL cut off by openGauss's stored-text cap. DB-free heuristics."""
    s = sql.strip()
    if not s:
        return False, ""
    # 1) Parenthesis balance, ignoring single-quoted string contents.
    depth, i, n = 0, 0, len(s)
    while i < n:
        c = s[i]
        if c == "'":
            i += 1
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    if depth > 0:
        return True, f"{depth} 个未闭合的左括号"
    # 2) Ends mid-statement (trailing comma/open-paren or a dangling keyword).
    tail = s.rstrip(";").rstrip()
    if tail.endswith(("(", ",")):
        return True, f"结尾停在 '{tail[-1]}'"
    words = tail.split()
    if words and words[-1].lower() in _INCOMPLETE_TAIL:
        return True, f"结尾停在残词 '{words[-1]}'"
    return False, ""


def sql_fetch(runner, raw_id: str) -> FetchResult:
    """经统一入口取数。走中间件还是直连由连接的 driver 决定，这里不感知。"""
    try:
        sid = int(raw_id.strip())
    except ValueError as exc:
        raise ValueError(
            f"sql id {raw_id!r}: must be a (possibly negative) integer") from exc

    rows, degraded = _history_rows(runner, HISTORY_SCRIPT, sid)
    if rows:
        schema, query = rows[0]["schema_name"], rows[0]["query"]
        source = "statement_history"
    else:
        srows = runner.run(STATEMENT_SCRIPT, {"sid": sid})
        if not srows:
            # **「查不到」最常见的原因不是配置问题,而是这条语句已经不在统计里了。**
            # dbe_perf.statement 是**自上次清零以来**的累计视图:实例一重启就清空
            # (og5 实测重启后整个视图只剩 51 条,上午那些 sql_id 全没了);
            # statement_history 则会随时间把老语句挤出去。
            # 只报一句 "not found ... check enable_stmt_track" 会把人往配置方向带,
            # 而真正该做的是换一条路:手上已经有 SQL 文本(比如 WDR 报告的 SQL 附录、
            # 大盘的 sql_id 抽屉)时,直接用 --sql-stdin 传进来,不必再按 id 碰运气。
            raise ValueError(
                f"sql id {raw_id} 在 dbe_perf.statement_history 与 dbe_perf.statement "
                f"里都查不到。常见原因按可能性排序:\n"
                f"  1. **实例重启过** —— dbe_perf.statement 是自上次清零以来的累计视图,"
                f"重启即清空,重启之前的 sql_id 一律查不到;\n"
                f"  2. 这条语句已从 statement_history 里**老化**出去(历史是滚动的);\n"
                f"  3. 统计没开:enable_stmt_track / track_stmt_parameter。\n"
                f"  已经有 SQL 文本时(WDR 报告的「SQL 附录」、大盘点 sql_id 的抽屉里都有),"
                f"改用 --sql-stdin 把文本传进来即可,不用按 id 再查一次;"
                f"参数化语句里的 ? 要先填上真实值才能出计划。"
                + (f"\n  另:statement_history 本身不可用:{degraded}" if degraded else ""))
        schema, query = "", srows[0]["query"]
        source = "statement"

    n = count_placeholders(query)
    truncated, reason = looks_truncated(query)
    return FetchResult(sql_id=raw_id, sql=query, schema=schema or "",
                       source=source, normalized=n > 0, placeholders=n,
                       truncated=truncated, truncated_reason=reason,
                       degraded_reason=degraded)


def _history_rows(runner, script: str, sid: int):
    """statement_history 查不了(备机上它是 unlogged 表读不到、没权限……)不是终点：
    退到 dbe_perf.statement 拿归一化文本，把原因带在结果里明写，而不是整条命令中断。"""
    import sys
    from common.grmp.errors import QueryError
    try:
        return runner.run(script, {"sid": sid}), ""
    except QueryError as exc:
        print(f"warning: statement_history 不可用，降级到 dbe_perf.statement（归一化文本）：{exc}",
              file=sys.stderr)
        return [], str(exc)
