#!/usr/bin/env python3
"""回答格式检查:一份回答是否照「回答模板」写(2026-10-26 回答模板化;评估用,不进客户环境)。

    python3 tools/answer_check.py --kind health  --file answer.md
    python3 tools/answer_check.py --kind qa      < answer.md
    kind: health | fault | sqltune | proctune | explain | ranking | qa | wdr

模板靠提示词执行,模型偶尔会漏段、并段、把出处写回旧格式 —— 这里在回归评估里把它们抓出来。只看格式,
不判断内容对不对(内容由人评审);引用编号在不在库里交给 `kb.py cite-check`。有 ✗ 退出 1。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.kb import citestyle  # noqa: E402

# 各类回答的段落(按顺序)。每段给几种可接受的写法,命中其一即可。
SECTIONS = {
    "health": (("现象",), ("根因和证据链", "根因与证据链"), ("解决方案",), ("总结和结论", "总结")),
    "fault": (("现象",), ("根因",), ("解决方案",), ("总结",)),
    "sqltune": (("现状与趋势",), ("SQL 文本和执行计划解读", "执行计划解读"), ("怎么优化",), ("总结",)),
    "proctune": (("存储过程与热点解读", "热点解读"), ("怎么优化",), ("总结",)),
    "explain": (("一句话结论",), ("执行计划",), ("计划走查",)),
    "ranking": (("榜单",), ("为什么排上来",), ("建议下一步",)),
    "qa": (("解答正文",), ("举例说明",), ("引申和拓展", "引申与拓展")),
    "wdr": (("负载画像",), ("问题与元凶",), ("解决方案",), ("总结和结论",), ("附录",)),
}
STATUS_BAND = {"health": "总体状态", "fault": "总体状态", "wdr": "总体状态", "sqltune": "验证状态", "proctune": "验证状态"}
BOTH_VIEWS = ("health", "fault", "sqltune", "wdr")
FORBIDDEN_IN_DIAGNOSIS = ("根据知识库", "知识库显示", "知识库检索", "客户知识库参照")
_FOLLOWUP = re.compile(r"需要(就|我|了解|看|对|把).{0,60}?[吗么][?？]")
_FONT = re.compile(r"<font color=(#[0-9a-fA-F]{6})>(.*?)</font>", re.S)
_AFTER = re.compile(r"\s{0,2}[(（]\s*引用自\s*([^)）]*?)\s+([A-Z][^\s,，;;:：、。)）]*)")
_INSIDE = re.compile(r"[(（]\s*引用自\s*([^)）]*?)\s+([A-Z][^\s,，;;:：、。)）]*)[^)）]*[)）]\s*$")
_HEADING = re.compile(r"^\s*(#{1,6}\s|\*\*|[①②③④⑤])")


@dataclass(frozen=True)
class Check:
    ok: bool
    name: str
    detail: str = ""


def _heading_lines(text: str) -> List[Tuple[int, str]]:
    out, pos = [], 0
    for line in text.splitlines(keepends=True):
        if _HEADING.match(line):
            out.append((pos, line))
        pos += len(line)
    return out


def check_sections(text: str, kind: str) -> Check:
    heads = _heading_lines(text)
    found: List[int] = []
    for alts in SECTIONS[kind]:
        hit = next((p for p, line in heads if any(a in line for a in alts)), None)
        if hit is None:
            return Check(False, "段落", "缺「%s」" % alts[0])
        found.append(hit)
    if found != sorted(found):
        return Check(False, "段落", "顺序不对:应为 " + " → ".join(a[0] for a in SECTIONS[kind]))
    return Check(True, "段落", " → ".join(a[0] for a in SECTIONS[kind]))


def check_opening(text: str, kind: str) -> Optional[Check]:
    if kind == "qa":
        first = next((line for line in text.splitlines() if line.strip()), "")
        ok = "解答来源" in first
        return Check(ok, "开头", "第一行是解答来源" if ok else "第一行不是「解答来源」")
    band = STATUS_BAND.get(kind)
    if not band:
        return None
    head = text[: max(400, len(text) // 4)]
    return Check(band in head, "开头", ("开头有%s" % band) if band in head else "开头没有「%s」" % band)


def check_followup(text: str, kind: str = "") -> Check:
    if kind == "wdr" and "## ④" in text:            # WDR 的展开提议在第 ④ 段,后面还有很长的附录
        end = text.find("## 附录")
        tail = text[text.index("## ④"):end if end > 0 else len(text)]
    else:
        tail = text[-max(600, len(text) // 5):]
    ok = bool(_FOLLOWUP.search(tail))
    return Check(ok, "展开提议", "结尾有「需要就 … 吗?」" if ok else "结尾没有展开提议")


def check_views(text: str, kind: str) -> Optional[Check]:
    if kind not in BOTH_VIEWS:
        return None
    a, m = text.count("应用视角"), text.count("数据库机制")
    return Check(a > 0 and m > 0, "两种读者", "应用视角 %d 处、数据库机制 %d 处" % (a, m))


def citation_checks(text: str, kind: str) -> List[Check]:
    out: List[Check] = []
    if "〔" in text and re.search(r"〔(规范|案例|手册|缺陷|基线|产品文档|Wiki|画像|修正)\s", text):
        out.append(Check(False, "引用写法", "还在用旧写法〔类型 ID〕"))
    if "本行无先例" in text:
        out.append(Check(False, "兜底说法", "旧兜底说法「本行无先例」,应为「知识库中未找到相关内容…」"))
    if kind != "qa":
        bad = [w for w in FORBIDDEN_IN_DIAGNOSIS if w in text]
        if bad:
            out.append(Check(False, "不特意声明来源", "出现了 " + "、".join(bad)))
    spans = list(_FONT.finditer(text))
    no_src, wrong = [], []
    for sp in spans:
        m = _AFTER.match(text, sp.end()) or _INSIDE.search(sp.group(2))   # 括号写进了标签里也认(只是括号也上了色)
        if m is None:
            if kind != "qa":                      # 知识问答的出处在第一行,句后可以不带括号
                no_src.append(sp.group(2)[:20])
            continue
        lib, tok = m.group(1).strip(), m.group(2)
        found = citestyle.library_for(tok)
        if found is None:
            continue
        if found[0] != lib:
            wrong.append("%s 写成了「%s」(应为%s)" % (tok, lib, found[0]))
        if found[1].lower() != sp.group(1).lower():
            wrong.append("%s 的颜色应为 %s" % (tok, found[1]))
    if no_src:
        hint = ";出处括号被包进了反引号(显示成代码),括号要写成普通文字" if re.search(r"</font>\s*`[(（]\s*引用自", text) else ""
        out.append(Check(False, "出处括号", "%d 处上色的句子后面没有「(引用自 …)」%s:%s" % (len(no_src), hint, ";".join(no_src[:3]))))
    if wrong:
        out.append(Check(False, "库名与颜色", ";".join(wrong[:5])))
    if spans and not no_src and not wrong:
        out.append(Check(True, "引用", "%d 处上色引用,出处与颜色都对" % len(spans)))
    return out


def check(text: str, kind: str) -> List[Check]:
    if kind not in SECTIONS:
        raise ValueError("kind 只能是 %s" % " / ".join(SECTIONS))
    checks = [check_sections(text, kind), check_opening(text, kind), check_followup(text, kind), check_views(text, kind)]
    return [c for c in checks if c is not None] + citation_checks(text, kind)


def render(checks: Sequence[Check]) -> str:
    lines = ["%s %s:%s" % ("✓" if c.ok else "✗", c.name, c.detail) for c in checks]
    bad = sum(1 for c in checks if not c.ok)
    return "\n".join(lines + ["合计 %d 项,%d 项不合格" % (len(checks), bad)])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kind", required=True, choices=sorted(SECTIONS))
    ap.add_argument("--file")
    args = ap.parse_args(argv)
    text = pathlib.Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    checks = check(text, args.kind)
    print(render(checks))
    return 1 if any(not c.ok for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
