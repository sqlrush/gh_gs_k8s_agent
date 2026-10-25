"""The 8 read-only health collectors — port of internal/probe/health/*.go.

Each collector takes (runner, thresholds, top) and returns a DimResult. Collectors
never raise: on query failure they return degraded(dim, reason) so one missing
view / permission gap cannot abort the whole check.

**waits / lwlock / bloat / locks 不在这里了。** 它们交给了各自专门的 skill
自己查、自己判断阈值：

    waits, lwlock -> gaussdb-waitevent（多窗口 DB time 分解 + 等待事件下钻，
                      两者是同一份报告里锁/轻量锁耗时占比的两个切面，拆不开）
    bloat         -> gaussdb-vacuum（死元组 / autovacuum 健康度评估）
    locks         -> gaussdb-lockwait（锁堵塞分析：谁挡了谁、阻塞链的根在哪）

health 通过 aggregate.py 以子进程调用它们、把它们的 findings 并入自己的
汇总报告，不再自己维护这 4 份 SQL 与阈值判断 —— 两份采集迟早会给出不一致
的数字，而这种不一致是静默的。详见 aggregate.py 顶部的说明、health.py 里
的 `_sub_skill_in_scope`（--include/--exclude 仍认这四个老维度名，只是
改成路由到对应子 skill）。
"""
from __future__ import annotations

from model import (
    DIM_CONCURRENCY, DIM_CONN, DIM_LOGS, DIM_OVERVIEW, DIM_REPL, DIM_SCHEMA,
    DIM_SLOWSQL, DIM_STORAGE, DIM_XACT, DimResult, Finding, Severity, degraded,
)
import running_sql
from thresholds import Thresholds, go_duration
from util import f2, human_bytes, i64, sev_by_duration, summarize_err, trunc

# common is resolved on sys.path by the entry script (health.py).
import common  # noqa: E402


import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/health/ —— 两条路径共用同一份定义
# 取数失败只认这一个类型。换一种数据库访问方式时，改的是访问模块，
# 不是这里 —— 详见 common/grmp/errors.py。
from common import access  # noqa: E402
from common import syssql  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_int, is_null  # noqa: E402


def _f(x, default: float = 0.0) -> float:
    """Coerce a possibly-None numeric (Decimal/float) to float."""
    return default if x is None else float(x)


# --- overview ----------------------------------------------------------------




def collect_overview(runner, th: Thresholds, _top: int) -> DimResult:
    try:
        rows = runner.run("health.overview")
    except access.QueryError as exc:
        return degraded(DIM_OVERVIEW, summarize_err(exc))
    r = rows[0]
    cache_hit = _f(r["cache_hit_pct"])
    backends = as_int(r["numbackends"])
    max_conn = as_int(r["max_conn"])
    # bool("f") 是 True —— 结果值全是字符串，必须用 as_bool 还原
    in_recovery = as_bool(r["in_recovery"])
    oldest = as_int(r["oldest_xact_s"])
    oldest_str = f"{oldest}s" if oldest > 0 else "无"
    d = DimResult(dimension=DIM_OVERVIEW, available=True,
                  headers=["cache_hit%", "connections", "max_conn", "in_recovery", "最老事务"],
                  rows=[[f2(cache_hit), i64(backends), i64(max_conn),
                         "true" if in_recovery else "false", oldest_str]])
    if max_conn <= 0:
        d.headline = "max_connections 不可读"
        return d
    conn_pct = 100.0 * backends / max_conn
    if conn_pct > th.conn_pct_warn:
        d.findings.append(Finding(DIM_OVERVIEW, "CONN_HIGH", Severity.WARN, "连接使用率",
                                  f"{conn_pct:.0f}% ({backends}/{max_conn})",
                                  f">{th.conn_pct_warn:.0f}%", "pg_stat_database.numbackends"))
    elif conn_pct > th.conn_pct_notice:
        d.findings.append(Finding(DIM_OVERVIEW, "CONN_HIGH", Severity.NOTICE, "连接使用率",
                                  f"{conn_pct:.0f}% ({backends}/{max_conn})",
                                  f">{th.conn_pct_notice:.0f}%", "pg_stat_database.numbackends"))
    if cache_hit < th.cache_hit_warn:
        d.findings.append(Finding(DIM_OVERVIEW, "CACHE_LOW", Severity.WARN, "缓存命中率",
                                  f2(cache_hit) + "%", f"<{th.cache_hit_warn:.0f}%",
                                  "pg_stat_database blks_hit/read"))
    elif cache_hit < th.cache_hit_notice:
        d.findings.append(Finding(DIM_OVERVIEW, "CACHE_LOW", Severity.NOTICE, "缓存命中率",
                                  f2(cache_hit) + "%", f"<{th.cache_hit_notice:.0f}%",
                                  "pg_stat_database blks_hit/read"))
    d.headline = (f"命中率 {cache_hit:.1f}%、连接 {backends}/{max_conn}、"
                  f"{'恢复中' if in_recovery else '未在恢复'}、最老事务 {oldest_str}")
    return d


# --- slow SQL ----------------------------------------------------------------

# health.slow_sql 在 SQL 里把正文截到 180 字
SLOWSQL_QUERY_CHARS = 180


