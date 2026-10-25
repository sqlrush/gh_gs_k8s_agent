"""The 7 read-only WDR snapshot-delta collectors + the Collect orchestrator.

Port of internal/probe/wdr/{loadprofile,dbstat,topsql,waits,checkpoint,cache,
fileio,wdr}.go. Snapshot ids / top-N are validated ints inlined into the SQL
exactly as the Go version did (fmt.Sprintf %d). Collectors never raise: on query
failure they return degraded(dim, reason).
"""
from __future__ import annotations

import dataclasses

from model import (
    DIM_CACHE, DIM_CHECKPOINT, DIM_DBSTAT, DIM_FILEIO, DIM_LOADPROFILE,
    DIM_TOPSQL, DIM_WAITS, DimResult, Evidence, Finding, Options, Severity,
    degraded, worst,
)
from native import generate_native, load_window
from util import f2, i64, mib, summarize_err, trunc

import common  # resolved on sys.path by the entry script

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/wdr/ —— 两条路径共用同一份定义
# 取数失败只认这一个类型。换一种数据库访问方式时，改的是访问模块，
# 不是这里 —— 详见 common/grmp/errors.py。
from common import access  # noqa: E402
from common import spillnote  # noqa: E402
from common import syssql  # noqa: E402
# 协议回来的值全是字符串,NULL 是空串而不是 None —— 累计计数器的增量**不能**
# 把取数失败悄悄当成 0,那会把「没取到」伪装成「恰好为零」。
from common.grmp.values import as_int, is_null  # noqa: E402



def _i(x) -> int:
    return 0 if x is None else int(x)


# --- 窗口内的真实 DB time ----------------------------------------------------
#
# **为什么不再用 wdr.load_profile(2026-10-17 修):** 那条脚本把 DB time 算成
# snap_summary_statement 里「两端都在的 sql_id」的耗时增量之和:
#
#     FROM e JOIN b USING (sid)     -- 内连接
#
# snap_summary_statement 是**滚动的语句历史**,语句集合每时每刻都在变。窗口中途才出现
# 的语句(在 begin 快照里没有)和窗口结束前就老化出历史的语句,全被这个内连接静默丢掉。
# 压测实测:22 分钟窗口里出现过 147 条语句,内连接只剩 55 条,算出来的 DB time 是
# **2.67 秒**,而内核自己的 DB_TIME 增量是 **23518 秒** —— 差 8800 倍,还不报错,
# 于是负载维度被判成「健康」。
#
# 现在改取 snap_global_instance_time 的 DB_TIME / CPU_TIME 增量(注册脚本
# waitevent.instance_time,**早就在白名单里**,客户不需要重新导入)。它是实例级累计
# 计数器,没有「集合会变」的问题。判定规则与 gaussdb-waitevent/scripts/dbtime.py
# 一致(那边是这套取数的参考实现,连重启判定一起):
#   · 取不到 / 缺项 / 值是 NULL  → 整个维度降级,绝不当 0;
#   · 任一项增量为负            → 窗口跨了实例重启,这个窗口的算术结果不可用。

_DB_TIME = "DB_TIME"
_CPU_TIME = "CPU_TIME"


def window_db_time(runner, opt: Options) -> tuple:
    """窗口内 (DB time 微秒, CPU time 微秒, 不可用原因)。原因非空时前两个值无意义。"""
    try:
        rows = runner.run("waitevent.instance_time", {"b": int(opt.begin), "e": int(opt.end)})
    except access.QueryError as exc:
        return 0, 0, summarize_err(exc)
    parsed = {}
    for r in rows:
        name = str(r.get("stat_name") or "")
        if name not in (_DB_TIME, _CPU_TIME):
            continue
        raw = r.get("delta_us")
        if is_null(raw):
            return 0, 0, "%s 的增量是空值 —— 累计计数器的增量不该有空值,不能当 0 处理" % name
        parsed[name] = as_int(raw)
    missing = [k for k in (_DB_TIME, _CPU_TIME) if k not in parsed]
    if missing:
        return 0, 0, "waitevent.instance_time 没返回 %s" % "、".join(missing)
    if any(v < 0 for v in parsed.values()):
        return 0, 0, ("窗口内有负增量 —— 跨了一次实例重启,计数器已清零,"
                      "这个窗口的 DB time 算出来是假的,不report")
    return parsed[_DB_TIME], parsed[_CPU_TIME], ""


# --- Load Profile ------------------------------------------------------------

