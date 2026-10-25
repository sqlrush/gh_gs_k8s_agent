"""Tuning evidence bundle (port of probe/collect.go, explain.go, schema.go,
tables.go, guc.go and analyze/risks.go).

Collect gathers everything deterministic the agent needs for root-cause
analysis in one pass: version → plan → findings → tables/indexes/stats → GUCs.

取数分两条口子，**刻意不统一**：

  固定查询（版本/表/索引/列统计/GUC）  走 runner —— 统一入口执行已注册脚本，
                                       走中间件还是直连由连接的 driver 决定
  EXPLAIN 用户 SQL                     走 db —— 原始会话。任意 SQL 无法预注册，
                                       白名单模型下没有对应的口子

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

# SQL 已迁到 scripts/registry/sqltune/ —— 两条路径共用同一份定义
VERSION_SCRIPT = "sqltune.version"
TABLES_SCRIPT = "sqltune.tables"
INDEXES_SCRIPT = "sqltune.indexes"
COLUMN_STATS_SCRIPT = "sqltune.column_stats"
KEY_GUCS_SCRIPT = "sqltune.key_gucs"
STATS_FRESHNESS_SCRIPT = "sqltune.stats_freshness"
PLAN_JSON_SCRIPT = "sqltune.plan_json"
# 带 search_path 的两语句模板(common/search_path.py 按 base + "_schema" 拼名;交付闸按全串检索,故写全):
#   sqltune.plan_text_schema / sqltune.plan_text_analyze_schema / sqltune.plan_json_schema
#   探测脚本 explain.multi_stmt_probe(与 explain skill 共用)


# --- 协议取值：所有列值都是字符串，NULL 是空串 ------------------------------
#
# 按列名取值而不是下标：列序变了会当场 KeyError，而不是安静地把 size_mb
# 当成 reltuples。取值本身一律走 common.grmp.values 的 as_bool / as_int /
# as_float，本文件只为一个 openGauss 特有的写法留一个包装。

def _pages(raw) -> int:
    """页数列。**openGauss 的 pg_class.relpages 是 double precision**，
    取回来是 "3704.0"，as_int 会当场报错（它刻意不接受非整数写法）。

    迁移前这里拿到的是 float 对象，int(3704.0) 直接截断；这里补上同一次截断，
    行为与迁移前逐字一致。**只在本列放宽，不下沉到 common.grmp.values** ——
    「声明为整数的列里出现小数」在别处多半意味着取错了列，共用层报错是对的。
    """
    text = "" if raw is None else str(raw).strip()
    if "." in text:
        return int(float(text))
    return as_int(text)


# --- table-name extraction:收在 common/sqlrefs.py,与 proctune 及慢 SQL 排行共用 ---
from common.sqlrefs import extract_table_refs, extract_tables  # noqa: E402,F401


# --- DML detection + EXPLAIN (port of explain.go) ----------------------------

# 判定统一走 common.grmp.statement —— 这里原先抄了一份
# `^\s*(insert|update|delete|merge)\b`，explain 和 proctune 各抄一份，共三份。
# 那个正则的 `^\s*` 跳空白但不跳注释，`/* c */ UPDATE ...` 判成非 DML。
# 本文件的 is_dml 同时把着 explain(analyze=True) 要不要包回滚和 verify.py 的
# 等价性校验要不要跳过 —— 判错一次两处一起失效。
from common.grmp.statement import is_dml  # noqa: E402,F401  (verify.py 从本模块导入)


def explain_plan_via_script(runner, sql_text: str, analyze: bool, schema: str = "") -> tuple:
    """走已注册的 EXPLAIN 模板 —— 没有原始会话时用这条。返回 (计划, 实际切到的 schema, 说明)。

    有 schema 就走「SET search_path; EXPLAIN」两语句模板(现场 400 的修法),中间件不支持两条语句时
    退回单语句模板并说明,见 common/search_path.py。
    调用前必须先过 ensure_explainable()：模板是文本替换，参数位就是注入面。
    """
    script = "sqltune.plan_text_analyze" if analyze else "sqltune.plan_text"
    return sp.explain_plan(runner, script, sql_text, schema)


def explain_via_script(runner, sql_text: str, analyze: bool) -> str:
    """旧签名:只要计划文本、不切 search_path。"""
    return explain_plan_via_script(runner, sql_text, analyze)[0]


def explain_json_via_script(runner, sql_text: str, schema: str = ""):
    """JSON 计划，走已注册模板 —— 中间件路径。给代价推演的校准闸用。

    **只有 ANALYZE false 一种。** 推演比对的是*估算*代价（规划器算出来的那个
    数），ANALYZE 会真执行用户 SQL 拿实际耗时，与要比对的东西不是一回事。
    调用前同样要过 ensure_explainable()：参数位是注入面。
    """
    return sp.explain_plan(runner, PLAN_JSON_SCRIPT, sql_text, schema)[0]


def explain_json(db, sql_text: str):
    """JSON 计划，走原始会话 —— 直连路径。

    返回值可能是字符串，也可能是 psycopg2 已经解码好的 list/dict；两种都直接
    交给 plantree.parse()，由它统一处理。这里**不做归一化**：把已解码的对象
    再 str() 回去会得到 Python 的单引号写法，json.loads 解不动。
    """
    _, rows = db.query(
        "EXPLAIN (ANALYZE false, BUFFERS false, FORMAT JSON) " + sql_text)
    if not rows:
        return ""
    first = rows[0][0]
    if isinstance(first, (list, dict)):
        return first
    return "\n".join(str(r[0]) for r in rows)


def explain(db, sql_text: str, analyze: bool) -> str:
    """EXPLAIN in TEXT format; analyze executes (DML wrapped in rollback).

    **db 是原始会话，不是 runner。** 这里的 SQL 是用户临时给的，每次都不同，
    注册不进白名单 —— 见模块头。调用方负责先用 access.session_for() 拿到会话。
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
    pages: int          # pg_class.relpages —— 冻结在上次 ANALYZE/VACUUM
    tuples: int         # pg_class.reltuples —— 同上
    cur_pages: int      # 实时块数。**规划器用的是这个**，不是 pages
    kind: str
    size_mb: float

    @property
    def planner_tuples(self) -> float:
        """规划器实际使用的行数估算。

        estimate_rel_size(): density = reltuples/relpages，再乘实时块数。
        表在上次 ANALYZE 之后长大了的话，这个值与 reltuples 不同 —— og5 上
        实测 snap_summary_statement 冻结 535865 行、换算后 554914 行，
        而 EXPLAIN 报的 Plan Rows 正是后者。
        """
        if self.pages <= 0:
            return float(self.tuples)
        density = float(self.tuples) / float(self.pages)
        return float(round(density * self.cur_pages))