def collect_slowsql(runner, th: Thresholds, top: int) -> DimResult:
    """已完成执行的慢 SQL(dbe_perf.statement)+ 执行中、还没结束的长 SQL(running_sql.py)。"""
    try:
        fetched = runner.run("health.slow_sql",
                             {"threshold_ms": int(th.slow_sql_avg_ms), "limit": top + syssql.SCREEN_MARGIN})
    except access.QueryError as exc:
        # dbe_perf.statement 取不到(权限 / 备机)不等于 pg_stat_activity 也取不到
        return running_sql.attach(degraded(DIM_SLOWSQL, summarize_err(exc)),
                                  running_sql.check(runner, th, top))
    return running_sql.attach(_completed_slowsql(runner, th, top, fetched),
                              running_sql.check(runner, th, top))


def _completed_slowsql(runner, th: Thresholds, top: int, fetched) -> DimResult:
    # 2026-10-22 客户要求:排除 GaussDB 自己的系统 SQL(common/syssql.py),标题里写明排除了几条
    screening = syssql.screen(fetched, want=top, sql_id=lambda r: r["unique_sql_id"], text=lambda r: r["query"],
                              shown_chars=SLOWSQL_QUERY_CHARS, full_text=syssql.full_text_lookup(runner),
                              seconds=lambda r: _f(r["total_sec"]), cache=syssql.verdict_cache())
    rows = screening.kept
    d = DimResult(dimension=DIM_SLOWSQL, available=True,
                  headers=["sql_id", "calls", "avg_ms", "total_s", "cpu_s", "query"])
    stmts = []
    for row in rows:
        sql_id = row["unique_sql_id"]
        query = row["query"]
        calls = as_int(row["calls"])
        avg_ms = _f(row["avg_ms"])
        total_s = _f(row["total_sec"])
        cpu_s = _f(row["cpu_sec"])
        stmts.append((sql_id, query, calls, avg_ms, total_s, cpu_s))
        d.rows.append([sql_id, i64(calls), f2(avg_ms), f2(total_s), f2(cpu_s), trunc(query, 50)])
    if stmts:
        sid, _q, calls, avg_ms, total_s, cpu_s = stmts[0]
        d.findings.append(Finding(DIM_SLOWSQL, "SLOWSQL_TOP", Severity.NOTICE, "慢 SQL",
                                  f"Top1 avg {avg_ms:.0f}ms ×{calls}",
                                  f">{th.slow_sql_avg_ms}ms", "dbe_perf.statement（/gaussdb-sqltune 深调）"))
        # CPU-light guard (DB-time trap): the slowest statement whose CPU is a
        # tiny fraction of its elapsed time is NOT compute/index-bound — time
        # went to waiting (locks/sleep) or I/O. Don't reach for sqltune first.
        if total_s > 0:
            cpu_ratio = cpu_s / total_s
            if cpu_ratio < th.slow_sql_low_cpu_ratio:
                d.findings.append(Finding(
                    DIM_SLOWSQL, "SLOWSQL_LOW_CPU", Severity.NOTICE, "慢但 CPU 极低",
                    f"Top1 CPU 仅占其耗时 {cpu_ratio * 100:.1f}%",
                    f"<{th.slow_sql_low_cpu_ratio * 100:.0f}%",
                    f"sqlid {sid} 总耗时 {total_s:.1f}s / CPU {cpu_s:.1f}s → 非 CPU 消耗主导"
                    "（锁等待/睡眠 或 I/O/排序溢出），非缺索引类资源问题；勿直接按缺索引上 "
                    "sqltune，先查锁与等待事件、必要时看临时文件/排序"))
        d.headline = f"Top1 avg {avg_ms:.0f}ms ×{calls}（共{len(stmts)}条超阈值）"
    else:
        d.headline = "无超阈值慢 SQL"
    if screening.brief():
        d.headline += "；" + screening.brief()
    return d


# --- long & idle transactions ------------------------------------------------



def _xact_threshold(code: str, sev: Severity, th: Thresholds) -> str:
    n, w, c = th.long_xact_notice, th.long_xact_warn, th.long_xact_crit
    if code == "XACT_IDLE":
        n, w, c = th.idle_xact_notice, th.idle_xact_warn, th.idle_xact_crit
    if sev == Severity.CRITICAL:
        return ">" + go_duration(c)
    if sev == Severity.WARN:
        return ">" + go_duration(w)
    return ">" + go_duration(n)


