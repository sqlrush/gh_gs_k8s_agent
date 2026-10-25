#!/usr/bin/env python3
"""proctune entry — stored-procedure analysis & cursor SELECT tuning.

Port of internal/probe/proc.go + internal/cli/proc.go. Two subcommands:

  collect <schema.proc>       advisory evidence: source + structural findings
                              + embedded statements + runtime note + GUC
  tune-cursor <schema.proc>   per read-only cursor: substituted SELECT evidence
                              + hypopg index verification; ineligible cursors
                              are listed under Skipped Cursors

The procedure is never executed; the session is read-only.

取数分两条口子：

  collect       全部是固定查询（过程定义 + 关键 GUC），走 access.for_conn()
                的统一入口，中间件与直连两条路径都跑得通
  tune-cursor   还需要一条**原始会话**：EXPLAIN 变量替换后的游标 SELECT
                （每个游标都不同，注册不进白名单）与 hypopg 虚拟索引验证
                （建索引与 EXPLAIN 必须同会话）。会话由 access.session_for()
                索取，拿不到就 exit 2 —— 不降级成没有计划的证据包

注：--timeout 只作用在 tune-cursor 的那条会话上；collect 走 runner，
超时由统一入口自己管（DirectRunner 默认 60s）。

Usage:
    proctune.py collect      -c <conn> <schema.proc>
    proctune.py tune-cursor  -c <conn> <schema.proc> [--cursor NAME ...] [--bind var=value ...]
"""
from __future__ import annotations

import argparse
import dataclasses
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
from common import kbsection  # noqa: E402
from common import proc_locate  # noqa: E402
from common import procname  # noqa: E402
from common.grmp.hints import ensure_hint  # noqa: E402
from common.sqlrefs import extract_table_refs  # noqa: E402
import procanalyze as pa  # noqa: E402
import render  # noqa: E402
import sysprocs  # noqa: E402
from evidence import Evidence, collect, collect_gucs, evidence_report  # noqa: E402
from hypoindex import MIN_SPEEDUP, verify_indexes  # noqa: E402

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/proctune/ —— 两条路径共用同一份定义
PROC_DEF_SCRIPT = "proctune.proc_def"
# 过程找不到时代为排查用的三条脚本(common/proc_locate.py 按前缀拼名;全名写在这里是给交付闸门看的)。
LOCATE_SCRIPTS = ("proctune.locate_context", "proctune.locate_schema", "proctune.locate_search")

# 单个游标取证失败时降级成 Skipped Cursor 的错误集合。
# access.QueryError 归一了两条路径的取数失败（中间件 GrmpError / 直连
# DBError）；会话那条口子不经过 runner，报的还是原始的 common.DBError。
# ColumnError / ParamError / SessionUnavailable **刻意不在此列** ——
# 那三类分别是脚本定义缺陷、调用方传参错误、能力缺口，被降级逻辑接住
# 就等于永远发现不了。
_EVIDENCE_ERRORS = (access.QueryError, common.DBError, ValueError)

_SESSION_REQUIRED = (
    "proctune tune-cursor 的两项核心能力都要求一条原始数据库会话：\n"
    "  · EXPLAIN 变量替换后的游标 SELECT —— 白名单按逻辑脚本名放行预注册的 SQL，"
    "而每个游标的 SELECT 都不同，无法预注册；\n"
    "  · hypopg 虚拟索引验证 —— 建虚拟索引与 EXPLAIN 必须落在同一会话，"
    "跨调用会**不报错地**得出「加索引没用」的错误结论。\n"
    "**该能力在白名单模型下不可用。**\n"
    "已迁到白名单的部分（过程定义、表/索引/列统计/GUC）本身能走中间件，"
    "但缺了执行计划与索引验证的证据包不足以支撑调优结论，所以整条子命令在此停止，"
    "不做半份输出。\n"
    "可选做法：为这类诊断保留一条直连通道（driver: psycopg2），"
    "或在客户环境不提供本能力；只做结构诊断可改用 "
    "`proctune.py collect`（走中间件）或 gaussdb-procinfo。"
)


