"""下一步要查什么 —— 给**模型**执行的计划，不进用户看的报告。

**为什么不能写进报告（上一版就是这么错的）。** 本 skill 的硬性要求是「最终报告 =
`wdr.py render` 的原样输出，逐字呈现给用户」。把 `python3 ../gaussdb-explain/scripts/explain.py
--sql-id 4198238080` 写进报告，等于把一行 shell 命令贴进聊天窗口让用户自己去跑 ——
用户要的是执行计划本身，不是取执行计划的命令。而且报告既然是「最终输出」，
模型渲染完就收工了，那几条命令**一条都不会被执行**：两头落空。

所以这份计划走**标准错误**（和大盘链接同一个出口），并且在 `collect` 阶段就给出 ——
那时模型还没写 `interp.json`，正好拿着它去跑，把结果并进根因与建议，
最后由 render 把**结论**铺进报告。报告里留下的是「执行计划长这样、所以慢在这里」，
不是「你去跑这条命令」。

**只反映当前的检查不能套在历史窗口上。** 阻塞链、死元组、正在执行的 SQL 都是「此刻」的；
拿它们去解释两小时前那个窗口，会得出一个与当时无关的结论 —— 那比老实说「查不到」更糟，
因为它看起来是有根据的。所以窗口结束超过 `STALE_AFTER_S` 就不发这类命令，
改成一句话说明为什么不发。算不出新鲜度时**也不发**（宁可少给一条，不要给一条可能误导的）。

**等待事件那条必须带上本次的快照号。** 上一版发的是裸 `waitevent.py` —— 它默认取
最近几个快照，跑出来是「现在」的等待构成，而报告正在分析的是另一段时间。
命令对了、窗口错了，输出看着完全正常，这种错最难发现。
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, NamedTuple, Optional, Tuple

# 窗口结束多久之后，「只反映当前」的检查就不再有参考价值。
# 15 分钟：足够覆盖「刚跑完一次压测马上来分析」，又不至于把半小时前的现场当成当时。
STALE_AFTER_S = 15 * 60

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M")


class Step(NamedTuple):
    """一条要执行的检查。

    now_only=True 表示它只能反映「此刻」，对历史窗口无效 —— 这类在窗口不新鲜时要撤掉。
    """
    code: str
    why: str
    cmd: str
    now_only: bool


# 发现码 → (为什么要查, 命令模板, 是否只反映当前)。
# 模板占位符：{sql_id} 本条发现指向的语句，{begin}/{end} 本次分析的快照号。
# 命令里**不带** `-c`：沿用本次登录的连接（api 模式要另带 `--session`，由模型按登录时的句柄补）。
_PLAN: Dict[str, Tuple[str, List[str], bool]] = {
    "WDR_TOPSQL_CPU_HEAVY": (
        "这条在真干活（CPU 占主导），要看它的执行计划慢在哪一步",
        ["python3 ../gaussdb-explain/scripts/explain.py --sql-id {sql_id}",
         "python3 ../gaussdb-sqltune/scripts/sqltune.py {sql_id}"],
        False),
    "WDR_TOPSQL_LOW_CPU": (
        "这条在等（CPU 近零），要查谁挡着它；**不要**去调它的执行计划",
        ["python3 ../gaussdb-lockwait/scripts/lockwait.py"],
        True),
    "WDR_WAIT_CLASS_SKEW": (
        "等待集中在某一类，要按本次窗口拆开看是哪个事件在争",
        ["python3 ../gaussdb-waitevent/scripts/waitevent.py --begin {begin} --end {end}"],
        False),
    "WDR_DBTIME_WAIT_HEAVY": (
        "时间主要花在等待上，真凶在等待事件里，不是 CPU 不够",
        ["python3 ../gaussdb-waitevent/scripts/waitevent.py --begin {begin} --end {end}"],
        False),
    # **死锁不发 lockwait 当"看死锁对"的手段。** lockwait 是在 Python 里对
    # pg_thread_wait_status 的边做环检测 —— 它只能抓**正在发生**的环,而 og5 的
    # deadlock_timeout 是 1 秒,内核 1 秒内就杀掉牺牲者解开环。等命令跑起来环早没了。
    # 发它只会让模型拿到一份"当前无死锁"然后说不出话。
    # 真正的去处是数据库日志,而本工具读不到(pg_read_file 要 initial account),
    # 所以这一条给的是**要告诉用户去哪看**,不是一条命令 —— 见 render_plan 里的 _MANUAL。
    "WDR_DEADLOCK": (
        "死锁次数是硬数据，但环的明细 WDR 里没有；lockwait 只能抓正在发生的环"
        "（内核 1 秒内就解开），所以这里不发命令 —— 要告诉用户去日志里看",
        [],
        False),
    "WDR_TEMP_SPILL": (
        "临时文件溢出，要定位落盘的语句并评估会话级 work_mem",
        ["python3 ../gaussdb-sqltune/scripts/sqltune.py {sql_id}"],
        False),
}

# WDR 根本不覆盖的两件事，**每次都要查**：不查就等于让用户以为已经查全了。
_ALWAYS: List[Tuple[str, str, bool]] = [
    ("WDR 不覆盖膨胀与回收水位（死元组、autovacuum 追不追得上、回收被谁卡住）",
     "python3 ../gaussdb-vacuum/scripts/vacuum.py", True),
    ("WDR 只统计**已完成**的执行；一直没跑完的 SQL 在这里一行都没有",
     "python3 ../gaussdb-health/scripts/health.py --include slowsql", True),
]


def staleness_s(end_ts: str, now: Optional[_dt.datetime] = None) -> Optional[float]:
    """窗口结束到现在过了多少秒。解析不出来返回 None（= 不知道，按不新鲜处理）。"""
    text = (end_ts or "").strip()
    if not text:
        return None
    at = None
    for fmt in _TS_FORMATS:
        try:
            at = _dt.datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if at is None:
        return None
    gap = ((now or _dt.datetime.now()) - at).total_seconds()
    # 负数 = 快照时间比本机时间还晚（容器与库的时钟不一致）。当作「刚结束」，
    # 不要返回负数：负数会一路当成「很新鲜」而不被任何阈值拦住。
    return max(0.0, gap)


def _spill_sql_id(headers: List[str], rows: List[list]) -> str:
    """Top SQL 表里落盘最多的那条的 sql_id。

    溢出那条发现来自库级计数器，它本身不知道是哪条语句落的盘；Top SQL 表里有 spill 列，
    取最大的那条，命令里才填得出 sql_id。
    """
    try:
        i_sid = headers.index("sql_id")
        i_spill = next(i for i, h in enumerate(headers) if str(h).startswith("spill"))
    except (ValueError, StopIteration, AttributeError):
        return ""
    best, best_v = "", 0.0
    for r in rows or []:
        try:
            v = float(r[i_spill])
        except (TypeError, ValueError, IndexError):
            continue
        if v > best_v:
            best, best_v = str(r[i_sid]), v
    return best


def plan(findings, window, top_sql_headers=(), top_sql_rows=(),
         fresh: bool = True) -> List[Step]:
    """确定性发现 → 要执行的检查清单（去重，顺序即建议的执行顺序）。

    `fresh=False` 时撤掉所有 now_only 的检查 —— 它们对历史窗口给不出有效结论。
    """
    begin = str(getattr(window, "begin_id", "") or "")
    end = str(getattr(window, "end_id", "") or "")
    spill = _spill_sql_id(list(top_sql_headers or []), list(top_sql_rows or []))

    steps: List[Step] = []
    seen = set()

    def add(code: str, why: str, cmd: str, now_only: bool) -> None:
        if cmd in seen:
            return
        seen.add(cmd)
        steps.append(Step(code=code, why=why, cmd=cmd, now_only=now_only))

    for f in findings or []:
        hit = _PLAN.get(getattr(f, "code", ""))
        if not hit:
            continue
        why, cmds, now_only = hit
        if now_only and not fresh:
            continue
        sid = getattr(f, "sql_id", "") or ""
        if not sid and f.code == "WDR_TEMP_SPILL":
            sid = spill
        for tpl in cmds:
            if "{sql_id}" in tpl and not sid:
                continue    # 填不出 sql_id 的命令不发 —— 带占位符的命令跑不起来
            if ("{begin}" in tpl or "{end}" in tpl) and not (begin and end):
                continue
            add(f.code, why, tpl.format(sql_id=sid, begin=begin, end=end), now_only)

    for why, cmd, now_only in _ALWAYS:
        if now_only and not fresh:
            continue
        add("", why, cmd, now_only)
    return steps


# 给模型的交代。不是给用户的 —— 它走标准错误，而用户看到的是 render 的 stdout。
_HEAD = ("下一步（给你执行，不要把命令贴给用户）：逐条跑完，把结论并进 interp.json 的"
         "根因与建议；跑不动的在报告里说明原因。命令沿用本次登录的连接，api 模式记得带 --session。")

_STALE_NOTE = ("窗口结束已过 %d 分钟，只反映「此刻」的检查（阻塞链、膨胀、正在执行的 SQL）"
               "已不再对应这个窗口，本次不发 —— 在报告里说明这几项要现场复测才看得到，"
               "**不要**拿现在的现场去解释当时的窗口。")

_UNKNOWN_NOTE = ("算不出窗口距今多久（快照时间取不到），只反映「此刻」的检查本次不发 ——"
                 "宁可少一条，也不要拿可能无关的现场去解释这个窗口。")


# 本工具做不到、但要**明确告诉用户去哪看**的事。按发现码挂。
# 给不出命令时给精确的去处,比给一个看起来有根据的猜测有用得多。
_MANUAL = {
    "WDR_DEADLOCK": (
        "死锁的完整明细（环上几个会话、各自持有/等待什么锁、完整语句、谁被选为牺牲者）"
        "只在**数据库日志**里。本工具读不到日志文件（需 initial account 权限）——"
        "请把这段话转给用户：到 `$PGDATA/pg_log/` 下找本窗口时间段的日志，"
        "`grep -A 12 'deadlock detected'`，紧随其后的 DETAIL 段就是完整的环，**把结果贴回对话里**。"
        "贴过来之后交给 `python3 ../gaussdb-lockwait/scripts/lockwait.py --deadlock-log -`"
        "（日志段从标准输入给，不连库），它会逐个解析出环、各自的语句与牺牲者，按模式归类并给出改法。"
        "用户贴之前，从 Top SQL 反推候选语句时**必须标明是候选、不是证据**。"),
}


def manual_notes(findings) -> List[str]:
    """本次发现里,有哪些事要让用户自己去库外看。去重,保持发现的顺序。"""
    out, seen = [], set()
    for f in findings or []:
        code = getattr(f, "code", "")
        if code in _MANUAL and code not in seen:
            seen.add(code)
            out.append(_MANUAL[code])
    return out


def render_plan(steps: List[Step], stale_s: Optional[float],
                manual: Optional[List[str]] = None) -> str:
    """给标准错误的文本。没有任何要执行的就返回空串（不打空段落）。"""
    note = ""
    if stale_s is None:
        note = _UNKNOWN_NOTE
    elif stale_s > STALE_AFTER_S:
        note = _STALE_NOTE % int(stale_s // 60)
    extra = ""
    for m in manual or []:
        extra += "  · " + m + "\n"
    if not steps:
        return (note + "\n" if note else "") + extra
    out = _HEAD + "\n"
    for i, s in enumerate(steps, 1):
        out += "  %d) %s\n     %s\n" % (i, s.why, s.cmd)
    if note:
        out += "  注：" + note + "\n"
    return out + extra