@dataclass(frozen=True)
class IndexInfo:
    table: str
    name: str
    is_unique: bool
    is_primary: bool
    pages: int      # 索引自身的页数（pg_class 里索引那一行，不是表那一行）
    tuples: int     # 索引条目数
    definition: str


@dataclass(frozen=True)
class ColumnStat:
    table: str
    column: str
    n_distinct: float
    null_frac: float
    avg_width: int
    correlation: Optional[float]
    # MCV / 直方图按**原文**存：解析规则（引号里的逗号、前导点小数）归
    # selectivity.py 管，采集层不掺和。这里存成解析后的结构，会让「怎么解析」
    # 散到两个地方，而这类解析出错是安静的 —— 只会让选择率偏。
    most_common_vals: str = ""
    most_common_freqs: str = ""
    histogram_bounds: str = ""


@dataclass(frozen=True)
class GUC:
    name: str
    setting: str
    unit: str


@dataclass(frozen=True)
class StatsFreshness:
    """统计信息新鲜度的**原始观测**，不含判定。

    判定要拿 live_tuples 和 TableInfo.tuples（pg_class.reltuples）比，跨两个
    采集结果，放在推演层（derive.py）做 —— 这里只负责如实带回观测值。

    last_analyze / last_autoanalyze 是已格式化的字符串，'never' 表示该表
    从未被 ANALYZE 过。空串意味着协议层出了问题（脚本里已 COALESCE 兜住 NULL），
    不该当成 'never' 处理。
    """
    schema: str
    table: str
    live_tuples: int
    dead_tuples: int
    last_analyze: str
    last_autoanalyze: str
    analyze_count: int
    autoanalyze_count: int