def collect_loadprofile(runner, opt: Options) -> DimResult:
    b, e = int(opt.begin), int(opt.end)
    db_time_us, cpu_time_us, why = window_db_time(runner, opt)
    if why:
        return degraded(DIM_LOADPROFILE, why)
    # Top SQL 的「占 DB time%」要拿这个真实值做分母,存一份给它用
    opt.db_time_us, opt.cpu_time_us = db_time_us, cpu_time_us
    try:
        rows = runner.run("wdr.db_summary", {"b": b, "e": e})
    except access.QueryError as exc:
        return degraded(DIM_LOADPROFILE, summarize_err(exc))
    commits, blks_read, blks_hit = (_i(rows[0]["xact_commit"]),
                                _i(rows[0]["blks_read"]),
                                _i(rows[0]["blks_hit"]))

    cpu_pct = 100.0 * cpu_time_us / db_time_us if db_time_us > 0 else 0.0

    # **平均并发活跃会话 = DB time ÷ 窗口墙钟。** 光看「DB time 23517 秒」没人判断得出
    # 这是轻还是重;除以窗口长度就直观了:22 分钟窗口里 23517 秒 ≈ 17.8 个会话一直在忙。
    # 这是个派生量,不设阈值(不知道对端有几个核),但必须摆出来让人能自己判。
    window_s = int(getattr(opt, "window_s", 0) or 0)
    avg_sessions = (db_time_us / 1e6 / window_s) if window_s > 0 else 0.0

    # **表头字面量必须留在 DimResult(headers=[...]) 这个位置上,新列只能往后追加。**
    # 大盘按列名取值,而 tests/test_frontend_contract_units.py 的守卫是从源码里正则抓
    # `headers=[...]` 这个关键字参数来核对列名的。第一版把表头改成了变量再 insert 到中间,
    # 守卫当场报「采集代码的表头里没有 DB time / CPU占DBtime」—— 那正是它该报的:
    # 列名一旦从源码里看不见,页面读不到哪一列就只能等上线才发现。
    # 往后追加还顺带避免列位移,已经在读这几列的消费方不受影响。
    d = DimResult(dimension=DIM_LOADPROFILE, available=True,
                  headers=["DB time(s)", "CPU time(s)", "CPU占DBtime%", "commits", "物理读(块)", "逻辑读(块)"],
                  rows=[[f2(db_time_us / 1e6), f2(cpu_time_us / 1e6), f2(cpu_pct),
                         i64(commits), i64(blks_read), i64(blks_hit)]])
    if avg_sessions > 0:
        d.headers.append("平均并发活跃会话")
        d.rows[0].append(f2(avg_sessions))

    # **CPU 占比很低 = 这个窗口的时间主要花在等待上,不是在干活。**
    # 这条发现存在的理由:本维度原来一条判定都没有,于是永远是 🟢健康 —— 2026-10-17 实测里
    # DB time 23517 秒、CPU 只占 14.9%、锁等待 15371 秒,维度照样显示「健康」,
    # 而读报告的人会据此以为负载正常。
    th = opt.thresholds
    if db_time_us > 0 and cpu_pct < th.cpu_share_notice:
        d.findings.append(Finding(
            DIM_LOADPROFILE, "WDR_DBTIME_WAIT_HEAVY", Severity.NOTICE,
            "CPU 占 DB time",
            f"{cpu_pct:.1f}%", f"<{th.cpu_share_notice:.0f}%",
            f"snap_global_instance_time：DB time {db_time_us / 1e6:.0f}s、"
            f"CPU {cpu_time_us / 1e6:.0f}s —— 时间主要花在等待(锁/IO/睡眠),"
            "真凶看等待事件维度,不是 CPU 不够"))

    d.headline = (f"DB time {db_time_us / 1e6:.1f}s（其中 CPU {cpu_time_us / 1e6:.1f}s="
                  f"{cpu_pct:.0f}%，余为等待/锁/IO/睡眠）"
                  + (f"、平均并发活跃会话 {avg_sessions:.1f}" if avg_sessions > 0 else "")
                  + f"、commits {commits}、物理读 {blks_read} 块、逻辑读 {blks_hit} 块")
    return d


# --- Database Stat -----------------------------------------------------------

# 死锁证据的措辞。**次数是硬数据,「哪几条语句成环」不是** ——
# WDR 快照里只有一个计数器,没有任何关于环的信息。而 methodology 的归因表
# 让模型去 Top SQL 里挑「cpu≈0 的同表 DML」当死锁对,那是**推断**:
# 2026-10-17 压测那次推对了,只因为 faultlab 只让 fl.acct 上的 UPDATE 互相打架、
# 候选只有一条;日志里实际还有**三方环**(id=20 → 21 → 22),按 Top SQL 推只会
# 得出「两条 UPDATE 在抢」。真实生产库上几十个 DML 混着跑,这种推断很不可靠 ——
# 而输出看起来和推对时一模一样。
#
# 所以证据里把边界讲死,并给出唯一权威来源的**精确去处**:数据库日志。
# 本工具读不到日志(pg_read_file 要 initial account,实测被拒),但能告诉用户去哪看、
# 看什么 —— 那比给一个看起来有根据的猜测有用得多。
_DEADLOCK_EV = (
    "snap_summary_stat_database.deadlocks —— **这个数字是硬数据，但「哪几条语句成环」"
    "WDR 里没有**：快照只有一个计数器，没有环的任何信息。"
    "要确定具体的死锁对（环上几个会话、各自持有/等待什么锁、完整语句、谁被选为牺牲者），"
    "唯一权威来源是**数据库日志**：在 `$PGDATA/pg_log/` 下按本窗口的时间段 "
    "grep `deadlock detected`，紧随其后的 DETAIL 段就是完整的环。"
    "本工具读不到日志文件（需 initial account 权限）——**请用户把那几段（连同后面的 DETAIL 几行）贴过来**，"
    "`gaussdb-lockwait --deadlock-log -` 能逐个解析出环、各自的语句与牺牲者，按模式归类并给出改法。"
    "若要从 Top SQL 反推候选语句，**必须标明是候选、不是证据** —— "
    "实测过三方环被反推成「两条语句在抢」的情形。")