def collect_xact(runner, th: Thresholds, top: int) -> DimResult:
    try:
        rows = runner.run("health.long_xact", {"limit": top})
    except access.QueryError as exc:
        return degraded(DIM_XACT, summarize_err(exc))
    d = DimResult(dimension=DIM_XACT, available=True,
                  headers=["pid", "user", "state", "时长(s)", "query"])
    worst_sev = Severity.OK
    worst_line = ""
    n_rows = 0
    max_secs = 0.0
    for row in rows:
        pid = as_int(row["pid"])
        user, state = row["usename"], row["state"]
        xact_age, state_age = _f(row["xact_age_s"]), _f(row["state_age_s"])
        query = row["query"]
        if state == "idle in transaction":
            secs, code = state_age, "XACT_IDLE"
            sev = sev_by_duration(secs, th.idle_xact_notice, th.idle_xact_warn, th.idle_xact_crit)
        else:
            secs, code = xact_age, "XACT_LONG"
            sev = sev_by_duration(secs, th.long_xact_notice, th.long_xact_warn, th.long_xact_crit)
        dur_str = f"{secs:.0f}"
        d.rows.append([i64(pid), user, state, dur_str, trunc(query, 60)])
        n_rows += 1
        if secs > max_secs:
            max_secs = secs
        if sev != Severity.OK:
            d.findings.append(Finding(DIM_XACT, code, sev, f"pid {pid} {state}",
                                      dur_str + "s", _xact_threshold(code, sev, th),
                                      f"pg_stat_activity pid={pid}"))
            if sev > worst_sev:
                worst_sev = sev
                worst_line = f"{code} pid {pid} {dur_str}s"
    if worst_line:
        d.headline = worst_line
    elif n_rows > 0:
        d.headline = f"{n_rows} 个客户端事务，均在阈值内（最长 {max_secs:.0f}s）"
    else:
        d.headline = "无活动客户端事务"
    return d


# --- connections -------------------------------------------------------------
# SQL 在 scripts/registry/health/conn_states.yaml(只数 sessionid <> 0,见那里的说明)。


def collect_conn(runner, th: Thresholds, _top: int) -> DimResult:
    try:
        rows = runner.run("health.conn_states")
    except access.QueryError as exc:
        return degraded(DIM_CONN, summarize_err(exc))
    d = DimResult(dimension=DIM_CONN, available=True, headers=["state", "会话数"])
    total = active = idle = iit = 0
    for row in rows:
        st, cnt = row["state"], as_int(row["cnt"])
        d.rows.append([st, i64(cnt)])
        total += cnt
        if st == "active":
            active = cnt
        elif st == "idle":
            idle = cnt
        elif st == "idle in transaction":
            iit = cnt
    if active > th.active_warn:
        d.findings.append(Finding(DIM_CONN, "ACTIVE_HIGH", Severity.WARN, "活跃会话数",
                                  i64(active), f">{th.active_warn}", "pg_stat_activity state=active"))
    elif active > th.active_notice:
        d.findings.append(Finding(DIM_CONN, "ACTIVE_HIGH", Severity.NOTICE, "活跃会话数",
                                  i64(active), f">{th.active_notice}", "pg_stat_activity state=active"))
    if active >= th.active_conc_floor:
        try:
            r2 = runner.run("health.conn_concentration")
        except access.QueryError:
            r2 = []
        if r2:
            top_q = r2[0]["q"]
            top_c = as_int(r2[0]["c"])
            real_total = as_int(r2[0]["total"] or 0)
            if real_total >= th.active_conc_floor and top_c > 0:
                conc = 100.0 * top_c / real_total
                if conc >= th.active_conc_pct:
                    d.findings.append(Finding(
                        DIM_CONN, "ACTIVE_SQL_HOT", Severity.NOTICE, "活跃集中在单条 SQL",
                        f"{conc:.0f}% ({top_c}/{real_total}) 在: {trunc(top_q, 40)}",
                        f">{th.active_conc_pct:.0f}%", "pg_stat_activity active client SQL"))
    _thread_pool(runner, th, d)
    d.headline = f"共 {total}：active {active}、idle {idle}、IIT {iit}"
    return d


def _thread_pool(runner, th: Thresholds, d: DimResult) -> None:
    """线程池用量。**开了线程池时真正的并发上限是池大小,不是 max_connections。**

    og5 实测:max_connections=1000 而 thread_pool_attr='800, 2, (nobind)' ——
    先碰到的是 800。池满之后新请求排队,现场表现是「库没响应」,而连接数看着
    离 1000 还远,于是这个维度一片绿。

    池大小的解析放在这里而不是 SQL 里:格式是 '<线程数>, <组数>, (绑核策略)',
    在 SQL 里 split_part 一层层剥,格式一变就静默给出错的分母。解析不出来
    就不判(并说明),不要拿 max_connections 顶上去当分母 —— 那个分母是错的。
    """
    try:
        rows = runner.run("health.thread_pool")
    except access.QueryError as exc:
        d.rows.append(["线程池", "取不到（%s）" % summarize_err(exc)])
        return
    kv = {r["item"]: r["value"] for r in rows}
    if str(kv.get("enable_thread_pool", "")).lower() not in ("on", "true", "t", "1"):
        d.rows.append(["线程池", "未开启（并发上限看 max_connections）"])
        return
    attr = str(kv.get("thread_pool_attr") or "")
    size = _pool_size(attr)
    if size is None:
        d.rows.append(["线程池", "已开启，但池大小解析不出来：thread_pool_attr=%s" % attr])
        return
    used = as_int(kv.get("real_sessions"))
    pct = 100.0 * used / size if size > 0 else 0.0
    d.rows.append(["线程池用量", "%d/%d（%.0f%%）" % (used, size, pct)])
    sev, thr = None, 0.0
    if pct >= th.thread_pool_warn:
        sev, thr = Severity.WARN, th.thread_pool_warn
    elif pct >= th.thread_pool_notice:
        sev, thr = Severity.NOTICE, th.thread_pool_notice
    if sev is not None:
        d.findings.append(Finding(
            DIM_CONN, "THREAD_POOL_HIGH", sev, "线程池用量",
            "%.0f%%（%d/%d）" % (pct, used, size), f">{thr:.0f}%",
            "thread_pool_attr 的池大小 %d，pg_stat_activity 里 sessionid<>0 的真实会话 %d。"
            "池满后新连接排队，表现为「库没响应」，而连接数离 max_connections 还远"
            % (size, used)))