def _quoted_list(names: list[str]) -> str:
    """['a', "b'c"] -> "'a','b''c'"。**不带外层括号** —— 括号写在脚本的
    `IN ({{names}})` 里。值和 SQL 各出一半括号是最容易踩的坑（会得到
    IN (('a','b'))），所以这里只出字面量。
    """
    if not names:
        # IN () 是语法错误。调用方都有空列表短路，走到这里说明短路漏了。
        raise ValueError("_quoted_list: 表名列表为空，无法构造 IN 列表")
    return ",".join("'" + n.replace("'", "''") + "'" for n in names)


def collect_tables(runner, names: list[str], scope=None) -> list[TableInfo]:
    if not names:
        return []
    rows = runner.run(TABLES_SCRIPT, {"names": _quoted_list(names)})
    if scope is not None:   # 同名表跨 schema:只留本次 SQL 所在 schema 的行(common/catalog_scope.py)
        rows = cs.keep_rows(rows, scope, table_key="relname", schema_key="nspname")
    return [TableInfo(r["nspname"], r["relname"], _pages(r["relpages"]),
                      as_int(r["reltuples"]), _pages(r["curpages"]),
                      r["relkind"], as_float(r["size_mb"]))
            for r in rows]


def collect_indexes(runner, names: list[str], scope=None) -> list[IndexInfo]:
    if not names:
        return []
    rows = runner.run(INDEXES_SCRIPT, {"names": _quoted_list(names)})
    if scope is not None:
        rows = cs.keep_rows(rows, scope, table_key="table_name", schema_key="schema_name")
    # bool("f") 是 True —— 直接 bool() 会把每个索引都报成 UNIQUE/PRIMARY
    # index_relpages 同样是 double precision（"128.0"），走 _pages 截断
    return [IndexInfo(r["table_name"], r["index_name"], as_bool(r["indisunique"]),
                      as_bool(r["indisprimary"]), _pages(r["index_relpages"]),
                      as_int(r["index_reltuples"]), r["index_def"])
            for r in rows]


def collect_column_stats(runner, names: list[str], scope=None) -> list[ColumnStat]:
    if not names:
        return []
    rows = runner.run(COLUMN_STATS_SCRIPT, {"names": _quoted_list(names)})
    if scope is not None:
        rows = cs.keep_rows(rows, scope, table_key="tablename", schema_key="schemaname")
    # correlation 经常是 NULL，默认值给 None 而不是 0.0：0.0 会被读成
    # 「物理顺序与索引顺序完全无关」，那是一个结论，不是「不知道」。
    # 协议把 NULL 与真空串渲染成同一个值，这里只能把空串一律当 NULL。
    return [ColumnStat(r["tablename"], r["attname"], as_float(r["n_distinct"]),
                       as_float(r["null_frac"]), as_int(r["avg_width"]),
                       as_float(r["correlation"], None),
                       str(r.get("most_common_vals", "") or ""),
                       str(r.get("most_common_freqs", "") or ""),
                       str(r.get("histogram_bounds", "") or ""))
            for r in rows]


def collect_gucs(runner) -> list[GUC]:
    rows = runner.run(KEY_GUCS_SCRIPT)
    return [GUC(r["name"], r["setting"], r["unit"]) for r in rows]


def collect_stats_freshness(runner, names: list[str], scope=None) -> list[StatsFreshness]:
    if not names:
        return []
    rows = runner.run(STATS_FRESHNESS_SCRIPT, {"names": _quoted_list(names)})
    if scope is not None:
        rows = cs.keep_rows(rows, scope, table_key="relname", schema_key="schemaname")
    # n_live_tup/n_dead_tup 在 openGauss 里是 bigint，不像 relpages 那样带小数
    return [StatsFreshness(r["schemaname"], r["relname"],
                           as_int(r["n_live_tup"]), as_int(r["n_dead_tup"]),
                           r["last_analyze"], r["last_autoanalyze"],
                           as_int(r["analyze_count"]), as_int(r["autoanalyze_count"]))
            for r in rows]


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
    freshness: list = field(default_factory=list)
    analyze_requested: bool = False   # 要没要 analyze;analyzed 是「真跑了没」,两者可以不一致
    search_path: str = ""             # EXPLAIN 前实际切到的 schema(空 = 没切)
    search_path_note: str = ""        # 想切没切成的原因(中间件不支持两条语句 / schema 名不合法),或已改为补全表名的说明
    schema: str = ""                  # 这条 SQL 原本执行的 schema(要求切到的那个);没切成时后续取 JSON 计划仍要带它