def collect_dbstat(runner, opt: Options) -> DimResult:
    b, e = int(opt.begin), int(opt.end)
    try:
        rows = runner.run("wdr.db_stat", {"b": b, "e": e})
    except access.QueryError as exc:
        return degraded(DIM_DBSTAT, summarize_err(exc))
    commits, rollbacks, deadlocks, temp_bytes, blks_hit, blks_read = (
        _i(rows[0][k]) for k in ("xact_commit", "xact_rollback", "deadlocks",
                                 "temp_bytes", "blks_hit", "blks_read"))

    total = commits + rollbacks
    rb_pct = 100.0 * rollbacks / total if total > 0 else 0.0
    cache_hit = 100.0 * blks_hit / (blks_hit + blks_read) if blks_hit + blks_read > 0 else 100.0

    th = opt.thresholds
    opt.temp_bytes = temp_bytes      # 收尾时把溢出归到语句上(_attribute_spill)
    d = DimResult(dimension=DIM_DBSTAT, available=True,
                  headers=["commits", "rollbacks", "回滚率%", "死锁", "临时溢出", "cache_hit%"],
                  rows=[[i64(commits), i64(rollbacks), f2(rb_pct), i64(deadlocks),
                         mib(temp_bytes), f2(cache_hit)]])
    if total >= th.rollback_floor:
        if rb_pct > th.rollback_pct_warn:
            d.findings.append(Finding(DIM_DBSTAT, "WDR_ROLLBACK_RATIO", Severity.WARN,
                                      "回滚率", f2(rb_pct) + "%", f">{th.rollback_pct_warn:.0f}%",
                                      "snap_summary_stat_database xact_commit/rollback"))
        elif rb_pct > th.rollback_pct_notice:
            d.findings.append(Finding(DIM_DBSTAT, "WDR_ROLLBACK_RATIO", Severity.NOTICE,
                                      "回滚率", f2(rb_pct) + "%", f">{th.rollback_pct_notice:.0f}%",
                                      "snap_summary_stat_database xact_commit/rollback"))
    if deadlocks >= th.deadlock_warn:
        d.findings.append(Finding(DIM_DBSTAT, "WDR_DEADLOCK", Severity.WARN, "死锁数",
                                  i64(deadlocks), f"≥{th.deadlock_warn}", _DEADLOCK_EV))
    elif deadlocks >= th.deadlock_notice:
        d.findings.append(Finding(DIM_DBSTAT, "WDR_DEADLOCK", Severity.NOTICE, "死锁数",
                                  i64(deadlocks), f"≥{th.deadlock_notice}", _DEADLOCK_EV))
    if temp_bytes > th.temp_spill_warn:
        d.findings.append(Finding(DIM_DBSTAT, "WDR_TEMP_SPILL", Severity.WARN, "临时文件溢出",
                                  mib(temp_bytes), ">" + mib(th.temp_spill_warn),
                                  "snap_summary_stat_database.temp_bytes"))
    elif temp_bytes > th.temp_spill_notice:
        d.findings.append(Finding(DIM_DBSTAT, "WDR_TEMP_SPILL", Severity.NOTICE, "临时文件溢出",
                                  mib(temp_bytes), ">" + mib(th.temp_spill_notice),
                                  "snap_summary_stat_database.temp_bytes"))
    if cache_hit < th.cache_hit_warn:
        d.findings.append(Finding(DIM_DBSTAT, "WDR_BUFFER_HIT_LOW", Severity.WARN, "缓存命中率",
                                  f2(cache_hit) + "%", f"<{th.cache_hit_warn:.0f}%",
                                  "snap_summary_stat_database blks_hit/read"))
    elif cache_hit < th.cache_hit_notice:
        d.findings.append(Finding(DIM_DBSTAT, "WDR_BUFFER_HIT_LOW", Severity.NOTICE, "缓存命中率",
                                  f2(cache_hit) + "%", f"<{th.cache_hit_notice:.0f}%",
                                  "snap_summary_stat_database blks_hit/read"))
    d.headline = (f"commit {commits} / rollback {rollbacks}（回滚率 {rb_pct:.1f}%）、"
                  f"死锁 {deadlocks}、临时溢出 {mib(temp_bytes)}、命中率 {cache_hit:.1f}%")
    return d


# 存档里单条 SQL 文本的上限。存档会经 HTTP 发给大盘,几百条语句各带几十 KB
# 会把这份 JSON 撑到打不开 —— 超了就截断并留标记,**明确告诉读的人这是截断过的**。
SQL_TEXT_MAX = 4000

# 根因里带的 SQL 片段长度:够认出是哪条语句,又不至于把一行证据撑爆。
SQL_BRIEF = 80

# 「在真干活」最多报几条 —— 一张榜刷出十条发现会把重点冲淡。
CPU_HEAVY_MAX = 2
# 各维度元凶从这么宽的一批里找,显示仍是前 top 条。2026-10-18 测试:落盘 4483.7MB(占语句级落盘 87%)
# 的那条只跑了 259 秒,不在按耗时排的前 10 名里 —— 元凶只在前 10 名里找,报告把溢出归错了语句。
CHAMPION_WIDTH = 200


def _strip_lead_comments(text: str) -> str:
    """剥掉 SQL **开头**的注释,只为显示。

    现场的 SQL 常常在最前面挂注释:ORM 的 `/* app=order-svc */`、批处理的说明、
    或者整段中文说明。表格里那一列只有几十个字,被注释占满之后显示出来的是
    「-- S6 窗口函数 + 大排序落盘(5 张表)…」——看着像一段注释,根本认不出是哪条语句,
    用户会以为报告里没有慢 SQL(2026-10-17 实测就是这样)。

    **必须在压缩空白之前按行剥。** 第一版先把换行压成空格再剥,行注释的边界就没了 ——
    只剥掉了第一个 `--` 和紧跟的一个词,后面几行注释照样留在显示里。

    只剥开头、不动中间和结尾:中间的注释可能是 hint 或业务标记。
    存档里的全文一个字都不剥 —— 要靠它去对现场的 SQL。
    整段都是注释(没有语句)时返回压缩后的原文,免得显示成空白。
    """
    t = str(text or "")
    while True:
        t = t.lstrip()
        if t.startswith("--"):
            nl = t.find("\n")
            if nl < 0:              # 整行都是注释且没有换行 → 后面没有语句了
                break
            t = t[nl + 1:]
            continue
        if t.startswith("/*"):
            end_ = t.find("*/")
            if end_ < 0:
                break
            t = t[end_ + 2:]
            continue
        break
    out = " ".join(t.split())
    return out or " ".join(str(text or "").split())


