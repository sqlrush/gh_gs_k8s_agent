"""把会话切成「一次提问」→「若干轮」→「各段」,算出每段用时并写出慢在哪。纯计算,不调模型、不联网。

一轮 = 一条助手消息(模型调用一次,可能接着调工具)。各段:
  等首字  这一轮开始 → 模型第一个字(思考或正文;没有时取第一个工具片段出现的时刻)
  思考与输出  第一个字 → 第一个工具开始执行(最后一轮是写完回答)
  工具    工具开始 → 结束;其中 GRMP 调用(取自 grmp-timing.log)单列,其余算「脚本本地」
  空档    上一轮结束 → 这一轮开始(运行时自己落库、拼上下文)
"""
from __future__ import annotations

import collections
import datetime
import re
import statistics
from typing import Dict, List, Optional

from . import grmplog
from .mask import Masker

CAP_THINK, CAP_TEXT, CAP_CMD, CAP_OUT = 12000, 8000, 6000, 8000    # 详细版每段原文上限(字)
TTFT_SLOW = 2.0       # 平均每轮等首字超过这么多秒才在结论里提示
SPEED_SLOW = 20.0     # 生成速度低于这么多 token/秒,算模型服务偏慢
INLINE_BIG = 800      # 命令里现写的脚本 / 数据超过这么多字就点出来
OUTPUT_BIG = 5000     # 工具输出超过这么多字就点出来(会撑大之后每一轮的上下文)
QUESTION_SHORT = 40   # 摘要版只留问题前这么多字

CAT_NAMES = {"script": "调技能脚本", "read": "读技能文件", "adhoc": "临时命令", "load": "加载技能",
             "builtin": "内置工具", "answer": "写回答", "compact": "上下文压缩"}
KIND_NAMES = {"ttft": "模型·等首字", "gen": "模型·思考与输出", "grmp": "工具·GRMP 与数据库",
              "local": "工具·脚本本地", "idle": "运行时空档"}
_SHELL = {"for", "do", "done", "if", "then", "fi", "while", "in", "echo", "true"}
_SCRIPT = re.compile(r"python3\s+\S*/skills/([\w-]+)/scripts/([\w.-]+\.py)((?:\s+\S+)*)")
_READ = re.compile(r"^\s*(cat|sed|head|tail|grep|ls)\b[^\n]*?/skills/([\w-]+)/([^\s'\")|;]+)")
_PROG = re.compile(r"(?:^|[;&|(])\s*([a-z][\w.-]*)(?![\w.-]|=)")
_INLINE = re.compile(r"<<\s*'?EOF|python3\s+-\s|python3\s+-c\s")


def classify(tool: str, inp: dict) -> tuple:
    """(类别, 标签)。标签只放技能名、脚本名、子命令或程序名,不放参数。"""
    if tool == "skill":
        return "load", "加载技能 " + str(inp.get("name", ""))
    if tool != "bash":
        return "builtin", "内置工具 " + tool
    cmd = inp.get("command") or ""
    m = _READ.match(cmd)
    if m:
        return "read", "读 %s/%s" % (m.group(2), "/".join(m.group(3).rstrip("/").split("/")[-2:]))
    m = _SCRIPT.search(cmd)
    if m:
        rest = re.split(r"[;|&]", m.group(3), 1)[0].split()
        sub = rest[0] if rest and re.fullmatch(r"[a-z][a-z_-]*", rest[0]) else ""
        return "script", "%s · %s%s" % (m.group(1), m.group(2), (" " + sub) if sub else "")
    if re.match(r"\s*python3\s+-", cmd):
        return "adhoc", "python3 内联脚本"
    progs = [p for p in _PROG.findall(re.sub(r"\b(do|then|else)\b", ";", cmd)) if p not in _SHELL]
    return "adhoc", ("for 循环 · " if cmd.lstrip().startswith("for ") else "") + (progs[0] if progs else "shell")