def _pool_size(attr: str):
    """'800, 2, (nobind)' → 800。解析不出来返回 None(调用方如实说明,不猜)。"""
    head = str(attr or "").split(",", 1)[0].strip()
    if not head.isdigit():
        return None
    n = int(head)
    return n if n > 0 else None


# --- checkpoint / WAL / archiving --------------------------------------------

def collect_logs(runner, th: Thresholds, _top: int) -> DimResult:
    d = DimResult(dimension=DIM_LOGS, available=True, headers=["指标", "值"])
    try:
        rows = runner.run("health.bgwriter")
    except access.QueryError as exc:
        return degraded(DIM_LOGS, summarize_err(exc))
    timed, req = (as_int(rows[0]["checkpoints_timed"]),
                  as_int(rows[0]["checkpoints_req"]))
    req_pct = 100.0 * req / (timed + req) if timed + req > 0 else 0.0
    d.rows.append(["checkpoint timed/req", f"{timed}/{req}"])
    d.rows.append(["checkpoint req 占比", f2(req_pct) + "%"])
    sev = Severity.OK
    thr = th.ckpt_req_notice
    if req_pct > th.ckpt_req_warn:
        sev, thr = Severity.WARN, th.ckpt_req_warn
    elif req_pct > th.ckpt_req_notice:
        sev = Severity.NOTICE
    if sev != Severity.OK:
        d.findings.append(Finding(DIM_LOGS, "CKPT_PRESSURE", sev, "checkpoint 请求占比",
                                  f2(req_pct) + "%", f">{thr:.0f}%", "pg_stat_bgwriter checkpoints_req"))
    am = "未知"
    try:
        _am = runner.run("health.archive_mode")
        val = _am[0]["setting"] if _am else None
        if not is_null(val):
            am = str(val)
            d.rows.append(["archive_mode", am])
    except access.QueryError:
        pass
    slots = _wal_slots(runner, th, d)
    d.headline = (f"checkpoint req 占比 {req_pct:.0f}%、归档 {am}"
                  + (f"、复制槽 {slots} 个" if slots else ""))
    return d


def _wal_slots(runner, th: Thresholds, d: DimResult) -> int:
    """复制槽钉住了多少 WAL。返回槽数量(给 headline 用)。

    **和 REPL_LAG 是两件事,两个都要有。** REPL_LAG 是「备库追不上」;
    这里是「槽钉住了 WAL 回收」—— 槽不消费,WAL 只增不减,直到把盘填满。
    一个 active 但消费很慢的槽会堆 WAL 却不一定挡 xmin;一个 inactive 的槽
    两样都挡,而且没有任何东西会自动清掉它。

    零行 = 没有槽(og5 这种单机库就是),不是取数失败 —— 不报任何东西。
    """
    try:
        rows = runner.run("health.wal_slots")
    except access.QueryError:
        return 0        # 脚本未登记(现场还没重导白名单)时静默跳过,不污染这一维
    for row in rows or []:
        name = str(row.get("slot_name") or "?")
        lag = row.get("lag_bytes")
        active = str(row.get("active") or "").lower() in ("t", "true", "on", "1")
        lag_n = as_int(lag) if not is_null(lag) else 0
        d.rows.append(["复制槽 " + trunc(name, 20),
                       "%s，落后 %s" % ("活跃" if active else "**未活跃**",
                                        human_bytes(lag_n))])
        # 未活跃的槽单独报:消费端已经没了,槽却还钉着回收下限。
        # 这一条不看落后量 —— 一个刚建的 inactive 槽落后很少,但它会一直涨。
        if not active:
            d.findings.append(Finding(
                DIM_LOGS, "WAL_SLOT_INACTIVE", Severity.WARN, "复制槽 %s" % name,
                "未活跃，落后 " + human_bytes(lag_n), "应为活跃",
                "pg_replication_slots.active=false —— 消费端已不在，但槽仍钉住 WAL 回收，"
                "WAL 会一直堆积直到填满磁盘；确认下游不再需要后由 DBA 删除该槽"))
            continue
        sev, thr = None, 0
        if lag_n > th.slot_lag_warn:
            sev, thr = Severity.WARN, th.slot_lag_warn
        elif lag_n > th.slot_lag_notice:
            sev, thr = Severity.NOTICE, th.slot_lag_notice
        if sev is not None:
            d.findings.append(Finding(
                DIM_LOGS, "WAL_SLOT_LAG", sev, "复制槽 %s 落后" % name,
                human_bytes(lag_n), ">" + human_bytes(thr),
                "pg_current_xlog_location() 与 restart_lsn 的差值 —— 这段 WAL 不能回收，"
                "下游消费跟不上就会持续占盘（与备库 replay 延迟是两件事）"))
    return len(rows or [])


# --- replication / standby ---------------------------------------------------