def _clip_sql(text: str) -> str:
    """存档用的全文:**保留换行**,只压每行里多余的空白、去掉空行。

    原来把整段压成一行(`" ".join(text.split())`)—— 带 `--` 行注释的 SQL 就整条变成了注释,
    存档里的「全文」拿去 --sql-stdin 根本跑不起来(2026-10-19 测试发现,faultlab 的语句都带行注释)。
    """
    lines = [" ".join(ln.split()) for ln in str(text or "").splitlines()]
    t = "\n".join(ln for ln in lines if ln)
    return t if len(t) <= SQL_TEXT_MAX else t[:SQL_TEXT_MAX] + " …（全文已截断）"


def _sql_brief(text: str) -> str:
    t = _strip_lead_comments(text)
    return t if len(t) <= SQL_BRIEF else t[:SQL_BRIEF] + "…"


# --- Top SQL -----------------------------------------------------------------

def collect_topsql(runner, opt: Options) -> DimResult:
    b, e, top = int(opt.begin), int(opt.end), int(opt.top)
    width = max(top, CHAMPION_WIDTH)
    try:
        rows = runner.run("wdr.top_sql", {"b": b, "e": e, "top": width})
    except access.QueryError as exc:
        return degraded(DIM_TOPSQL, summarize_err(exc))

    # each list item: dict-like tuple (id, query, calls, elapsed, cpu, spill, phys)
    fetched = [(str(r["sid"]), str(r["query"]), _i(r["calls"]), _i(r["elapsed_us"]),
                _i(r["cpu_us"]), _i(r["spill_kb"]), _i(r["phys_blocks"]))
               for r in rows]
    # 2026-10-22 客户要求:排除 GaussDB 自己的系统 SQL(common/syssql.py)。这里的正文是全文,
    # 只有被内核截断(track_activity_query_size)的才按 sql_id 补取。榜单、元凶、附录都只看排除后的;
    # 落盘总量仍按全部语句算 —— 系统 SQL 落的盘也是「能归到语句的」。
    screening = syssql.screen(fetched, sql_id=lambda it: it[0], text=lambda it: it[1],
                              full_text=syssql.full_text_lookup(runner), seconds=lambda it: it[3] / 1e6,
                              cache=syssql.verdict_cache())
    wide = screening.kept
    items = wide[:top]                 # 显示与按耗时的判定只看前 top 条;元凶从 wide 里找
    champs = _champions(wide)
    opt.stmt_spill_kb = sum(it[5] for it in fetched)
    opt.stmt_rows_capped = len(fetched) >= width
    by_spill = champs["溢出"]
    opt.spill_champ = ((by_spill[0], by_spill[5], by_spill[0] in {it[0] for it in items})
                       if by_spill and by_spill[5] > 0 else ())
    off_list = _off_list(champs, {it[0] for it in items})

    # **全文留给存档**(显示仍然截断)。根因里提到某条 SQL 时要能给出 sql_id + 全文,
    # 大盘的详情面板和执行计划深挖也靠它。`wdr.top_sql` 返回的是未截断的文本,
    # 所以这件事不需要改白名单。超长的按 SQL_TEXT_MAX 截并留标记 —— 存档要经 HTTP
    # 发给大盘,几百条语句各带几十 KB 会把这份 JSON 撑到没法用。
    texts = getattr(opt, "sql_texts", None)
    if texts is not None:
        for sid, query, *_rest in items + [it for it, _ in off_list]:
            if sid and sid not in texts:
                texts[sid] = _clip_sql(query)

    # **分母必须是窗口的真实 DB time,不能是本榜单的合计(2026-10-17 修)。**
    # 原来拿「Top N 行的 elapsed 之和」当分母,于是 Top N 截断得越狠、每条的占比越高 ——
    # 压测里一条跑了 1 秒的 TRUNCATE 被算成「占 DB time 37.7%」,还触发了
    # 「单条 SQL 占比过高」的告警。真实占比是 1.01s / 23518s ≈ 0.004%。
    # 拿不到真实 DB time 时**改列名**说明分母变了,不假装还是占 DB time。
    db_time_us = int(getattr(opt, "db_time_us", 0) or 0)
    list_total = sum(it[3] for it in items)
    total_elapsed = db_time_us if db_time_us > 0 else list_total
    pct_header = "占DB time%" if db_time_us > 0 else "占本榜合计%"

    d = DimResult(dimension=DIM_TOPSQL, available=True,
                  headers=["sql_id", "calls", "elapsed_s", "cpu_s", "spill_MB", "物理读(块)", pct_header, "query"])
    if not items:
        d.headline = ("窗口内只有内核系统 SQL：" + screening.brief()) if screening.brief() else "窗口内无正增量 SQL"
        return d
    def row(it, mark=""):
        sid, query, calls, elapsed, cpu, spill, phys = it
        pct = 100.0 * elapsed / total_elapsed if total_elapsed > 0 else 0.0
        return [sid, i64(calls), f2(elapsed / 1e6), f2(cpu / 1e6), f2(spill / 1024), i64(phys), f2(pct),
                mark + trunc(_strip_lead_comments(query), 50)]
    d.rows = [row(it) for it in items]
    # 不在前 top 名里的元凶也列出来,标明是榜外的哪一项冠军 —— 不然表里找不到它
    d.rows += [row(it, "〔榜外·%s冠军〕" % "/".join(labels)) for it, labels in off_list]

    th = opt.thresholds
    t_sid, t_query, t_calls, t_elapsed, t_cpu, t_spill, t_phys = items[0]
    top_pct = 100.0 * t_elapsed / total_elapsed if total_elapsed > 0 else 0.0

    def mk(sev: Severity, thr: str):
        d.findings.append(Finding(DIM_TOPSQL, "WDR_TOPSQL_DBTIME", sev, "单条 SQL 占 DB time",
                                  f2(top_pct) + "%", thr,
                                  f"snap_summary_statement：sqlid {t_sid}（{_sql_brief(t_query)}）"
                                  f" elapsed {t_elapsed / 1e6:.1f}s ×{t_calls}"
                                  + (f"，窗口 DB time {total_elapsed / 1e6:.0f}s" if db_time_us > 0
                                     else "，分母是本榜合计(窗口 DB time 取不到)"),
                                  sql_id=t_sid))
    if top_pct > th.top_sql_dbtime_crit:
        mk(Severity.CRITICAL, f">{th.top_sql_dbtime_crit:.0f}%")
    elif top_pct > th.top_sql_dbtime_warn:
        mk(Severity.WARN, f">{th.top_sql_dbtime_warn:.0f}%")
    elif top_pct > th.top_sql_dbtime_notice:
        mk(Severity.NOTICE, f">{th.top_sql_dbtime_notice:.0f}%")

    # **这一条不能挂在占比门槛后面。** 原来写的是 `top_pct > notice and cpu_ratio < …`,
    # 2026-10-17 实测:榜首等了 5963 秒、CPU 0.03 秒,占 DB time 25.4% —— 没够 30%,
    # 于是整个维度一条发现都没出、判成健康。而「等几千秒、CPU 近零」本身就是被锁住的
    # 典型信号,和它占 DB time 多少无关。改成:占比够高**或**绝对耗时够长,都要报。
    cpu_ratio = t_cpu / t_elapsed if t_elapsed > 0 else 0.0
    long_enough = t_elapsed / 1e6 >= th.top_sql_low_cpu_min_elapsed_s

    # **把「真在干活」的语句单独报出来,并指明下一步是看执行计划。**
    # 榜首不一定是锁受害者:CPU 占自身耗时过半说明它在算、在排序、在扫数据 ——
    # 这类才该去 sqltune / explain。两类语句的处置方向相反,报告里必须分开:
    # 2026-10-17 实测那个窗口榜首是锁受害者(CPU 0.0%),而真正该调计划的 S2/S6
    # 排在后面、一条发现都没有,用户只能靠自己翻表。
    # **扫整张榜,不是只看前几名。** 按耗时排序时,锁受害者往往霸占前几位
    # (它们等得最久),真正在干活的语句排在后面:2026-10-17 实测榜前 5 名 CPU 全是 0%,
    # 而 61%/62%/66% 的那三条排在 6~8 位 —— 只看前 3 名就一条都报不出来。
    # 最多报 CPU_HEAVY_MAX 条,免得一张榜刷出十条发现把真正的重点冲淡。
    heavy = 0
    for sid, query, calls, elapsed, cpu, spill, phys in items:
        if heavy >= CPU_HEAVY_MAX:
            break
        if elapsed / 1e6 < th.top_sql_low_cpu_min_elapsed_s:
            continue
        ratio = cpu / elapsed if elapsed > 0 else 0.0
        if ratio < th.top_sql_cpu_heavy_ratio:
            continue
        heavy += 1
        d.findings.append(Finding(
            DIM_TOPSQL, "WDR_TOPSQL_CPU_HEAVY", Severity.NOTICE, "高耗时且 CPU 占主导",
            f"CPU 占其耗时 {ratio * 100:.0f}%（{elapsed / 1e6:.0f}s × {calls} 次）",
            f">{th.top_sql_cpu_heavy_ratio * 100:.0f}%",
            f"snap_summary_statement：sqlid {sid}（{_sql_brief(query)}）—— 在真干活"
            "（算/排序/扫数据），不是被锁住；下一步看执行计划与调优，不要往锁的方向查",
            sql_id=sid))

    if (top_pct > th.top_sql_dbtime_notice or long_enough) and cpu_ratio < th.top_sql_low_cpu_ratio:
        d.findings.append(Finding(
            DIM_TOPSQL, "WDR_TOPSQL_LOW_CPU", Severity.NOTICE, "高 DB time 但 CPU 极低",
            f"CPU 仅占其耗时 {cpu_ratio * 100:.1f}%", f"<{th.top_sql_low_cpu_ratio * 100:.0f}%",
            f"sqlid {t_sid}（{_sql_brief(t_query)}）elapsed {t_elapsed / 1e6:.1f}s / "
            f"cpu {t_cpu / 1e6:.1f}s → "
            "阻塞/睡眠主导(锁/等待/pg_sleep)，非资源消耗；真凶看 CPU time 与等待事件",
            sql_id=t_sid))

    # 各维度元凶：每个资源维度的冠军 SQL 往往不是同一条,而且不一定在按耗时排的前 top 名里。
    by_cpu, by_phys, by_calls = champs["CPU"], champs["物理读"], champs["调用"]
    d.headline = (
        f"各维度元凶 — DB time:{t_sid}({top_pct:.0f}%,CPU占{cpu_ratio * 100:.0f}%) ｜ "
        f"CPU:{by_cpu[0]}({by_cpu[4] / 1e6:.0f}s) ｜ 溢出:{by_spill[0]}({by_spill[5] / 1024:.0f}MB) ｜ "
        f"物理读:{by_phys[0]}({by_phys[6]}块) ｜ 调用:{by_calls[0]}({by_calls[2]}次)；"
        f"元凶从耗时前 {len(wide)} 条里找，表里显示前 {len(items)} 条"
        + (f"，另列 {len(off_list)} 条榜外元凶" if off_list else "")
        + (f"；{screening.brief()}" if screening.brief() else ""))
    return d


