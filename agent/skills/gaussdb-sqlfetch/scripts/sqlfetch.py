#!/usr/bin/env python3
"""sqlfetch — resolve a unique_sql_id to full SQL text.

Port of internal/probe/sqlfetch.go + internal/cli/sqlfetch.go. statement_history
first (literal values), dbe_perf.statement as the normalized fallback.

Usage:
    sqlfetch.py -c <conn> <unique_sql_id> [--format json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break
        
for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break
        
# SQL 已迁到 scripts/registry/sqlfetch/ —— 两条路径共用同一份定义

import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
import render  # noqa: E402

_PLACEHOLDER_RE = re.compile(r"\?|\$\d+|(?:^|[^:])(:[a-zA-Z_]\w*)")

# Tokens that cannot legitimately end a complete statement — if the stored text
# stops here, openGauss cut it off mid-statement (track_activity_query_size cap).
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
    tail = s.rstrip(";").rstrip()
    if tail.endswith(("(", ",")):
        return True, f"结尾停在 '{tail[-1]}'"
    words = tail.split()
    if words and words[-1].lower() in _INCOMPLETE_TAIL:
        return True, f"结尾停在残词 '{words[-1]}'"
    return False, ""


HISTORY_SCRIPT = "sqlfetch.from_history"
STATEMENT_SCRIPT = "sqlfetch.from_statement"


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
    from common.grmp.errors import QueryError
    try:
        return runner.run(script, {"sid": sid}), ""
    except QueryError as exc:
        print(f"warning: statement_history 不可用，降级到 dbe_perf.statement（归一化文本）：{exc}",
              file=sys.stderr)
        return [], str(exc)


def fetch_report(r: FetchResult) -> str:
    out = f"## SQL Fetch {r.sql_id}\n\n- Source: `dbe_perf.{r.source}`\n"
    if r.schema:
        out += f"- Schema: `{r.schema}`\n"
    if r.degraded_reason:
        out += (f"- ⚠️ **statement_history 不可用，已降级到 `dbe_perf.statement`**（归一化文本，参数值是占位符）："
                f"{r.degraded_reason.splitlines()[0]}\n"
                f"  要真实参数值：备机的话用主库 IP 重新 gaussdb-login；否则按提示处理后重跑。\n")
    if r.truncated:
        out += (f"- 🛑 **SQL 被 openGauss 截断**（{r.truncated_reason}）：留存文本受 "
                f"`track_activity_query_size` 限制，数据库里没有完整 SQL。**不要**拿这段半截 SQL "
                f"去 EXPLAIN/调优——请向用户索要完整 SQL 并用 `--sql-stdin` 传入。\n")
    if r.normalized:
        out += (f"- ⚠️ Normalized SQL with {r.placeholders} placeholder(s): "
                f"replace them with real values before EXPLAIN/collect.\n")
    out += "\n" + render.code_block("sql", r.sql)
    # 现场(客户 09-09 早):模型按这里的提示走 explain --sql-stdin 贴文本,schema 在这一步丢了 → 400。
    # 下一步要指向按 id 的入口(它们自己取 SQL 并带 schema);非要贴文本,Schema 行必须带成 --schema,不许猜。
    carry = (f"贴文本走 `--sql-stdin` 时必须带 `--schema {r.schema}`(就是上面 Schema 行的值),不要猜。" if r.schema
             else "输出里没有 Schema 行:贴文本前先问用户这条 SQL 平时在哪个 schema 下跑,不要猜 public 或别的 schema 去试。")
    out += (f"\nNext: 按 id 走——看计划用 gaussdb-explain 的 `explain.py -c <conn> --sql-id {r.sql_id}`,"
            f"调优用 gaussdb-sqltune 的 `sqltune.py -c <conn> {r.sql_id}`(它们自己取 SQL 并带上这条 SQL 的 schema)。{carry}\n")
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="sqlfetch.py",
                                 description="Resolve a unique_sql_id to full SQL text")
    ap.add_argument("sql_id", help="unique_sql_id (integer, may be negative)")
    ap.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--timeout", type=int, default=None)
    args = ap.parse_args(argv)
    cli.apply_session_arg(args)

    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        r = sql_fetch(runner, args.sql_id)
        if args.format == "json":
            print(json.dumps(r.__dict__, ensure_ascii=False, indent=2))
        else:
            print(fetch_report(r), end="")
        return 0
    except (ValueError, KeyError, common.DBError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:          # 渲染/协议层的失败也要清楚报出来
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
