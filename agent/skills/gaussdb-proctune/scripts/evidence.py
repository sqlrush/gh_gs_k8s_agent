"""Tuning evidence bundle (port of probe/collect.go, explain.go, schema.go,
tables.go, guc.go and analyze/risks.go).

Collect gathers everything deterministic the agent needs for root-cause
analysis in one pass: version → plan → findings → tables/indexes/stats → GUCs.

取数分两条口子，**刻意不统一**（与 gaussdb-sqltune 的同名模块同形）：

  固定查询（版本/表/索引/列统计/GUC）  走 runner —— 统一入口执行已注册脚本，
                                       走中间件还是直连由连接的 driver 决定
  EXPLAIN 游标 SELECT                  走 db —— 原始会话。变量替换后的语句
                                       每次都不同，无法预注册

后者是**安全策略的边界**，不是技术难点：把任意 SQL 塞进一条「直通脚本」
等于把白名单拆了。所以它只走 access.session_for()，拿不到就在入口失败。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import render

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# 字符串值的还原一律用共用层：bool("f") 是 True 这类坑错一次就是静默出
# 错误结论，不该有各 skill 自己的一份实现
from common.grmp.values import as_bool, as_float, as_int  # noqa: E402
from common import explain_actual as ea  # noqa: E402
from common import search_path as sp  # noqa: E402
from common import catalog_scope as cs  # noqa: E402

# SQL 已迁到 scripts/registry/proctune/ —— 两条路径共用同一份定义
DB_VERSION_SCRIPT = "proctune.db_version"
# 带 search_path 的两语句模板(common/search_path.py 按 base + "_schema" 拼名;交付闸按全串检索,故写全):
#   proctune.plan_text_schema / proctune.plan_text_analyze_schema;探测脚本 explain.multi_stmt_probe
TABLES_SCRIPT = "proctune.tables"
INDEXES_SCRIPT = "proctune.indexes"
COLUMN_STATS_SCRIPT = "proctune.column_stats"
KEY_GUCS_SCRIPT = "proctune.key_gucs"

# 协议把 NULL 渲染成空串，与真正的空串不可区分（settings.null_text = ''）。
# 数值列上的空串只可能来自 NULL。
_NULL_TEXT = ""

# --- table-name extraction:收在 common/sqlrefs.py,与 proctune 及慢 SQL 排行共用 ---
from common.sqlrefs import extract_table_refs, extract_tables  # noqa: E402,F401


# --- DML detection + EXPLAIN (port of explain.go) ----------------------------

# 判定统一走 common.grmp.statement —— 这里原先抄了一份
# `^\s*(insert|update|delete|merge)\b`，explain 和 sqltune 各抄一份，共三份。
# 那个正则的 `^\s*` 跳空白但不跳注释，`/* c */ UPDATE ...` 判成非 DML。
# 本文件的 is_dml 同时把着 explain(analyze=True) 要不要包回滚（下面 _EXPLAIN
# 那段）和 verify.py 的等价性校验要不要跳过 —— 判错一次两处一起失效。
# 复制粘贴的判定改不动：修一处，另两处照旧带着 bug 跑。
from common.grmp.statement import is_dml  # noqa: E402,F401  (verify.py 从本模块导入)


def explain_plan_via_script(runner, sql_text: str, analyze: bool, schema: str = "") -> tuple:
    """走已注册的 EXPLAIN 模板 —— 没有原始会话时用这条。返回 (计划, 实际切到的 schema, 说明)。

    游标 SELECT 的表名多半不带 schema——它们本来就在过程所在的 schema 下解析。有 schema 就走
    「SET search_path; EXPLAIN」两语句模板,中间件不支持时退回单语句模板并说明(common/search_path.py)。
    调用前必须先过 ensure_explainable()：模板是文本替换，参数位就是注入面。
    """
    script = "proctune.plan_text_analyze" if analyze else "proctune.plan_text"
    return sp.explain_plan(runner, script, sql_text, schema)


def explain_via_script(runner, sql_text: str, analyze: bool) -> str:
    """旧签名:只要计划文本、不切 search_path。"""
    return explain_plan_via_script(runner, sql_text, analyze)[0]


def explain(db, sql_text: str, analyze: bool) -> str:
    """EXPLAIN in TEXT format; analyze executes (DML wrapped in rollback).

    **db 是原始会话，不是 runner。** 这里的 SQL 是变量替换后的游标 SELECT，
    每个游标都不同，注册不进白名单 —— 见模块头。调用方负责先用
    access.session_for() 拿到会话。
    """
    stmt = (f"EXPLAIN (ANALYZE {str(analyze).lower()}, "
            f"BUFFERS {str(analyze).lower()}, FORMAT TEXT) {sql_text}")
    if analyze and is_dml(sql_text):
        _, rows = db.query_in_rollback(stmt)
    else:
        _, rows = db.query(stmt)
    return "\n".join(r[0] for r in rows)


# --- deterministic plan findings (port of analyze/risks.go) ------------------

@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str  # warn | info
    detail: str
    advice: str


def scan_plan(plan_text: str) -> list[Finding]:
    lower = plan_text.lower()
    orig_lines = plan_text.split("\n")
    out: list[Finding] = []
    for i, line in enumerate(lower.split("\n")):
        trimmed = line.strip()
        if trimmed.startswith("->"):
            trimmed = trimmed[2:].strip()
        detail = orig_lines[i].strip()
        if trimmed.startswith("seq scan"):
            out.append(Finding("seq_scan", "warn", detail,
                "Full table scan; consider an index on the Filter columns if "
                "the table is large and selectivity is high."))
        elif trimmed.startswith("sort"):
            out.append(Finding("sort", "warn", detail,
                "Explicit sort; an index matching ORDER BY may remove it. "
                "Check work_mem if the sort spills to disk."))
    if "nested loop" in lower and "seq scan" in lower:
        out.append(Finding("nestloop_seqscan", "warn",
            "Nested Loop combined with Seq Scan",
            "Inner-side full scans inside a nested loop multiply cost; "
            "consider an index on the join key."))
    if "hash join" in lower:
        out.append(Finding("hash_join", "info", "Hash Join present",
            "Usually fine for large joins; verify hash memory fits work_mem."))
    return out


# --- schema probes (port of schema.go) ---------------------------------------

@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    pages: int
    tuples: int
    kind: str
    size_mb: float


@dataclass(frozen=True)
class IndexInfo:
    table: str
    name: str
    is_unique: bool
    is_primary: bool
    definition: str


@dataclass(frozen=True)
class ColumnStat:
    table: str
    column: str
    n_distinct: float
    null_frac: float
    avg_width: int
    correlation: Optional[float]


@dataclass(frozen=True)
class GUC:
    name: str
    setting: str
    unit: str


def _quoted_list(names: list[str]) -> str:
    """['a', "b'c"] -> "'a','b''c'"。**不带外层括号** —— 括号写在脚本的
    `IN ({{names}})` 里。值和 SQL 各出一半括号是最容易踩的坑（会得到
    IN (('a','b'))），所以这里只出字面量。与 gaussdb-sqltune 同名函数同形。

    ⚠️ 这里顺带修掉了一个既有缺陷：迁移前三处调用都是
    `SQL.format(names=names)`，names 是**Python 列表**，渲染出的是
    `IN ['orders', 'customers']` —— 方括号在 SQL 里是语法错误。
    模块里原本就摆着为此写的 _quote_literals()，但从来没人调用它。
    实测直连路径迁移前就是坏的（ERROR: syntax error at or near "["），
    后果是 tune-cursor 的每个游标都在证据采集这一步失败、进 Skipped
    Cursors，整条子命令产不出任何一条已验证结论。
    """
    if not names:
        # IN () 是语法错误。调用方都有空列表短路，走到这里说明短路漏了。
        raise ValueError("_quoted_list: 表名列表为空，无法构造 IN 列表")
    return ",".join("'" + n.replace("'", "''") + "'" for n in names)


def collect_tables(runner, names: list[str], scope=None) -> list[TableInfo]:
    if not names:
        return []
    rows = runner.run(TABLES_SCRIPT, {"names": _quoted_list(names)})
    if scope is not None:   # 同名表跨 schema:只留过程所在 schema 的行(common/catalog_scope.py)
        rows = cs.keep_rows(rows, scope, table_key="relname", schema_key="nspname")
    return [TableInfo(r["nspname"], r["relname"], as_int(r["relpages"]),
                      as_int(r["reltuples"]), r["relkind"], as_float(r["size_mb"]))
            for r in rows]


def collect_indexes(runner, names: list[str], scope=None) -> list[IndexInfo]:
    if not names:
        return []
    rows = runner.run(INDEXES_SCRIPT, {"names": _quoted_list(names)})
    if scope is not None:
        rows = cs.keep_rows(rows, scope, table_key="table_name", schema_key="schema_name")
    # bool("f") 是 True —— 直接 bool() 会把每个索引都报成 UNIQUE/PRIMARY
    return [IndexInfo(r["table_name"], r["index_name"], as_bool(r["indisunique"]),
                      as_bool(r["indisprimary"]), r["index_def"]) for r in rows]


def collect_column_stats(runner, names: list[str], scope=None) -> list[ColumnStat]:
    if not names:
        return []
    rows = runner.run(COLUMN_STATS_SCRIPT, {"names": _quoted_list(names)})
    if scope is not None:
        rows = cs.keep_rows(rows, scope, table_key="tablename", schema_key="schemaname")
    out = []
    for r in rows:
        # correlation 允许为 NULL，而 NULL 与空串在协议里不可区分。这是数值
        # 列，空串只可能来自 NULL —— 保住 None，报表里才会显示 n/a 而不是 0.00。
        raw_corr = r["correlation"]
        corr = None if raw_corr == _NULL_TEXT else as_float(raw_corr)
        out.append(ColumnStat(r["tablename"], r["attname"], as_float(r["n_distinct"]),
                              as_float(r["null_frac"]), as_int(r["avg_width"]), corr))
    return out


def collect_gucs(runner) -> list[GUC]:
    rows = runner.run(KEY_GUCS_SCRIPT)
    return [GUC(r["name"], r["setting"], r["unit"]) for r in rows]


# --- evidence orchestration (port of collect.go) -----------------------------

@dataclass(frozen=True)
class Evidence:
    sql: str
    version: str
    plan: str
    analyzed: bool
    tables: list = field(default_factory=list)
    indexes: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    gucs: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    analyze_requested: bool = False   # 要没要 analyze;analyzed 是「真跑了没」,两者可以不一致
    search_path: str = ""             # EXPLAIN 前实际切到的 schema(空 = 没切)
    search_path_note: str = ""        # 想切没切成的原因


def collect(runner, db, sql_text: str, do_analyze: bool, schema: str = "") -> Evidence:
    """runner 取固定查询；EXPLAIN 游标 SELECT 视有无原始会话走两条路。

    db 为 None 表示这条连接给不了持久会话。EXPLAIN 单条零状态，走注册模板；
    真正要会话的只有 hypopg 索引验证，由调用方跳过并标注。
    """
    rows = runner.run(DB_VERSION_SCRIPT)
    version = rows[0]["version"] if rows else ""
    applied, sp_note = "", ""
    if db is None:
        plan, applied, sp_note = explain_plan_via_script(runner, sql_text, do_analyze, schema)
    else:
        try:
            applied = sp.set_search_path(db, schema)
        except ValueError as exc:
            sp_note = str(exc)
        plan = explain(db, sql_text, do_analyze)
    names = extract_tables(sql_text)
    scope = cs.Scope(explicit=cs.explicit_schemas(extract_table_refs(sql_text)), default=schema or "")
    return Evidence(
        sql=sql_text,
        version=version,
        plan=plan,
        analyzed=ea.analyzed_for_real(plan, do_analyze),   # 看计划有没有 actual 行,不看请求标志
        analyze_requested=do_analyze,
        search_path=applied,
        search_path_note=sp_note,
        findings=scan_plan(plan),
        tables=collect_tables(runner, names, scope),
        indexes=collect_indexes(runner, names, scope),
        columns=collect_column_stats(runner, names, scope),
        gucs=collect_gucs(runner),
    )


# --- evidence renderer (port of cli/collect.go evidenceReport) ---------------

def evidence_report(ev: Evidence) -> str:
    out = (
        "# Tuning Evidence Bundle\n\n## Environment\n\n- Version: " + ev.version +
        "\n- Analyzed: " + str(ev.analyzed).lower() + "\n" +
        "\n## SQL\n\n" + render.code_block("sql", ev.sql) +
        "\n## Execution Plan\n\n" + render.code_block("", ev.plan)
    )
    if getattr(ev, "analyze_requested", False) and not ev.analyzed:
        out += "\n> " + ea.FIELD_ANALYZE_OFF_NOTE + "\n"
    if getattr(ev, "search_path", ""):
        out += f"\n> search_path 已切到 `{ev.search_path}`:EXPLAIN 按该 schema 解析不带前缀的表名。\n"
    elif getattr(ev, "search_path_note", ""):
        out += f"\n> search_path 未切换:{ev.search_path_note}\n"
    out += "\n## Deterministic Findings\n\n"
    if not ev.findings:
        out += "None.\n"
    for f in ev.findings:
        out += f"- **[{f.severity}] {f.kind}**: {f.detail} — {f.advice}\n"

    t_rows = [[t.schema, t.name, str(t.pages), str(t.tuples), t.kind, f"{t.size_mb:.1f}"]
              for t in ev.tables]
    out += "\n## Tables\n\n" + render.table(
        ["SCHEMA", "TABLE", "PAGES", "TUPLES", "KIND", "SIZE_MB"], t_rows)

    i_rows = [[ix.table, ix.name, str(ix.is_unique).lower(), str(ix.is_primary).lower(),
               render.truncate(ix.definition, 120)] for ix in ev.indexes]
    out += "\n## Indexes\n\n" + render.table(
        ["TABLE", "INDEX", "UNIQUE", "PRIMARY", "DEF"], i_rows)

    c_rows = []
    for c in ev.columns:
        corr = "n/a" if c.correlation is None else f"{c.correlation:.2f}"
        c_rows.append([c.table, c.column, f"{c.n_distinct:.2f}",
                       f"{c.null_frac:.3f}", str(c.avg_width), corr])
    out += "\n## Column Statistics\n\n" + render.table(
        ["TABLE", "COLUMN", "N_DISTINCT", "NULL_FRAC", "AVG_WIDTH", "CORRELATION"], c_rows)

    g_rows = [[g.name, g.setting, g.unit] for g in ev.gucs]
    out += "\n## Key Parameters (GUC)\n\n" + render.table(
        ["NAME", "SETTING", "UNIT"], g_rows)
    return out