def collect_repl(runner, th: Thresholds, _top: int) -> DimResult:
    try:
        rows = runner.run("health.replication")
    except access.QueryError as exc:
        return degraded(DIM_REPL, summarize_err(exc))
    d = DimResult(dimension=DIM_REPL, available=True,
                  headers=["standby", "client", "state", "sync", "replay_lag"])
    n = 0
    for row in rows:
        app, caddr, state, sync = (row["application_name"], row["client_addr"],
                                   row["state"], row["sync_state"])
        lag = row["lag_bytes"]
        n += 1
        d.rows.append([trunc(app, 24), caddr, state, sync, human_bytes(int(lag or 0))])
        if state != "Streaming":
            d.findings.append(Finding(DIM_REPL, "REPL_NOT_STREAMING", Severity.WARN,
                                      f"备库 {app} 状态", state, "=Streaming",
                                      "pg_stat_replication.state"))
        if not is_null(lag) and as_int(lag) > th.repl_lag_notice:
            sev = Severity.NOTICE
            thr = th.repl_lag_notice
            if int(lag) > th.repl_lag_warn:
                sev, thr = Severity.WARN, th.repl_lag_warn
            d.findings.append(Finding(DIM_REPL, "REPL_LAG", sev, f"备库 {app} replay 延迟",
                                      human_bytes(int(lag)), ">" + human_bytes(thr),
                                      "pg_stat_replication sent vs replay"))
    sync_note = _sync_commit(runner, d, rows)
    d.headline = ("无下游备库（单机，或本节点为备库）" if n == 0 else f"{n} 个备库")
    if sync_note:
        d.headline += "；" + sync_note
    return d


# 会等同步备确认的 synchronous_commit 取值。local / off 不等。
_SYNC_WAITS = ("on", "remote_write", "remote_apply")


def _sync_commit(runner, d: DimResult, repl_rows: list) -> str:
    """同步提交会不会把主库卡住。返回一句给 headline 的话(没结论就空串)。

    **必须先分辨单机/集群,这是这段代码存在的全部理由。** og5 实测:
    `synchronous_commit=on`、`synchronous_standby_names='*'`、
    `pg_stat_replication` 一个备库都没有 —— 按 PostgreSQL 的语义每次提交都该
    无限等待,但 og5 写入完全正常。原因是 openGauss 的同步复制要靠
    `replconninfo1..N` 定义对端才生效,而单机部署上那些全是空的。

    所以只看两个 GUC 就报警,会在**每一个单机库上乱报** —— 而单机库是客户
    环境里最多的一类。虚报一次之后,真的配错时也没人会再看这条。
    """
    try:
        rows = runner.run("health.sync_commit")
    except access.QueryError:
        return ""       # 脚本未登记时静默跳过
    kv = {r["item"]: str(r["value"] or "") for r in rows or []}
    if not kv:
        return ""
    peers = as_int(kv.get("peers_configured"))
    commit = kv.get("sync_commit", "").lower()
    names = kv.get("standby_names", "").strip()
    in_recovery = kv.get("in_recovery", "").lower() in ("t", "true", "on", "1")

    # **不往 d.rows 里塞这些。** 这一维的表是「每行一个备库」,表头是
    # standby/client/state/sync/replay_lag 五列;塞一行 ["synchronous_commit","on"]
    # 会被补成五格、标签落在 standby 那一列下面,读起来像有个叫
    # synchronous_commit 的备库(自测时实际渲染成这样了)。
    # 配置事实放进抬头,真有风险时由 finding 带全部细节。
    cfg = "synchronous_commit=%s、standby_names=%s" % (commit or "未知", names or "(空)")

    if peers <= 0:
        # 单机:这一项整体不适用。**明说不适用,而不是「健康」** ——
        # 说成健康,会让人以为"同步复制这块已经检查过并且没问题"。
        return ("单机部署（replconninfo 未配置），同步提交不生效、本项不适用（%s）" % cfg)
    if in_recovery:
        return "本节点为备库，同步提交的风险在主库那边（%s）" % cfg
    if commit not in _SYNC_WAITS or not names:
        return "集群，异步提交、不等备库确认（%s）" % cfg

    synced = sum(1 for r in repl_rows or []
                 if str(r.get("sync_state") or "").lower() == "sync")
    if synced == 0:
        d.findings.append(Finding(
            DIM_REPL, "SYNC_COMMIT_NO_STANDBY", Severity.CRITICAL, "同步提交无可用同步备",
            "synchronous_commit=%s、standby_names=%s，但当前 0 个 sync 备库"
            % (commit, trunc(names, 30)), "至少 1 个",
            "集群已配置 replconninfo（%d 个对端）且要求同步确认，而 pg_stat_replication 里"
            "没有 sync_state=sync 的备库 —— **主库的提交会一直等下去**，表现为写入全部挂住。"
            "处置：恢复备库，或由 DBA 临时把 synchronous_commit 降为 local" % peers))
        return "⚠ 要求同步确认但没有同步备"
    return "集群，%d 个同步备在线（%s）" % (synced, cfg)


# --- schema / objects --------------------------------------------------------

_SCHEMA_SYS_FILTER = ("('pg_catalog','information_schema','snapshot','dbe_perf',"
                      "'dbe_pldeveloper','cstore','pg_toast')")


