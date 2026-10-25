#!/usr/bin/env python3
"""Verify a SQL rewrite (port of internal/probe/verify.go + cli/verify.go).

Compares planner cost of original vs rewritten SQL via EXPLAIN, and optionally
checks result-set equivalence via md5 row-hash sampling. Combined mode adds
hypothetical indexes (gs_index_advise auto-discovery and/or explicit DDLs).

Both --original and --rewrite must be fully executable SQL (no placeholders).

**本脚本走 access.session_for()，即直连的持久会话，不走已注册脚本那条口子。**
两个原因，缺一不可：

  1. 组合验证（--auto-index / --index）要「SET enable_hypo_index → 建虚拟索引
     → 在同一会话里 EXPLAIN」，离开会话就会静默给出错误结论：虚拟索引没了，
     EXPLAIN 看到原计划，于是判「加索引没用」。
  2. cost 比较用 EXPLAIN (FORMAT JSON)。那一列会被驱动解成 Python 对象，
     协议再 str() 一遍就成了 Python repr（单引号），json.loads 解不回来 ——
     即便不要求会话，这条也过不去白名单那条路径。

所以在 grmp 连接上本脚本会**明确报错**而不是降级：该能力在客户的白名单
模型下本来就不存在，假装能跑等于交付一个错误结论。

Usage:
    verify.py -c <conn> --original '<sql>' --rewrite '<sql>' [--no-equiv]
              [--auto-index] [--index 'CREATE INDEX ...' ...] [--format json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
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
from common import access  # noqa: E402
from common import cli  # noqa: E402
from cost import explain_cost, quote_columns, quote_ident, quote_sql_literal  # noqa: E402
from evidence import is_dml  # noqa: E402
from sqlfetch import count_placeholders  # noqa: E402

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
        # SessionUnavailable 是 AccessError 的子类，必须先接 —— 它要给的是
        # 「该能力在这条连接上不存在」，而不是笼统的「连不上」。
        db = access.session_for(args.conn)
    except access.SessionUnavailable as exc:
        print("error: verify 需要一条持久会话，当前连接给不了。\n%s" % exc,
              file=sys.stderr)
        return 2
    except (common.ConfigError, common.CredentialError,
            common.DBError, access.AccessError) as exc:
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