_RUNTIME_NOTE = ("运行时归因（embedded SQL 的真实 calls/avg/total）需实例开启 "
                 "track_stmt_stat_level 捕获嵌套语句；当前按纯静态分析。见 references/proc-setup.md。")


@dataclass(frozen=True)
class EmbeddedStmt:
    line: int
    kind: str
    sql: str


@dataclass(frozen=True)
class ProcEvidence:
    proc: pa.ProcDef
    structure: list
    embedded: list
    runtime_note: str
    gucs: list


@dataclass(frozen=True)
class CursorEvidence:
    name: str
    kind: str
    orig_sql: str
    var_subs: list
    evidence: Evidence
    verified_indexes: list = field(default_factory=list)
    index_verify_note: str = ""


@dataclass(frozen=True)
class CursorTuneResult:
    proc: pa.ProcDef
    cursors: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def fetch_proc_def(runner, qualified: str) -> pa.ProcDef:
    """经统一入口取数。名字支持 schema.package.proc(包内过程);同名过程不止一个时拒绝,不猜
    (common/procname.py)。走中间件还是直连由连接的 driver 决定，这里不感知。

    **系统自带的过程在这里就挡住**(2026-09-21 策略):判定用的是查回来的 `nspname`,
    不是用户输入的那串 —— 用户可能不带 schema,由 search_path 解析到系统 schema 上去。
    挡在这里,collect 与 tune-cursor 两条路各一次调用,都覆盖到。
    """
    r = procname.lookup(runner, PROC_DEF_SCRIPT, qualified)
    v = sysprocs.verdict(r["nspname"], r["proname"], str(r.get("package", "") or ""))
    if v.is_system:
        raise sysprocs.SystemProcSkipped(v)
    # 协议把 NULL 渲染成空串，原来那句 `x if x is not None else ""` 的效果由入口保证，这里不必再兜。
    d = pa.analyze(r["nspname"], r["proname"], r["lanname"], r["prosrc"], r["args"])
    return dataclasses.replace(d, package=str(r.get("package", "") or ""))


def proc_collect(runner, qualified: str) -> ProcEvidence:
    proc = fetch_proc_def(runner, qualified)
    embedded = [EmbeddedStmt(line=c.line, kind=f"cursor:{c.name}", sql=c.select_sql)
                for c in pa.extract_cursors(proc.body)]
    return ProcEvidence(
        proc=proc,
        structure=pa.scan_structure(proc.body),
        embedded=embedded,
        runtime_note=_RUNTIME_NOTE,
        gucs=collect_gucs(runner),
    )


_NO_HYPOPG_BODY = (
    "**索引建议未经验证。** 本次连接不提供跨语句的持久会话，而 hypopg 虚拟索引"
    "必须与随后的 EXPLAIN 落在同一条连接里 —— 跨调用会不报错地得出"
    "「加这个索引没用」的错误结论。\n"
    "下面的索引建议来自表/列统计与执行计划的推断，**加索引前请人工验证**。"
)

# 收尾那句按访问路径分叉，理由同 sqltune：白名单部署里没有可切换的直连
# 通道，劝客户「改用 driver: psycopg2」不是建议，是把人往绕过白名单的方向引。
_HYPOPG_HINT_DIRECT = "需要验证背书请改用 driver: psycopg2 的连接重跑。"
_HYPOPG_HINT_WHITELIST = (
    "本次走的是白名单访问路径（只执行预注册脚本、每次调用独立连接），"
    "该能力在这套部署里不提供 —— 没有可切换的选项，以人工验证为准。"
)

# 兼容旧名：降级标注不能丢，tests/test_degrade_contract_units.py 钉着它。
_NO_HYPOPG_NOTE = _NO_HYPOPG_BODY + _HYPOPG_HINT_DIRECT


def no_hypopg_note(runner) -> str:
    """按访问路径给出降级标注。runner 就是「这条路是什么」的载体。"""
    if getattr(runner, "whitelist_only", False):
        return _NO_HYPOPG_BODY + _HYPOPG_HINT_WHITELIST
    return _NO_HYPOPG_BODY + _HYPOPG_HINT_DIRECT