# 元凶的维度 → 在 item 元组里的下标
_CHAMP_IDX = (("CPU", 4), ("溢出", 5), ("物理读", 6), ("调用", 2))


def _champions(wide: list) -> dict:
    """每个资源维度的冠军(在取回的整批里找)。并列时取在前面的(耗时更高的)那条。"""
    out = {}
    for label, idx in _CHAMP_IDX:
        best = wide[0] if wide else None
        for it in wide:
            if it[idx] > best[idx]:
                best = it
        out[label] = best
    return out


def _off_list(champs: dict, shown: set) -> list:
    """不在显示的前 N 名里、且该维度确有用量的元凶:[(item, [维度...])],同一条语句只列一次。"""
    out, seen = [], {}
    for label, idx in _CHAMP_IDX:
        it = champs.get(label)
        if it is None or it[0] in shown or it[idx] <= 0:
            continue
        if it[0] in seen:
            seen[it[0]][1].append(label)
            continue
        seen[it[0]] = (it, [label])
        out.append(seen[it[0]])
    return out


# --- Wait Events / Classes ---------------------------------------------------
#
# 等待类之外还要给出**事件名**。faultlab L1(2026-10-24,3149→3150):提交风暴造出的是 WALFlushWait,
# 这一维只给了「LWLOCK_EVENT 50%」一行,看不出是 WAL 相关,只能另跑 waitevent 才知道。
# 事件级增量取 waitevent.events:与 wdr.waits 同源(snap_global_wait_events 两端快照相减)、
# 排除 STATUS/NONE 的谓词逐字一致,两边的总量对得上;它早就在白名单里(gaussdb-waitevent 用),
# 客户不需要重新导入。取不到只影响事件名这一列,等待类照常出。

