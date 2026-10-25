"""aspchain —— 时段锁回溯：从活跃会话采样（ASP）还原一段时间里谁挡了谁。

只读，**不生成 kill 语句**：时段已经过去，当时的会话可能已结束，会话号也可能被复用；
要处置先跑此刻的 lockwait 核对。

**样本在两个地方。** ASP 每 asp_sample_interval 秒采一次，先放在内存里（dbe_perf.local_active_session，
最多 asp_sample_num 个），**内存写满才**把其中每 asp_flush_rate 个落 1 个进 gs_asp（保留 asp_retention_days 天）。
所以最近几个小时只在内存里 —— 2026-10-24 faultlab 第二轮：09:46–10:06 在 10:07 回看，gs_asp 一个样本都没有，
内存里 5.3 万条，只读 gs_asp 的旧版报成了「回看不了 … 采样当时没开」（实际 enable_asp=on），
直到 11:25 内存写满才落表。现在两处一起读（白名单脚本里合并），内存那一侧只取将来会落表的那一批
（need_flush_sample），两处步长一致，同一时段落表前后查到的结果一样。

**时长是估算。** 一个阻塞对「采到 N 次」≈ 等了 N × (asp_sample_interval × asp_flush_rate) 秒。
短于这个间隔的等待可能一次也采不到 —— 「没采到」不等于「没有」。

**链按时间重叠连接。** 阻塞对是整段时间聚合出来的：A 等 B、B 等 C 只有两段时间有重叠才连成一条链，
否则是两件事，硬连起来会编出一条不存在的链。

实测（og5，10-21 18:32–18:52，faultlab 第一轮）：5146→5144→5145→5143→5142→5141，
根 5141 是 idle in transaction（fl:K1/root），全链等在 fl.acct 上。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from common.finding import Finding, Severity
from common.grmp.values import as_int, is_null

PAIRS_SCRIPT = "lockwait.asp_pairs"
SESSIONS_SCRIPT = "lockwait.asp_sessions"
STATUS_SCRIPT = "lockwait.asp_status"

TS_FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_STEP_S = 10.0          # openGauss 默认 asp_sample_interval=1 × asp_flush_rate=10
MAX_CHAINS = 50                # 链的条数上限(防止病态数据把输出撑爆)
# 阻塞对要**取全**再连链:只取前 20 对时,被截掉的那一对会把链从中间断开,断点被当成「根」——
# 2026-10-22 实测第二轮:5560(fl:K1/wait0,自己也在等)被报成根。展示条数另由 --limit 控制。
FETCH_LIMIT = 500
IDLE_XACT = "idle in transaction"
# 根会话在时段内一次也没被采到:ASP 只采活跃会话(含 idle in transaction),纯空闲的不采。
# 实测 og5:advisory 锁链的根 5185 拿着会话级 advisory 锁、事务已结束,采样里没有它
NOT_SAMPLED = "未采到（采样不记空闲会话，可能是会话级锁如 advisory 锁的持有者）"
MAX_IDLE_ROOTS = 3


@dataclass(frozen=True)
class Pair:
    waiter: int
    holder: int
    event: str
    mode: str
    lock_object: str
    lock_decoded: str
    sql_id: str
    query: str
    samples: int
    first: str
    last: str


@dataclass(frozen=True)
class Session:
    sessionid: int
    state: str
    last_event: str
    sql_id: str
    query: str
    app: str
    client: str
    xact_start: str
    samples: int


@dataclass(frozen=True)
class Status:
    enabled: bool
    step_s: float
    sample_interval: str
    flush_rate: str
    retention_days: str
    samples: int                # 时段内两处合计
    db_now: str
    table_samples: int = 0      # 落表的 gs_asp
    memory_samples: int = 0     # 内存里还没落表的 local_active_session(只数将来会落表的那一批)
    memory_first: str = ""      # 内存样本覆盖的起止(内存是连续的:实例重启会清空它)
    memory_last: str = ""
    table_last: str = ""        # 落表样本最新到哪
    flush_mode: str = ""        # asp_flush_mode:table / file / all
    sample_num: str = ""        # asp_sample_num:内存最多留几个样本
    memory_known: bool = False  # 新版 lockwait.asp_status 才返回内存那几行;客户没重导白名单时为 False


@dataclass(frozen=True)
class Chain:
    path: Tuple[int, ...]       # 等待者 → … → 根
    wait_s: float               # 链头(最末端的等待者)的估算等待
    first: str
    last: str
    lock: str                   # 等待事件 / 请求的锁模式
    lock_object: str
    sql_id: str                 # 链头(最末端等待者)的语句
    query: str


@dataclass(frozen=True)
class AspReport:
    begin: str
    end: str
    status: Optional[Status]
    unavailable: str            # 非空 = 这段时间回看不了,写明原因
    pairs: Tuple[Pair, ...] = ()
    sessions: Tuple[Session, ...] = ()
    chains: Tuple[Chain, ...] = ()
    truncated: bool = False     # 阻塞对取满了 FETCH_LIMIT,链可能不全

    def session(self, sid: int) -> Optional[Session]:
        return next((s for s in self.sessions if s.sessionid == sid), None)

    @property
    def roots(self) -> Tuple[int, ...]:
        seen: List[int] = []
        for c in self.chains:
            if c.path[-1] not in seen:
                seen.append(c.path[-1])
        return tuple(seen)


# ---------------------------------------------------------------- 解析

def _text(v) -> str:
    return "" if is_null(v) else " ".join(str(v).split())


def _int(v, default: int = 0) -> int:
    return default if is_null(v) else as_int(v)


def parse_status(rows) -> Status:
    kv = {str(r.get("item") or ""): _text(r.get("value")) for r in rows or []}
    try:
        step = float(kv.get("asp_sample_interval") or 0) * float(kv.get("asp_flush_rate") or 0)
    except ValueError:
        step = 0.0
    table, memory = _int(kv.get("samples_in_window")), _int(kv.get("memory_samples_in_window"))
    return Status(enabled=kv.get("enable_asp", "").lower() in ("on", "true", "1"),
                  step_s=step if step > 0 else DEFAULT_STEP_S,
                  sample_interval=kv.get("asp_sample_interval", ""),
                  flush_rate=kv.get("asp_flush_rate", ""),
                  retention_days=kv.get("asp_retention_days", ""),
                  samples=table + memory,
                  db_now=kv.get("now", ""),
                  table_samples=table, memory_samples=memory,
                  memory_first=kv.get("memory_first", ""), memory_last=kv.get("memory_last", ""),
                  table_last=kv.get("table_last", ""),
                  flush_mode=kv.get("asp_flush_mode", "").lower(),
                  sample_num=kv.get("asp_sample_num", ""),
                  memory_known="memory_first" in kv)


def parse_pairs(rows) -> Tuple[Pair, ...]:
    return tuple(Pair(waiter=_int(r.get("waiter_sessionid")), holder=_int(r.get("holder_sessionid")),
                      event=_text(r.get("wait_event")), mode=_text(r.get("waiter_mode")),
                      lock_object=_text(r.get("lock_object")), lock_decoded=_text(r.get("lock_decoded")),
                      sql_id=_text(r.get("waiter_sql_id")), query=_text(r.get("waiter_query")),
                      samples=_int(r.get("samples")), first=_text(r.get("first_seen")),
                      last=_text(r.get("last_seen")))
                 for r in rows or [])


def parse_sessions(rows) -> Tuple[Session, ...]:
    return tuple(Session(sessionid=_int(r.get("sessionid")), state=_text(r.get("state")),
                         last_event=_text(r.get("last_event")), sql_id=_text(r.get("sql_id")),
                         query=_text(r.get("query")), app=_text(r.get("application_name")),
                         client=_text(r.get("client_addr")), xact_start=_text(r.get("xact_start")),
                         samples=_int(r.get("samples")))
                 for r in rows or [])


# ---------------------------------------------------------------- 连链

def est_seconds(p: Pair, step_s: float) -> float:
    return p.samples * step_s


def _overlap(a: Pair, b: Pair) -> bool:
    # 时间串同一格式(YYYY-MM-DD HH:MM:SS),按字符串比即按时间比
    return a.first <= b.last and b.first <= a.last


def _walk(cur: Pair, by_waiter: Dict[int, List[Pair]], seen: Tuple[int, ...]) -> List[Tuple[Pair, ...]]:
    """从一对出发顺着「阻塞者也在等别人(且时间重叠)」往上走,返回所有走到头的路径。遇环停下。"""
    nxt = [p for p in by_waiter.get(cur.holder, ()) if _overlap(cur, p) and p.holder not in seen]
    if not nxt:
        return [(cur,)]
    out: List[Tuple[Pair, ...]] = []
    for p in nxt:
        for tail in _walk(p, by_waiter, seen + (p.holder,)):
            out.append((cur,) + tail)
            if len(out) >= MAX_CHAINS:
                return out
    return out


def _as_chain(edges: Tuple[Pair, ...], step_s: float) -> Chain:
    locked = next((e for e in edges if e.lock_object), None)
    head = edges[0]
    return Chain(path=tuple(e.waiter for e in edges) + (edges[-1].holder,),
                 wait_s=est_seconds(head, step_s), first=head.first, last=head.last,
                 lock="%s %s" % (head.event, head.mode) if head.mode else head.event,
                 lock_object=locked.lock_object if locked else "",
                 sql_id=head.sql_id, query=head.query)


def build_chains(pairs: Tuple[Pair, ...], step_s: float) -> Tuple[Chain, ...]:
    """链头 = 只在等、不挡别人的会话(时段内);从它往上走到根。去掉被更长的链包含的。"""
    holders = {p.holder for p in pairs}
    by_waiter: Dict[int, List[Pair]] = {}
    for p in pairs:
        by_waiter.setdefault(p.waiter, []).append(p)
    heads = [p for p in pairs if p.waiter not in holders] or list(pairs)   # 全成环时退回逐对
    paths: List[Tuple[Pair, ...]] = []
    for p in heads:
        paths += _walk(p, by_waiter, (p.waiter, p.holder))
        if len(paths) >= MAX_CHAINS:
            break
    chains = [_as_chain(e, step_s) for e in paths]
    chains.sort(key=lambda c: (-len(c.path), -c.wait_s, c.path))
    kept: List[Chain] = []
    for c in chains:
        if not any(_contains(k.path, c.path) for k in kept):
            kept.append(c)
    return tuple(kept)


def _contains(big: Tuple[int, ...], small: Tuple[int, ...]) -> bool:
    n = len(small)
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


def blocked_by_root(chains: Tuple[Chain, ...]) -> Dict[int, int]:
    """根 → 它直接或间接挡住的不同会话数。"""
    out: Dict[int, set] = {}
    for c in chains:
        out.setdefault(c.path[-1], set()).update(c.path[:-1])
    return {k: len(v) for k, v in out.items()}


# ---------------------------------------------------------------- 覆盖范围

def _dt(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, TS_FMT)
    except (TypeError, ValueError):
        return None


def _retention_floor(st: Status) -> Optional[datetime]:
    """落表样本保留期的下限(此刻 − asp_retention_days)。"""
    now = _dt(st.db_now)
    try:
        days = float(st.retention_days or 0)
    except ValueError:
        days = 0.0
    return now - timedelta(days=days) if now and days else None


def uncovered(st: Status, begin: str, end: str) -> Optional[Tuple[str, str]]:
    """时段里第一段不在任何一处采样里的 (起, 止);都覆盖到返回 None。

    落表的 gs_asp 覆盖 [保留期下限, table_last],内存里的覆盖 [memory_first, 此刻]。两者之间的空档
    只会因为实例重启出现(重启会丢掉内存里还没落表的样本)。旧版 asp_status 不返回这几行,说不清就不说。
    """
    b, e = _dt(begin), _dt(end)
    if not st.memory_known or b is None or e is None:
        return None
    spans = []
    if _dt(st.table_last):
        spans.append((_retention_floor(st) or datetime.min, _dt(st.table_last)))
    if _dt(st.memory_first):
        spans.append((_dt(st.memory_first), datetime.max))
    cur = b
    for lo, hi in sorted(spans):
        if hi <= cur:
            continue
        if lo > cur:
            break
        cur = hi
    if cur >= e:
        return None
    nxt = min([lo for lo, _hi in spans if lo > cur] + [e])
    return cur.strftime(TS_FMT), nxt.strftime(TS_FMT)


def coverage_text(st: Status) -> str:
    """两处采样各覆盖到哪(一句话,状态行与「回看不了」的原因共用)。"""
    table = ("落表的 gs_asp 到 %s 为止（保留 %s 天）" % (st.table_last, st.retention_days or "?")
             if st.table_last else "落表的 gs_asp 里没有样本")
    memory = ("内存里的 dbe_perf.local_active_session 覆盖 %s – %s" % (st.memory_first, st.memory_last)
              if st.memory_first else "内存里没有样本")
    return ("%s；%s（内存最多留 asp_sample_num=%s 个样本，写满才把每 asp_flush_rate=%s 个落 1 个进 gs_asp）"
            % (table, memory, st.sample_num or "?", st.flush_rate or "?"))


def _no_samples_reason(st: Status, begin: str, end: str) -> str:
    """时段内两处都没有样本时:说清是哪种情况,只陈述参数与覆盖范围,不猜「没开」。

    返回空串 = 这段时间在内存采样的连续覆盖范围内、只是一个活跃会话都没有 —— 那就是没有锁等待,不是回看不了。
    """
    floor, e = _retention_floor(st), _dt(end)
    if floor and e and e <= floor:
        return ("这段时间已超过活跃会话采样的保留期（asp_retention_days=%s 天），样本已清理，锁阻塞回看不了"
                % st.retention_days)
    if not st.memory_known:
        return ("落表的 gs_asp 里这段时间一个样本都没有；最近一段的样本在内存写满之前只在内存里"
                "（dbe_perf.local_active_session），而白名单里的 lockwait.asp_status 是旧版、没查内存 —— "
                "重新导入新版白名单后再看")
    gap = uncovered(st, begin, end)
    if gap is None:
        if _dt(st.memory_first) and _dt(begin) and _dt(begin) >= _dt(st.memory_first):
            return ""
        return ("这段时间在落表样本的范围内（%s），但一个样本也没有：当时没有活跃会话，或实例在这批样本落表之前"
                "重启过（内存里还没落表的样本会随重启丢失），分不清是哪一种" % coverage_text(st))
    if gap != (begin, end):
        return ""                                  # 只缺一段:有覆盖的那部分没有活动,缺的那段在依据里点名
    if st.flush_mode == "file" and not (_dt(st.memory_first) and _dt(st.memory_first) <= _dt(end)):
        return ("这段时间的样本已从内存刷到文件（asp_flush_mode=file，不进 gs_asp 表），本工具读不到；"
                "内存里的样本从 %s 开始" % (st.memory_first or "?"))
    return ("这段时间不在任何一处采样里：%s。中间这段的样本没有留下来（实例重启会丢掉内存里还没落表的样本）"
            % coverage_text(st))


# ---------------------------------------------------------------- 取数

def collect(runner, begin: str, end: str) -> AspReport:
    """取数并连链。ASP 没开 / 没样本时 unavailable 写明原因 —— 不和「没有锁等待」混为一谈。"""
    win = {"begin_time": begin, "end_time": end}
    st = parse_status(runner.run(STATUS_SCRIPT, win))
    if not st.enabled:
        return AspReport(begin, end, st, "数据库没开活跃会话采样（enable_asp=off），这段时间没有采样，"
                                         "锁阻塞回看不了；是否开启由 DBA 评估")
    if st.samples <= 0:
        return AspReport(begin, end, st, _no_samples_reason(st, begin, end))
    pairs = parse_pairs(runner.run(PAIRS_SCRIPT, dict(win, limit=FETCH_LIMIT)))
    sessions = parse_sessions(runner.run(SESSIONS_SCRIPT, dict(win, limit=FETCH_LIMIT * 2))) if pairs else ()
    return AspReport(begin, end, st, "", pairs, sessions, build_chains(pairs, st.step_s),
                     truncated=len(pairs) >= FETCH_LIMIT)


# ---------------------------------------------------------------- 判定

def judge(rep: AspReport, dim: str, warn_s: float, crit_s: float) -> List[Finding]:
    """阈值与此刻模式同一套(WAIT_WARN_S / WAIT_CRIT_S),值是估算时长。确定性的,LLM 不得更改。"""
    if rep.unavailable or not rep.pairs:
        return []
    step = rep.status.step_s
    top = max(rep.pairs, key=lambda p: (p.samples, p.first))
    longest = est_seconds(top, step)
    if longest >= crit_s:
        code, sev, thr = "LOCK_WAIT_LONG", Severity.CRITICAL, ">=%.0fs" % crit_s
    elif longest >= warn_s:
        code, sev, thr = "LOCK_WAIT", Severity.WARN, ">=%.0fs" % warn_s
    else:
        code, sev, thr = "LOCK_BLOCKED", Severity.NOTICE, ">0"
    waiters = {p.waiter for p in rep.pairs}
    where = top.lock_object or ("%s 锁" % top.event)
    out = [Finding(
        dim, code, sev, "最长锁等待（时段回看）", "约 %.0fs" % longest, thr,
        "%s – %s 活跃会话采样里 %d 个会话被挡；最久的是会话 %d 等会话 %d（%s，%s），采到 %d 次（%s – %s，每 %.0f 秒一个样本）"
        % (rep.begin[5:16], rep.end[5:16], len(waiters), top.waiter, top.holder, where, top.mode or top.event,
           top.samples, top.first[11:], top.last[11:], step))]
    blocked = blocked_by_root(rep.chains)
    idle = [(r, rep.session(r)) for r in sorted(rep.roots, key=lambda x: -blocked.get(x, 0))]
    idle = [(r, s) for r, s in idle if s and s.state.startswith(IDLE_XACT)]
    if idle:
        out.append(Finding(
            dim, "LOCK_ROOT_IDLE_XACT", Severity.WARN, "空闲事务持锁（时段回看）",
            "、".join("会话 %d" % r for r, _ in idle[:MAX_IDLE_ROOTS]) + ("等 %d 个" % len(idle) if len(idle) > MAX_IDLE_ROOTS else ""),
            "不应长期持有",
            "时段内最后一次采到时状态 idle in transaction（事务开着、不干活、锁不放）：" + "；".join(
                "%d（应用 %s，客户端 %s，事务从 %s 开始，直接或间接挡住 %d 个会话）"
                % (r, s.app or "未知", s.client or "未知", s.xact_start or "未知", blocked.get(r, 0))
                for r, s in idle[:MAX_IDLE_ROOTS])
            + "。这是事后回看：当时的会话多半已结束、会话号可能被复用，**不要据此 kill**；"
              "现在还堵着就先跑此刻的锁等待分析，长期处置是改应用的事务边界"))
    return out


# ---------------------------------------------------------------- 输出

def _sql_label(sql_id: str, query: str, n: int = 80) -> str:
    q = query[:n] + ("…" if len(query) > n else "")
    return ("`%s` %s" % (sql_id, q)).strip() if sql_id else q


def basis_note(rep: AspReport) -> str:
    st = rep.status
    if st is None:
        return ""
    cut = ("阻塞对超过 %d 对，只取了采到次数最多的 %d 对，链可能不全。" % (FETCH_LIMIT, FETCH_LIMIT)
           if rep.truncated else "")
    step = ("每 %.0f 秒一个样本（asp_sample_interval=%s × asp_flush_rate=%s）。等待时长 = 采到次数 × %.0f 秒，"
            "是估算；短于 %.0f 秒的等待可能采不到。"
            % (st.step_s, st.sample_interval or "?", st.flush_rate or "?", st.step_s, st.step_s))
    if not st.memory_known:
        return cut + ("来源：活跃会话采样（gs_asp），时段内 %d 个样本，保留 %s 天；%s白名单里的 lockwait.asp_status "
                      "是旧版，只查了落表的样本，最近一段（内存写满之前）只在内存里，导入新版后才看得到。"
                      % (st.samples, st.retention_days or "?", step))
    gap = uncovered(st, rep.begin, rep.end)
    missing = ("**时段里 %s – %s 这一段不在采样里**（%s），这一段的锁等待看不到。" % (gap[0], gap[1], coverage_text(st))
               if gap else "")
    return cut + missing + (
        "来源：活跃会话采样，时段内 %d 个样本（落表的 gs_asp %d 个 + 内存里还没落表的 dbe_perf.local_active_session "
        "%d 个；内存那一侧只取将来会落表的那一批，与 gs_asp 同一步长）；%s。%s"
        % (st.samples, st.table_samples, st.memory_samples, coverage_text(st), step))


def render_markdown(rep: AspReport, findings: List[Finding], conn: str, limit: int = 20) -> str:
    out = "# 时段锁回溯 — %s\n\n时段：%s – %s（数据库所在时区）\n\n" % (conn, rep.begin, rep.end)
    if rep.unavailable:
        return out + "> **回看不了：**%s。\n" % rep.unavailable
    out += "> %s\n\n" % basis_note(rep)
    if not rep.pairs and rep.status.samples <= 0:
        # 内存采样连续覆盖了这段时间、一个样本也没有:没有会话在干活,也就没人在等锁。
        # 只覆盖了一部分时,只能对有覆盖的那部分下这个结论(缺的那段已在依据里点名)
        part = "有采样覆盖的那部分" if uncovered(rep.status, rep.begin, rep.end) else ""
        return out + "**时段内%s一个活跃会话都没采到**，也就没有锁等待。\n" % part
    if not rep.pairs:
        return out + ("**时段内没有采到锁等待**（%d 个样本里没有一个在等锁）。短于 %.0f 秒的等待可能采不到。\n"
                      % (rep.status.samples, rep.status.step_s))
    out += "## 判定\n\n| 严重度 | Code | 指标 | 值 | 阈值 | 证据 |\n|---|---|---|---|---|---|\n"
    for f in findings:
        out += "| %s | %s | %s | %s | %s | %s |\n" % (f.severity.name, f.code, f.metric, f.value,
                                                     f.threshold, f.evidence)
    out += "\n" + render_chains(rep) + render_roots(rep) + render_pairs(rep, limit)
    out += ("## 说明\n\n- 这是事后回看，**不生成 kill 语句**：当时的会话可能已结束，会话号也可能被复用。"
            "现在还堵着的话，先跑此刻的锁等待分析（不带 --from/--to）再处置。\n"
            "- 等在 `transactionid` 上是行锁冲突（在等对方事务结束），锁对象看等待语句；"
            "`tuple` 是排队等同一行。\n\n")
    return out


def render_chains(rep: AspReport) -> str:
    out = "## 阻塞链（等待者 → … → 根）\n\n"
    for i, c in enumerate(rep.chains[:10], 1):
        root = rep.session(c.path[-1])
        tag = "（根，%s）" % root.state if root and root.state else "（根，%s）" % NOT_SAMPLED
        out += ("%d. %s → **%d%s** — 链头等 %s%s，约 %.0f 秒（%s – %s）；链头语句 %s\n"
                % (i, " → ".join(str(x) for x in c.path[:-1]), c.path[-1], tag, c.lock,
                   "（%s）" % c.lock_object if c.lock_object else "", c.wait_s, c.first[11:], c.last[11:],
                   _sql_label(c.sql_id, c.query) or "（采样里没记语句）"))
    if len(rep.chains) > 10:
        out += "\n…另有 %d 条较短的链，见下面的阻塞对明细。\n" % (len(rep.chains) - 10)
    return out + "\n"


def render_roots(rep: AspReport) -> str:
    blocked = blocked_by_root(rep.chains)
    out = ("## 根阻塞者\n\n| 会话 | 时段内最后状态 | 应用 | 客户端 | 事务开始 | 最后语句 | 挡住会话数 |\n"
           "|---|---|---|---|---|---|---|\n")
    for r in rep.roots:
        s = rep.session(r) or Session(r, "", "", "", "", "", "", "", 0)
        out += "| %d | %s | %s | %s | %s | %s | %d |\n" % (
            r, s.state or NOT_SAMPLED, s.app, s.client, s.xact_start, _sql_label(s.sql_id, s.query, 60) or "（无）",
            blocked.get(r, 0))
    return out + "\n"


def render_pairs(rep: AspReport, limit: int = 20) -> str:
    step = rep.status.step_s
    out = ("## 阻塞对明细\n\n| 等待会话 | 被谁挡 | 等待事件 | 请求的锁 | 锁对象 | 约等了 | 采到次数 | 首次 – 末次 | 等待语句 |\n"
           "|---|---|---|---|---|---|---|---|---|\n")
    for p in rep.pairs[:max(1, limit)]:
        out += "| %d | %d | %s | %s | %s | %.0fs | %d | %s – %s | %s |\n" % (
            p.waiter, p.holder, p.event, p.mode, p.lock_object or "—", est_seconds(p, step), p.samples,
            p.first[11:], p.last[11:], _sql_label(p.sql_id, p.query, 60) or "—")
    if len(rep.pairs) > limit:
        out += "\n…共 %d 对，按采到次数列了前 %d 对（--limit 调整）。\n" % (len(rep.pairs), limit)
    return out + "\n"


def to_dict(rep: AspReport) -> dict:
    """--format json 的 asp 块(health 的时段体检靠它画「锁与阻塞链」)。"""
    st = rep.status
    blocked = blocked_by_root(rep.chains)
    roots = []
    for r in rep.roots:
        s = rep.session(r)
        roots.append({"sessionid": r, "state": s.state if s else NOT_SAMPLED, "app": s.app if s else "",
                      "client": s.client if s else "", "xact_start": s.xact_start if s else "",
                      "sql_id": s.sql_id if s else "", "query": s.query if s else "",
                      "blocked": blocked.get(r, 0)})
    return {"begin": rep.begin, "end": rep.end, "unavailable": rep.unavailable,
            "basis": basis_note(rep),
            "step_s": st.step_s if st else None, "samples": st.samples if st else 0,
            "sources": ({"table": st.table_samples, "memory": st.memory_samples, "memory_first": st.memory_first,
                         "memory_last": st.memory_last, "table_last": st.table_last} if st else {}),
            "truncated": rep.truncated,
            "chains": [dict(c.__dict__, path=list(c.path)) for c in rep.chains],
            "roots": roots,
            "pairs": [dict(p.__dict__, wait_s=est_seconds(p, st.step_s)) for p in rep.pairs]}