def tune_cursors(runner, db, qualified: str,
                 only: list[str], binds: dict) -> CursorTuneResult:
    """runner 取固定查询；db 是原始会话，EXPLAIN 与 hypopg 两段都要用它。

    hypopg 是「建虚拟索引 → 在**同一会话**里 EXPLAIN」，两步必须落在一条
    连接上。统一入口的两条路径都不提供跨调用的持久会话（中间件每次调用
    独立连接，DirectRunner 每次 run() 也开关连接），所以会话由 main()
    显式向 access.session_for() 索取，拿不到就整条命令停下 —— 不降级、
    不产出没有计划的证据包。
    """
    proc = fetch_proc_def(runner, qualified)
    only_set = {c.lower() for c in only}
    cursors: list[CursorEvidence] = []
    skipped: list[pa.CursorDecl] = []

    # Record/composite variables (e.g. an outer cursor's `rec_N record`). A
    # nested cursor SELECT that references `recvar.field` depends on the enclosing
    # loop and cannot be EXPLAINed standalone — skip it cleanly with a clear
    # reason instead of letting substitution mangle it into a syntax error.
    record_vars = pa.record_var_names(proc.vars)

    for cur in pa.extract_cursors(proc.body):
        if only_set and cur.name.lower() not in only_set:
            continue
        if cur.eligible:
            rv = pa.references_record_var(cur.select_sql, record_vars)
            if rv:
                cur.eligible = False
                cur.skip_reason = (
                    f"引用外层游标记录变量 {rv}.*（依赖循环上下文，非独立可执行）")
        if not cur.eligible:
            skipped.append(cur)
            continue
        sub = pa.substitute_vars(cur.select_sql, proc.vars, binds)
        try:
            ev = collect(runner, db, sub.sql, False, schema=proc.schema)
        except _EVIDENCE_ERRORS as exc:
            cur.eligible = False
            cur.skip_reason = "证据采集失败：" + str(exc)
            skipped.append(cur)
            continue

        verified, note = [], ""
        if db is None:
            # 没有原始会话 —— hypopg 虚拟索引必须与随后的 EXPLAIN 同处一条连接，
            # 跨调用会**不报错地**得出「加这个索引没用」的错误结论。
            note = no_hypopg_note(runner)
        else:
            try:
                verified = verify_indexes(db, sub.sql, MIN_SPEEDUP)
            except Exception as exc:
                note = ("索引验证不可用（hypopg/gs_index_advise 未启用或不支持）："
                        + str(exc))

        cursors.append(CursorEvidence(
            name=cur.name, kind=cur.kind, orig_sql=cur.select_sql,
            var_subs=sub.subs, evidence=ev,
            verified_indexes=verified, index_verify_note=note))

    return CursorTuneResult(proc=proc, cursors=cursors, skipped=skipped)


# --- 客户知识库检索项 ----------------------------------------------------------

# 结构发现的中文叫法(与 SKILL.md 第 7 步同一套说法):检索文本用中文,才对得上客户案例里的写法。
_STRUCT_LABELS = {
    "loop_sql": "循环里跑 SQL",
    "per_row_dml": "循环内逐行 DML",
    "exception_in_loop": "循环内 EXCEPTION 块",
    "dynamic_sql": "动态 SQL(EXECUTE)",
}
_KB_TABLES_MAX = 8          # 检索文本里最多带几张表:够对上案例里的对象名,又不把查询词稀释掉


def _proc_name(p: pa.ProcDef) -> str:
    return procname.qualified(p.schema, getattr(p, "package", ""), p.name)


def _proc_tables(p: pa.ProcDef) -> list:
    """过程体里引用的表。去掉与过程变量同名的 —— `SELECT … INTO v_cnt` 会被当成表名摘出来。"""
    local = {str(v).lower() for v in (p.vars or {})}
    return [t for t in extract_table_refs(p.body) if t.rsplit(".", 1)[-1] not in local][:_KB_TABLES_MAX]


