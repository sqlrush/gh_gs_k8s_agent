#!/usr/bin/env python3
"""sqltune entry — one-shot SQL tuning pipeline (port of probe/sqltune.go +
cli/sqltune.go).

  1. Fetch normalized SQL by unique_sql_id (or read from --sql-stdin)
  2. Auto-substitute placeholders with synthetic values (override with --bind)
  3. Collect the full evidence bundle (plan + schema + GUCs + findings)
  4. Hard-verify index candidates via hypopg (best-effort; non-fatal)

Usage:
    sqltune.py -c <conn> <unique_sql_id> [--bind V ...] [--analyze]
    sqltune.py -c <conn> --sql-stdin <<'SQL'
    SELECT ...
    SQL
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass, field, replace
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

import coltypes  # noqa: E402
import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
from common import reports  # noqa: E402
from common.grmp.hints import ensure_hint  # noqa: E402
from common import kernel_funcs as kf  # noqa: E402
from common import search_path as sp  # noqa: E402
from common import schema_infer  # noqa: E402
from common.grmp import statement as stmt_mod  # noqa: E402
from common.grmp.statement import (  # noqa: E402
    ExplainNotAllowed,
    ensure_explainable,
)
import render  # noqa: E402
import systables  # noqa: E402
from evidence import TABLES_SCRIPT  # noqa: E402  —— 贴文本没带 --schema 时按表名查目录推断 schema
from evidence import (  # noqa: E402
    Evidence,
    collect,
    evidence_report,
    explain_json,
    explain_json_via_script,
)
from hypoindex import MIN_SPEEDUP, IndexCandidate, verify_indexes  # noqa: E402
import placeholder  # noqa: E402
import planrules  # noqa: E402
from placeholder import SubstituteResult, substitute  # noqa: E402
from sqlfetch import sql_fetch  # noqa: E402

# 代价推演。hypopg 走不通时它是唯一的定量证据来源 —— 见 _derivation_report。
import calibrate  # noqa: E402
import catalog  # noqa: E402
import costconst  # noqa: E402
import derivation  # noqa: E402
import plantree  # noqa: E402
import resolve  # noqa: E402
import whatif  # noqa: E402

# 本 skill 的取数分两条口子（见 evidence.py 模块头）：
#
#   runner   固定查询 —— 版本 / 表 / 索引 / 列统计 / GUC / 按 id 取 SQL 原文。
#            已迁到 scripts/registry/sqltune/，走中间件还是直连由 driver 决定。
#   session  原始会话 —— 两件事白名单模型撑不住：
#              1. EXPLAIN 用户临时给的任意 SQL：每次都不同，注册不进去
#              2. hypopg 虚拟索引验证：建虚拟索引与 EXPLAIN 必须在同一会话里
#
# 第 2 条尤其危险：中间件每次调用独立连接、DirectRunner 每次 run() 也开关连接，
# 硬走 runner 不会报错，只会让第二次调用看到没有虚拟索引的原计划，
# 从而得出「加这个索引没用」的**错误结论**。所以宁可在入口失败。
#
# 不为任意 SQL 注册「EXPLAIN {{user_sql}}」这类直通脚本：那等于给白名单开一个
# 通用入口，任何 SQL 都能从这一条进去。要不要开属于客户的安全策略决策，
# 不是交付方能替客户定的技术选择。同 gaussdb-explain 的处理。
_SESSION_REQUIRED = (
    "sqltune 的两项核心能力都要求一条原始数据库会话：\n"
    "  · EXPLAIN 用户给的任意 SQL —— 白名单按逻辑脚本名放行预注册的 SQL，"
    "而这里的 SQL 每次都不同，无法预注册；\n"
    "  · hypopg 虚拟索引验证 —— 建虚拟索引与 EXPLAIN 必须落在同一会话，"
    "跨调用会**不报错地**得出「加索引没用」的错误结论。\n"
    "**该能力在白名单模型下不可用。**\n"
    "已迁到白名单的部分（按 id 取 SQL 原文、表/索引/列统计/GUC）本身能走中间件，"
    "但缺了执行计划的证据包不足以支撑调优结论，所以整条命令在此停止，不做半份输出。\n"
    "可选做法：为这类诊断保留一条直连通道（driver: psycopg2），"
    "或在客户环境不提供本 skill；只看 SQL 原文/慢 SQL 清单可改用 "
    "gaussdb-sqlfetch / gaussdb-topsql / gaussdb-slowsql / gaussdb-health。"
)


@dataclass(frozen=True)
class TuneResult:
    original_sql: str
    substitution: SubstituteResult
    evidence: Evidence
    sql_id: str = ""
    source: str = ""
    schema: str = ""
    schema_source: str = ""   # statement_history(记录值)/ user_name(推测)/ --schema(用户指定)
    verified_indexes: list = field(default_factory=list)
    index_verify_note: str = ""
    derivation_report: str = ""
    # 运行态计划(GaussDB 私有 gs_get_explain):按 sql_id 找到正在执行的会话时才有;
    # 没有时 runtime_note 说明原因(openGauss 为空串——它本来就没有这个函数)。
    runtime_plan: str = ""
    runtime_plan_pid: int = 0
    runtime_note: str = ""


# 运行态计划依赖的白名单脚本(走中间件必须先注册;交付闸按脚本名全串在 skills/ 里检索,故写全):
#   explain.kernel_funcs / explain.active_pid / explain.runtime_plan
def _runtime_plan_for(runner, sql_id) -> tuple:
    """(plan, pid, note)。尽力而为:任何失败都只落到 note,不影响调优主流程。

    gs_get_explain 只能看**正在执行**的语句,所以先按 unique_sql_id 在 pg_stat_activity 找活跃会话;
    找不到是常态(SQL 早跑完了),说明一句即可。函数在但返回为空按文档前提说明(track_activities /
    plan_collect_thresh)。openGauss 没有这个函数,不说「该有而没有」。
    """
    probe = kf.probe(runner)
    if not probe.has_explain:
        return "", 0, kf.missing_note(probe)
    try:
        sess = kf.active_session_for_sql(runner, sql_id)
    except (access.QueryError, common.DBError, ValueError) as exc:
        return "", 0, f"定位正在执行该 SQL 的会话失败:{exc}"
    if sess is None:
        return "", 0, ("该 SQL 当前没有正在执行的会话,未取运行态计划——gs_get_explain 只能看正在执行的语句;"
                       "上面的 EXPLAIN 估算计划仍然有效。")
    try:
        return kf.runtime_plan(runner, sess.pid, probe.explain_args), sess.pid, ""
    except kf.NoRuntimePlan as exc:
        return "", sess.pid, str(exc)
    except (access.QueryError, common.DBError) as exc:
        return "", sess.pid, f"gs_get_explain 调用失败:{exc}"


_NO_HYPOPG_BODY = (
    "**hypopg 虚拟索引验证在本次连接下不可用。** 虚拟索引必须与随后的 EXPLAIN "
    "落在同一条连接里，而本次访问路径不提供跨语句的持久会话 —— 跨调用会不报错"
    "地得出「加这个索引没用」的错误结论。\n"
    "**替代证据见下面的「代价推演」一节**：它用规划器自己的公式复算当前计划的"
    "每一个节点并与 EXPLAIN 实测逐节点比对，通过了才说明模型在这个实例上可信。\n"
    "注意两者的区别，别混为一谈：hypopg 是**实测**加了索引之后的计划；"
    "代价推演校准的是**基线**，即「当前这个代价是怎么算出来的」。\n"
    "所以下面的索引建议依然**未经验证** —— 推演没有回答「加了这条索引会变成"
    "多少」，加索引前请**人工验证**。"
)

# 收尾那句按访问路径分叉。同一件事，两边的下一步不一样：
#   本机直连  下一步是换一条 driver: psycopg2 的连接重跑，确实能拿到背书
#   白名单    压根没有直连通道 —— 那句话在客户环境不是建议，是噪音，
#             还会把人往「绕过白名单」的方向引。这边只能是人工验证。
_HYPOPG_HINT_DIRECT = "需要 hypopg 背书请改用 driver: psycopg2 的连接重跑。"
_HYPOPG_HINT_WHITELIST = (
    "本次走的是白名单访问路径（只执行预注册脚本、每次调用独立连接），"
    "该能力在这套部署里不提供 —— 没有可切换的选项，索引建议以人工验证为准。"
)

# 兼容旧名：降级标注不能丢，tests/test_degrade_contract_units.py 钉着它。
_NO_HYPOPG_NOTE = _NO_HYPOPG_BODY + _HYPOPG_HINT_DIRECT


def no_hypopg_note(runner) -> str:
    """按访问路径给出降级标注。runner 就是「这条路是什么」的载体。"""
    if getattr(runner, "whitelist_only", False):
        return _NO_HYPOPG_BODY + _HYPOPG_HINT_WHITELIST
    return _NO_HYPOPG_BODY + _HYPOPG_HINT_DIRECT


def _derivation_report(runner, db, sql_text: str, ev) -> str:
    """跑一遍代价推演，返回报告正文。

    **任何一步失败都返回一段说明，不抛异常。** 推演是附加证据，拿不到不该让
    整条调优命令失败。但失败原因必须落到报告里 —— 静默省略这一节，读的人会
    以为「没有推演」而不是「推演没做成」，而这两件事对结论可信度的影响不同。
    """
    header = "\n## 代价推演\n\n"
    try:
        cost = costconst.from_gucs(ev.gucs)
    except costconst.MissingConstant as exc:
        return header + "未进行：代价常数不全 —— %s\n" % exc
    try:
        cat = catalog.from_evidence(ev)
    except catalog.CatalogError as exc:
        return header + "未进行：%s\n" % exc
    try:
        raw = (explain_json(db, sql_text) if db is not None
               else explain_json_via_script(runner, sql_text,
                                            schema=getattr(ev, "search_path", "") or getattr(ev, "schema", "")))
        root = plantree.parse(raw)
    except Exception as exc:            # 取计划失败的形态太多，统一兜住
        return header + "未进行：拿不到 JSON 格式的执行计划 —— %s\n" % exc

    try:
        cal = calibrate.calibrate_best_variant(
            root, lambda v: resolve.make_resolver(cat, cost, v))
        verdicts = cat.freshness_report([t.name for t in ev.tables])
        proposals = _index_proposals(root, cat, cost, cal, verdicts)
        return "\n" + derivation.render_report(cal, cost, verdicts,
                                               proposals=proposals)
    except Exception as exc:            # noqa: BLE001 —— 推演是附加证据,任何一步失败都只落到报告
        # og5 实测:没 ANALYZE 过的表,计划里一个 Index Scan 就让 catalog.column 抛 CatalogError
        # (pg_stats 里没那一列),原先这里没兜住,整条 sqltune 命令 Traceback。
        return header + "未进行：推演中途失败 —— %s\n" % exc


def _index_proposals(root, cat, cost, cal, verdicts) -> list:
    """算候选索引的假设代价。

    **门没过就一条都不算。** 不是算了再标成不可信 —— 数字一旦印出来就会被读，
    旁边那行免责声明拦不住。这个判断在这里做一次，报告里再做一次，两处都做
    是有意的：将来有人直接调 render_report 传进 proposals，也拦得住。
    """
    ok, _ = derivation.may_emit_advice(cal, verdicts)
    if not ok:
        return []

    resolver = resolve.make_resolver(cat, cost, cal.variant)
    out = []
    for cand in whatif.propose_from_plan(root, cat, cost, plantree.walk):
        stat = cand["stat"]
        if stat.avg_width <= 0 or stat.correlation is None:
            continue
        try:
            scan = whatif.hypothetical_index_scan(
                cand["table"], stat, cand["selectivity"], cost,
                cat.total_table_pages(), stat.avg_width,
                # propose_from_plan 的选择率来自该节点的 Plan Rows，是实测反推
                selectivity_from_plan=True)
            rec = whatif.recompute_with_override(root, resolver, cand["node"],
                                                 scan)
        except Exception:
            # 假设路径算不出来不该影响已经校准好的基线报告
            continue
        out.append(whatif.Proposal(
            ddl=cand["ddl"], table=cand["table"].name, column=cand["column"],
            baseline_total=root.total_cost, hypothetical_total=rec.root_total,
            scan_estimate=scan, recomputed=rec))
    return out


def _guard_sql(sql_text: str, analyze: bool) -> None:
    """没有会话时，用户 SQL 要走 EXPLAIN 模板 —— 先过注入守卫。

    DML + --analyze 在这条路上**不可用**，必须报错而不是悄悄不 analyze：
    静默降级会让用户以为拿到的是实际执行的计划，实测两者能差 2.3 倍。

    白名单路径(中间件)连不带 ANALYZE 的写语句也拦:客户中间件只受理 SELECT 的执行计划,
    UPDATE / INSERT / DELETE 递过去一律 400(客户 09-08 清单第 2 项第 3 点,要求 skill 层拦截)。
    直连原始会话那条路不经这里,EXPLAIN UPDATE 不执行语句,直连照常出计划。
    """
    ensure_explainable(sql_text, analyze=analyze)
    if not stmt_mod.is_read_only(sql_text):
        raise ExplainNotAllowed(
            "中间件路径只受理只读语句的执行计划,本次是 %s。写语句的计划中间件不受理(递过去就是 400),"
            "本 skill 不再发出。可把它的 WHERE 部分改写成 SELECT 后再调优,或改用直连(driver: psycopg2)。"
            % (stmt_mod.leading_keyword(sql_text).upper() or "非查询语句"))


# 合成值被数据库按类型拒绝后、按报错换值重取计划的轮数上限。每轮换掉同一个坏值的全部位置,
# 一条 SQL 里猜错的值很少超过两种;换过的值再被拒就如实报错,不无限试。
MAX_FALLBACK_ROUNDS = 3


def _collect_tolerant(runner, db, original_sql: str, sub: SubstituteResult, do_analyze: bool,
                      schema: str) -> tuple:
    """取证据包,返回 (证据, 最终的占位符替换结果)。

    **列类型推断不出来不该让整条调优失败**(og5 2026-10-24:S3 的 `c.grade + ?` 被猜成 'test',
    EXPLAIN 报 invalid input syntax for integer,rc=1)。数据库拒了一个合成值时,报错里写着它要的类型:
    按 coltypes.fallback_for_error 换成那个类型解析得了的值重取计划,替换表里记成 fallback、报告里说明。
    bind 值不替用户改;换值后仍失败、或报错与合成值无关,就带着点名坏值的说明原样抛。
    """
    first, rounds = sub, 0
    while True:
        try:
            return collect(runner, db, sub.sql, do_analyze, schema=schema), sub
        except Exception as exc:
            fb = (coltypes.fallback_for_error(str(exc), sub.substitutions)
                  if rounds < MAX_FALLBACK_ROUNDS else None)
            if not fb:
                err = _explain_failure(exc, sub, first)
                if err is exc:
                    raise
                raise err from exc
            rounds += 1
            note = coltypes.fallback_note(str(exc), sub.substitutions, fb)
            sub = placeholder.with_values(sub, original_sql, fb, note=note)
            if db is None:
                _guard_sql(sub.sql, do_analyze)     # 换过值的 SQL 照样要过模板守卫


def _explain_failure(exc, sub: SubstituteResult, first: SubstituteResult):
    """类型转换错时点名坏值出自哪个占位符;与占位符无关的报错原样返回。

    换过值之后坏值可能已不在当前替换表里,那就对照首轮的替换表点名(首轮才是猜错的那一处)。
    """
    enriched = coltypes.enrich_type_error(str(exc), sub.substitutions)
    if not enriched and sub is not first:
        enriched = coltypes.enrich_type_error(str(exc), first.substitutions)
    if not enriched:
        return exc
    if sub is not first:
        enriched += "\n(已按报错里的目标类型给合成值换过值重取计划,仍被拒绝。)"
    try:
        return type(exc)(enriched)
    except Exception:  # 异常类构造签名特殊时,宁可保留原报错
        return exc


def _with_root_causes(ev, original_sql: str):
    """证据包里补上根因规则的发现(planrules);一条都没有就原样返回。"""
    causes = planrules.root_causes(ev, original_sql=original_sql)
    return replace(ev, findings=list(causes) + list(ev.findings)) if causes else ev


def _tune(runner, db, *, original_sql: str, binds: list[str], do_analyze: bool,
          sql_id: str = "", source: str = "", schema: str = "") -> TuneResult:
    verdict = systables.system_verdict(original_sql)
    if verdict.is_system:
        raise systables.SystemSQLSkipped(verdict.system_objects)
    types = coltypes.infer_types(runner, original_sql, schema=schema)
    sub = substitute(original_sql, binds, types=types)
    coltypes.validate_binds(sub.substitutions, types)
    if db is None:
        # 没有会话时 SQL 要递进 EXPLAIN 模板 —— 无论它从哪来都得过守卫。
        # 按 sql_id 取的 SQL 走的是另一条入口，早先漏了这一道。
        _guard_sql(sub.sql, do_analyze)
    ev, sub = _collect_tolerant(runner, db, original_sql, sub, do_analyze, schema)
    # 根因规则(planrules)排在通用的 seq_scan / sort 警告前面:那几条只说「有全表扫」,不说为什么
    ev = _with_root_causes(ev, original_sql)

    verified: list[IndexCandidate] = []
    note = ""
    if db is None:
        # 没有原始会话 —— hypopg 的虚拟索引必须与随后的 EXPLAIN 同处一条连接，
        # 跨调用会**不报错地**得出「加这个索引没用」的错误结论。所以不做，
        # 并且把这件事写进报告：本次的索引建议没有验证背书。
        note = no_hypopg_note(runner)
    else:
        try:
            verified = verify_indexes(db, sub.sql, MIN_SPEEDUP)
        except Exception as exc:  # best-effort: degrade gracefully (non-fatal)
            note = ("索引验证不可用（OpenGauss hypopg/gs_index_advise 未启用或不支持）："
                    + str(exc))

    # 推演两条路径都跑：直连路径也要它。hypopg 只回答「加了索引之后代价多少」，
    # 回答不了「当前这个代价是怎么来的」—— 后者才是让人能复核结论的那部分。
    deriv = _derivation_report(runner, db, sub.sql, ev)

    return TuneResult(original_sql=original_sql, substitution=sub, evidence=ev,
                      sql_id=sql_id, source=source, schema=schema,
                      verified_indexes=verified, index_verify_note=note,
                      derivation_report=deriv)


def tune_by_id(runner, db, raw_id: str, binds: list[str], do_analyze: bool,
               schema_override: str = "") -> TuneResult:
    fr = sql_fetch(runner, raw_id)
    # schema 优先级:用户显式给的 > statement_history 记录值 > 按执行账号 user_name 推测
    schema = schema_override or fr.schema
    schema_source = "--schema" if schema_override else getattr(fr, "schema_source", "")
    if fr.truncated:
        raise ValueError(
            f"sql id {raw_id} 的文本被 openGauss 截断（{fr.truncated_reason}）——"
            f"track_activity_query_size 限制了留存长度，数据库里就没有完整 SQL。"
            f"无法对半截 SQL 做调优。请改用 `--sql-stdin` 传入完整 SQL 文本"
            f"（或调大 track_activity_query_size 并让该 SQL 重新执行后再按 id 取）。")
    schema, schema_source = resolve_schema(runner, fr.sql, schema, schema_source)   # 推测值让位给目录唯一匹配
    try:
        tr = _tune(runner, db, original_sql=fr.sql, binds=binds, do_analyze=do_analyze,
                   sql_id=fr.sql_id, source=fr.source, schema=schema)
    except (access.QueryError, common.DBError) as exc:
        # 切了 search_path 仍报表不存在(或中间件不支持两条语句没切成):把 schema 说出来——
        # DBA 要知道该给执行账号设哪个 search_path,这个名字就是答案。直连原始会话报的是 DBError,同样接。
        # schema 可能是 statement_history 记的搜索路径串(`fl, public`):DBA 命令按路径给、不再叠一个 public,
        # 「表名写全」按第一段给(写成 `fl, public.<表>` 没法照抄)。
        if "does not exist" in str(exc) and schema and "ALTER ROLE" not in str(exc):   # search_path 层已附过说明就不叠
            raise type(exc)(
                f"{exc}\n补充:这条 SQL 的执行 schema 是 {schema}({_SCHEMA_SOURCE_LABEL.get(schema_source, schema_source)}),"
                f"执行账号当前的 search_path 里多半没有它。可让 DBA 给执行账号在该库上设置 search_path 包含 {schema}"
                f"(ALTER ROLE <执行账号> IN DATABASE <库> SET search_path = {sp.dba_search_path(schema)}),"
                f"或把 SQL 里的表名写成 {sp.primary_schema(schema)}.<表> 后用 --sql-stdin 重跑。") from exc
        raise
    plan, pid, note = _runtime_plan_for(runner, fr.sql_id)
    return replace(tr, runtime_plan=plan, runtime_plan_pid=pid, runtime_note=note,
                   schema_source=schema_source if schema else "")


def tune_by_sql(runner, db, sql_text: str, binds: list[str], do_analyze: bool,
                schema: str = "") -> TuneResult:
    schema, source = resolve_schema(runner, sql_text, schema, "--schema" if schema else "")
    tr = _tune(runner, db, original_sql=sql_text, binds=binds, do_analyze=do_analyze, schema=schema)
    return replace(tr, schema_source=source) if schema else tr


def resolve_schema(runner, sql_text: str, schema: str, source: str) -> tuple[str, str]:
    """贴文本没带 --schema(客户 09-09 早:模型漏了参数 → 400)/ 按 id 只有按账号名推测的值时,按表名在目录里找:
    唯一匹配就用(来源记 catalog);同名表跨 schema 又定不下来就拒绝并列出候选——不猜;目录里没有就照旧。
    用户显式给的 --schema 和 statement_history 的记录值不动。"""
    if schema and source != "user_name":
        return schema, source
    inf = schema_infer.infer(runner, TABLES_SCRIPT, sql_text, guess=schema)
    if inf.ambiguous:
        raise ValueError(schema_infer.describe(inf))
    if inf.schema and not inf.via_guess:
        return inf.schema, "catalog"
    return schema, source


_SCHEMA_SOURCE_LABEL = {
    "catalog": "按表名在目录里唯一匹配推断",
    "statement_history": "statement_history 记录值",
    "user_name": "按执行账号 user_name 推测",
    "--schema": "用户指定",
}


def kb_items(tr: TuneResult) -> list:
    """给知识库检索的「发现」:整条 SQL(涉及的表)一项 + 计划里的每条确定性发现各一项(纯函数)。"""
    from common.kb.query import SimpleFinding

    ev = tr.evidence
    tables = ", ".join(f"{t.schema}.{t.name}" if t.schema else t.name for t in ev.tables)
    sql_text = (tr.original_sql or ev.sql or "").strip().replace("\n", " ")
    items = [SimpleFinding(code="SQL", dimension="sqltune", metric="涉及对象", value=tables,
                           evidence=f"{tables} {sql_text[:200]}".strip())]
    for f in ev.findings:
        items.append(SimpleFinding(code=f"PLAN_{f.kind.upper()}", dimension="plan", metric=f.kind,
                                   value="", evidence=f"{f.detail} {tables}".strip(), severity=f.severity))
    return items


def kb_section(tr: TuneResult) -> tuple:
    """「客户知识库参照」小节 + JSON;知识库任何问题都降级成「未接入(原因)」,不影响调优报告。"""
    try:
        from common.kb import query as kbquery
    except ImportError as exc:
        text = f"## 客户知识库参照\n> 知识库未接入(common/kb 未安装:{exc})\n\n"
        return text, {"status": {"attached": False, "reason": f"common/kb 未安装:{exc}"}, "items": []}
    return kbquery.section_for(kb_items(tr))


def _substitution_section(sub: SubstituteResult) -> str:
    """`## Placeholder Substitution` 小节;没有占位符时空串。"""
    if sub.placeholders <= 0:
        return ""
    # 小节名固定以 "## Placeholder Substitution" 开头(SKILL.md 按它取节),
    # 但**不能无条件自称合成值**:调用方全程用 --bind 传了真实值时,再劝
    # 一句"re-run with --bind"会让模型给真实结论硬加一条合成值免责,
    # 把已经可靠的倍数说弱。降级要说出口,没降级也别装。
    if any(s.source != "bind" for s in sub.substitutions):
        out = "## Placeholder Substitution (synthetic values)\n\n"
        out += ("> Placeholders have been replaced with synthetic values to generate "
                "an execution plan. **Plan shape is reliable; row counts and "
                "selectivity estimates are approximate.**\n")
        out += "> For precise analysis, re-run with `--bind` to supply real values.\n\n"
    else:
        out = "## Placeholder Substitution (real values from `--bind`)\n\n"
        out += ("> Every placeholder was replaced with a real value supplied via "
                "`--bind`. **Row counts and selectivity reflect these actual "
                "parameters** — the approximate-value caveat does not apply "
                "to this report.\n\n")
    rows = [[str(i + 1), s.token, s.value, s.source, render.truncate(s.context, 60)]
            for i, s in enumerate(sub.substitutions)]
    out += render.table(["#", "Token", "Value", "Source", "Context"], rows) + "\n"
    # 换过值(Source = fallback)要说出口:那几个位置的类型是数据库报错告诉我们的,不是推断出来的
    for note in getattr(sub, "notes", ()) or ():
        out += "> " + note + "\n\n"
    return out


def sqltune_report(tr: TuneResult) -> str:
    sb = ["# SQL Tune\n"]
    if tr.sql_id:
        sb.append(f"- SQL_ID: `{tr.sql_id}`")
        if tr.source:
            sb.append(f"- Source: `dbe_perf.{tr.source}`")
    if tr.schema:
        label = _SCHEMA_SOURCE_LABEL.get(getattr(tr, "schema_source", ""), "")
        sb.append(f"- Schema: `{tr.schema}`" + (f"（{label}）" if label else ""))
    ev = tr.evidence
    if getattr(ev, "search_path", ""):
        sb.append(f"- Search path: 已切到 `{ev.search_path}`,EXPLAIN 按该 schema 解析不带前缀的表名")
    elif getattr(ev, "search_path_note", ""):
        sb.append(("- Search path: 未切换,已改为补全表名取计划 —— " if "补全" in ev.search_path_note
                   else "- Search path: 未切换 —— ") + ev.search_path_note)
    if len(sb) > 1:
        sb.append("")
    out = "\n".join(sb) + "\n"
    out += _substitution_section(tr.substitution)
    out += evidence_report(tr.evidence)

    # 运行态计划紧跟估算计划:两者不一致时以运行态为准,这是 gs_get_explain 存在的全部意义。
    # getattr:别的测试用 SimpleNamespace 造 TuneResult 的替身,没有这三个字段也不能炸。
    runtime_plan = getattr(tr, "runtime_plan", "")
    runtime_note = getattr(tr, "runtime_note", "")
    if runtime_plan:
        out += (f"\n## Runtime Plan (gs_get_explain, pid={getattr(tr, 'runtime_plan_pid', 0)})\n\n"
                "> 内核返回的**运行态计划**:该会话此刻实际在走的计划,未执行该 SQL。"
                "与上面的 EXPLAIN 估算计划不同时,以运行态计划为准分析"
                "(估算计划受绑定值 / 统计信息 / 计划跳变影响)。\n\n" +
                render.code_block("", runtime_plan) + "\n")
    elif runtime_note:
        out += "\n## Runtime Plan\n\n> " + runtime_note + "\n"

    # 客户知识库对这条 SQL 与计划发现怎么说——放在结论性小节之前,处置建议以它为首选依据。
    out += "\n" + kb_section(tr)[0]

    out += "\n## Verified Index Candidates\n\n"
    if tr.index_verify_note:
        out += tr.index_verify_note + "\n"
    elif not tr.verified_indexes:
        out += ("No index candidate passed verification (gs_index_advise found none, "
                "or none reduced cost ≥1.3×).\n")
    else:
        rows = []
        for i, c in enumerate(tr.verified_indexes):
            rows.append([str(i + 1), c.ddl, f"{c.orig_cost:.2f}", f"{c.hypo_cost:.2f}",
                         f"{c.speedup:.2f}×", "✓" if c.used else "—"])
        out += render.table(["#", "Index DDL", "Orig Cost", "Hypo Cost", "Speedup", "Used"], rows)
        out += ("\n> These indexes were verified with hypothetical (virtual) indexes — "
                "costs are real EXPLAIN comparisons, no index was actually built.\n")

    if tr.derivation_report:
        out += tr.derivation_report
    return out


def archive_tune(payload: dict) -> None:
    """按 sql_id 存到实例目录(unique_sql_id 只在一个库里唯一;大盘的逐条卡按 sql_id 找)。
    --sql-stdin 那条路没有 sql_id,不存。"""
    sql_id = str(payload.get("sql_id") or "").strip()
    if sql_id:
        reports.archive("sqltune", payload, name=sql_id, instance=str(payload.get("conn") or ""))


def skip_payload(sql_id: str, conn: str, objects: list) -> dict:
    """按策略跳过(系统 SQL)时存的那份:大盘据此把这一行标成「系统 SQL · 按策略不调优」,
    不再挂一个注定被拒的「调优 →」。"""
    return {"sql_id": sql_id, "conn": conn, "skipped": "system", "system_objects": list(objects)}


def _to_jsonable(tr: TuneResult) -> dict:
    return {
        "sql_id": tr.sql_id,
        "source": tr.source,
        "schema": tr.schema,
        "original_sql": tr.original_sql,
        "substitution": {
            "sql": tr.substitution.sql,
            "placeholders": tr.substitution.placeholders,
            "substitutions": [s.__dict__ for s in tr.substitution.substitutions],
            "notes": list(getattr(tr.substitution, "notes", ()) or ()),
        },
        "evidence": {
            "version": tr.evidence.version,
            "analyzed": tr.evidence.analyzed,
            "plan": tr.evidence.plan,
            "findings": [f.__dict__ for f in tr.evidence.findings],
            "tables": [t.__dict__ for t in tr.evidence.tables],
            "indexes": [i.__dict__ for i in tr.evidence.indexes],
            "columns": [c.__dict__ for c in tr.evidence.columns],
            "gucs": [g.__dict__ for g in tr.evidence.gucs],
        },
        "verified_indexes": [c.__dict__ for c in tr.verified_indexes],
        "index_verify_note": tr.index_verify_note,
        "derivation_report": tr.derivation_report,
        "runtime_plan": getattr(tr, "runtime_plan", ""),
        "runtime_plan_pid": getattr(tr, "runtime_plan_pid", 0),
        "runtime_note": getattr(tr, "runtime_note", ""),
        "kb_refs": kb_section(tr)[1],
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="sqltune.py",
                                 description="One-shot SQL tuning evidence + hypopg index verification")
    ap.add_argument("sql_id", nargs="?", help="unique_sql_id (integer, may be negative)")
    ap.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    ap.add_argument("--sql-stdin", action="store_true", help="read SQL text from stdin")
    ap.add_argument("--bind", action="append", default=[],
                    help="bind value for placeholder (repeatable, positional order)")
    ap.add_argument("--analyze", action="store_true",
                    help="EXPLAIN ANALYZE (executes the SQL; DML wrapped in rollback)")
    ap.add_argument("--schema", default="",
                    help="SQL 原本执行时的 schema 或搜索路径(如 'fl, public'):EXPLAIN 前先切 search_path"
                         "(表名不带 schema 时必需);按 sql_id 时默认取 statement_history 记录值,这里可以覆盖")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--timeout", type=int, default=None, help="statement timeout (s)")
    args = ap.parse_args(argv)
    cli.apply_session_arg(args)
    # 与 statement_history.schema_name 同形:单个 schema 或逗号分隔的搜索路径(`fl, public`),每段单独校验
    if args.schema and not sp.valid_search_path(args.schema):
        print(f"error: --schema {args.schema!r} 不是合法的 schema 标识符或搜索路径"
              f"(每段字母或下划线开头,只含字母数字下划线$;多个 schema 用逗号分隔)",
              file=sys.stderr)
        return 2

    has_id = args.sql_id is not None
    if not has_id and not args.sql_stdin:
        ap.error("provide a <sql_id> positional arg or --sql-stdin")
    if has_id and args.sql_stdin:
        ap.error("provide either <sql_id> or --sql-stdin, not both")

    sql_text = None
    if args.sql_stdin:
        sql_text = sys.stdin.read()
        if not sql_text.strip():
            ap.error("empty SQL on stdin")

    db = None
    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
        # 先问再取。中间件这条路注定给不了会话，原先仍要先 session_for() 一次
        # 再从 SessionUnavailable 里恢复 —— 白跑一趟，且降级路径是靠 except
        # 分支拼出来的。问一句就知道走不走得通，不必拿异常当控制流。
        if access.may_provide_session(args.conn):
            try:
                # 有会话就用 —— 索引验证只有这条路
                db = access.session_for(args.conn, read_only=not args.analyze)
            except access.SessionUnavailable:
                # gsql 要建连之后才看得出没有会话（每条语句起独立子进程），
                # 所以这条 except 还得留着，只是不再兼管中间件那种情形。
                db = None
        if db is None:
            # 没有会话不等于什么都做不了：证据与执行计划照采，
            # 只是索引建议拿不到 hypopg 背书。降级的事实写进报告，不隐瞒。
            #
            # 按 sql_id 取的 SQL 此刻还没到手，守卫挪到取回之后（_tune 里）。
            # 直接给的 SQL 现在就能校验，早报错早收工。
            if not has_id:
                _guard_sql(sql_text, args.analyze)
    except (common.ConfigError, common.CredentialError, common.DBError,
            ExplainNotAllowed, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        if db is not None:
            db.set_statement_timeout(
                args.timeout if args.timeout is not None
                else access.DEFAULT_SKILL_TIMEOUT_SECONDS)
        if has_id:
            tr = tune_by_id(runner, db, args.sql_id, args.bind, args.analyze, schema_override=args.schema)
        else:
            tr = tune_by_sql(runner, db, sql_text, args.bind, args.analyze, schema=args.schema)

        if len(args.bind) > tr.substitution.placeholders:
            print(f"warning: {len(args.bind)} --bind value(s) given but only "
                  f"{tr.substitution.placeholders} placeholder(s) found; extras ignored",
                  file=sys.stderr)

        payload = _to_jsonable(tr)
        payload["conn"] = common.config.resolved_name(args.conn)   # 归实例目录、大盘显示是哪个库
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(sqltune_report(tr), end="")
        archive_tune(payload)
        return 0
    except systables.SystemSQLSkipped as exc:
        # 策略性跳过是确定性结论,不是失败——exit 0,免得现场 agent 当错误反复重试。
        if args.format == "json":
            print(json.dumps(systables.skip_json(exc.objects), ensure_ascii=False, indent=2))
        else:
            print(systables.skip_report(exc.objects), end="")
        if has_id:
            archive_tune(skip_payload(args.sql_id, common.config.resolved_name(args.conn), exc.objects))
        return 0
    # access.QueryError 归一了两条路径的取数失败（中间件 GrmpError / 直连
    # DBError），skill 只认这一个类型；common.DBError 仍要留着 —— 会话那条口子
    # 不经过 runner，报的还是原始的 DBError。
    # ColumnError / ParamError 刻意不接：那是脚本定义缺陷，必须响亮失败。
    except (ValueError, KeyError, common.DBError, access.QueryError) as exc:
        print(f"error: {ensure_hint(str(exc))}", file=sys.stderr)   # 直连路径的 DBError 没经过 runner,这里补中文提示
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
