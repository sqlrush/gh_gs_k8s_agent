"""白名单增量的顺序与各份内容(2026-11-19):部署自检据此告诉现场「从哪一份起、按顺序导完哪几份」。

为什么在这里有一份:docs/delivery/ 的增量文件只给中间件团队,不随技能装进技能目录,自检读不到。
这份是从那些文件解析出来的数据(新增了哪些脚本、更新了哪些、哪些改了执行库),守卫测试
agent/tests/test_grmp_deltas_units.py 逐份核对两边一致 —— 新出一份增量,这里要跟着补一行。

只按脚本名推断:缺的脚本最早由哪一份新增、执行库对不上的脚本最早由哪一份改过,就从那一份起导到最后一份。
只改正文的增量(名字都在)推断不出来;推断不出的时候让现场把白名单导出交给我方核对。
"""
from __future__ import annotations

from typing import Iterable, List, NamedTuple, Optional, Tuple

FULL = "scripts.sql"          # 首次导入用的全量


class Delta(NamedTuple):
    file: str
    added: Tuple[str, ...]    # 这一份新增的脚本
    changed: Tuple[str, ...]  # 这一份更新的脚本(正文 / 参数 / 执行库)
    routing: Tuple[str, ...]  # 其中改了执行库(database_type 与 dbName)的


# 从旧到新;每一份都以前一份导完为前提
DELTAS: Tuple[Delta, ...] = (
    Delta("scripts-delta-20261018.sql",
          added=(
              "config.settings", "health.live_counters", "health.live_waits", "health.partitions",
              "health.snap_counters", "health.snap_waits", "health.storage", "health.sync_commit",
              "health.thread_pool", "health.wal_slots", "topsql.snap_statement", "vacuum.bloat_estimate",
              "vacuum.freeze_age",
          ),
          changed=(
              "proctune.plan_text_analyze", "sqltune.plan_text_analyze", "topsql.top_sql", "wdr.cache",
              "wdr.db_stat", "wdr.db_summary", "wdr.file_io", "wdr.snapshots", "wdr.top_sql",
          ),
          routing=()),
    Delta("scripts-delta-20261022.sql",
          added=(
              "explain.plan_from_history", "lockwait.asp_pairs", "lockwait.asp_sessions", "lockwait.asp_status",
              "wdr.plan_in_window",
          ),
          changed=(),
          routing=()),
    Delta("scripts-delta-20261024.sql",
          added=(),
          changed=(
              "health.conn_concentration", "health.conn_states", "health.long_xact", "health.overview",
              "health.stale_stats", "lockwait.asp_pairs", "lockwait.asp_sessions", "lockwait.asp_status",
              "memanalyze.gucs", "topsql.snap_statement", "topsql.top_sql", "vacuum.oldest_xmin",
          ),
          routing=()),
    Delta("scripts-delta-20261026.sql",
          added=(
              "sqltune.exec_history", "sqltune.stat_now", "sqltune.stat_trend",
          ),
          changed=(),
          routing=()),
    Delta("scripts-delta-20261030.sql",
          added=(
              "procinfo.source_adm", "proctune.source_adm",
          ),
          changed=(),
          routing=()),
    Delta("scripts-delta-20261105.sql",
          added=(
              "health.table_writes", "health.write_sessions", "health.write_sql", "health.xlog_by_session",
              "health.xlog_position", "vacuum.xid_lag", "vacuum.xmin_holder", "wdr.redo_io", "wdr.table_writes",
          ),
          changed=(
              "topsql.top_sql", "vacuum.dead_tuples",
          ),
          routing=()),
    Delta("scripts-delta-20261108.sql",
          added=(),
          changed=(
              "health.slow_sql", "health.storage", "slowsql.slow_sql", "topsql.top_sql",
          ),
          routing=()),
    Delta("scripts-delta-20261111.sql",
          added=(
              "login.current_db",
          ),
          changed=(
              "config.settings", "explain.active_pid", "explain.from_history", "explain.from_statement",
              "explain.kernel_funcs", "explain.multi_stmt_probe", "explain.plan_text",
              "explain.plan_text_analyze", "explain.plan_text_analyze_schema", "explain.plan_text_schema",
              "explain.relation_schemas", "explain.runtime_plan", "explain.runtime_plan_int4",
              "explain.session_by_pid", "health.invalid_index", "health.live_counters", "health.live_waits",
              "health.partitions", "health.snap_counters", "health.snap_waits", "health.stale_stats",
              "health.storage", "health.sync_commit", "health.table_writes", "health.thread_pool",
              "health.unused_index", "health.wal_slots", "health.write_sessions", "health.write_sql",
              "health.xlog_by_session", "health.xlog_position", "lockwait.pairs", "memanalyze.cols_bare",
              "memanalyze.cols_qualified", "procinfo.locate_schema", "procinfo.locate_search",
              "procinfo.proc_def", "procinfo.source_adm", "proctune.column_stats", "proctune.indexes",
              "proctune.locate_schema", "proctune.locate_search", "proctune.plan_text",
              "proctune.plan_text_analyze", "proctune.plan_text_analyze_schema", "proctune.plan_text_schema",
              "proctune.proc_def", "proctune.source_adm", "proctune.tables", "sqlreview.indexes",
              "sqlreview.tables", "sqltune.column_stats", "sqltune.column_types", "sqltune.indexes",
              "sqltune.plan_json", "sqltune.plan_json_schema", "sqltune.plan_text", "sqltune.plan_text_analyze",
              "sqltune.plan_text_analyze_schema", "sqltune.plan_text_schema", "sqltune.stats_freshness",
              "sqltune.tables", "topproc.top_procs", "topsql.snap_statement", "vacuum.bloat_estimate",
              "vacuum.dead_tuples", "vacuum.freeze_age", "vacuum.xid_lag", "vacuum.xmin_holder", "wdr.redo_io",
              "wdr.table_writes",
          ),
          routing=(
              "config.settings", "explain.active_pid", "explain.from_history", "explain.from_statement",
              "explain.kernel_funcs", "explain.multi_stmt_probe", "explain.plan_text",
              "explain.plan_text_analyze", "explain.plan_text_analyze_schema", "explain.plan_text_schema",
              "explain.relation_schemas", "explain.runtime_plan", "explain.runtime_plan_int4",
              "explain.session_by_pid", "health.invalid_index", "health.live_counters", "health.live_waits",
              "health.partitions", "health.snap_counters", "health.snap_waits", "health.stale_stats",
              "health.storage", "health.sync_commit", "health.table_writes", "health.thread_pool",
              "health.unused_index", "health.wal_slots", "health.write_sessions", "health.write_sql",
              "health.xlog_by_session", "health.xlog_position", "lockwait.pairs", "memanalyze.cols_bare",
              "memanalyze.cols_qualified", "procinfo.locate_schema", "procinfo.locate_search",
              "procinfo.proc_def", "procinfo.source_adm", "proctune.column_stats", "proctune.indexes",
              "proctune.locate_schema", "proctune.locate_search", "proctune.plan_text",
              "proctune.plan_text_analyze", "proctune.plan_text_analyze_schema", "proctune.plan_text_schema",
              "proctune.proc_def", "proctune.source_adm", "proctune.tables", "sqlreview.indexes",
              "sqlreview.tables", "sqltune.column_stats", "sqltune.column_types", "sqltune.indexes",
              "sqltune.plan_json", "sqltune.plan_json_schema", "sqltune.plan_text", "sqltune.plan_text_analyze",
              "sqltune.plan_text_analyze_schema", "sqltune.plan_text_schema", "sqltune.stats_freshness",
              "sqltune.tables", "topproc.top_procs", "topsql.snap_statement", "vacuum.bloat_estimate",
              "vacuum.dead_tuples", "vacuum.freeze_age", "vacuum.xid_lag", "vacuum.xmin_holder", "wdr.redo_io",
              "wdr.table_writes",
          )),
    Delta("scripts-delta-20261112.sql",
          added=(
              "memanalyze.sql_mem_history", "wdr.redo_io_live",
          ),
          changed=(),
          routing=()),
    Delta("scripts-delta-20261116.sql",
          added=(),
          changed=(
              "lockwait.pairs", "vacuum.dead_tuples",
          ),
          routing=()),
    Delta("scripts-delta-20261119.sql",
          added=(),
          changed=(
              "explain.kernel_funcs", "health.stale_stats", "health.unused_index", "lockwait.pairs",
          ),
          routing=()),
)