def kb_items_collect(pe: ProcEvidence) -> list:
    """collect 给知识库检索的「发现」:过程本身(名字 + 涉及的表)一项 + 每类结构发现各一项(纯函数)。

    同一类结构发现在过程里出现多处只查一次(检索文本一样,参照也一样),证据带第一处的源码行。
    """
    name = _proc_name(pe.proc)
    tables = " ".join(_proc_tables(pe.proc))
    items = [kbsection.Item(code="PROC", dimension="proctune", metric="存储过程", value=name,
                            evidence=f"{name} {tables}".strip())]
    by_kind: dict = {}
    for f in pe.structure:
        by_kind.setdefault(f.kind, []).append(f)
    for kind, fs in by_kind.items():
        label = _STRUCT_LABELS.get(kind, kind)
        items.append(kbsection.Item(code=f"PROC_{kind.upper()}", dimension="proctune", metric=label,
                                    value=f"{len(fs)} 处", evidence=f"{label} {fs[0].snippet} {name}".strip()))
    return items


def kb_items_tune(tr: CursorTuneResult) -> list:
    """tune-cursor 给知识库检索的「发现」:游标计划里的每类确定性发现一项(各游标合并),
    证据带涉及的表 —— 与 sqltune 的 PLAN_* 同一口径(纯函数)。过程本身那一项 collect 已经查过。"""
    name = _proc_name(tr.proc)
    by_kind: dict = {}
    for ce in tr.cursors:
        tables = ", ".join(f"{t.schema}.{t.name}" if t.schema else t.name for t in ce.evidence.tables)
        for f in ce.evidence.findings:
            by_kind.setdefault(f.kind, []).append((ce.name, f, tables))
    items = []
    for kind, hits in by_kind.items():
        cursors = "、".join(dict.fromkeys(c for c, _f, _t in hits))
        _c, f, tables = hits[0]
        items.append(kbsection.Item(code=f"PLAN_{kind.upper()}", dimension="plan", metric=kind,
                                    value=f"游标 {cursors}", evidence=f"{f.detail} {tables} {name}".strip(),
                                    severity=f.severity))
    return items


# --- reports (port of cli/proc.go) -------------------------------------------

def _arg_string(args: list) -> str:
    return ", ".join(f"{a.name} {a.type}" for a in args)


def proc_collect_report(pe: ProcEvidence) -> str:
    d = pe.proc
    b = ["# Proc Collect\n\n## Procedure Source\n",
         f"- Name: `{procname.qualified(d.schema, getattr(d, 'package', ''), d.name)}`"
         + ("（包内过程）" if getattr(d, "package", "") else ""),
         f"- Language: `{d.lang}`",
         "- Args: `" + _arg_string(d.args) + "`",
         f"- Rollback-safe: {str(d.rollback_safe).lower()}\n"]
    out = "\n".join(b) + "\n" + render.code_block("", d.body)

    out += "\n## Structural Findings\n\n"
    if not pe.structure:
        out += "None.\n"
    else:
        rows = [[f"[H{i + 1}]", str(f.line), f.kind, render.truncate(f.snippet, 80)]
                for i, f in enumerate(pe.structure)]
        out += render.table(["Marker", "Line", "Kind", "Snippet"], rows)

    out += "\n## Embedded Statements\n\n"
    if not pe.embedded:
        out += "None statically extracted.\n"
    else:
        rows = [[str(e.line), e.kind, render.truncate(e.sql, 100)] for e in pe.embedded]
        out += render.table(["Line", "Kind", "SQL"], rows)

    out += "\n## Runtime Attribution\n\n> " + pe.runtime_note + "\n"

    out += "\n## Key Parameters (GUC)\n\n"
    out += render.table(["NAME", "SETTING", "UNIT"],
                        [[g.name, g.setting, g.unit] for g in pe.gucs])
    # 客户知识库对这个过程与它的结构发现怎么说;--format json 不带(形状不变)
    out += "\n" + kbsection.markdown(kb_items_collect(pe))
    return out