#_UNUSED_IDX_Q = """
#SELECT s.schemaname||'.'||s.indexrelname, pg_relation_size(s.indexrelid)
#FROM pg_stat_user_indexes s
#JOIN pg_index i ON i.indexrelid = s.indexrelid
#WHERE s.idx_scan=0 AND pg_relation_size(s.indexrelid) > %s
#  AND NOT i.indisprimary AND NOT i.indisunique
#  AND s.schemaname NOT IN """ + _SCHEMA_SYS_FILTER + """
#ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT %s"""


def collect_schema(runner, th: Thresholds, top: int) -> DimResult:
    d = DimResult(dimension=DIM_SCHEMA, available=True, headers=["项", "对象", "值"])
    n_unused = n_stale = 0
    invalid = 0
    # 1) unused indexes (failure of this primary query degrades the dimension)
    try:
        rows = runner.run("health.unused_index",
                          {"min_bytes": int(th.index_unused_bytes),
                           "schema_filter": _SCHEMA_SYS_FILTER, "limit": top})
    except access.QueryError as exc:
        return degraded(DIM_SCHEMA, summarize_err(exc))
    # 观测窗口：idx_scan 是**自上次统计重置以来**的累计值，不知道「以来」
    # 是多久，等于 0 什么都说明不了。统计刚重置时全库索引都是 0。
    window_text, window_seconds = "无法确定", -1.0
    try:
        wrows = runner.run("health.stats_window")
        if wrows:
            window_seconds = _f(wrows[0]["window_seconds"])
            reset = wrows[0]["stats_reset"]
            window_text = (f"自 {reset} 起 {window_seconds / 86400.0:.1f} 天"
                           if window_seconds >= 0 else "统计从未重置过")
    except access.QueryError:
        pass
    d.rows.append(["观测窗口", "pg_stat_database.stats_reset", window_text])

    for row in rows:
        name, sz = row["idx_name"], as_int(row["idx_bytes"])
        scans = as_int(row["idx_scan"])
        constraint = as_bool(row["indisprimary"]) or as_bool(row["indisunique"])
        table_idx = as_int(row["table_idx_scan"])
        table_seq = as_int(row["table_seq_scan"])
        kind = "约束索引" if constraint else "普通索引"
        d.rows.append([kind, name,
                       f"{human_bytes(sz)} 扫描 {scans} 次"
                       f"（该表索引扫描共 {table_idx}、顺扫 {table_seq}）"])
        if scans > 0:
            continue
        if constraint:
            # 主键/唯一约束背书的索引，不论用量都不能删 —— 列出来只为让人
            # 看到这张表上还有什么，不产生任何建议。
            continue
        if window_seconds >= 0 and window_seconds < th.index_unused_min_window_s:
            # 窗口太短，0 次不构成证据。**不出这条结论**，而不是出了再加个警告。
            continue
        n_unused += 1
        context = (f"该表其他索引共被扫描 {table_idx} 次"
                   if table_idx > 0 else "该表所有索引在本窗口内均未被扫描")
        d.findings.append(Finding(
            DIM_SCHEMA, "INDEX_UNUSED", Severity.NOTICE,
            # 措辞不能写死成「无用」：只是**这个观测窗口内**没被用到。
            f"{name} 在当前统计窗口内未被使用",
            f"{human_bytes(sz)}，idx_scan=0，窗口 {window_text}",
            f">{human_bytes(th.index_unused_bytes)} 且窗口内 0 次扫描",
            f"pg_stat_user_indexes.idx_scan=0；{context}。"
            f"反例需排除后再决定是否删除：统计重置或实例重启会让计数归零；"
            f"月度/季度/年终报表用的索引可以数周为 0；"
            f"主备分离时备机上的使用不计入本机计数器。"))
    # 2) invalid indexes (best-effort)
    try:
        _iv = runner.run("health.invalid_index")
        invalid = int(_iv[0]["cnt"] if _iv else 0)
    except access.QueryError:
        invalid = 0
    if invalid > 0:
        d.rows.append(["失效索引", "(invalid)", i64(invalid)])
        d.findings.append(Finding(DIM_SCHEMA, "INDEX_INVALID", Severity.WARN,
                                  "失效索引数", i64(invalid), ">0", "pg_index.indisvalid=false"))
    # 3) stale stats (best-effort)
    try:
        srows = runner.run("health.stale_stats",
                           {"min_rows": int(th.stale_min_rows), "min_pages": int(th.stale_min_pages),
                            "schema_filter": _SCHEMA_SYS_FILTER, "limit": top})
    except access.QueryError as exc:
        # 说出来:白名单还是旧版(没有 min_pages 参数)时就会走到这里,静默的话像是「没有陈旧的表」
        d.rows.append(["统计新鲜度", "（取不到）", summarize_err(exc)])
        srows = None
    for row in srows or []:
        name = row["tbl_name"]
        frozen_pages = _f(row["frozen_pages"])
        cur_pages = _f(row["cur_pages"])
        stat_columns = as_int(row["stat_columns"])
        last_analyze = row["last_analyze"]
        drift = (abs(cur_pages - frozen_pages) / frozen_pages
                 if frozen_pages > 0 else None)
        drift_text = "无法计算" if drift is None else f"{drift * 100:.1f}%"
        d.rows.append(["统计新鲜度", name,
                       f"冻结 {frozen_pages:.0f} 页 / 实时 {cur_pages:.0f} 页"
                       f"（偏离 {drift_text}），pg_stats {stat_columns} 列，"
                       f"上次 ANALYZE {last_analyze}"])

        # 判据只用不会被 pg_stat_reset() 清掉的信号。og5 实测过：
        # gsbench.fact_sales 报 last_analyze=never、n_live_tup=0，但 pg_stats
        # 里有 8 列统计信息 —— 拿 last_analyze 当判据会把好表判成从未分析。
        if stat_columns == 0:
            reason = ("pg_stats 里一列统计信息都没有 —— 该表确实从未被 ANALYZE "
                      "覆盖（或统计被删过），规划器只能靠默认估算。")
        elif drift is None:
            reason = "pg_class.relpages 为 0，没有可比对的冻结基准。"
        elif drift > th.stale_page_drift:
            reason = (f"冻结页数 {frozen_pages:.0f} 与实时页数 {cur_pages:.0f} "
                      f"相差 {drift_text}，超过阈值 {th.stale_page_drift * 100:.0f}% "
                      f"—— 表在上次 ANALYZE 之后长大了，而 n_distinct、"
                      f"correlation 不会跟着更新。")
        else:
            continue
        n_stale += 1
        d.findings.append(Finding(
            DIM_SCHEMA, "STALE_STATS", Severity.NOTICE,
            f"{name} 的统计信息不足以支撑计划估算", reason,
            f"页偏离>{th.stale_page_drift * 100:.0f}% 或无统计列",
            f"pg_class.relpages={frozen_pages:.0f}，实时页数={cur_pages:.0f}，"
            f"pg_stats 统计列数={stat_columns}，reltuples={as_int(row['frozen_tuples'])}，"
            f"n_live_tup={as_int(row['live_tuples'])}。"
            f"（last_analyze={last_analyze} 仅供参考：该计数器可被 pg_stat_reset "
            f"清除，而 ANALYZE 的成果存在 pg_statistic 里不受影响，"
            f"所以它不参与本判定。）"))
    # 措辞不写「无用索引」：那是个关于未来的断言，而我们只观测到了一个窗口。
    n_part = _partitions(runner, th, d)
    d.headline = (f"窗口内未使用的索引 {n_unused}、失效索引 {invalid}、"
                  f"统计不足以支撑估算的表 {n_stale if srows is not None else '取不到'}"
                  f"（观测窗口 {window_text}）"
                  + (f"；分区表 {n_part} 张" if n_part else ""))
    return d


