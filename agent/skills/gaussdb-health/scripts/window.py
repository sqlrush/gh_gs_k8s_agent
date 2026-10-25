"""健康检查的「某个时间段 / 最近 N 分钟」—— 时段体检。

原则(用户 2026-10-18 定):**覆盖住用户说的时间段即可,并告诉用户实际评估的是哪一段、为什么。**
端点怎么选、说明怎么写在 common/timewin.py;这里负责取数、相减、判定。

**能回看的**(累计计数器,两个时刻相减):
  负载    DB time、CPU、平均活跃会话
  事务    提交、回滚、回滚率、死锁
  IO      缓存命中、临时溢出、检查点
  等待    各等待类的次数与时长
  Top SQL 交给 gaussdb-topsql --from/--to(子进程,与其他子 skill 同一个做法)

**回看不了的**(瞬时量,当时没有采):连接与线程池、锁阻塞链、长事务、死元组与膨胀、
冻结水位、复制、占用量。窗口终点是此刻时给**当前值**并标明;终点在过去时**明列回看不了** ——
不留空白,空白会被读成「这项查过了、没问题」。

**等待时间不除以 DB time。** 等待事件统计包含后台线程(刷盘、WAL 写入……)的等待,DB time
只算前台会话,两者口径不同,相除可以超过 100%。2026-10-18 那次测试里,空闲库上 LWLOCK
「占 DB time 171.8%」、加上 CPU 的 78.6% 合计 250%,健康检查却据此给了 🟠 总体告警。所以这里:
  · DB time 构成只拆 CPU / 非 CPU(同一来源 instance_time,两者之和就是 DB time);
  · 等待类分布只算「占总等待时长」。

**空闲窗口不判比例。** 平均活跃会话低于 IDLE_SESSIONS 时(og5 那几个小时窗口每小时 DB time
只有 0.2 秒),任何比例都是噪声 —— 一次 0.03 秒的后台锁等待就能「占」一大半。
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from common import access, spillnote, syssql, timewin, windata
from common.finding import findings_from_json

from model import (DIM_WIN_COVER, DIM_WIN_IO, DIM_WIN_LOAD, DIM_WIN_TOPSQL, DIM_WIN_TXN,
                   DIM_LOCKS, DIM_WIN_WAITS, DimResult, Finding, HealthEvidence, Severity, worst)
from util import human_bytes

SNAP_COUNTERS = "health.snap_counters"
LIVE_COUNTERS = "health.live_counters"
SNAP_WAITS = "health.snap_waits"
LIVE_WAITS = "health.live_waits"
# 溢出归到语句:复用 WDR 的 Top SQL 取数(已在白名单,带 spill_kb 列),只在两端都是快照时可用
WDR_TOP_SQL = "wdr.top_sql"
SPILL_WIDTH = 200

_NUM_KEYS = ("db_time_us", "cpu_time_us", "xact_commit", "xact_rollback", "deadlocks",
             "temp_bytes", "blks_read", "blks_hit", "ckpt_timed", "ckpt_req")

# 平均活跃会话低于这个数就不判比例(见模块说明)。0.05 = 窗口里平均只有 1/20 个会话在干活。
IDLE_SESSIONS = 0.05

# 空闲类等待:会话在等客户端发下一条命令("wait cmd")。og5 故障窗口里它占了全部等待的 65%,
# 不剔除的话「等待最多的是 STATUS」会盖过真正的锁等待。脚本里已经剔除(health.snap_waits /
# live_waits);这里再剔一次,防现场导的是没剔的旧版本。
IDLE_WAIT_CLASSES = frozenset({"STATUS", "NONE"})


@dataclass(frozen=True)
class WinThresholds:
    """时段阈值。**与 gaussdb-wdr 的窗口阈值一致** —— 同一种分析,不该同一段时间两处结论不同。"""
    cpu_share_notice: float = 30        # CPU 占 DB time 低于此 → 时间主要花在等待上
    rollback_pct_notice: float = 5
    rollback_pct_warn: float = 15
    deadlock_notice: int = 1
    deadlock_warn: int = 5
    cache_hit_notice: float = 99
    cache_hit_warn: float = 95
    temp_notice: int = 512 << 20
    temp_warn: int = 2 << 30
    ckpt_req_notice: float = 30
    ckpt_req_warn: float = 50
    wait_skew_pct: float = 60           # 某一类等待占总等待超过此
    min_blocks: int = 10000             # 读块总数低于此不判命中率(样本太少)


# 回看不了的瞬时维度:终点在过去时逐一列出,说清为什么。
_INSTANT = [
    ("Connections", "连接数与线程池用量是此刻的状态"),
    ("Long & Idle Transactions", "长事务与空闲事务是此刻的状态，快照里没有事务开始时间"),
    ("Dead Tuples & Bloat", "死元组与膨胀是此刻的状态；快照里的死元组数不可靠（实测 540 万记成 0）"),
    ("Replication / Standby", "复制延迟是此刻的状态"),
    ("Storage Usage", "占用量是此刻的状态"),
]


# ---------------------------------------------------------------- 取数

def _kv(rows) -> Dict[str, float]:
    out = {}
    for r in rows or []:
        k = r.get("item")
        if k in _NUM_KEYS:
            try:
                out[k] = float(r.get("value") or 0)
            except (TypeError, ValueError):
                continue
    return out


def _waits(rows) -> Dict[Tuple[str, str], Tuple[float, float]]:
    out = {}
    for r in rows or []:
        if str(r.get("wait_class") or "").upper() in IDLE_WAIT_CLASSES:
            continue
        try:
            out[(str(r["wait_class"]), str(r["event"]))] = (float(r["waits"] or 0),
                                                            float(r["wait_us"] or 0))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch(runner, point: timewin.Point):
    """一个端点的(计数器, 等待事件)。"""
    if point.kind == "snapshot":
        c = runner.run(SNAP_COUNTERS, {"snap": int(point.ref)})
        w = runner.run(SNAP_WAITS, {"snap": int(point.ref)})
    else:
        c = runner.run(LIVE_COUNTERS, {})
        w = runner.run(LIVE_WAITS, {})
    return _kv(c), _waits(w)


class Delta(dict):
    """计数器增量;reset=True 表示有计数器变小(窗口内统计被清零过,增量不可用)。"""
    reset = False


def subtract(a: Dict[str, float], b: Dict[str, float]) -> Delta:
    d = Delta()
    for k in _NUM_KEYS:
        if k in a and k in b:
            d[k] = b[k] - a[k]
            if d[k] < 0:
                d.reset = True
    return d


def wait_delta(a, b) -> Dict[str, Dict]:
    """按等待类汇总的增量:{类: {waits, wait_us, events: [(事件, 时长)]}}。"""
    out: Dict[str, Dict] = {}
    for key, (n2, t2) in b.items():
        n1, t1 = a.get(key, (0.0, 0.0))
        dn, dt = n2 - n1, t2 - t1
        if dn <= 0 and dt <= 0:
            continue
        c = out.setdefault(key[0], {"waits": 0.0, "wait_us": 0.0, "events": []})
        c["waits"] += max(0.0, dn)
        c["wait_us"] += max(0.0, dt)
        c["events"].append((key[1], max(0.0, dt)))
    return out


# ---------------------------------------------------------------- 各维度

def _pct(a, b) -> Optional[float]:
    return 100.0 * a / b if b > 0 else None


def dim_cover(cov: timewin.Coverage) -> DimResult:
    d = DimResult(dimension=DIM_WIN_COVER, available=True, headers=["项", "值"])
    d.headline = cov.note
    d.rows = [["要看的时段", "%s → %s" % (cov.want_from.strftime("%Y-%m-%d %H:%M:%S"),
                                        cov.want_to.strftime("%Y-%m-%d %H:%M:%S"))],
              ["实际评估", "%s → %s" % (cov.start.label(), cov.end.label())],
              ["窗口长度", "%.1f 分钟" % (cov.span_s / 60.0)],
              ["是否完整覆盖", "是" if cov.complete else "**否**（见上面的说明）"]]
    return d


def dim_load(dl: Delta, span_s: float, th: WinThresholds) -> Tuple[DimResult, bool]:
    """返回 (维度, 是否空闲)。空闲时后面所有比例类判定都不做。"""
    d = DimResult(dimension=DIM_WIN_LOAD, available=True, headers=["指标", "值"])
    db, cpu = dl.get("db_time_us", 0) / 1e6, dl.get("cpu_time_us", 0) / 1e6
    aas = db / span_s if span_s > 0 else 0.0
    share = _pct(cpu, db)
    d.rows = [["DB time", "%.2f 秒" % db], ["CPU time", "%.2f 秒" % cpu],
              ["CPU 占 DB time", "—" if share is None else "%.1f%%" % share],
              ["平均活跃会话", "%.3f" % aas]]
    idle = aas < IDLE_SESSIONS
    if idle:
        d.headline = ("窗口几乎空闲：平均活跃会话 %.3f（低于 %.2f），这段时间库基本没在干活 —— "
                      "比例类指标（CPU 占比、等待分布、命中率）在这么小的分母上是噪声，本次不据此判定"
                      % (aas, IDLE_SESSIONS))
        return d, True
    d.headline = "DB time %.1f 秒、平均活跃会话 %.2f%s" % (
        db, aas, "" if share is None else "、CPU 占 %.0f%%" % share)
    if share is not None and share < th.cpu_share_notice:
        d.findings.append(Finding(
            DIM_WIN_LOAD, "WIN_WAIT_HEAVY", Severity.NOTICE, "CPU 占 DB time", "%.1f%%" % share,
            "<%.0f%%" % th.cpu_share_notice,
            "DB time %.1f 秒里 CPU 只有 %.1f 秒 —— 时间主要花在等待上（锁 / IO / 睡眠），"
            "真凶看下面的「时段等待」，不是 CPU 不够" % (db, cpu)))
    return d, False


def dim_txn(dl: Delta, th: WinThresholds, idle: bool) -> DimResult:
    d = DimResult(dimension=DIM_WIN_TXN, available=True, headers=["指标", "值"])
    cm, rb, dl_n = dl.get("xact_commit", 0), dl.get("xact_rollback", 0), dl.get("deadlocks", 0)
    rp = _pct(rb, cm + rb)
    d.rows = [["提交", "%d" % cm], ["回滚", "%d" % rb],
              ["回滚率", "—" if rp is None else "%.2f%%" % rp], ["死锁", "%d" % dl_n]]
    d.headline = "提交 %d / 回滚 %d%s、死锁 %d" % (cm, rb, "" if rp is None else "（%.2f%%）" % rp, dl_n)
    if dl_n >= th.deadlock_notice:
        sev = Severity.WARN if dl_n >= th.deadlock_warn else Severity.NOTICE
        d.findings.append(Finding(
            DIM_WIN_TXN, "WIN_DEADLOCK", sev, "时段内死锁", "%d 次" % dl_n, ">=%d" % th.deadlock_notice,
            "pg_stat_database.deadlocks 在窗口两端的差。**次数是硬数据，环上是哪几条语句这里没有**；"
            "把这段时间数据库日志里 `deadlock detected` 那几段贴给我，`gaussdb-lockwait --deadlock-log -` "
            "可以逐个解析出环、语句与牺牲者"))
    if rp is not None and not idle and rp > th.rollback_pct_notice:
        sev = Severity.WARN if rp > th.rollback_pct_warn else Severity.NOTICE
        d.findings.append(Finding(
            DIM_WIN_TXN, "WIN_ROLLBACK_HIGH", sev, "时段回滚率", "%.1f%%" % rp,
            ">%.0f%%" % th.rollback_pct_notice,
            "窗口内 %d 次回滚 / %d 次提交 —— 应用异常、约束冲突或死锁牺牲都会推高它" % (rb, cm)))
    return d


def dim_io(dl: Delta, th: WinThresholds, idle: bool) -> DimResult:
    d = DimResult(dimension=DIM_WIN_IO, available=True, headers=["指标", "值"])
    rd, hit = dl.get("blks_read", 0), dl.get("blks_hit", 0)
    hp = _pct(hit, rd + hit)
    tmp = dl.get("temp_bytes", 0)
    ct, cr = dl.get("ckpt_timed", 0), dl.get("ckpt_req", 0)
    crp = _pct(cr, ct + cr)
    d.rows = [["缓存命中率", "—" if hp is None else "%.2f%%" % hp], ["物理读（块）", "%d" % rd],
              ["临时文件溢出", human_bytes(int(tmp))],
              ["checkpoint 定时 / 请求", "%d / %d" % (ct, cr)]]
    d.headline = "命中率 %s、溢出 %s、checkpoint %d 次" % (
        "—" if hp is None else "%.1f%%" % hp, human_bytes(int(tmp)), int(ct + cr))
    if tmp > th.temp_notice:
        sev = Severity.WARN if tmp > th.temp_warn else Severity.NOTICE
        d.findings.append(Finding(
            DIM_WIN_IO, "WIN_TEMP_SPILL", sev, "时段临时文件溢出", human_bytes(int(tmp)),
            ">" + human_bytes(th.temp_notice),
            "排序 / 哈希放不进 work_mem 时落盘。**这是库级合计**（pg_stat_database.temp_bytes）"))
    if hp is not None and not idle and rd + hit >= th.min_blocks and hp < th.cache_hit_notice:
        sev = Severity.WARN if hp < th.cache_hit_warn else Severity.NOTICE
        d.findings.append(Finding(
            DIM_WIN_IO, "WIN_CACHE_LOW", sev, "时段缓存命中率", "%.2f%%" % hp,
            "<%.0f%%" % th.cache_hit_notice,
            "窗口内物理读 %d 块、命中 %d 块。刚重启过的实例缓存是冷的，命中率偏低属正常" % (rd, hit)))
    if crp is not None and ct + cr >= 3 and crp > th.ckpt_req_notice:
        sev = Severity.WARN if crp > th.ckpt_req_warn else Severity.NOTICE
        d.findings.append(Finding(
            DIM_WIN_IO, "WIN_CKPT_PRESSURE", sev, "时段 checkpoint 请求占比", "%.0f%%" % crp,
            ">%.0f%%" % th.ckpt_req_notice,
            "请求型 checkpoint 多说明 WAL 产生得快、等不到定时触发"))
    return d


def dim_waits(wd: Dict[str, Dict], th: WinThresholds, idle: bool) -> DimResult:
    """等待类分布。**只算占总等待**,不除以 DB time(见模块说明)。"""
    d = DimResult(dimension=DIM_WIN_WAITS, available=True,
                  headers=["等待类", "等待次数", "等待时长(秒)", "占总等待", "主要事件"])
    total = sum(c["wait_us"] for c in wd.values())
    for cls, c in sorted(wd.items(), key=lambda kv: kv[1]["wait_us"], reverse=True):
        top = sorted(c["events"], key=lambda e: e[1], reverse=True)[:3]
        d.rows.append([cls, "%d" % c["waits"], "%.2f" % (c["wait_us"] / 1e6),
                       "—" if total <= 0 else "%.1f%%" % (100.0 * c["wait_us"] / total),
                       "、".join("%s %.1fs" % (e, t / 1e6) for e, t in top if t > 0) or "—"])
    if not d.rows:
        d.headline = "窗口内没有等待"
        return d
    lead = d.rows[0]
    d.headline = ("等待最多的是 %s（%s 秒，占总等待 %s）。占比是「占全部等待时长」，"
                  "**不是占 DB time** —— 等待统计包含后台线程，与 DB time 不同口径"
                  % (lead[0], lead[2], lead[3]))
    if not idle and total > 0:
        cls, c = max(wd.items(), key=lambda kv: kv[1]["wait_us"])
        share = 100.0 * c["wait_us"] / total
        if share > th.wait_skew_pct:
            d.findings.append(Finding(
                DIM_WIN_WAITS, "WIN_WAIT_SKEW", Severity.NOTICE, "时段等待集中在 %s" % cls,
                "%.0f%%" % share, ">%.0f%%" % th.wait_skew_pct,
                "占全部等待时长的比例（含后台线程）。要拆到具体事件与会话，用同一时段的 WDR 或 gaussdb-waitevent"))
    return d


# 终点在过去时,报告顶部「本次未采集到的维度」的说明(子 skill 只看此刻,不是被 --include 排除的)
PAST_SUB_NOTE = ("回看过去的时段：锁阻塞改从活跃会话采样（ASP）回看，见「%s」；膨胀这类子 skill 只能看此刻，"
                 "本次没有运行 —— 对应的维度在下面逐项标了「回看不了」和原因；等待分布见「%s」。"
                 % (DIM_LOCKS, DIM_WIN_WAITS))


def dims_unavailable_instant() -> List[DimResult]:
    return [DimResult(dimension=name, available=False,
                      note="这段时间当时没有采集，回看不了：%s。要看这一项，只能在出问题的当时巡检" % why,
                      headline="回看不了")
            for name, why in _INSTANT]


# ---------------------------------------------------------------- Top SQL(子进程)

def topsql_dim(cov: timewin.Coverage, top: int, script_path, timeout: int,
               conn: str = "") -> DimResult:
    """交给 gaussdb-topsql 算同一个窗口。**传的是实际评估窗口**,两边用的是同一段时间。

    不传 env —— 子进程继承会话句柄(GSDB_SESSION)与中间件令牌,与 aggregate.py 同一个做法。
    """
    argv = [sys.executable, str(script_path), "--format", "json", "--limit", str(top),
            "--from", cov.start.at.strftime("%Y-%m-%d %H:%M:%S")]
    if cov.end.kind != "live":
        argv += ["--to", cov.end.at.strftime("%Y-%m-%d %H:%M:%S")]
    if conn:
        argv += ["-c", conn]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 15)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return DimResult(dimension=DIM_WIN_TOPSQL, available=False,
                         note="gaussdb-topsql 没取到这个时段的 Top SQL", headline="不可用")
    d = DimResult(dimension=DIM_WIN_TOPSQL, available=True,
                  headers=["sql_id", "窗口内调用", "窗口内耗时(s)", "平均(ms)", "新出现", "query"])
    for r in data["rows"][:top]:
        d.rows.append([str(r.get("sql_id", "")), "%d" % int(r.get("calls") or 0),
                       "%.2f" % float(r.get("total_sec") or 0), "%.2f" % float(r.get("avg_ms") or 0),
                       "新" if r.get("is_new") else "", str(r.get("query") or "")[:80]])
    d.headline = ("按耗时排的前 %d 条。要看全文、执行计划或调优，直接说「看 sql_id … 的执行计划」"
                  % len(d.rows)) if d.rows else "窗口内没有语句产生新的耗时"
    brief = str((data.get("system_sql") or {}).get("brief") or "")     # topsql 已排除内核系统 SQL
    if brief:
        d.headline += "；" + brief
    return d


# ---------------------------------------------------------------- 锁阻塞(子进程,活跃会话采样)

LOCK_CHAIN_ROWS = 8


def lock_dim(cov: timewin.Coverage, script_path, timeout: int, conn: str = "") -> DimResult:
    """过去时段的锁阻塞:交给 gaussdb-lockwait --from/--to 读活跃会话采样(ASP),同一个评估窗口。

    原来这一维标「回看不了：锁释放后不留痕迹」—— 不准确:锁视图确实不留,但 ASP 每个样本带
    block_sessionid(2026-10-21 测试发现)。样本在两处:最近几个小时只在内存里(dbe_perf.local_active_session),
    内存写满才落进 gs_asp、保留 asp_retention_days 天;lockwait 两处一起读(2026-10-24 起,只读 gs_asp 时
    刚过去的那段一律误报「回看不了」,这一维跟着不可用),见 lockwait/aspchain.py。
    ASP 没开 / 过了保留期 / 两处都没覆盖到 / 白名单缺脚本时,如实标回看不了并给原因,不当成「没有锁等待」。
    """
    argv = [sys.executable, str(script_path), "--format", "json",
            "--from", cov.start.at.strftime("%Y-%m-%d %H:%M:%S"),
            "--to", cov.end.at.strftime("%Y-%m-%d %H:%M:%S")]
    if conn:
        argv += ["-c", conn]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 15)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
        err = (proc.stderr or "").strip().splitlines()[-1:] if proc.returncode else []
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        data, err = None, [str(exc)]
    asp = data.get("asp") if isinstance(data, dict) else None
    if not isinstance(asp, dict):
        why = err[0].replace("error: ", "", 1) if err else "gaussdb-lockwait 没给出结果"
        return DimResult(dimension=DIM_LOCKS, available=False, headline="不可用",
                         note="锁阻塞要从活跃会话采样回看，这次没取到：%s" % why)
    if asp.get("unavailable"):
        return DimResult(dimension=DIM_LOCKS, available=False, headline="回看不了",
                         note=str(asp["unavailable"]))
    return _lock_dim_from(asp, findings_from_json(json.dumps(data)))


def _lock_dim_from(asp: dict, findings: list) -> DimResult:
    d = DimResult(dimension=DIM_LOCKS, available=True, note=str(asp.get("basis") or ""),
                  headers=["阻塞链（等待者 → … → 根）", "根会话状态", "锁 / 对象", "约等了", "时段", "链头语句"],
                  findings=list(findings))
    roots = {r.get("sessionid"): r for r in asp.get("roots") or []}
    chains = asp.get("chains") or []
    for c in chains[:LOCK_CHAIN_ROWS]:
        path = [str(x) for x in c.get("path") or []]
        root = roots.get((c.get("path") or [None])[-1]) or {}
        lock = " ".join(x for x in (c.get("lock"), c.get("lock_object")) if x)
        q = str(c.get("query") or "")
        d.rows.append([" → ".join(path), str(root.get("state") or "未采到"), lock,
                       "%.0fs" % float(c.get("wait_s") or 0),
                       "%s – %s" % (str(c.get("first") or "")[11:], str(c.get("last") or "")[11:]),
                       ("%s %s" % (c.get("sql_id") or "", q[:60])).strip()])
    if not chains:
        d.headline = "时段内 %s 个活跃会话样本里没有采到锁等待（短于采样间隔的等待可能采不到）" % asp.get("samples", "?")
        return d
    waiters = {p.get("waiter") for p in asp.get("pairs") or []}
    top = max(chains, key=lambda c: float(c.get("wait_s") or 0))
    r0 = max(asp.get("roots") or [{}], key=lambda r: int(r.get("blocked") or 0))
    d.headline = ("活跃会话采样回看：%d 条阻塞链、%d 个会话被挡，最长约 %.0f 秒；最大的根是会话 %s（%s，挡住 %s 个）"
                  % (len(chains), len(waiters), float(top.get("wait_s") or 0), r0.get("sessionid", "?"),
                     r0.get("state") or "状态未采到", r0.get("blocked", "?")))
    return d


# ---------------------------------------------------------------- 汇总

def collect(runner, cov: timewin.Coverage, th: WinThresholds = WinThresholds()) -> HealthEvidence:
    """时段体检的可回看部分。瞬时维度与 Top SQL 由调用方按终点类型补。"""
    ev = HealthEvidence()
    ev.dims.append(dim_cover(cov))
    try:
        c0, w0 = fetch(runner, cov.start)
        c1, w1 = fetch(runner, cov.end)
    except access.QueryError as exc:
        ev.dims.append(DimResult(dimension=DIM_WIN_LOAD, available=False,
                                 note="取窗口端点失败：%s" % exc, headline="不可用"))
        return ev
    dl = subtract(c0, c1)
    if dl.reset:
        ev.dims.append(DimResult(
            dimension=DIM_WIN_LOAD, available=False, headline="不可用",
            note="窗口内有计数器变小 —— 统计被重置过（pg_stat_reset 或实例重启），"
                 "这段时间的增量算不出来，**不是「没有活动」**"))
        return ev
    load, idle = dim_load(dl, cov.span_s, th)
    io = attribute_spill(runner, cov, dim_io(dl, th, idle), dl.get("temp_bytes", 0))
    ev.dims += [load, dim_txn(dl, th, idle), io, dim_waits(wait_delta(w0, w1), th, idle)]
    return ev


def _spill_extra(runner, cov: timewin.Coverage, temp_bytes: float) -> Tuple[str, str]:
    """(证据补充, 冠军 sql_id)。2026-10-19 测试:时段体检只说「库级 7.8G」,模型据此点了两条聚合查询
    当候选,处置也指向它们;真正落盘 87% 的是另一条、不在按耗时排的榜上。"""
    if cov.start.kind != "snapshot" or cov.end.kind != "snapshot":
        return ("终点是此刻：语句级落盘只能在两个快照之间算，这一段归不到具体语句；要定位是哪条语句落的盘，"
                "对包住这段时间的两个快照跑 WDR（按落盘量排的冠军不一定在按耗时排的榜上）", "")
    try:
        rows = runner.run(WDR_TOP_SQL, {"b": int(cov.start.ref), "e": int(cov.end.ref), "top": SPILL_WIDTH})
    except access.QueryError:
        return "取语句级落盘失败，归不到具体语句；用同一时段的 WDR 定位", ""
    items, pool = [], []
    for r in rows or []:
        try:
            it = (str(r["sid"]), float(r.get("spill_kb") or 0) * 1024)
        except (KeyError, TypeError, ValueError):
            continue
        items.append(it)
        # 冠军不从内核系统 SQL 里挑(2026-10-22 客户要求);它们的落盘仍算进「能归到语句的」总量
        q = str(r.get("query") or "")
        if syssql.classify(q, not syssql.looks_cut(q)) != syssql.SYSTEM:
            pool.append(it)
    stmt = sum(b for _, b in items)
    champ = max(pool, key=lambda x: x[1]) if pool else None
    return (spillnote.describe(temp_bytes, stmt, champ, capped=len(items) >= SPILL_WIDTH),
            champ[0] if champ and champ[1] > 0 and stmt > 0 else "")


def attribute_spill(runner, cov: timewin.Coverage, d: DimResult, temp_bytes: float) -> DimResult:
    """WIN_TEMP_SPILL 补上「能归到语句的有多少、落盘最多的是哪条」(措辞与 WDR 同一份:common/spillnote)。"""
    if not any(f.code == "WIN_TEMP_SPILL" for f in d.findings):
        return d
    extra, sid = _spill_extra(runner, cov, temp_bytes)
    d.findings = [dataclasses.replace(f, evidence="%s。%s" % (f.evidence, extra), sql_id=f.sql_id or sid)
                  if f.code == "WIN_TEMP_SPILL" else f for f in d.findings]
    return d


def finish(ev: HealthEvidence) -> HealthEvidence:
    ev.findings = [f for d in ev.dims for f in d.findings]
    ev.overall = worst([f.severity for f in ev.findings])
    return ev


def plan(runner, from_text: str, to_text: str, window_text: str):
    """(Coverage, Clock) 或抛 ValueError(说明为什么做不了)。"""
    ck = windata.clock(runner, with_stats_reset=True)
    if ck is None:
        raise ValueError("取不到数据库时钟（白名单里还没有 health.live_counters 这条脚本？），"
                         "时段体检做不了；不带时间参数的普通巡检照常可用")
    want_from, want_to = timewin.parse_span(from_text, to_text, window_text, ck.db_now)
    pts = windata.snapshot_points(runner)
    if not pts:
        raise ValueError("取不到任何 WDR 快照（enable_wdr_snapshot 未开启，或快照已全部过期），"
                         "累计计数器没有历史，回看不了这段时间；不带时间参数的普通巡检照常可用")
    return timewin.cover(want_from, want_to, pts, ck.db_now, ck.reset_at), ck