def _verified_index_block(indexes: list, note: str) -> str:
    out = "## Verified Index Candidates\n\n"
    if note:
        return out + note + "\n"
    if not indexes:
        return out + ("No index candidate passed verification (gs_index_advise found none, "
                      "or none reduced cost ≥1.3×).\n")
    rows = []
    for i, c in enumerate(indexes):
        rows.append([str(i + 1), c.ddl, f"{c.orig_cost:.2f}", f"{c.hypo_cost:.2f}",
                     f"{c.speedup:.2f}×", "✓" if c.used else "—"])
    out += render.table(["#", "Index DDL", "Orig Cost", "Hypo Cost", "Speedup", "Used"], rows)
    out += ("\n> These indexes were verified with hypothetical (virtual) indexes — "
            "costs are real EXPLAIN comparisons, no index was actually built.\n")
    return out


def cursor_tune_report(tr: CursorTuneResult) -> str:
    p = tr.proc
    out = f"# Cursor Tune  (proc: `{procname.qualified(p.schema, getattr(p, 'package', ''), p.name)}`, lang: {p.lang})\n"
    if not tr.cursors:
        out += "\n没有可处理的只读游标 SELECT。见 `## Skipped Cursors`。\n"
    for ce in tr.cursors:
        out += f"\n## Cursor {ce.name}\n\n- Kind: `{ce.kind}`\n\n原始游标 SELECT（含变量）：\n\n"
        out += render.code_block("sql", ce.orig_sql)
        if ce.var_subs:
            out += "\n## Variable Substitution\n\n"
            rows = [[s.var, s.type, s.value, s.source] for s in ce.var_subs]
            out += render.table(["Var", "Type", "Value", "Source"], rows)
        out += "\n" + evidence_report(ce.evidence)
        out += "\n" + _verified_index_block(ce.verified_indexes, ce.index_verify_note)

    out += "\n## Skipped Cursors\n\n"
    if not tr.skipped:
        out += "None.\n"
    else:
        rows = [[s.name, s.kind, s.skip_reason] for s in tr.skipped]
        out += render.table(["Name", "Kind", "Reason"], rows)
    # 客户知识库对游标计划里的发现怎么说;--format json 不带(形状不变)
    out += "\n" + kbsection.markdown(kb_items_tune(tr))
    return out


# --- JSON serialization ------------------------------------------------------

def _evidence_dict(ev: Evidence) -> dict:
    return {
        "version": ev.version, "analyzed": ev.analyzed, "plan": ev.plan,
        "findings": [f.__dict__ for f in ev.findings],
        "tables": [t.__dict__ for t in ev.tables],
        "indexes": [i.__dict__ for i in ev.indexes],
        "columns": [c.__dict__ for c in ev.columns],
        "gucs": [g.__dict__ for g in ev.gucs],
    }


def _proc_dict(p: pa.ProcDef) -> dict:
    return {"schema": p.schema, "package": getattr(p, "package", ""), "name": p.name, "lang": p.lang,
            "args": [a.__dict__ for a in p.args], "vars": p.vars,
            "rollback_safe": p.rollback_safe}


def _collect_json(pe: ProcEvidence) -> str:
    return json.dumps({
        "proc": _proc_dict(pe.proc),
        "structure": [f.__dict__ for f in pe.structure],
        "embedded": [e.__dict__ for e in pe.embedded],
        "runtime_note": pe.runtime_note,
        "gucs": [g.__dict__ for g in pe.gucs],
    }, ensure_ascii=False, indent=2)


def _tune_json(tr: CursorTuneResult) -> str:
    return json.dumps({
        "proc": _proc_dict(tr.proc),
        "cursors": [{
            "name": ce.name, "kind": ce.kind, "orig_sql": ce.orig_sql,
            "var_subs": [v.__dict__ for v in ce.var_subs],
            "evidence": _evidence_dict(ce.evidence),
            "verified_indexes": [c.__dict__ for c in ce.verified_indexes],
            "index_verify_note": ce.index_verify_note,
        } for ce in tr.cursors],
        "skipped": [s.__dict__ for s in tr.skipped],
    }, ensure_ascii=False, indent=2)


