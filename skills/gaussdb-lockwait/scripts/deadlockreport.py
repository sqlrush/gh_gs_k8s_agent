"""死锁日志解析结果的呈现(markdown / json)。解析本身在 deadlocklog.py。

版式:先一句总数与时间跨度,再按模式逐个列「最近一次的环 + 怎么改」,最后是通用的应用侧处理。
每个环都标出牺牲者 —— 用户最常问的是「为什么偏偏是这条报错」。
"""
from __future__ import annotations

from typing import List

import deadlocklog as L
import render

MAX_PATTERNS = 10       # 模式再多也只列前这么多(按出现次数),其余给个数
SQL_SHOWN = 120


def _ts(at: str) -> str:
    return at[5:23] if len(at) >= 19 else (at or "时间未知")       # MM-DD HH:MM:SS.mmm


def _span(p: L.Pattern) -> str:
    a, b = p.events[0].at, p.latest.at
    return _ts(a) if a == b else "%s – %s" % (_ts(a), _ts(b))


def _ring_table(d: L.Deadlock) -> str:
    rows = []
    for w in d.waits:
        who = w.pid + ("（**牺牲者**，事务被回滚）" if w.pid == d.victim else "")
        sql = L.stmt_of(d, w.pid) or "（日志里没有这条语句）"
        rows.append([who, "`%s`" % render.truncate(sql, SQL_SHOWN).replace("`", "'"),
                     L.explain_wait(w), w.blocker])
    return render.table(["进程", "正在执行", "在等什么", "挡住它的进程"], rows)


def _pattern_block(i: int, p: L.Pattern) -> str:
    kinds = "、".join(p.kinds)
    out = ["## 模式 %d：%d 个事务的%s环 —— 出现 %d 次（%s）\n" % (i, p.size, kinds, len(p.events), _span(p))]
    uniq = sorted(set(p.shapes))
    out.append("涉及的语句（常量已换成 ?）：\n")
    for sh in uniq:
        n = p.shapes.count(sh)
        out.append("- `%s`%s" % (render.truncate(sh, SQL_SHOWN).replace("`", "'"),
                                 "（环上 %d 个进程都在执行它）" % n if n > 1 else ""))
    d = p.latest
    out.append("\n最近一次（%s）的环%s：\n" % (_ts(d.at), "" if d.closed else "——**首尾没有接上，日志段可能不完整**"))
    out.append(_ring_table(d))
    out.append("**怎么改**：%s\n" % L.advice(p))
    return "\n".join(out)


def render_markdown(parsed: L.Parsed) -> str:
    out = ["# 死锁日志解析\n"]
    ds = parsed.deadlocks
    if parsed.truncated:
        out.append("> 输入超过 %d MB，只解析了前面一部分。请只截取 `deadlock detected` 前后的那几段再给我。\n"
                   % (L.MAX_BYTES >> 20))
    if not ds:
        out.append("在给出的文本里没有找到死锁的明细。" + (
            "有 %d 处 `deadlock detected`，但后面没有 `Process … waits for … blocked by process …` 这几行 —— "
            "通常是只复制了 ERROR 那一行；请把它后面紧跟的 DETAIL 几行一起给我。\n" % parsed.bare if parsed.bare else
            "请从数据库日志里找含 `deadlock detected` 的行，连同它后面的 DETAIL / STATEMENT 几行一起复制过来。\n"))
        return "\n".join(out)
    pats = L.group(ds)
    times = sorted(d.at for d in ds if d.at)
    when = "（%s – %s）" % (_ts(times[0]), _ts(times[-1])) if times else ""
    out.append("共解析出 **%d** 次死锁%s，归成 **%d** 种模式。" % (len(ds), when, len(pats))
               + ("另有 %d 处只有 `deadlock detected`、没有环的明细，没有计入。" % parsed.bare if parsed.bare else ""))
    out.append("\n> 以下全部来自你给的日志原文 —— 数据库里不留死锁明细（只有次数），本解析也不连库。"
               "「在等什么」一列是内核在日志里写的锁；`transaction` 锁的意思是在等对方事务结束，原因是对方锁着它要改的行。\n")
    for i, p in enumerate(pats[:MAX_PATTERNS], 1):
        out.append(_pattern_block(i, p))
    if len(pats) > MAX_PATTERNS:
        out.append("另有 %d 种模式各出现次数更少，未逐个列出（`--format json` 可看全部）。\n" % (len(pats) - MAX_PATTERNS))
    out.append("## 应用侧\n\n%s\n" % L.RETRY)
    return "\n".join(out)


def _wait_dict(w: L.Wait) -> dict:
    return {"pid": w.pid, "mode": w.mode, "target": w.target, "kind": w.kind, "blocker": w.blocker}


def _event_dict(d: L.Deadlock) -> dict:
    return {"at": d.at, "victim": d.victim, "closed": d.closed,
            "waits": [_wait_dict(w) for w in d.waits],
            "sql": {w.pid: L.stmt_of(d, w.pid) for w in d.waits}}


def to_dict(parsed: L.Parsed) -> dict:
    pats: List[L.Pattern] = L.group(parsed.deadlocks)
    return {"deadlocks": len(parsed.deadlocks), "bare": parsed.bare, "truncated": parsed.truncated,
            "patterns": [{"size": p.size, "kinds": list(p.kinds), "statements": list(p.shapes),
                          "count": len(p.events), "first": p.events[0].at, "last": p.latest.at,
                          "advice": L.advice(p), "latest": _event_dict(p.latest)} for p in pats],
            "retry": L.RETRY}