WAIT_EVENTS_SCRIPT = "waitevent.events"
WAIT_EVENTS_TOP = 30          # 事件取前 30:够每个主要等待类摊到几个,又不把这一维撑大
EVENTS_PER_CLASS = 3


def _top_events(runner, b: int, e: int) -> tuple:
    """(等待类 → [(事件, 微秒)…] 按耗时降序, 取不到的原因)。"""
    try:
        rows = runner.run(WAIT_EVENTS_SCRIPT, {"b": b, "e": e, "top": WAIT_EVENTS_TOP})
    except access.QueryError as exc:
        return {}, summarize_err(exc)
    out: dict = {}
    for r in rows:
        out.setdefault(str(r["wait_class"]), []).append((str(r["event"]), _i(r["wait_us"])))
    return {k: sorted(v, key=lambda x: -x[1]) for k, v in out.items()}, ""


def _events_cell(events: list, why: str) -> str:
    if why:
        return "（取不到）"
    return "、".join(f"{name} {f2(us / 1e6)}s" for name, us in events[:EVENTS_PER_CLASS]) or "—"


def collect_waits(runner, opt: Options) -> DimResult:
    b, e, top = int(opt.begin), int(opt.end), int(opt.top)
    try:
        rows = runner.run("wdr.waits", {"b": b, "e": e, "top": top})
    except access.QueryError as exc:
        return degraded(DIM_WAITS, summarize_err(exc))
    items = [(str(r["wait_class"]), _i(r["waits"]), _i(r["wait_us"])) for r in rows]
    total = sum(it[2] for it in items)
    opt.wait_total_us = total          # 收尾时与 DB time 交叉校验(见 _check_consistency)

    d = DimResult(dimension=DIM_WAITS, available=True,
                  headers=["等待类", "waits", "wait_s", "占比%", "主要事件"])
    if not items:
        d.headline = "窗口内无显著非空闲等待"
        return d
    events, why = _top_events(runner, b, e)
    for cls, waits, usec in items:
        pct = 100.0 * usec / total if total > 0 else 0.0
        d.rows.append([cls, i64(waits), f2(usec / 1e6), f2(pct), _events_cell(events.get(cls, []), why)])
    if why:
        d.note = (f"事件名取不到（{why}），这一维只有等待类聚合；"
                  f"要拆到具体事件，用 gaussdb-waitevent --begin {b} --end {e}")

    th = opt.thresholds
    top_cls = items[0][0]
    top_pct = 100.0 * items[0][2] / total if total > 0 else 0.0
    lead = (events.get(top_cls) or [("", 0)])[0]
    which = (f"；其中最多的是 {lead[0]}（{f2(lead[1] / 1e6)}s）" if lead[0]
             else "；事件名取不到" if why else "")
    evidence = "snap_global_wait_events 等待类聚合" + which
    if top_pct > th.wait_skew_warn:
        d.findings.append(Finding(DIM_WAITS, "WDR_WAIT_CLASS_SKEW", Severity.WARN, "等待类倾斜",
                                  f"{top_cls} {top_pct:.0f}%", f">{th.wait_skew_warn:.0f}%", evidence))
    elif top_pct > th.wait_skew_notice:
        d.findings.append(Finding(DIM_WAITS, "WDR_WAIT_CLASS_SKEW", Severity.NOTICE, "等待类倾斜",
                                  f"{top_cls} {top_pct:.0f}%", f">{th.wait_skew_notice:.0f}%", evidence))
    d.headline = f"Top 等待类 {top_cls} 占 {top_pct:.0f}%" + (f"，最多的事件 {lead[0]}" if lead[0] else "")
    return d


# --- Checkpoint / BgWriter / Redo --------------------------------------------