def _parse_bind_pairs(pairs: list[str]) -> dict:
    m: dict = {}
    for p in pairs:
        k = p.find("=")
        if k < 1:
            raise ValueError(f"--bind {p!r} must be var=value")
        m[p[:k].strip().lower()] = p[k + 1:]
    return m


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="proctune.py",
                                 description="Stored-procedure analysis & cursor SELECT tuning")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("collect", help="advisory evidence for a procedure")
    pc.add_argument("proc", help="schema.proc")
    pc.add_argument("-c", "--conn", default="")
    cli.add_session_arg(pc)
    pc.add_argument("--format", choices=["markdown", "json"], default="markdown")
    pc.add_argument("--timeout", type=int, default=None)

    pt = sub.add_parser("tune-cursor", help="tune read-only cursor SELECTs")
    pt.add_argument("proc", help="schema.proc")
    pt.add_argument("-c", "--conn", default="")
    cli.add_session_arg(pt)
    pt.add_argument("--cursor", action="append", default=[],
                    help="only process the named cursor(s) (repeatable)")
    pt.add_argument("--bind", action="append", default=[],
                    help="override a cursor variable: var=value (repeatable)")
    pt.add_argument("--format", choices=["markdown", "json"], default="markdown")
    pt.add_argument("--timeout", type=int, default=None)

    args = ap.parse_args(argv)

    cli.apply_session_arg(args)

    try:
        binds = _parse_bind_pairs(args.bind) if args.cmd == "tune-cursor" else {}
    except ValueError as exc:
        ap.error(str(exc))

    # collect 全部走已注册脚本，中间件上跑得通；tune-cursor 还要一条原始会话
    # （EXPLAIN 任意游标 SELECT + hypopg），在此显式索取，拿不到就当场停。
    needs_session = args.cmd == "tune-cursor"
    db = None
    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
        # 先问再取：中间件那条路注定给不了会话，没必要先试一次再从异常里恢复。
        # gsql 仍要建连后才看得出（每条语句起独立子进程），所以 except 留着。
        if needs_session and access.may_provide_session(args.conn):
            try:
                db = access.session_for(args.conn)
            except access.SessionUnavailable:
                # 没有会话不等于什么都做不了：证据与执行计划照采，
                # 只是索引建议拿不到 hypopg 背书。降级写进报告，不隐瞒。
                db = None
    except (common.ConfigError, common.CredentialError,
            common.DBError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        if db is not None:
            db.set_statement_timeout(
                args.timeout if args.timeout is not None
                else access.DEFAULT_SKILL_TIMEOUT_SECONDS)
        if args.cmd == "collect":
            pe = proc_collect(runner, args.proc)
            out = _collect_json(pe) if args.format == "json" else proc_collect_report(pe)
        else:
            tr = tune_cursors(runner, db, args.proc, args.cursor, binds)
            out = _tune_json(tr) if args.format == "json" else cursor_tune_report(tr)
        print(out, end="" if args.format == "markdown" else "\n")
        return 0
    except sysprocs.SystemProcSkipped as exc:
        # 策略性跳过是确定性结论,不是失败——exit 0,免得现场 agent 当错误反复重试。
        # 与 sqltune 的系统表闸同一套收尾(skills/gaussdb-sqltune/scripts/systables.py)。
        if args.format == "json":
            print(json.dumps(sysprocs.skip_json(exc.verdict), ensure_ascii=False, indent=2))
        else:
            print(sysprocs.skip_report(exc.verdict), end="")
        return 0
    except procname.NotFound as exc:
        # 找不到不再只是一句 error:代为排查连的库 / 权限 / 本身在不在,给用户问题清单(stdout,模型原样转达)。
        print(proc_locate.report(runner, "proctune", exc, fmt=args.format), end="")
        return 1
    # access.QueryError 归一了两条路径的取数失败；common.DBError 仍要留着 ——
    # 会话那条口子不经过 runner，报的还是原始的 DBError。
    # ColumnError / ParamError 刻意不接：那是脚本定义缺陷，必须响亮失败。
    except (ValueError, KeyError, common.DBError, access.QueryError) as exc:
        print(f"error: {ensure_hint(str(exc))}", file=sys.stderr)   # 直连路径的 DBError 没经过 runner,这里补中文提示
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
