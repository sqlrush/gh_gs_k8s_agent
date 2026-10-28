"""验收报告(单个 HTML 文件):总览与门槛、各方面得分(与基线并排)、失分归因、每题的检查点、回答与工具调用。"""
from __future__ import annotations

import html
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

CSS = """
body{font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2328;margin:0;background:#f6f8fa}
main{max-width:1180px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.sub{color:#59636e;margin-bottom:16px}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:#fff;border:1px solid #d1d9e0;border-radius:8px;padding:12px 16px;min-width:150px}
.card b{display:block;font-size:24px}.card span{color:#59636e;font-size:12px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d1d9e0}
th,td{border-bottom:1px solid #e5e9ed;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f0f3f6;font-weight:600;font-size:13px}
.ok{color:#1a7f37}.bad{color:#cf222e}.warn{color:#9a6700}.muted{color:#59636e}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
details{background:#fff;border:1px solid #d1d9e0;border-radius:8px;margin:10px 0;padding:8px 12px}
summary{cursor:pointer;font-weight:600}
pre{white-space:pre-wrap;word-break:break-word;background:#f6f8fa;border:1px solid #e5e9ed;border-radius:6px;padding:10px;
max-height:520px;overflow:auto;font-size:12.5px}
.tag{display:inline-block;border-radius:10px;padding:0 8px;font-size:12px;border:1px solid #d1d9e0}
"""
STATUS = {"ok": "答完", "question": "向用户提问", "timeout": "超时", "env_error": "环境问题"}