def collect(runner, db, sql_text: str, do_analyze: bool, schema: str = "") -> Evidence:
    """runner 取固定查询；EXPLAIN 用户 SQL 视有无原始会话走两条路。

    db 为 None 表示这条连接给不了持久会话（中间件、或每语句起子进程的 gsql）。
    这时 EXPLAIN 走已注册模板 —— 单条 EXPLAIN 本来就零状态，不需要会话。
    真正需要会话的只有 hypopg 索引验证，由调用方跳过并标注。
    schema 是这条 SQL 原本执行时的 schema(statement_history.schema_name 等):有原始会话就在会话上
    SET search_path,没有就走两语句模板——业务 SQL 的表名不带 schema,不切的话中间件执行账号解析不到。
    """
    rows = runner.run(VERSION_SCRIPT)
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
    # 目录行按 schema 过滤:SQL 里显式写的 schema.表 优先,其次本次解析出的 schema;都没有就不过滤。
    # schema 是搜索路径串(`fl, public`)时按第一段过滤:keep_rows 在第一段一行都没有时自己退到 public,
    # 正好是 `fl, public` 的解析顺序;拿整串去比 nspname 永远比不上,同名表就会错选成 public 那张。
    scope = cs.Scope(explicit=cs.explicit_schemas(extract_table_refs(sql_text)), default=sp.primary_schema(schema))
    return Evidence(
        sql=sql_text,
        version=version,
        plan=plan,
        analyzed=ea.analyzed_for_real(plan, do_analyze),   # 看计划有没有 actual 行,不看请求标志
        analyze_requested=do_analyze,
        search_path=applied,
        search_path_note=sp_note,
        schema=schema or "",
        findings=scan_plan(plan),
        tables=collect_tables(runner, names, scope),
        indexes=collect_indexes(runner, names, scope),
        columns=collect_column_stats(runner, names, scope),
        gucs=collect_gucs(runner),
        freshness=collect_stats_freshness(runner, names, scope),
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

    # PAGES/TUPLES 是冻结值，CUR_PAGES/PLANNER_TUPLES 是规划器实际用的那一份。
    # 两组并排，才看得出「统计有多旧」和「旧到什么程度影响了估算」。
    t_rows = [[t.schema, t.name, str(t.pages), str(t.tuples), str(t.cur_pages),
               "%.0f" % t.planner_tuples, t.kind, f"{t.size_mb:.1f}"]
              for t in ev.tables]
    out += "\n## Tables\n\n" + render.table(
        ["SCHEMA", "TABLE", "PAGES", "TUPLES", "CUR_PAGES", "PLANNER_TUPLES",
         "KIND", "SIZE_MB"], t_rows)

    # 紧跟在 Tables 后面：上面那张表里的 PAGES/TUPLES 是上次 ANALYZE 的快照，
    # 这一节给的是「那是多久以前」。分开看容易把冻结值当现值。
    f_rows = [[fr.schema, fr.table, str(fr.live_tuples), str(fr.dead_tuples),
               fr.last_analyze, fr.last_autoanalyze,
               str(fr.analyze_count + fr.autoanalyze_count)] for fr in ev.freshness]
    out += "\n## Statistics Freshness\n\n" + render.table(
        ["SCHEMA", "TABLE", "LIVE_TUP", "DEAD_TUP", "LAST_ANALYZE",
         "LAST_AUTOANALYZE", "ANALYZE_CNT"], f_rows)

    i_rows = [[ix.table, ix.name, str(ix.is_unique).lower(), str(ix.is_primary).lower(),
               str(ix.pages), str(ix.tuples),
               render.truncate(ix.definition, 120)] for ix in ev.indexes]
    out += "\n## Indexes\n\n" + render.table(
        ["TABLE", "INDEX", "UNIQUE", "PRIMARY", "PAGES", "TUPLES", "DEF"], i_rows)

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