# 一张表的分区数超过这个值,规划开销就开始明显上升(每次规划都要遍历分区元数据)。
# 1000 不是内核限制,是个工程判断:按月分区能撑 80 年,到了四位数基本说明
# 分区粒度设计有问题(常见是按天甚至按小时分,又从不清理历史分区)。
PARTS_NOTICE = 1000
PARTS_WARN = 4000


def _partitions(runner, th: Thresholds, d: DimResult) -> int:
    """分区表体检:每张表几个分区。返回分区表张数(给 headline 用)。

    **只回答「分区数量与策略」,不回答「分区裁剪有没有生效」。** 裁剪是执行计划的
    属性,要看具体 SQL 的 plan(走 gaussdb-explain);一条目录查询给不出这个结论。
    把两件事混在一起,会让人以为分区数正常就等于裁剪正常 —— 而最常见的分区故障
    恰恰是"分区数很合理,但查询没带分区键、每次全分区扫"。
    """
    try:
        rows = runner.run("health.partitions")
    except access.QueryError:
        return 0        # 脚本未登记(现场还没重导白名单)时静默跳过
    for row in rows or []:
        name = "%s.%s" % (row.get("schema"), row.get("table"))
        n = as_int(row.get("parts"))
        if n < PARTS_NOTICE:
            continue        # 表行只列偏多的那些,否则几十张分区表会把这一维刷满
        d.rows.append([trunc(name, 32), "%d 个分区（策略 %s）"
                       % (n, row.get("strategy") or "?")])
        sev, thr = (Severity.WARN, PARTS_WARN) if n >= PARTS_WARN \
            else (Severity.NOTICE, PARTS_NOTICE)
        d.findings.append(Finding(
            DIM_SCHEMA, "PARTITION_COUNT_HIGH", sev, "%s 的分区数" % name,
            str(n), f">={thr}",
            "pg_partition 里该表有 %d 个分区 —— 每次查询规划都要遍历分区元数据，"
            "分区数到四位数时规划开销明显上升。常见成因是按天/按小时分区又从不清理"
            "历史分区。**这一项不代表分区裁剪有问题**：裁剪要看具体 SQL 的执行计划"
            "（gaussdb-explain），分区数正常也可能每次都在全分区扫。" % n))
    return len(rows or [])


# --- transactions / concurrency ----------------------------------------------

# 死锁次数之外数据库不留明细;环上是谁只写在数据库日志里,本工具读不到(pg_read_file 要 initial account)
_DEADLOCK_EV = ("pg_stat_database.deadlocks（只有次数）。环上是哪几条语句、谁被牺牲，只在数据库日志里："
                "把含 `deadlock detected` 的那几段（连同后面的 DETAIL 几行）贴给我，"
                "`gaussdb-lockwait --deadlock-log -` 能逐个解析出环、语句与牺牲者")