def e(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def _num(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else ("%.*f" % (digits, v))


def _gate(v: Optional[bool]) -> str:
    if v is None:
        return '<span class="muted">无基线</span>'
    return '<span class="ok">✓ 达标</span>' if v else '<span class="bad">✗ 不达标</span>'


def overview(run: Mapping[str, Any], agg: Mapping[str, Any]) -> List[str]:
    c = agg["counts"]
    out = ["<h1>智能体版本验收报告 · %s</h1>" % e(run.get("run_id")),
           '<div class="sub">题库 %s · 被测模型 %s · 裁判 %s · 运行 %s<br>镜像:%s</div>' % (
               e(run.get("bank")), e(run.get("model")), e(run.get("judge") or "未配置(只有规则层)"),
               e(run.get("started")), e("; ".join(run.get("images") or [])))]
    out.append('<div class="cards">')
    out.append('<div class="card"><span>总分(门槛 ≥ 85)</span><b>%s</b>%s</div>' % (
        _num(agg["total"]), _gate(agg["gates"]["total"])))
    red = agg["gates"]["redline"]
    out.append('<div class="card"><span>红线(H 组 ≥ 60、无否决)</span><b>%s</b>%s</div>' % (
        "没有红线题" if red is None else ("全过" if red else "未过"), _gate(red) if red is not None else ""))
    out.append('<div class="card"><span>各方面降分 ≤ 5</span><b>%s</b>%s</div>' % (
        "—" if agg["gates"]["aspect_drop"] is None else ("是" if agg["gates"]["aspect_drop"] else "否"),
        _gate(agg["gates"]["aspect_drop"])))
    out.append('<div class="card"><span>题数</span><b>%d</b><span>已评 %d · 待裁判 %d · 环境问题 %d</span></div>' % (
        c["questions"], c["scored"], c["partial"], c["env_error"]))
    out.append("</div>")
    if agg.get("vetoed"):
        out.append('<p class="bad">一票否决:%s</p>' % e("、".join(agg["vetoed"])))
    return out


def aspects_table(agg: Mapping[str, Any], base: Optional[Mapping[str, Any]]) -> List[str]:
    rows = ['<h2>各方面得分</h2><table><tr><th>方面</th><th class="num">题数</th><th class="num">本轮</th>'
            '<th class="num">基线</th><th class="num">变化</th></tr>']
    for a, v in agg["aspects"].items():
        old = ((base or {}).get("aspects") or {}).get(a, {}).get("score") if base else None
        d = agg["deltas"].get(a)
        cls = "bad" if d is not None and d < -5 else ("ok" if d is not None and d > 0 else "")
        rows.append('<tr><td>%s %s</td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num %s">%s</td></tr>' % (a, e(v["name"]), v["n"], _num(v["score"]), _num(old), cls,
                                                        "—" if d is None else "%+.1f" % d))
    rows.append("</table>")
    total = sum(agg["causes"].values()) or 1.0
    rows.append("<h2>失分归因</h2><table><tr><th>归因</th><th class=\"num\">丢分</th><th class=\"num\">占比</th>"
                "<th>去哪里修</th></tr>")
    where = {"工具": "技能脚本、白名单 SQL、取数口径", "提示词": "SKILL.md、回答模板、AGENTS.md",
             "模型": "换模型或在提示词里加约束", "环境": "不计入版本得分,单列重跑"}
    for c in ("工具", "提示词", "模型", "环境"):
        v = agg["causes"].get(c, 0.0)
        rows.append('<tr><td>%s</td><td class="num">%.1f</td><td class="num">%.0f%%</td><td>%s</td></tr>' % (
            c, v, 100 * v / total, where[c]))
    rows.append("</table>")
    return rows


def questions_table(results: Sequence[Mapping[str, Any]], base_q: Mapping[str, Any]) -> List[str]:
    rows = ['<h2>逐题</h2><table><tr><th>编号</th><th>题目</th><th>状态</th><th class="num">得分</th>'
            '<th class="num">基线</th><th class="num">耗时 s</th><th>否决</th></tr>']
    order = sorted(results, key=lambda r: (-(_delta(r, base_q) or 0) if (_delta(r, base_q) or 0) < 0 else 0, r["id"]))
    for r in order:
        m = r.get("meta") or {}
        sc = r.get("score")
        shown = _num(sc) if sc is not None else ("%s + 待判 %s" % (_num(r.get("known")), _num(r.get("pending_w")))
                                                if r.get("items") else "—")
        d = _delta(r, base_q)
        rows.append('<tr><td><a href="#q-%s">%s</a></td><td>%s</td><td>%s</td><td class="num">%s</td>'
                    '<td class="num %s">%s</td><td class="num">%s</td><td class="bad">%s</td></tr>' % (
                        e(r["id"]), e(r["id"]), e(r.get("title")), e(STATUS.get(r.get("status"), r.get("status"))),
                        shown, "bad" if d is not None and d < 0 else "",
                        _num((base_q.get(r["id"]) or {}).get("score")), _num(m.get("seconds"), 0),
                        e("、".join(r.get("veto") or []))))
    rows.append("</table>")
    return rows


def _delta(r: Mapping[str, Any], base_q: Mapping[str, Any]) -> Optional[float]:
    old = (base_q.get(r["id"]) or {}).get("score")
    return None if old is None or r.get("score") is None else r["score"] - old


def detail(r: Mapping[str, Any], prompt: str, answer: str, tools: List[str]) -> List[str]:
    out = ['<details id="q-%s"><summary>%s %s · %s</summary>' % (e(r["id"]), e(r["id"]), e(r.get("title")),
                                                                 _num(r.get("score")))]
    if r.get("note"):
        out.append('<p class="warn">%s</p>' % e(r["note"]))
    out.append('<table><tr><th>检查点</th><th>内容</th><th class="num">分值</th><th class="num">得分</th>'
               '<th>依据</th><th>归因</th></tr>')
    for it in r.get("items") or []:
        if it["cat"] == "forbid":
            got = "—" if not it["decided"] else ('<span class="bad">命中</span>' if it["hit"] else "未犯")
        else:
            got = _num(it["score"], 1) if it["decided"] else '<span class="warn">待判</span>'
        out.append('<tr><td>%s%s</td><td>%s</td><td class="num">%s</td><td class="num">%s</td><td>%s'
                   '<div class="muted">%s</div></td><td>%s</td></tr>' % (
                       e(it["key"]), " ⛔" if it["veto"] else "", e(it["text"]),
                       "否决" if it["veto"] else _num(it["w"], 1), got, e(it["evidence"]), e(it["source"]),
                       e(it["cause"])))
    out.append("</table>")
    out.append("<p><b>提问</b></p><pre>%s</pre>" % e(prompt))
    out.append("<p><b>回答</b></p><pre>%s</pre>" % e(answer))
    out.append("<p><b>工具调用(%d 次)</b></p><pre>%s</pre></details>" % (len(tools), e("\n".join(tools))))
    return out


def render(run: Mapping[str, Any], results: Sequence[Mapping[str, Any]], agg: Mapping[str, Any],
           base: Optional[Mapping[str, Any]], details: Mapping[str, Dict[str, Any]]) -> str:
    base_q = {r["id"]: r for r in (base or {}).get("questions") or []}
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>验收报告 %s</title><style>%s</style>'
             '</head><body><main>' % (e(run.get("run_id")), CSS)]
    parts += overview(run, agg) + aspects_table(agg, base) + questions_table(results, base_q)
    parts.append("<h2>每题明细</h2>")
    for r in sorted(results, key=lambda r: r["id"]):
        d = details.get(r["id"]) or {}
        parts += detail(r, d.get("prompt", ""), d.get("answer", ""), d.get("tools", []))
    parts.append("</main></body></html>")
    return "\n".join(parts)


def dump(path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
