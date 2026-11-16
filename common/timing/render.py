"""出报告:文本版(终端里看)与 HTML(浏览器里看,单文件、不联网、不引外部资源)。"""
from __future__ import annotations

import json
import pathlib

from .analysis import KIND_NAMES, fmt

TEMPLATE = pathlib.Path(__file__).with_name("report.html")
_LV = {"serious": "严重", "warning": "注意", "info": "提示", "good": "正常"}


def text(meta: dict, qs: list) -> str:
    L = ["智能体交互耗时报告（%s）" % ("详细版" if meta["detail"] else "摘要版"),
         "生成于 %s · 会话数据 %s · GRMP 计时 %s" % (meta["generated"], meta["source"], meta["grmp_source"]), ""]
    if meta["detail"]:
        L.insert(2, "注:含思考原文、命令参数与工具输出,只在内部查看;已遮掉 %s。" % (meta["masked_text"] or "无"))
    for q in qs:
        T = q["totals"]
        L.append("=" * 78)
        L.append("第 %d 问  %s  「%s」" % (q["idx"], q["start"], q["text"]))
        L.append("总时长 %s | 模型 %d 轮 | 工具 %d 次 | GRMP %d 次%s | 上下文 %.1fK → %.1fK token | 模型 %s"
                 % (fmt(q["total"]), q["rounds_n"], q["tools_n"], q["grmp_n"],
                    "(失败 %d)" % q["grmp_fail"] if q["grmp_fail"] else "", q["ctx_first"] / 1000, q["ctx_peak"] / 1000, q["model"]))
        for k in ("ttft", "gen", "grmp", "local", "idle"):
            share = T[k] / q["total"] * 100 if q["total"] else 0
            L.append("  %-16s %10s  %5.1f%%  %s" % (KIND_NAMES[k], fmt(T[k]), share, "#" * int(round(share / 2.5))))
        L.append("结论:")
        L += ["  [%s] %s:%s" % (_LV[d["lvl"]], d["title"], d["detail"]) for d in q["diag"]]
        if q["grmp_scripts"]:
            L.append("GRMP 调用(按总用时):")
            L += ["  %-34s %3d 次  %10s%s" % (g["name"], g["n"], fmt(g["secs"]), "  失败 %d" % g["fail"] if g["fail"] else "")
                  for g in q["grmp_scripts"][:10]]
        L.append("最慢 10 段:")
        L += ["  %2d. 第 %2d 轮  %-30s %10s  %4.1f%%" % (n, s["round"], s["what"][:30], fmt(s["secs"]), s["share"] * 100)
              for n, s in enumerate(q["slow"], 1)]
        L.append("最慢 3 轮:")
        for r in sorted(q["rounds"], key=lambda r: -(r["end"] - r["t"]))[:3]:
            L.append("  ── 第 %d 轮  %s  等首字 %.2f | 思考 %.1f 秒 %d token | 输出 %d token | 工具 %.2f(GRMP %.2f)"
                     % (r["i"], fmt(r["end"] - r["t"]), r["ttft"], r["think"]["secs"], r["rsn"], r["out"],
                        r["grmp"] + r["local"], r["grmp"]))
            L += ["     为什么:" + w for w in r["why"]]
            if meta["detail"]:
                for p in r["think"]["parts"][:1]:
                    L.append("     思考:" + p["text"][:240].replace("\n", " ") + ("…" if p["chars"] > 240 else ""))
            for t in r["tools"]:
                if meta["detail"]:
                    L.append("     命令:" + t["cmd"][:240].replace("\n", " ⏎ ") + ("…" if t["cmd_chars"] > 240 else ""))
                L.append("     %s:退出码 %s,输出 %d 字 / %d 行%s" % (t["label"], "—" if t["exit"] is None else t["exit"], t["out_chars"], t["out_lines"],
                                                             ",被截断" if t["truncated"] else ""))
                L += ["       GRMP %-28s %-8s %8.2f 秒  %s 行%s" % (g["name"], g["db"], g["e"] - g["s"], g["rows"],
                                                               "" if g["ok"] else "  失败 " + g["status"]) for g in t["grmp"]]
        L.append("每轮明细:  轮   开始    等首字  思考输出   工具   其中GRMP   空档   上下文  动作")
        L += ["          %3d %7.1f %7.2f %8.2f %7.2f %8.2f %7.2f %7.1fK  %s"
              % (r["i"], r["t"], r["ttft"], r["gen"], r["grmp"] + r["local"], r["grmp"], r["idle"], r["ctx"] / 1000, r["act"])
              for r in q["rounds"]]
        L.append("")
    return "\n".join(L)


def html(meta: dict, qs: list, text_version: str) -> str:
    data = {"meta": meta, "questions": qs, "text": text_version}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", blob)
