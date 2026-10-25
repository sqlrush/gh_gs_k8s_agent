#!/usr/bin/env python3
"""topsql — top resource-consuming statements (no threshold).

Port of internal/probe/topsql.go + internal/cli/topsql.go. Ranks
dbe_perf.statement by a whitelisted sort key (--by).

Usage:
    topsql.py -c <conn> [--by time|avg|calls|reads|rows] [--limit 10] [--format json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break
        
for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break
        
# SQL 已迁到 scripts/registry/topsql/top_sql.yaml —— 两条路径共用同一份定义

import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
from common import reports  # noqa: E402
from common import syssql  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_float, as_int  # noqa: E402
import delta as delta_mod  # noqa: E402
import render  # noqa: E402
import span as span_mod  # noqa: E402
import waits  # noqa: E402
from common import timewin  # noqa: E402

# Whitelisted --by values; ORDER BY clause is injected, so it MUST come from
# this map only (never from user input directly).
_SORT_COLS = {
    "time": "total_elapse_time DESC",
    "avg": "total_elapse_time/NULLIF(n_calls,0) DESC",
    "calls": "n_calls DESC",
    "reads": "n_blocks_hit + n_blocks_fetched DESC",
    "rows": "n_returned_rows DESC",
    # 自身耗时 = CPU + 数据 IO,不含等锁等等待(waits.py)。两列 2026-10-24 才进取数脚本,
    # 老白名单上按它排会报列不存在 —— top_sql() 里接住并说明。
    "self": "cpu_time + data_io_time DESC",
    "self_avg": "(cpu_time + data_io_time)/NULLIF(n_calls,0) DESC",
}
SORT_KEYS = ["time", "avg", "calls", "reads", "rows", "self", "self_avg"]


@dataclass(frozen=True)
class StmtRow:
    sql_id: str
    query: str
    calls: int
    total_sec: float
    avg_ms: float
    rows: int
    # None = 取数脚本里没有这一列(客户还没重导白名单),不是 0 —— 当 0 会把每条都标成「全在等」
    cpu_sec: Optional[float] = None
    io_sec: Optional[float] = None

    @property
    def self_sec(self) -> Optional[float]:
        return waits.self_sec(self.cpu_sec, self.io_sec)

    @property
    def wait_ratio(self) -> Optional[float]:
        return waits.wait_ratio(self.total_sec, self.self_sec)


TOP_SQL_SCRIPT = "topsql.top_sql"

# 脚本 SELECT 列表的列名。用名字取值而不是下标：列序变了会当场 KeyError，
# 而不是安静地把 avg_ms 当成 total_sec。
TOP_SQL_COLUMNS = (
    "unique_sql_id", "query", "calls", "total_sec", "avg_ms", "rows", "cpu_sec", "io_sec",
)


def top_sql(runner, by: str, limit: int) -> list[StmtRow]:
    """经统一入口取数。走中间件还是直连由连接的 driver 决定，这里不感知。"""
    order = _SORT_COLS.get(by)
    if order is None:
        raise ValueError(f"--by {by!r}: must be one of {SORT_KEYS}")
    # order 落在 ORDER BY 位，类型校验对标识符位无效 —— 取值只能来自
    # 上面那张白名单，绝不能直接来自用户输入。
    try:
        rows = runner.run(TOP_SQL_SCRIPT, {"order": order, "limit": int(limit)})
    except access.QueryError as exc:
        if by in waits.SELF_KEYS and ("cpu_time" in str(exc) or "data_io_time" in str(exc)):
            raise ValueError("按自身耗时排要用 2026-10-24 起的 topsql.top_sql(聚合里带 cpu_time / data_io_time);"
                             "这套白名单里还是旧版本,请先重新注册。改用 --by time / avg 仍可用。原始报错:%s"
                             % exc) from exc
        raise
    return [
        StmtRow(r["unique_sql_id"], r["query"], as_int(r["calls"]),
                as_float(r["total_sec"]), as_float(r["avg_ms"]), as_int(r["rows"]),
                # 可选列用 get:老白名单没有它们,缺列要降级成「未知」,不能 KeyError
                as_float(r.get("cpu_sec"), None), as_float(r.get("io_sec"), None))
        for r in rows
    ]


def _screener(runner, include_system: bool):
    """排行用的筛子:(排好序的行, 要几条) → Screening。include_system 时返回 None(不筛)。

    2026-10-22 客户要求:Top SQL 排除 GaussDB 自己的系统 SQL(WDR 快照、监控采集、诊断工具查系统视图),
    报告里写明排除了几条(common/syssql.py)。正文只有 80 字,长语句按 sql_id 补取全文再判。
    """
    if include_system:
        return None
    lookup, cache = syssql.full_text_lookup(runner), syssql.verdict_cache()

    def run(rows, want):
        return syssql.screen(rows, want=want, sql_id=lambda r: r.sql_id, text=lambda r: r.query,
                             shown_chars=QUERY_CHARS, full_text=lookup, seconds=lambda r: r.total_sec,
                             cache=cache)
    return run


def _screen_line(system_sql: Optional[dict]) -> str:
    note = (system_sql or {}).get("note") or ""
    return ("\n> %s要连系统 SQL 一起看,加 `--include-system`。\n" % note) if note else ""


def _sample(runner, at: str) -> "delta_mod.Sample":
    """采一刻的累计值。**按累计耗时取宽一些**(SAMPLE_WIDTH),增量再在这批里算 ——
    累计榜靠后的语句完全可能是本窗口的冲高元凶。"""
    rows = top_sql(runner, "time", delta_mod.SAMPLE_WIDTH)
    return delta_mod.to_sample(at, rows)


def _now() -> str:
    """采样时刻,用**本容器的时钟**(UTC)。

    两次采样都由同一个进程打时间戳,所以相隔多久是准的 —— 这正是增量需要的。
    没有去查数据库的 now():白名单里没有「只返回当前时间」的脚本,而为了一个时间戳
    去加一条下发脚本、让客户重导,不值得。报告里会写明这是采样时刻。
    """
    return reports.utc_stamp()


def _stats_window(runner) -> str:
    """累计值到底覆盖了多长时间 —— 解释一切累计值的前提。

    这是「累计拉平」问题的另一半:就算不算增量,至少要告诉用户「这个榜是过去 23 天的总量」,
    他才知道十分钟的冲高会被摊成什么样。脚本 health.stats_window 早在白名单里。
    """
    try:
        rows = runner.run("health.stats_window", {})
        if not rows:
            return ""
        r = rows[0]
        reset, secs = str(r.get("stats_reset") or ""), as_float(r.get("window_seconds") or -1)
        if reset == "never" or secs < 0:
            return "累计值的起算时间未知(统计从未重置过)"
        if secs >= 86400:
            span = "%.1f 天" % (secs / 86400.0)
        elif secs >= 3600:
            span = "%.1f 小时" % (secs / 3600.0)
        else:
            span = "%d 秒" % int(secs)
        return "下表是**累计值**,自 %s 起算,已累计 %s" % (reset, span)
    except Exception:          # noqa: BLE001 —— 取不到不影响主结果
        return ""


# QUERY 列的长度。截断发生在 SQL 里(topsql.top_sql / snap_statement 的 LEFT(…, 80)),
# 这里只是写明它 —— 原先页脚写「100 字」,实际只有 80。
QUERY_CHARS = 80
_QUERY_FOOT = ("\n> QUERY 只截到 %d 字。要看某条的全文或执行计划，"
               "直接说「看 sql_id <SQL_ID> 的执行计划」。\n" % QUERY_CHARS)


def delta_table(title: str, res: "delta_mod.DeltaResult", by: str) -> str:
    """增量结果的表格。**表头写明是增量**,不让人误读成累计值。"""
    if res.reset:
        return "## %s\n\n%s\n" % (title, res.note)
    if not res.rows:
        return ("## %s\n\n这个窗口里没有语句产生新的耗时(增量为零)。%s\n"
                % (title, res.note))
    body = [[str(i + 1), r.sql_id, str(r.calls), "%.2f" % r.avg_ms, "%.2f" % r.total_sec,
             waits.sec_cell(r.self_sec), waits.share_cell(waits.wait_ratio(r.total_sec, r.self_sec)),
             str(r.rows), ("新" if r.is_new else ""), render.truncate(r.query, QUERY_CHARS)]
            for i, r in enumerate(res.rows)]
    return ("## " + title + "\n\n" +
            render.table(["#", "SQL_ID", "窗口内调用", "窗口内平均(ms)", "窗口内耗时(s)", "窗口内CPU+IO(s)",
                          "等待占比", "窗口内返回行", "窗口内新出现", "QUERY"], body) +
            "\n" + res.note + "\n" + waits.board_note(res.rows, by) + _QUERY_FOOT + _screen_line(res.system_sql))


def stmt_table(title: str, rows: list[StmtRow], by: str = "time") -> str:
    if not rows:
        return (f"## {title}\n\nNo matching statements. "
                f"Check `enable_stmt_track` / lower --threshold.\n")
    # 等待占比 = 1 − (CPU + 数据 IO) / 总耗时:榜首被等锁语句占住时,读榜的人要一眼看出它不是自己慢(waits.py)
    body = [[str(i + 1), r.sql_id, str(r.calls), f"{r.avg_ms:.2f}", f"{r.total_sec:.2f}",
             waits.sec_cell(r.self_sec), waits.share_cell(r.wait_ratio),
             str(r.rows), render.truncate(r.query, QUERY_CHARS)]
            for i, r in enumerate(rows)]
    return ("## " + title + "\n\n" +
            render.table(["#", "SQL_ID", "CALLS", "AVG_MS", "TOTAL_S", "CPU+IO_S", "等待占比", "ROWS", "QUERY"],
                         body) +
            waits.board_note(rows, by) + _QUERY_FOOT)


def _leave_baseline(runner, conn_name: str) -> None:
    """累计榜跑完也留一份基线。

    **这是让「最近 10 分钟」真的能回看的前提。** 累计计数器没有历史,回看只能靠基线;
    而基线原先只在增量模式下才留 —— 于是用户第一次问「最近 10 分钟谁最耗时」时必然
    没有基线可用,只能现场采一小段,体验上像是功能不管用。累计榜本来就要取数,
    顺手多取一次宽采样留下基线,下一次就答得上了。

    失败完全静默:留不下基线不该影响这次的榜单(下次照样会再试)。
    """
    try:
        base_dir = reports.reports_dir()
        bpath = delta_mod.baseline_path(base_dir, reports.instance_key(conn_name))
        if bpath is None:
            return
        delta_mod.save_baseline(bpath, _sample(runner, _now()))
    except Exception:       # noqa: BLE001 —— 基线是锦上添花,任何失败都不能冒泡
        return


_BY_CN = {"time": "耗时", "avg": "平均耗时", "calls": "调用次数", "rows": "返回行数",
          "reads": "读块数", "self": "自身耗时(CPU+IO)", "self_avg": "平均自身耗时(CPU+IO)"}


def _run_span(runner, args, conn_name: str) -> Optional[int]:
    """「某个时间段 / 最近 N 分钟」:选一个**包住**用户时间段的窗口,两端相减,并说清实际窗口。

    返回 None = 这条路走不了(取不到数据库时钟、或一个快照和采样都没有)——
    `--window` 退回老做法(自存基线 / 现场采一小段),`--from` 直接报错说明原因。
    """
    base_dir = reports.reports_dir()
    bpath = delta_mod.baseline_path(base_dir, reports.instance_key(conn_name))
    ck = span_mod.clock(runner)
    if ck is None:
        return None
    try:
        want_from, want_to = timewin.parse_span(args.from_, args.to, args.window, ck.db_now)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.by not in delta_mod.DELTA_SORT_KEYS:
        print("error: 时间段模式下 --by 只能是 %s(%s 的原始值取数脚本里没有,算不出增量)"
              % ("/".join(delta_mod.DELTA_SORT_KEYS), args.by), file=sys.stderr)
        return 2
    hist = delta_mod.load_history(bpath)
    pl = span_mod.plan(runner, want_from, want_to, hist, ck)
    if pl is None:
        return None
    cov = pl.coverage
    if cov.start is None:
        print("## Top SQL（时间段）\n\n口径：%s\n" % cov.note)
        return 0

    live = {}

    def live_sample():
        if "s" not in live:
            live["s"] = _sample(runner, _now())
        return live["s"]

    try:
        before = span_mod.fetch(runner, cov.start, span_mod.START_WIDTH, live_sample)
        after = span_mod.fetch(runner, cov.end, delta_mod.SAMPLE_WIDTH, live_sample)
    except access.QueryError as exc:
        print("error: 取窗口端点失败:%s" % exc, file=sys.stderr)
        return 2
    res = delta_mod.diff(before, after, cov.span_s, by=args.by, limit=args.limit,
                         screen=_screener(runner, args.include_system))
    if "s" in live:                       # 此刻的采样留进历史,下次问「最近」能贴得更近
        delta_mod.save_baseline(bpath, live["s"])

    payload = {"conn": conn_name, "by": args.by, "limit": args.limit, "mode": "span",
               "want_from": want_from.strftime("%Y-%m-%d %H:%M:%S"),
               "want_to": want_to.strftime("%Y-%m-%d %H:%M:%S"),
               "eval_from": cov.start.at.strftime("%Y-%m-%d %H:%M:%S"),
               "eval_to": cov.end.at.strftime("%Y-%m-%d %H:%M:%S"),
               "complete": cov.complete, "span_note": cov.note,
               # 存档索引按它筛时间段(common/reports._window_summary):报告的存档时间是「现在」,
               # 而它说的是另一段时间,不带这个字段,大盘按时间段筛时会找不到它。
               "window": {"begin_ts": cov.start.at.strftime("%Y-%m-%d %H:%M:%S"),
                          "end_ts": cov.end.at.strftime("%Y-%m-%d %H:%M:%S")},
               "window_s": int(cov.span_s), "reset": res.reset,
               "system_sql": res.system_sql,
               "rows": [r._asdict() for r in res.rows]}
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(delta_table(span_mod.title(cov, _BY_CN.get(args.by, args.by)), res, args.by), end="")
        # 口径必须跟着表走:用户问的是 A–B,拿到的是 S–E,不说清楚他会把这张表
        # 当成 A–B 的答案(用户 2026-10-18 定的要求)。
        print("\n口径：%s\n" % cov.note)
    saved = reports.archive("topsql", payload,
                            name="%s.%s.span" % (reports.utc_stamp(), args.by),
                            instance=conn_name, latest=False)
    reports.announce("topsql", saved, conn_name, by=args.by,
                     span=(cov.start.at.strftime("%Y-%m-%dT%H:%M"),
                           cov.end.at.strftime("%Y-%m-%dT%H:%M")))
    return 0


def _run_delta(runner, args, conn_name: str) -> int:
    """增量模式。两条路:现场采两次(--window)或与上次基线相减(--since-last)。

    **存档与累计模式分开**(name 带 `.delta`):大盘按维度页签取最近一份,如果增量报告覆盖了
    累计报告的文件名,历史曲线会把两种口径的数混在一条线上 —— 那种图比没有图更糟。
    """
    base_dir = reports.reports_dir()
    inst_key = reports.instance_key(conn_name)
    bpath = delta_mod.baseline_path(base_dir, inst_key)

    extra = ""
    if args.live_sample:
        # 显式「盯着采 N 秒」。**只有用户点名要等的时候才阻塞**,且有上限。
        try:
            span = delta_mod.parse_window(args.live_sample)
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        if span > delta_mod.MAX_LIVE_EXPLICIT_S:
            print("error: --live-sample 最多 %d 秒（再长会把对话挂死；要看更长的一段，"
                  "先跑一次留下基线，过一会儿再用 --since-last）"
                  % delta_mod.MAX_LIVE_EXPLICIT_S, file=sys.stderr)
            return 2
        before = _sample(runner, _now())
        print("已采第一次样，等 %d 秒后再采一次 …" % span, file=sys.stderr)
        time.sleep(span)
        after = _sample(runner, _now())
        label = "现场采样 %s" % args.live_sample
    elif args.window:
        try:
            want = delta_mod.parse_window(args.window)
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        # 从**采样历史**里挑一刻最合适的 —— 不是「最近那一刻」。
        # 只留一刻的话,连着问两次时第二次读到的基线是刚写下的(年龄 1 秒),
        # 又退回现场采样,回看功能等于永远用不上(实测过)。
        now_at = _now()
        hist = delta_mod.load_history(bpath)
        cached, age = delta_mod.pick_baseline(
            hist, want, lambda s: _span_seconds(s.at, now_at) if s.at else None)
        # age 为 None = 历史里一刻可用的都没有(空文件 / 时间戳都解析不了);
        # 挑不出但 age 有值 = 有采样但都太新或太旧,choose_source 会分别说明是哪一种。
        src = delta_mod.choose_source(want, age)
        extra = src.note
        if src.kind == "baseline" and cached is not None:
            before, after = cached, _sample(runner, now_at)
            span = src.span_s
            label = "最近 %s" % delta_mod.human_span(src.span_s)
        else:
            # **不等满用户问的时长。** 过去回看不了就老实采一小段,并说明这是多长一段。
            if not hist:
                extra = "还没有采样历史,这是第一次对这个实例取数。" + extra
            before = _sample(runner, _now())
            print("无法回看，改为现场采 %d 秒 …" % int(src.wait_s), file=sys.stderr)
            time.sleep(src.wait_s)
            after = _sample(runner, _now())
            span = _span_seconds(before.at, after.at) or src.span_s
            label = "现场采样 %s" % delta_mod.human_span(span)
    else:
        before, why = delta_mod.load_baseline(bpath)
        after = _sample(runner, _now())
        if before is None:
            # 第一次跑:留下基线,**明确告诉用户这次给不出增量**,不要悄悄退回累计榜
            delta_mod.save_baseline(bpath, after)
            print("无法给出增量:%s。\n已留下基线,下次用 `--since-last` 就能算出"
                  "「距上次」的增量;要立刻看最近一段时间,用 `--window 10m`。" % why)
            return 0
        span = max(0.0, _span_seconds(before.at, after.at))
        label = "距上次采样(%s)" % (before.at or "未知时刻")

    try:
        res = delta_mod.diff(before, after, span, by=args.by, limit=args.limit,
                             screen=_screener(runner, args.include_system))
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    # 两刻都存进历史 —— 下次问「最近一段时间」时,靠前那一刻才是能回看的那一刻。
    # 只存 after 的话历史里永远只有「刚刚」,回看不了(实测过)。
    if before is not None and before.at != after.at:
        delta_mod.save_baseline(bpath, before)
    delta_mod.save_baseline(bpath, after)

    payload = {"conn": conn_name, "by": args.by, "limit": args.limit,
               "mode": "delta", "window_s": int(res.span_s), "reset": res.reset,
               "sampled_at": after.at, "baseline_at": before.at,
               "span_note": extra,          # 实际口径,大盘上也要看得到
               "system_sql": res.system_sql,
               "rows": [r._asdict() for r in res.rows]}
    if args.format == "json":
        print(json.dumps(payload["rows"], ensure_ascii=False, indent=2))
        if (res.system_sql or {}).get("note"):
            print(res.system_sql["note"], file=sys.stderr)
    else:
        print(delta_table("Top SQL(%s,按%s)" % (label, args.by), res, args.by), end="")
        # 口径说明要跟着表走 —— 用户问的是「最近 10 分钟」,拿到的可能是「最近 55 秒」,
        # 不说清楚他会把这张表当成那 10 分钟的答案。
        if extra:
            print("\n口径：%s\n" % extra)
    saved = reports.archive("topsql", payload,
                            name="%s.%s.delta" % (reports.utc_stamp(), args.by),
                            instance=conn_name, latest=False)
    reports.announce("topsql", saved, conn_name, by=args.by)
    return 0


def _span_seconds(a: str, b: str) -> float:
    """两个 utc_stamp(`YYYYMMDDTHHMMSSZ`)之间的秒数。解析不了就返回 0,由调用方照实呈现。"""
    import datetime as _dt
    fmt = "%Y%m%dT%H%M%SZ"
    try:
        return (_dt.datetime.strptime(b, fmt) - _dt.datetime.strptime(a, fmt)).total_seconds()
    except (ValueError, TypeError):
        return 0.0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="topsql.py",
                                 description="Top resource-consuming statements")
    ap.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    ap.add_argument("--by", choices=SORT_KEYS, default="time", help="sort key")
    ap.add_argument("--limit", type=int, default=10, help="max rows")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--timeout", type=int, default=None)
    ap.add_argument("--include-system", action="store_true",
                    help="不排除内核系统 SQL(默认排除只访问系统表 / 系统视图 / 系统函数的语句,并写明排除了几条)")
    # 不带这两个开关 = 原来的行为(累计榜)。带了就是**窗口增量**,回答「最近这段时间谁在冲高」。
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--window", default="", metavar="10m",
                   help="回看最近这段时间的增量(10s~6h)。优先拿已有基线相减、**不阻塞**;"
                        "回看不了时现场采一小段(最多 %d 秒)并如实标明实际覆盖多久"
                        % delta_mod.MAX_LIVE_SAMPLE_S)
    g.add_argument("--since-last", action="store_true",
                   help="与上次运行留下的基线相减,给出「距上次」的增量(几乎不耗时)")
    g.add_argument("--live-sample", default="", metavar="60s",
                   help="现场盯着采这么久再出结果(**会阻塞**,最多 %d 秒)。"
                        "只在确实要观察当下时用" % delta_mod.MAX_LIVE_EXPLICIT_S)
    ap.add_argument("--from", dest="from_", default="", metavar="'YYYY-MM-DD HH:MM'",
                    help="某个时间段的起点(数据库所在时区)。窗口取「包住」这个时间段的快照 / 采样 / 此刻,"
                         "并说明实际评估的是哪一段")
    ap.add_argument("--to", default="", metavar="'YYYY-MM-DD HH:MM'",
                    help="时间段终点;不给 = 到此刻")
    args = ap.parse_args(argv)
    cli.apply_session_arg(args)
    if (args.from_ or args.to) and (args.window or args.since_last or args.live_sample):
        print("error: --from/--to 不能与 --window/--since-last/--live-sample 一起用", file=sys.stderr)
        return 2
    if args.to and not args.from_:
        print("error: 给了 --to 就要给 --from", file=sys.stderr)
        return 2

    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        conn_name = common.config.resolved_name(args.conn)
        if args.from_ or args.window:
            rc = _run_span(runner, args, conn_name)
            if rc is not None:
                return rc
            if args.from_:
                print("error: 回看某个时间段要用 WDR 快照,而本实例取不到快照或数据库时钟"
                      "(WDR 未开启 enable_wdr_snapshot,或白名单里还没有 wdr.snapshots / "
                      "health.live_counters 这两条脚本)。累计榜没有时间维度,倒推不了过去。",
                      file=sys.stderr)
                return 2
        if args.window or args.since_last or args.live_sample:
            return _run_delta(runner, args, conn_name)
        screen = _screener(runner, args.include_system)
        fetched = top_sql(runner, args.by, args.limit + (syssql.SCREEN_MARGIN if screen else 0))
        screening = screen(fetched, args.limit) if screen else syssql.keep_all(fetched[:args.limit])
        rows = screening.kept
        system_sql = screening.as_dict() if screen else None
        _leave_baseline(runner, conn_name)
        # conn 一定要带:没有它报告没法归到实例目录,大盘也没法显示这是哪个库
        payload = {"conn": conn_name, "by": args.by, "limit": args.limit,
                   "mode": "cumulative", "system_sql": system_sql,
                   "rows": [r.__dict__ for r in rows]}
        if args.format == "json":
            print(json.dumps(payload["rows"], ensure_ascii=False, indent=2))   # stdout 形状不变
            if screening.note():
                print(screening.note(), file=sys.stderr)
        else:
            # 累计榜必须写明「累计了多久」—— 否则十分钟的冲高被几十天的总量摊平,
            # 而看榜的人完全不知道自己看的是多长时间的总量。
            win = _stats_window(runner)
            if screening.all_excluded():
                print("## Top SQL by %s\n\n%s。\n" % (args.by, screening.all_excluded_text(
                    args.limit + syssql.SCREEN_MARGIN, "按%s排的" % _BY_CN.get(args.by, args.by))))
            else:
                print(stmt_table("Top SQL by " + args.by, rows, args.by), end="")
            print(_screen_line(system_sql), end="")
            if win:
                print("\n> %s。要看最近一段时间的变化,用 `--window 10m` 或 `--since-last`。\n" % win)
        # 存档带 by:大盘按维度页签找最近一份;失败只 warn,不影响输出与退出码
        saved = reports.archive("topsql", payload, name="%s.%s" % (reports.utc_stamp(), args.by),
                                instance=payload["conn"])
        reports.announce("topsql", saved, payload["conn"], by=args.by)
        return 0
    except (ValueError, KeyError, common.DBError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:          # 渲染/协议层的失败也要清楚报出来
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