def collect_checkpoint(runner, opt: Options) -> DimResult:
    b, e = int(opt.begin), int(opt.end)
    try:
        rows = runner.run("wdr.checkpoint", {"b": b, "e": e})
    except access.QueryError as exc:
        return degraded(DIM_CHECKPOINT, summarize_err(exc))
    timed, req = _i(rows[0]["checkpoints_timed"]), _i(rows[0]["checkpoints_req"])
    total = timed + req
    req_pct = 100.0 * req / total if total > 0 else 0.0

    th = opt.thresholds
    d = DimResult(dimension=DIM_CHECKPOINT, available=True,
                  headers=["timed_ckpt", "req_ckpt", "req占比%"],
                  rows=[[i64(timed), i64(req), f2(req_pct)]])
    if total > 0:
        if req_pct > th.ckpt_req_warn:
            d.findings.append(Finding(DIM_CHECKPOINT, "WDR_CKPT_REQ_HIGH", Severity.WARN,
                                      "强制 checkpoint 占比", f2(req_pct) + "%", f">{th.ckpt_req_warn:.0f}%",
                                      "snap_global_bgwriter_stat checkpoints_req/timed"))
        elif req_pct > th.ckpt_req_notice:
            d.findings.append(Finding(DIM_CHECKPOINT, "WDR_CKPT_REQ_HIGH", Severity.NOTICE,
                                      "强制 checkpoint 占比", f2(req_pct) + "%", f">{th.ckpt_req_notice:.0f}%",
                                      "snap_global_bgwriter_stat checkpoints_req/timed"))
    d.headline = f"checkpoint timed {timed} / req {req}（req 占 {req_pct:.0f}%）"
    return d


# --- Cache / Memory ----------------------------------------------------------

def collect_cache(runner, opt: Options) -> DimResult:
    b, e, top = int(opt.begin), int(opt.end), int(opt.top)
    try:
        rows = runner.run("wdr.cache", {"b": b, "e": e, "top": top})
    except access.QueryError as exc:
        return degraded(DIM_CACHE, summarize_err(exc))
    d = DimResult(dimension=DIM_CACHE, available=True, headers=["对象", "物理读(块)", "逻辑读(块)"])
    for r in rows:
        d.rows.append([str(r["snap_relname"]), i64(_i(r["phys_read"])),
                       i64(_i(r["logical_read"]))])
    if not d.rows:
        d.headline = "窗口内无显著物理读对象"
    else:
        d.headline = f"物理读 Top 对象：{d.rows[0][0]}（命中率详见 Database Stat）"
    return d


# --- File IO -----------------------------------------------------------------

def collect_fileio(runner, opt: Options) -> DimResult:
    b, e, top = int(opt.begin), int(opt.end), int(opt.top)
    try:
        rows = runner.run("wdr.file_io", {"b": b, "e": e, "top": top})
    except access.QueryError as exc:
        return degraded(DIM_FILEIO, summarize_err(exc))
    d = DimResult(dimension=DIM_FILEIO, available=True, headers=["文件", "物理读", "物理写"])
    for r in rows:
        d.rows.append([trunc(str(r["filename"]), 40), i64(_i(r["reads"])),
                       i64(_i(r["writes"]))])
    if not d.rows:
        d.headline = "窗口内无显著文件 IO"
    else:
        d.headline = f"物理读 Top 文件：{d.rows[0][0]}"
    return d


# --- orchestrator ------------------------------------------------------------

def registry():
    """Collectors in evidence-pack display order (= report section order)."""
    return [collect_loadprofile, collect_dbstat, collect_topsql, collect_waits,
            collect_checkpoint, collect_cache, collect_fileio]


# 等待时长超过 DB time 这么多倍,就认定 DB time 不可信(留足余量:两个计数器的
# 口径本来就不完全可比 —— DB time 按线程累加,等待事件也按线程累加,但采样边界不同)。
_DB_TIME_SANITY_FACTOR = 1.5


def _check_consistency(ev: Evidence, opt: Options) -> None:
    """跨维度自检:**DB time 比总等待时长还小,就不是「负载很轻」,是这个数不可信。**

    2026-10-17 压测抓到的真实后果:同一份报告里 DB time 写 2.67 秒、锁等待写 15371 秒,
    两者并存本身就说明前者是错的,而当时没有任何提示,负载维度还被判成「健康」,
    看报告的人完全无从察觉。取数的 bug 已经修了(见 window_db_time),这条校验是**兜底**:
    以后不管哪个数据源再出类似问题,至少会明说「这个数不可信」,而不是悄悄给个绿灯。
    """
    db_time_us = int(getattr(opt, "db_time_us", 0) or 0)
    wait_us = int(getattr(opt, "wait_total_us", 0) or 0)
    if db_time_us <= 0 or wait_us <= 0:
        return                      # 有一边没取到,不做这个判断(取不到已各自降级说明)
    if wait_us <= db_time_us * _DB_TIME_SANITY_FACTOR:
        return
    for d in ev.dims:
        if d.dimension != DIM_LOADPROFILE:
            continue
        note = ("DB time(%.1fs)比总等待时长(%.1fs)还小 —— 这个 DB time 不可信,"
                "不要据此判断负载轻重,改看等待事件与原生 WDR。"
                % (db_time_us / 1e6, wait_us / 1e6))
        d.note = (d.note + "；" + note) if d.note else note
        d.findings.append(Finding(
            DIM_LOADPROFILE, "WDR_DBTIME_INCONSISTENT", Severity.WARN,
            "DB time 与等待时长矛盾",
            "DB time %.1fs < 等待 %.1fs" % (db_time_us / 1e6, wait_us / 1e6),
            f">{_DB_TIME_SANITY_FACTOR:g}×",
            "snap_global_instance_time DB_TIME 增量 vs snap_global_wait_events 总等待 —— "
            "两者不可能是这个关系,DB time 取数有问题"))
        ev.findings.extend(d.findings[-1:])
        return


