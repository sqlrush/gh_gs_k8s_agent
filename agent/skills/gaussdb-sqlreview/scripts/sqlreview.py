#!/usr/bin/env python3
"""sqlreview — review SQL against the standards in references/rules.yaml.

Three sources, one rule engine:
    --file / --stdin   static SQL text (no DB needed)
    --sql-id / --top   SQL that actually ran, pulled from dbe_perf (needs -c)
    --schema           existing tables and indexes in the catalog (needs -c)

Exit codes: 0 = script ran (violations or not), 1 = runtime error,
2 = connection/config error. Violations do NOT change the exit code — the
verdict is in stdout, the exit code only says whether the script itself worked.

Usage:
    sqlreview.py --file changes.sql
    sqlreview.py -c <conn> --schema public
    sqlreview.py -c <conn> --sql-id <id> [--format json]
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

import common  # noqa: E402
from common import access, session  # noqa: E402
from common import cli  # noqa: E402

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/sqlreview/ —— 两条路径共用同一份定义

import checks  # noqa: E402
import lexer  # noqa: E402
import objects  # noqa: E402
import report  # noqa: E402
import sqlfetch  # noqa: E402
from model import ReviewResult, RuleError  # noqa: E402
from rules import DEFAULT_RULES_PATH, load_rules  # noqa: E402

TOP_SQL_SCRIPT = "sqlreview.top_sql"


def review_text(sql: str, rules, source: str, notes=()) -> ReviewResult:
    """Lex the SQL and run every text rule over it."""
    stmts = lexer.split(sql)
    return ReviewResult(
        source=source,
        findings=checks.check_statements(stmts, rules),
        statements=len(stmts),
        notes=tuple(notes),
    )


def review_objects(runner, schema: str, rules) -> ReviewResult:
    """Snapshot the catalog and run every object rule over it."""
    facts = objects.collect_facts(runner, schema)
    return ReviewResult(
        source=f"schema:{schema}",
        findings=checks.check_objects(facts, rules),
        objects=len(facts.tables) + len(facts.indexes),
        notes=facts.notes,
    )


def _sql_from_id(runner, sql_id: str) -> tuple[str, list[str]]:
    res = sqlfetch.sql_fetch(runner, sql_id)
    notes: list[str] = []
    if res.truncated:
        notes.append(f"SQL 文本疑似被截断（{res.truncated_reason}），审查结果可能不完整")
    if res.normalized:
        notes.append(f"SQL 为归一化文本（{res.placeholders} 个占位符），字面量已丢失")
    return res.sql, notes


def _sql_from_top(runner, top: int) -> tuple[str, list[str]]:
    rows = runner.run(TOP_SQL_SCRIPT, {"limit": int(top)})
    if not rows:
        return "", ["dbe_perf.statement 无数据（检查 enable_stmt_track）"]
    text = ";\n".join(str(r["query"]).rstrip().rstrip(";") for r in rows) + ";"
    return text, [f"取自 dbe_perf.statement 的 Top {len(rows)} SQL（按总耗时）"]


def _parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="sqlreview.py",
        description="Review SQL against the standards in references/rules.yaml")
    ap.add_argument("-c", "--conn", help="连接名（--sql-id/--top/--schema 需要）")
    cli.add_session_arg(ap)

    src = ap.add_argument_group("输入源（三选一）")
    src.add_argument("--file", help="待审查的 SQL 文件")
    src.add_argument("--stdin", action="store_true", help="从标准输入读 SQL")
    src.add_argument("--sql-id", help="审查某个 unique_sql_id 对应的线上 SQL")
    src.add_argument("--top", type=int, help="审查耗时最高的 N 条线上 SQL")
    src.add_argument("--schema", help="审查该 schema 下已存在的表与索引")

    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--timeout", type=int, default=None, help="查询超时（秒）")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    cli.apply_session_arg(args)

    chosen = [n for n, v in (("--file", args.file), ("--stdin", args.stdin),
                             ("--sql-id", args.sql_id), ("--top", args.top),
                             ("--schema", args.schema)) if v]
    if len(chosen) != 1:
        print("error: 需要且只能指定一个输入源："
              "--file / --stdin / --sql-id / --top / --schema", file=sys.stderr)
        return 1

    try:
        rules = load_rules(DEFAULT_RULES_PATH)
    except RuleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # 要连库的取数源：没给 -c 时回落到 gaussdb-login 建立的会话。
    #
    # 这里原先直接要求 -c —— 别的 skill 都改成「省略即用会话」之后，只有它
    # 还在拦，表现成「明明登录了，sqlreview 却说要指定连接名」。
    # 两者都没有时由 config.resolve() 报错，那条消息会告诉用户去跑 login。
    needs_db = bool(args.sql_id or args.top or args.schema)
    if needs_db and not args.conn:
        try:
            live = session.current()
        except common.ConfigError as exc:
            # 沙箱里多个会话又没带句柄:session.current() 会抛出候选清单——这是给用户看的一句话,
            # 不能裸露成 Traceback(场景矩阵抓到的:两个会话并存时 sqlreview 三条用例直接崩)。
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if live is None:
            print(f"error: {chosen[0]} 需要连接。{session.NOT_LOGGED_IN}gsql 模式也可用 -c/--conn 指定连接名。", file=sys.stderr)
            return 1

    # --- DB-free sources -------------------------------------------------
    if not needs_db:
        try:
            if args.stdin:
                sql, source = sys.stdin.read(), "stdin"
            else:
                sql = pathlib.Path(args.file).read_text(encoding="utf-8")
                source = f"file:{args.file}"
        except OSError as exc:
            print(f"error: 读取 SQL 失败：{exc}", file=sys.stderr)
            return 1
        _emit(review_text(sql, rules, source), args.format)
        return 0

    # --- DB-backed sources -----------------------------------------------
    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.schema:
            res = review_objects(runner, args.schema, rules)
        elif args.sql_id:
            sql, notes = _sql_from_id(runner, args.sql_id)
            res = review_text(sql, rules, f"sql_id:{args.sql_id}", notes)
        else:
            sql, notes = _sql_from_top(runner, args.top)
            res = review_text(sql, rules, f"top:{args.top}", notes)
        _emit(res, args.format)
        return 0
    except (ValueError, KeyError, RuleError, access.QueryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:          # 渲染/协议层的失败也要清楚报出来
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _emit(res: ReviewResult, fmt: str) -> None:
    out = report.render_json(res) if fmt == "json" else report.render_markdown(res)
    print(out, end="" if out.endswith("\n") else "\n")


if __name__ == "__main__":
    raise SystemExit(main())