def plan(missing: Iterable[str] = (), misrouted: Iterable[str] = ()) -> Optional[List[str]]:
    """从最早一份能解释缺口的增量起,到最新一份为止的文件名;推断不出来(有缺口不是任何一份增量带来的)返回 None。"""
    missing, misrouted = set(missing), set(misrouted)
    if not missing and not misrouted:
        return []
    starts = []
    for names, field in ((missing, "added"), (misrouted, "routing")):
        for n in names:
            hit = [i for i, d in enumerate(DELTAS) if n in getattr(d, field)]
            if not hit:
                return None
            starts.append(min(hit))
    return [d.file for d in DELTAS[min(starts):]]


def advice(missing: Iterable[str] = (), misrouted: Iterable[str] = ()) -> str:
    """给自检用的一句话:按顺序导哪几份,或者把白名单导出交给我方核对。"""
    files = plan(missing, misrouted)
    if files is None:
        return ("推断不出这套白名单导到了哪一份增量:请把中间件里的白名单导出交给我方核对"
                "(首次导入用全量 %s)" % FULL)
    if not files:
        return ""
    head = "从 %s 起按顺序导完 %d 份增量:%s" % (files[0], len(files), "、".join(files))
    return head + "(一份都不能跳,每份都以前一份导完为前提);拿不准导到了哪一份时,把白名单导出交给我方核对"


__all__ = ["FULL", "Delta", "DELTAS", "plan", "advice"]