def fmt(s: float) -> str:
    if s >= 120:
        return "%d 分 %.0f 秒" % (s // 60, s % 60)
    return ("%.2f 秒" if s < 1 else "%.1f 秒") % s


def _union(spans) -> float:
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(spans):
        if cur_e is None or s > cur_e:
            total += (cur_e - cur_s) if cur_e is not None else 0
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    return total + ((cur_e - cur_s) if cur_e is not None else 0)


class Builder:
    def __init__(self, detail: bool, grmp: List[dict], tz: datetime.tzinfo):
        self.detail, self.grmp, self.tz, self.mask = detail, grmp, tz, Masker()

    # ---- 一轮 ----------------------------------------------------------------------------------
    def tool(self, p: dict, tc: int, rel) -> dict:
        st = p["state"]
        inp = st.get("input") or {}
        cat, label = classify(p["tool"], inp)
        cmd = inp.get("command") or ""
        out = str(st.get("output") or "")
        meta = st.get("metadata") or {}
        s, e = int(st["time"]["start"]), int(st["time"]["end"])
        t = {"tool": p["tool"], "cat": cat, "catn": CAT_NAMES[cat], "label": label, "s": rel(s), "e": rel(e),
             "secs": (e - s) / 1000, "cmd_chars": len(cmd), "inline": bool(_INLINE.search(cmd)),
             "status": st.get("status", ""), "exit": meta.get("exit"), "truncated": bool(meta.get("truncated")),
             "out_chars": len(out), "out_lines": out.count("\n") + (1 if out else 0),
             "abs_s": s, "abs_e": e, "cmd_raw": cmd}
        if self.detail:
            t.update(cmd=self.mask(cmd or str(inp), CAP_CMD), out=self.mask(out, CAP_OUT),
                     desc=self.mask(str(inp.get("description") or ""), 300))
        return t

    def grmp_rows(self, t: dict, rel) -> List[dict]:
        rows = []
        for r in t.pop("grmp_raw", []):
            s, e = max(r["start"], t["abs_s"]), min(r["end"], t["abs_e"])
            db = str(r.get("db") or "")
            rows.append({"name": r.get("script", ""), "kind": r.get("kind", ""), "s": rel(s), "e": rel(max(s, e)),
                         "ms": int(r.get("ms") or 0), "rows": r.get("rows"), "bytes": r.get("bytes"),
                         "ok": bool(r.get("ok")), "status": r.get("status") or r.get("error") or "",
                         "db": "—" if r.get("kind") == "list" else
                               (db or "postgres") if self.detail else ("postgres" if db in ("", "postgres") else "业务库")})
        return rows

    def round(self, i: int, m: dict, ps: list, t0: int) -> dict:
        T = int(m["time"]["created"])
        done = int(m["time"].get("completed") or max([tu for _, tu, _ in ps] + [T]))
        rel = lambda x: round((x - t0) / 1000, 3)       # noqa: E731
        reasons = [p for _, _, p in ps if p.get("type") == "reasoning" and (p.get("time") or {}).get("start")]
        texts = [p for _, _, p in ps if p.get("type") == "text" and (p.get("text") or "").strip()]
        traw = [(tc, p) for tc, _, p in ps if p.get("type") == "tool" and ((p.get("state") or {}).get("time") or {}).get("end")]
        first = min([int(p["time"]["start"]) for p in reasons + texts if (p.get("time") or {}).get("start")]
                    + [tc for tc, _ in traw] + [done])
        first = max(first, T)
        tools = [self.tool(p, tc, rel) for tc, p in traw]
        grmplog.assign(self.grmp, tools)
        segs = [{"k": "ttft", "s": rel(T), "e": rel(first)}]
        if tools:
            ts, te = min(t["abs_s"] for t in tools), max(t["abs_e"] for t in tools)
            gen_end, busy_end = max(ts, first), te
            segs.append({"k": "gen", "s": rel(first), "e": rel(gen_end)})
            spans = []
            for t in tools:
                t["grmp"] = self.grmp_rows(t, rel)
                cur = t["s"]
                for g in t["grmp"]:
                    if g["s"] > cur:
                        segs.append({"k": "local", "s": cur, "e": g["s"]})
                    segs.append({"k": "grmp", "s": g["s"], "e": g["e"]})
                    spans.append((g["s"], g["e"]))
                    cur = max(cur, g["e"])
                segs.append({"k": "local", "s": cur, "e": t["e"]})
            span = (te - ts) / 1000
            grmp = min(_union(spans), span)
        else:
            gen_end = busy_end = done
            segs.append({"k": "gen", "s": rel(first), "e": rel(done)})
            span = grmp = 0.0
        for t in tools:
            for k in ("abs_s", "abs_e", "cmd_raw"):
                t.pop(k, None)
        tk = m.get("tokens") or {}
        cache = tk.get("cache") or {}
        think_secs = sum((int(p["time"].get("end") or p["time"]["start"]) - int(p["time"]["start"])) / 1000 for p in reasons)
        gen = (gen_end - first) / 1000
        rsn, out = int(tk.get("reasoning") or 0), int(tk.get("output") or 0)
        out_secs = max(0.0, gen - think_secs)
        compact = bool(m.get("summary")) or m.get("mode") == "compaction"
        main = ({"cat": "compact", "label": "上下文压缩"} if compact else tools[0] if tools
                else {"cat": "answer", "label": "写回答"})
        return {
            "i": i, "t": rel(T), "end": rel(busy_end), "done": rel(done), "segs": [s for s in segs if s["e"] > s["s"]],
            "tools": tools, "ttft": (first - T) / 1000, "gen": gen, "grmp": grmp, "local": span - grmp,
            "ctx_in": int(tk.get("input") or 0), "ctx_read": int(cache.get("read") or 0),
            "ctx_write": int(cache.get("write") or 0), "out": out, "rsn": rsn, "cat": main["cat"], "act": main["label"],
            "model": m.get("modelID", ""), "finish": m.get("finish", "") or (m.get("error") or {}).get("name", ""),
            "think": {"secs": think_secs, "speed": rsn / think_secs if think_secs > 0.2 else None,
                      "parts": [{"secs": (int(p["time"].get("end") or p["time"]["start"]) - int(p["time"]["start"])) / 1000,
                                 "chars": len(p.get("text") or ""),
                                 "text": self.mask(p.get("text") or "", CAP_THINK) if self.detail else ""} for p in reasons]},
            "outp": {"secs": out_secs, "speed": out / out_secs if out_secs > 0.2 else None,
                     "texts": [{"chars": len(p["text"]), "text": self.mask(p["text"], CAP_TEXT) if self.detail else ""}
                               for p in texts]},
        }

    # ---- 一次提问 ------------------------------------------------------------------------------
    def question(self, idx: int, sid: str, umsg: dict, text: str, raw_rounds: list) -> dict:
        t0 = int(umsg["time"]["created"])
        rounds, prev_end = [], 0.0
        for i, (m, ps) in enumerate(raw_rounds, 1):
            r = self.round(i, m, ps, t0)
            r["idle"] = max(0.0, r["t"] - prev_end)
            if r["idle"] > 0:
                r["segs"].insert(0, {"k": "idle", "s": round(prev_end, 3), "e": r["t"]})
            prev_end = r["end"]
            rounds.append(r)
        last = rounds[-1]
        totals = {k: sum(r[k] for r in rounds) for k in ("ttft", "gen", "grmp", "local", "idle")}
        totals["idle"] += max(0.0, last["done"] - last["end"])
        short = self.mask(text[:QUESTION_SHORT]) + ("…" if len(text) > QUESTION_SHORT else "")
        q = {"idx": idx, "session": sid, "start": datetime.datetime.fromtimestamp(t0 / 1000, self.tz).strftime("%Y-%m-%d %H:%M:%S"),
             "text": short, "full": self.mask(text, 2000) if self.detail else short, "total": last["done"], "rounds": rounds,
             "totals": totals}
        explain(q)
        summarize(q)
        q["diag"] = diagnose(q)
        return q


def explain(q: dict) -> None:
    """给每一轮写「为什么慢」:只陈述数据,并和本问的其他轮比。"""
    rounds = q["rounds"]
    for r in rounds:
        r["ctx"] = r["ctx_in"] + r["ctx_read"] + r["ctx_write"]
        r["speed"] = (r["rsn"] + r["out"]) / r["gen"] if r["gen"] > 0.5 else None
    speeds = [r["speed"] for r in rounds if r["speed"]]
    med = statistics.median(speeds) if speeds else 0
    q["speed_median"] = med
    rank = {r["i"]: k for k, r in enumerate(sorted(rounds, key=lambda r: -r["rsn"]), 1)}
    for n, r in enumerate(rounds):
        nxt = rounds[n + 1]["ctx"] if n + 1 < len(rounds) else None
        r["ctx_next_delta"] = (nxt - r["ctx"]) if nxt is not None else None
        why = []
        if r["ttft"] >= 3:
            why.append("等首字 %.1f 秒" % r["ttft"])
        if r["rsn"] >= 800 and rank[r["i"]] <= 3:
            why.append("思考 %d token（本问第 %d 多）" % (r["rsn"], rank[r["i"]]))
        sp = r["speed"]
        if sp and med and r["gen"] >= 2 and sp < 0.5 * med:
            why.append("生成速度 %.0f token/秒，只有本问中位数的 %.0f%%：模型服务可能在这一轮变慢" % (sp, sp / med * 100))
        for t in r["tools"]:
            g = sum(x["e"] - x["s"] for x in t["grmp"])
            if t["inline"] and t["cmd_chars"] >= INLINE_BIG:
                why.append("在命令里现写了 %d 字的脚本 / 数据（输出 %d token）" % (t["cmd_chars"], r["out"]))
            if t["secs"] >= 5:
                why.append("工具「%s」执行 %.1f 秒%s" % (t["label"], t["secs"], "（其中 GRMP %.1f 秒）" % g if g else ""))
            if t["exit"] not in (None, 0):
                why.append("工具「%s」退出码 %s" % (t["label"], t["exit"]))
            bad = [x["name"] for x in t["grmp"] if not x["ok"]]
            if bad:
                why.append("GRMP 调用失败 %d 次（%s）" % (len(bad), "、".join(sorted(set(bad)))))
        big = [t["out_chars"] for t in r["tools"] if t["out_chars"] >= OUTPUT_BIG]
        if big:                                         # 一轮几个工具的输出合起来撑大下一轮上下文:合成一句,不重复算
            grow = r["ctx_next_delta"]
            why.append("工具输出%s %d 字%s" % ("共" if len(big) > 1 else "", sum(big),
                                          "（%d 个工具）" % len(big) if len(big) > 1 else "")
                       + ("，下一轮上下文 +%.1fK token" % (grow / 1000) if grow and grow > 0 else ""))
        if r["cat"] == "compact":
            why.append("运行时在压缩上下文（上下文太长时自动触发）")
        elif not r["tools"]:
            why.append("写回答 %d 字" % sum(x["chars"] for x in r["outp"]["texts"]))
        if not why:
            parts = {"等首字": r["ttft"], "思考与输出": r["gen"], "工具": r["grmp"] + r["local"], "轮前空档": r["idle"]}
            k = max(parts, key=parts.get)
            why.append("主要花在%s %s，各项都在本问的正常范围" % (k, fmt(parts[k])))
        r["why"] = why


def summarize(q: dict) -> None:
    rounds, total = q["rounds"], q["total"]
    cats: Dict[str, dict] = collections.OrderedDict()
    for r in rounds:
        c = cats.setdefault(r["cat"], {"cat": r["cat"], "name": CAT_NAMES[r["cat"]], "n": 0, "secs": 0.0})
        c["n"] += 1
        c["secs"] += r["end"] - r["t"]
    slow = []
    for r in rounds:
        slow.append({"k": "ttft", "round": r["i"], "what": "等模型第一个字", "secs": r["ttft"]})
        slow.append({"k": "gen", "round": r["i"], "what": "模型思考与输出", "secs": r["gen"]})
        if r["idle"] > 0:
            slow.append({"k": "idle", "round": r["i"], "what": "轮前空档", "secs": r["idle"]})
        for t in r["tools"]:
            g = sum(x["e"] - x["s"] for x in t["grmp"])
            slow.append({"k": "grmp" if g > (t["secs"] - g) else "local", "round": r["i"], "what": t["label"],
                         "secs": t["secs"], "grmp": g, "cat": t["catn"]})
    slow = sorted(slow, key=lambda x: -x["secs"])[:10]
    for s in slow:
        s["share"] = s["secs"] / total if total else 0
    gcalls = [g for r in rounds for t in r["tools"] for g in t["grmp"]]
    gsum: Dict[str, dict] = {}
    for g in gcalls:
        x = gsum.setdefault(g["name"], {"name": g["name"], "n": 0, "secs": 0.0, "fail": 0, "rows": 0})
        x["n"] += 1
        x["secs"] += g["e"] - g["s"]
        x["fail"] += 0 if g["ok"] else 1
        x["rows"] += g["rows"] or 0
    q.update({
        "rounds_n": len(rounds), "tools_n": sum(len(r["tools"]) for r in rounds),
        "grmp_n": len(gcalls), "grmp_fail": sum(1 for g in gcalls if not g["ok"]),
        "grmp_scripts": sorted(gsum.values(), key=lambda x: -x["secs"]),
        "ctx_first": rounds[0]["ctx"], "ctx_peak": max(r["ctx"] for r in rounds),
        "tok": {"in": sum(r["ctx_in"] for r in rounds), "read": sum(r["ctx_read"] for r in rounds),
                "write": sum(r["ctx_write"] for r in rounds), "out": sum(r["out"] for r in rounds),
                "rsn": sum(r["rsn"] for r in rounds)},
        "cats": sorted(cats.values(), key=lambda c: -c["secs"]), "slow": slow,
        "model": collections.Counter(r["model"] for r in rounds).most_common(1)[0][0],
    })


def diagnose(q: dict) -> List[dict]:
    """按规则给出「慢在哪」。只陈述数据,不猜原因;推测的部分写成「可能」。"""
    T, tot, out = q["totals"], q["total"] or 1, []
    model, tool = T["ttft"] + T["gen"], T["grmp"] + T["local"]
    top = max((s for s in q["slow"] if s["k"] in ("grmp", "local")), key=lambda s: s["secs"], default=None)
    if tool / tot >= 0.5 and top:
        out.append({"lvl": "serious", "title": "慢在一次工具调用", "round": top["round"],
                    "detail": "第 %d 轮的「%s」（%s）用了 %s，占这次提问的 %.0f%%%s。"
                              % (top["round"], top["what"], top["cat"], fmt(top["secs"]), top["secs"] / tot * 100,
                                 "，其中 GRMP 与数据库 %s" % fmt(top["grmp"]) if top.get("grmp") else "")})
    if T["grmp"] / tot >= 0.3 and q["grmp_scripts"]:
        g = q["grmp_scripts"][0]
        out.append({"lvl": "serious", "title": "慢在 GRMP 与数据库",
                    "detail": "GRMP 调用 %d 次共 %s，占 %.0f%%；最耗时的是 %s（%d 次共 %s）。"
                              % (q["grmp_n"], fmt(T["grmp"]), T["grmp"] / tot * 100, g["name"], g["n"], fmt(g["secs"]))})
    if q["grmp_fail"]:
        out.append({"lvl": "warning", "title": "有 GRMP 调用失败",
                    "detail": "失败 %d 次：%s。失败的那几项在回答里会写「无法查询」。"
                              % (q["grmp_fail"], "、".join("%s ×%d" % (g["name"], g["fail"]) for g in q["grmp_scripts"] if g["fail"]))})
    if model / tot >= 0.6 and q["rounds_n"] >= 15:
        out.append({"lvl": "serious", "title": "慢在轮数",
                    "detail": "模型来回 %d 轮，平均每轮 %s，模型时间占 %.0f%%。单轮不慢，是轮数把总时长堆上去的。"
                              % (q["rounds_n"], fmt(model / q["rounds_n"]), model / tot * 100)})
    heavy = sorted(q["rounds"], key=lambda r: -r["gen"])[:2]
    if heavy and heavy[0]["gen"] >= 8:
        out.append({"lvl": "warning", "title": "有几轮思考与输出特别久", "round": heavy[0]["i"],
                    "detail": "；".join("第 %d 轮 %s（思考 %d token、输出 %d token）" % (r["i"], fmt(r["gen"]), r["rsn"], r["out"])
                                       for r in heavy) + "。"})
    side = {c["cat"]: c for c in q["cats"]}
    detour = sum(side[k]["n"] for k in ("adhoc", "read") if k in side)
    if detour and detour / q["rounds_n"] >= 0.3:
        out.append({"lvl": "warning", "title": ("多数" if detour / q["rounds_n"] > 0.5 else "不少") + "轮次不是在调技能脚本",
                    "detail": "%d 轮里：调技能脚本 %d 轮；临时命令 %d 轮、读技能文件 %d 轮，合计 %s。"
                              % (q["rounds_n"], side.get("script", {}).get("n", 0), side.get("adhoc", {}).get("n", 0),
                                 side.get("read", {}).get("n", 0), fmt(sum(side[k]["secs"] for k in ("adhoc", "read") if k in side)))})
    if model and T["ttft"] / model >= 0.4 and T["ttft"] / q["rounds_n"] >= TTFT_SLOW:
        out.append({"lvl": "warning", "title": "等首字占模型时间的大头",
                    "detail": "模型时间里 %.0f%% 在等第一个字（平均每轮 %s）。可能是模型服务排队，或每轮发送的上下文太长。"
                              % (T["ttft"] / model * 100, fmt(T["ttft"] / q["rounds_n"]))})
    if q["speed_median"] and q["speed_median"] < SPEED_SLOW:
        out.append({"lvl": "warning", "title": "模型生成速度偏慢",
                    "detail": "生成速度中位数只有每秒 %.0f token。可能是模型服务侧限速或负载高。" % q["speed_median"]})
    if q["ctx_first"] and q["ctx_peak"] / q["ctx_first"] >= 2:
        all_in = q["tok"]["in"] + q["tok"]["read"] + q["tok"]["write"]
        out.append({"lvl": "info", "title": "上下文越滚越大",
                    "detail": "每轮发给模型的上下文从 %.1fK 涨到 %.1fK token（每轮都要整段重发）；缓存命中 %.0f%%。"
                              % (q["ctx_first"] / 1000, q["ctx_peak"] / 1000, q["tok"]["read"] / max(1, all_in) * 100)})
    return out or [{"lvl": "good", "title": "没有明显的慢点", "detail": "各段用时分布均匀。"}]


def questions(sessions, detail: bool, grmp: List[dict], tz: datetime.tzinfo,
              session: Optional[str] = None, since: Optional[int] = None, until: Optional[int] = None,
              last: int = 1) -> tuple:
    """挑出要报告的提问(按时间,取最近 last 次;给了时间段就取段内全部)。返回 (提问列表, 遮蔽统计)。"""
    b = Builder(detail, grmp, tz)
    picked = []
    for s in sessions:
        if session and not s.id.startswith(session):
            continue
        if not session and s.parent:
            continue                                   # 子会话(子任务)算在父会话那次工具调用里
        cur = None
        for mid, m, ps in s.messages:
            if m.get("role") == "user":
                text = " ".join(p.get("text", "") for _, _, p in ps if p.get("type") == "text" and not p.get("synthetic")).strip()
                cur = (s.id, m, text, [])
                picked.append(cur)
            elif cur is not None and m.get("role") == "assistant" and ps:
                cur[3].append((m, ps))
    picked = [p for p in picked if p[3]]
    if since is not None or until is not None:
        picked = [p for p in picked if (since is None or p[1]["time"]["created"] >= since)
                  and (until is None or p[1]["time"]["created"] <= until)]
    else:
        picked = sorted(picked, key=lambda p: p[1]["time"]["created"])[-max(1, last):]
    picked = sorted(picked, key=lambda p: p[1]["time"]["created"])
    out = [b.question(i, sid, m, text, rr) for i, (sid, m, text, rr) in enumerate(picked, 1)]
    return out, dict(b.mask.count)