def collect_concurrency(runner, th: Thresholds, _top: int) -> DimResult:
    d = DimResult(dimension=DIM_CONCURRENCY, available=True, headers=["指标", "值"])
    try:
        rows = runner.run("health.db_concurrency")
    except access.QueryError as exc:
        return degraded(DIM_CONCURRENCY, summarize_err(exc))
    deadlocks, commit, rollback = (as_int(rows[0]["deadlocks"]),
                                   as_int(rows[0]["xact_commit"]),
                                   as_int(rows[0]["xact_rollback"]))
    total = commit + rollback
    rb_pct = 100.0 * rollback / total if total > 0 else 0.0
    d.rows.append(["deadlocks", i64(deadlocks)])
    d.rows.append(["commit/rollback", f"{commit}/{rollback} ({rb_pct:.1f}%回滚)"])
    # 原始值带给收尾处理算「距上次新增」(health.py 的 annotate_counters)
    d.raw.update({"deadlocks": deadlocks, "xact_commit": commit, "xact_rollback": rollback})
    if deadlocks > 0:
        d.findings.append(Finding(DIM_CONCURRENCY, "DEADLOCKS", Severity.NOTICE,
                                  "死锁累计数", i64(deadlocks), ">0", _DEADLOCK_EV))
    if total > th.rollback_floor and rb_pct > th.rollback_pct:
        d.findings.append(Finding(DIM_CONCURRENCY, "ROLLBACK_HIGH", Severity.NOTICE,
                                  "事务回滚率", f"{rb_pct:.1f}%", f">{th.rollback_pct:.0f}%",
                                  "pg_stat_database commit/rollback"))
    prepared = 0
    try:
        _px = runner.run("health.prepared_xacts")
        prepared = int(_px[0]["cnt"] if _px else 0)
        d.rows.append(["prepared 2PC", i64(prepared)])
        if prepared > 0:
            d.findings.append(Finding(DIM_CONCURRENCY, "PREPARED_XACT", Severity.WARN,
                                      "悬挂的两阶段事务", i64(prepared), ">0", "pg_prepared_xacts"))
    except access.QueryError:
        pass
    d.headline = f"死锁 {deadlocks}、回滚率 {rb_pct:.1f}%、2PC {prepared}"
    return d


# --- registry ----------------------------------------------------------------

def collect_storage(runner, th: Thresholds, top: int) -> DimResult:
    """库与表空间的占用量。

    **刻意不叫「磁盘水位」,也绝不说「盘快满了」。** 剩余空间在这个内核上取不到:
    `pg_stat_file` / `pg_ls_dir` 要 initial account(omm),诊断账号是 sysadmin
    也被拒(实测:"must be initial account to get file information")。
    所以这一维只能回答「占了多少」,回答不了「还剩多少」——
    按一个取不到的事实下「磁盘告警」的结论,正是这套工具一直在修的那类错。

    判定因此只按**绝对量**提示「这个库/表空间很大,注意它所在的盘」,
    并在证据里写明剩余空间不可知。
    """
    try:
        rows = runner.run("health.storage")
    except access.QueryError as exc:
        return degraded(DIM_STORAGE, summarize_err(exc))
    d = DimResult(dimension=DIM_STORAGE, available=True,
                  headers=["类型", "名称", "占用"])
    dbs = tss = 0
    biggest = 0
    for row in rows or []:
        kind = str(row.get("kind") or "")
        name = str(row.get("name") or "")
        n = as_int(row.get("bytes"))
        if kind == "database":
            dbs += 1
        else:
            tss += 1
        biggest = max(biggest, n)
        d.rows.append([("库" if kind == "database" else "表空间"),
                       trunc(name, 32), human_bytes(n)])
        sev, thr = None, 0
        if n > th.storage_warn:
            sev, thr = Severity.WARN, th.storage_warn
        elif n > th.storage_notice:
            sev, thr = Severity.NOTICE, th.storage_notice
        if sev is not None:
            d.findings.append(Finding(
                DIM_STORAGE, "STORAGE_LARGE", sev,
                ("库 " if kind == "database" else "表空间 ") + name,
                human_bytes(n), ">" + human_bytes(thr),
                "pg_%s_size —— 只是占用量；**本内核取不到文件系统剩余空间**"
                "（pg_stat_file 需 initial account），所以这不是「盘快满了」的判定，"
                "请结合操作系统侧的磁盘监控看"
                % ("database" if kind == "database" else "tablespace")))
    if not d.rows:
        d.headline = "取不到占用量"
        return d
    d.headline = ("%d 个库、%d 个表空间，最大 %s（剩余空间不可知，见证据）"
                  % (dbs, tss, human_bytes(biggest)))
    return d


def registry():
    """Ordered (key, collector_fn) list — order is the report's section order.

    Only the 8 dimensions health still collects itself. waits/lwlock/bloat/locks
    are gone — see the module docstring for where they went and health.py's
    `_sub_skill_in_scope` for how --include/--exclude still route those 4 names.
    """
    return [
        ("overview", collect_overview),
        ("slowsql", collect_slowsql),
        ("xact", collect_xact),
        ("conn", collect_conn),
        ("logs", collect_logs),
        ("repl", collect_repl),
        ("schema", collect_schema),
        ("storage", collect_storage),
        ("concurrency", collect_concurrency),
    ]