def _attribute_spill(ev: Evidence, opt: Options) -> None:
    """库级溢出那条发现(WDR_TEMP_SPILL)补上「能归到语句的有多少、落盘最多的是哪条」。

    2026-10-18 测试:库级溢出 7.8G,模型把它归到按耗时排的榜首上;实际语句级落盘最多的那条
    (4483.7MB,占语句级的 87%)只跑了 259 秒、不在前 10 名里。库级计数器本身不知道是谁落的盘,
    所以这里写明:语句级合计多少、占库级多少、冠军是谁、在不在榜上;其余部分归不到具体语句。
    两边口径对不上(语句级反而更大)时直说这个比例不可信,不替它编解释。
    """
    db_bytes = int(getattr(opt, "temp_bytes", 0) or 0)
    if db_bytes <= 0 or not any(f.code == "WDR_TEMP_SPILL" for f in ev.findings):
        return
    stmt_bytes = int(getattr(opt, "stmt_spill_kb", 0) or 0) * 1024
    champ = tuple(getattr(opt, "spill_champ", ()) or ())
    off = ("" if not champ or champ[2] else
           f"它**不在按耗时排的前 {opt.top} 名里** —— 按耗时找元凶会漏掉它")
    extra = spillnote.describe(db_bytes, stmt_bytes, (champ[0], champ[1] * 1024) if champ else None,
                               capped=bool(getattr(opt, "stmt_rows_capped", False)), off_list=off)

    def fix(f):
        if f.code != "WDR_TEMP_SPILL":
            return f
        return dataclasses.replace(f, evidence="%s（库级合计）。%s" % (f.evidence, extra),
                                   sql_id=f.sql_id or (champ[0] if champ else ""))

    for d in ev.dims:
        d.findings = [fix(f) for f in d.findings]
    ev.findings = [fix(f) for f in ev.findings]


def collect_evidence(runner, opt: Options) -> Evidence:
    """Run every collector over the window and assemble evidence. Per-collector
    failures degrade; only the window/native prep can fail (propagated)."""
    if opt.top <= 0:
        opt.top = 10
    ev = Evidence()
    ev.window = load_window(runner, opt)            # validate snaps + scope/node + wdr-enabled
    # 窗口墙钟长度给 Load Profile 用:DB time ÷ 墙钟 = 平均并发活跃会话数
    opt.window_s = int(getattr(ev.window, "duration_min", 0) or 0) * 60
    opt.sql_texts = {}                              # 采集过程中由 Top SQL 填,见 _clip_sql
    ev.native = generate_native(runner, opt, ev.window)  # best-effort留底
    for fn in registry():
        d = fn(runner, opt)
        ev.dims.append(d)
        ev.findings.extend(d.findings)
    ev.sql_texts = dict(getattr(opt, "sql_texts", {}) or {})
    _check_consistency(ev, opt)                     # 跨维度兜底,必须在全部维度采完之后
    _attribute_spill(ev, opt)                       # 溢出归到语句:要等 Top SQL 采完
    ev.findings.sort(key=lambda f: int(f.severity), reverse=True)
    ev.overall = worst([f.severity for f in ev.findings])
    ev.sql_plans = collect_plans(runner, ev)        # SQL 附录的执行计划:要等发现排好序
    return ev


PLAN_SCRIPT = "wdr.plan_in_window"
PLAN_MAX = 6                                        # 附录最多取这么多条的计划(按发现的严重度)


def _plan_ts(ts: str, tail: str) -> str:
    """窗口端点 'YYYY-MM-DD HH:MM' → 白名单 DateTime 要的 'YYYY-MM-DD HH:MM:SS'。"""
    t = str(ts or "").strip()[:16]
    return t + tail if len(t) == 16 else ""


def _plan_text(raw) -> str:
    plan = str(raw or "").strip("\n")
    if plan.startswith("Datanode Name:"):           # 第一行是节点名,不是计划
        plan = plan.split("\n", 1)[1] if "\n" in plan else ""
    return plan.rstrip()


def collect_plans(runner, ev: Evidence) -> dict:
    """根因会提到的 sql_id(发现里带的、有全文的)→ 窗口内最近一次真实执行计划。

    2026-10-21 测试:附录的计划原来只靠模型调 explain 取,文本是归一化的就取不到,附录写「本次未取到」,
    而 statement_history 里窗口内 34 次执行都记着真实计划。取不到也记原因,附录如实写。
    白名单里没有这条脚本(旧版本客户)时第一条就失败,后面不再试。
    """
    wanted = []
    for f in ev.findings:
        sid = getattr(f, "sql_id", "") or ""
        if sid and sid in ev.sql_texts and sid not in wanted:
            wanted.append(sid)
    b, e = _plan_ts(ev.window.begin_ts, ":00"), _plan_ts(ev.window.end_ts, ":59")
    if not wanted or not b or not e:
        return {}
    out = {}
    for sid in wanted[:PLAN_MAX]:
        try:
            rows = runner.run(PLAN_SCRIPT, {"sid": int(sid), "begin_time": b, "end_time": e})
        except ValueError:
            continue
        except access.QueryError as exc:
            why = "取 SQL 历史里的计划失败：%s" % summarize_err(exc)
            return dict(out, **{s: {"plan": "", "why": why} for s in wanted[:PLAN_MAX] if s not in out})
        plan = _plan_text(rows[0].get("query_plan")) if rows else ""
        if not plan:
            out[sid] = {"plan": "", "why": "SQL 历史里这个窗口内没有记下它的执行计划"
                                            "（全量 SQL 只保留 track_stmt_retention_time 设定的时长，慢 SQL 才必记计划）"}
            continue
        out[sid] = {"plan": plan, "start_time": str(rows[0].get("start_time") or ""),
                    "db_time_ms": str(rows[0].get("db_time_ms") or ""),
                    "rows": str(rows[0].get("n_returned_rows") or "")}
    return out
