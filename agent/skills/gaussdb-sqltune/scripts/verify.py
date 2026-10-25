#!/usr/bin/env python3
"""Verify a SQL rewrite (port of internal/probe/verify.go + cli/verify.go).

Compares planner cost of original vs rewritten SQL via EXPLAIN, and optionally
checks result-set equivalence via md5 row-hash sampling. Combined mode adds
hypothetical indexes (gs_index_advise auto-discovery and/or explicit DDLs).

Both --original and --rewrite must be fully executable SQL (no placeholders).

Usage:
    verify.py -c <conn> --original '<sql>' --rewrite '<sql>' [--no-equiv]
              [--auto-index] [--index 'CREATE INDEX ...' ...] [--format json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

import common  # noqa: E402
import systables  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
from cost import explain_cost, quote_columns, quote_ident, quote_sql_literal  # noqa: E402
from evidence import is_dml  # noqa: E402
from sqlfetch import count_placeholders  # noqa: E402

# CREATE INDEX ... WITH (parallel_workers=N) —— openGauss 的并行建索引开关。
_PARALLEL_BUILD_RE = re.compile(r"(?i)\bparallel_workers\s*=")

# **本脚本一条 SQL 都迁不到 scripts/registry/。** 它做的三件事都以用户临时
# 给的 SQL 为输入：
#
#   EXPLAIN --original / --rewrite   任意 SQL，每次都不同，注册不进白名单
#   md5 行哈希等价性校验              把用户 SQL 包进子查询，同上
#   hypopg 组合验证                   还额外要求跨语句的持久会话
#
# 所以它整体走 access.session_for()：一条原始会话，拿不到就在入口失败。
# 不注册「EXPLAIN {{user_sql}}」这类直通脚本 —— 那等于给白名单开一个通用
# 入口，任何 SQL 都能从这一条进去。是否开这个口子属于客户的安全策略决策。
_SESSION_REQUIRED = (
    "verify 的输入是用户给的任意 SQL（--original / --rewrite），"
    "要对它们做 EXPLAIN、行哈希比对、hypopg 组合验证 —— "
    "三件事都无法表达成预注册脚本，其中 hypopg 还要求跨语句的持久会话。\n"
    "**该能力在白名单模型下不可用。**\n"
    "可选做法：为这类诊断保留一条直连通道（driver: psycopg2），"
    "或在客户环境不提供改写验证能力 —— 但那样就不能再向用户呈现"
    "任何「已验证」的改写结论，未经验证的建议一律归入「未验证想法」。"
)

MIN_SPEEDUP = 1.3
_EQUIV_TIMEOUT_MS = 30000
_EQUIV_SAMPLE = 1000


@dataclass(frozen=True)
class RewriteVerdict:
    orig_cost: float
    new_cost: float
    speedup: float
    accepted: bool = False
    equivalent: Optional[bool] = None
    equiv_note: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CombinedVerdict:
    orig_cost: float
    combo_cost: float
    speedup: float
    accepted: bool = False
    equivalent: Optional[bool] = None
    equiv_note: str = ""
    applied_indexes: list = field(default_factory=list)
    failed_indexes: list = field(default_factory=list)
    reason: str = ""


def _trim_trailing_semicolon(sql: str) -> str:
    return sql.rstrip(" \t\n\r").rstrip(";")


def _row_hash(db, sql_text: str, limit: int) -> Optional[str]:
    inner = _trim_trailing_semicolon(sql_text)
    stmt = ("SELECT md5(string_agg(row_text, '|' ORDER BY row_text)) FROM "
            "(SELECT (sub.*)::text AS row_text FROM (" + inner +
            f"\n) AS sub ORDER BY row_text LIMIT {limit}) t")
    _, rows = db.query(stmt)
    if not rows:
        return None
    val = rows[0][0]
    return None if val is None else str(val)


def _verify_equivalence(db, orig_sql: str, rewrite_sql: str, limit: int) -> tuple[bool, str]:
    orig_hash = _row_hash(db, orig_sql, limit)
    new_hash = _row_hash(db, rewrite_sql, limit)
    orig_empty, new_empty = orig_hash is None, new_hash is None
    if orig_empty and new_empty:
        return True, "两侧结果集均为空"
    if orig_empty != new_empty:
        return False, "行哈希不一致"
    if orig_hash == new_hash:
        return True, f"行哈希一致(采样{limit}行)"
    return False, "行哈希不一致"


def _precheck(orig_sql: str, rewrite_sql: str, what: str) -> None:
    if count_placeholders(orig_sql) > 0:
        raise ValueError(f"verify {what}: origSQL contains placeholder(s) — substitute before calling")
    if count_placeholders(rewrite_sql) > 0:
        raise ValueError(f"verify {what}: rewriteSQL contains placeholder(s) — substitute before calling")
    if is_dml(orig_sql) != is_dml(rewrite_sql):
        raise ValueError(f"verify {what}: origSQL 和 rewriteSQL 语句类型不同（一个是 DML）")
    v = systables.system_verdict(orig_sql)
    if v.is_system:
        raise ValueError(
            f"verify {what}: 目标 SQL 只访问系统对象（{', '.join(v.system_objects)}）——"
            f"按策略不对系统表/系统视图做调优，无需验证改写")


def _check_index_ddl(ddls: list[str]) -> None:
    """并行建索引对生产冲击大,策略上禁止——在验证入口就拦掉,别让它进报告。"""
    bad = [d for d in ddls if _PARALLEL_BUILD_RE.search(d)]
    if bad:
        raise ValueError(
            "索引 DDL 含并行构建选项，按生产温和性策略不予验证/推荐"
            "（并行建索引对生产 CPU/IO 冲击大；请去掉该选项，建索引默认串行 + 低峰执行）:\n"
            + "\n".join(f"  {d}" for d in bad))


def verify_rewrite(db, orig_sql: str, rewrite_sql: str,
                   min_speedup: float = MIN_SPEEDUP, check_equiv: bool = True) -> RewriteVerdict:
    min_speedup = max(min_speedup, 1.0)
    _precheck(orig_sql, rewrite_sql, "rewrite")
    db.execute(f"SET statement_timeout = {_EQUIV_TIMEOUT_MS}")

    orig_cost = explain_cost(db, orig_sql)
    if orig_cost <= 0:
        raise ValueError(f"verify rewrite: orig cost {orig_cost} <= 0, cannot compare")
    new_cost = explain_cost(db, rewrite_sql)
    if new_cost <= 0:
        raise ValueError(f"verify rewrite: rewrite cost {new_cost} <= 0, cannot compare")
    speedup = orig_cost / new_cost

    equivalent: Optional[bool] = None
    equiv_note = ""
    if check_equiv:
        if is_dml(orig_sql) or is_dml(rewrite_sql):
            equiv_note = "跳过等价性校验（DML）"
        else:
            equivalent, equiv_note = _verify_equivalence(db, orig_sql, rewrite_sql, _EQUIV_SAMPLE)

    cost_ok = speedup >= min_speedup
    equiv_ok = equivalent is None or equivalent
    accepted = cost_ok and equiv_ok
    reason = ""
    if not cost_ok:
        reason = f"cost 改善不足，仅 {speedup:.2f}×（需 ≥{min_speedup:.2f}×）"
    elif equivalent is not None and not equivalent:
        reason = "结果集不等价"

    return RewriteVerdict(orig_cost, new_cost, speedup, accepted, equivalent, equiv_note, reason)


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def verify_combined(db, orig_sql: str, rewrite_sql: str, explicit_indexes: list[str],
                    auto_index: bool, min_speedup: float = MIN_SPEEDUP,
                    check_equiv: bool = True) -> CombinedVerdict:
    min_speedup = max(min_speedup, 1.0)
    _precheck(orig_sql, rewrite_sql, "combined")
    _check_index_ddl(explicit_indexes)
    db.execute(f"SET statement_timeout = {_EQUIV_TIMEOUT_MS}")

    orig_cost = explain_cost(db, orig_sql)
    if orig_cost <= 0:
        raise ValueError(f"verify combined: orig cost {orig_cost} <= 0, cannot compare")

    db.execute("SET enable_hypo_index = on")
    try:
        db.execute("SELECT hypopg_reset_index()")

        ddls: list[str] = list(explicit_indexes)
        if auto_index:
            advise = (f"SELECT schema, \"table\", \"column\", indextype "
                      f"FROM gs_index_advise('{quote_sql_literal(rewrite_sql)}')")
            _, adv_rows = db.query(advise)
            for row in adv_rows:
                schema, table, column = row[0], row[1], row[2]
                if not column:
                    continue
                ddls.append(
                    f"CREATE INDEX ON {quote_ident(schema)}.{quote_ident(table)}({quote_columns(column)})")
        ddls = _dedup(ddls)
        if not ddls:
            raise ValueError(
                f"verify combined: 无可用索引（autoIndex={auto_index} 未发现候选，"
                f"explicit={len(explicit_indexes)}）；改用 verify 单独验改写")

        applied, failed = [], []
        for ddl in ddls:
            try:
                name = db.scalar(f"SELECT indexname FROM hypopg_create_index('{quote_sql_literal(ddl)}')")
                if name is None:
                    failed.append(ddl)
                else:
                    applied.append(ddl)
            except common.DBError:
                failed.append(ddl)
        if not applied:
            raise ValueError(f"verify combined: none of the {len(ddls)} DDL(s) could be created hypothetically")

        combo_cost = explain_cost(db, rewrite_sql)
        if combo_cost <= 0:
            raise ValueError(f"verify combined: combo cost {combo_cost} <= 0, cannot compare")
    finally:
        try:
            db.execute("SELECT hypopg_reset_index()")
        except common.DBError:
            pass

    speedup = orig_cost / combo_cost
    equivalent: Optional[bool] = None
    equiv_note = ""
    if check_equiv:
        if is_dml(orig_sql) or is_dml(rewrite_sql):
            equiv_note = "跳过等价性校验（DML）"
        else:
            equivalent, equiv_note = _verify_equivalence(db, orig_sql, rewrite_sql, _EQUIV_SAMPLE)

    cost_ok = speedup >= min_speedup
    equiv_ok = equivalent is None or equivalent
    accepted = cost_ok and equiv_ok
    reason = ""
    if not cost_ok:
        reason = f"cost 改善不足，仅 {speedup:.2f}×（需 ≥{min_speedup:.2f}×）"
    elif equivalent is not None and not equivalent:
        reason = "结果集不等价"

    return CombinedVerdict(orig_cost, combo_cost, speedup, accepted, equivalent,
                           equiv_note, applied, failed, reason)


def verify_report(v: RewriteVerdict) -> str:
    sb = ["## Rewrite Verification\n",
          f"- Original cost: {v.orig_cost:.2f}",
          f"- Rewrite cost: {v.new_cost:.2f}",
          f"- Speedup: {v.speedup:.2f}×\n"]
    if v.equivalent is None:
        sb.append(f"⚠️ 未验证等价性：{v.equiv_note or '未执行等价性校验'}\n")
    elif v.equivalent:
        sb.append("✅ 结果集等价\n")
    else:
        sb.append("❌ 结果集不等价\n")
    if v.accepted:
        sb.append("✅ ACCEPTED（cost 改善达标且未发现不等价）")
    else:
        sb.append(f"❌ REJECTED：{v.reason}")
    return "\n".join(sb) + "\n"


def combined_report(cv: CombinedVerdict) -> str:
    sb = ["## Combined Verification (rewrite + indexes)\n",
          f"- Original cost: {cv.orig_cost:.2f}",
          f"- Rewrite + {len(cv.applied_indexes)} index(es) cost: {cv.combo_cost:.2f}",
          f"- Speedup: {cv.speedup:.2f}×\n"]
    if cv.equivalent is None:
        sb.append(f"⚠️ 未验证等价性：{cv.equiv_note or '未执行等价性校验'}\n")
    elif cv.equivalent:
        sb.append("✅ 结果集等价\n")
    else:
        line = "❌ 结果集不等价"
        if cv.equiv_note:
            line += f"：{cv.equiv_note}"
        sb.append(line + "\n")

    sb.append("### Applied Indexes (hypothetical)\n")
    if not cv.applied_indexes:
        sb.append("none\n")
    else:
        sb.extend(f"- `{d}`" for d in cv.applied_indexes)
        sb.append("")
    if cv.failed_indexes:
        sb.append("### Failed to create\n")
        sb.extend(f"- `{d}`" for d in cv.failed_indexes)
        sb.append("\n> 以上 DDL 可能存在语法问题，hypopg_create_index 未能创建。\n")
    if cv.accepted:
        sb.append("✅ ACCEPTED — 改写+索引组合达标，建议落地（先在真库 EXPLAIN 复核）")
    else:
        sb.append(f"❌ REJECTED：{cv.reason}")
    sb.append("\n> Costs are real EXPLAIN comparisons using hypothetical (virtual) indexes — "
              "no index was actually built. Apply the rewrite AND create these indexes together "
              "to realize the gain.")
    return "\n".join(sb) + "\n"


def _to_dict(v) -> dict:
    return {k: val for k, val in v.__dict__.items()}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="verify.py", description="Verify a SQL rewrite (cost + equivalence)")
    ap.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    ap.add_argument("--original", required=True, help="original SQL text")
    ap.add_argument("--rewrite", required=True, help="rewritten SQL text")
    ap.add_argument("--no-equiv", action="store_true", help="skip result-set equivalence check")
    ap.add_argument("--auto-index", action="store_true",
                    help="discover & combine indexes via gs_index_advise on the rewrite")
    ap.add_argument("--index", action="append", default=[],
                    help="explicit CREATE INDEX DDL to combine (repeatable)")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--timeout", type=int, default=None, help="statement timeout (s)")
    args = ap.parse_args(argv)
    cli.apply_session_arg(args)

    if not args.original.strip():
        ap.error("--original must not be empty")
    if not args.rewrite.strip():
        ap.error("--rewrite must not be empty")

    combined = args.auto_index or bool(args.index)
    try:
        # 只读会话，与迁移前的 Database.connect(name) 默认值一致
        db = access.session_for(args.conn)
    except access.SessionUnavailable as exc:
        print(f"error: {exc}\n{_SESSION_REQUIRED}", file=sys.stderr)
        return 2
    except (common.ConfigError, common.CredentialError, common.DBError,
            access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        db.set_statement_timeout(
                args.timeout if args.timeout is not None
                else access.DEFAULT_SKILL_TIMEOUT_SECONDS)
        if combined:
            cv = verify_combined(db, args.original, args.rewrite, args.index,
                                 args.auto_index, MIN_SPEEDUP, not args.no_equiv)
            out = json.dumps(_to_dict(cv), ensure_ascii=False, indent=2) if args.format == "json" else combined_report(cv)
        else:
            v = verify_rewrite(db, args.original, args.rewrite, MIN_SPEEDUP, not args.no_equiv)
            out = json.dumps(_to_dict(v), ensure_ascii=False, indent=2) if args.format == "json" else verify_report(v)
        # Markdown reports already end with a newline; match gdaa's fmt.Print exactly.
        print(out, end="" if args.format == "markdown" else "\n")
        return 0
    except (ValueError, common.DBError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
