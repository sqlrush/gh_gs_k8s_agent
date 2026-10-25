"""Agent-friendly Markdown primitives (port of internal/render/markdown.go).

Vendored per-skill: produces stable section headings that SKILL.md references.
"""
from __future__ import annotations

import math


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a GFM table, escaping pipes; rows padded/truncated to header len."""
    n = len(headers)
    esc_headers = [h.replace("|", "\\|") for h in headers]
    out = ["| " + " | ".join(esc_headers) + " |", "|" + "---|" * n]
    for row in rows:
        cells = []
        for i in range(n):
            cells.append(row[i].replace("|", "\\|") if i < len(row) else "")
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def _longest_backtick_run(s: str) -> int:
    longest = current = 0
    for ch in s:
        if ch == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def code_block(lang: str, body: str) -> str:
    """Fenced block; fence extended past the longest backtick run in body."""
    fence = "`" * max(3, _longest_backtick_run(body) + 1)
    return f"{fence}{lang}\n{body.rstrip(chr(10))}\n{fence}\n"


def truncate(s: str, max_len: int) -> str:
    """Shorten s to max_len runes with an ellipsis. '' when max_len < 1."""
    if max_len < 1:
        return ""
    if len(s) <= max_len:
        return s
    if max_len == 1:
        return "…"
    return s[: max_len - 1] + "…"

# 方块字符条:对话里能显示的「图」。
#
# 为什么用字符而不是真图表:skill 的输出是 Markdown 文本,对话窗口渲染不了 SVG 或图片。
# 而纯数字表格看不出"倾斜"——「LOCK_EVENT 86%、IO_EVENT 10%、LWLOCK 3%」要读三个数字
# 再在脑子里比;一条占了八成的横条,一眼就看出来。真正的曲线图/柱状图在大盘上,
# 这里只补"一眼看出比例"这一件事。
#
# 宽度固定 20 格:太宽会在窄终端里折行,折了行比没有图更难读。
BAR_WIDTH = 20
BAR_FULL = "█"
BAR_EMPTY = "░"


def bar(pct: float, width: int = BAR_WIDTH) -> str:
    """0~100 的比例 → 方块条。越界的值夹到两端(不抛错:报告里一个格式问题不该让整份报告失败)。"""
    try:
        v = float(pct)
    except (TypeError, ValueError):
        v = 0.0
    # NaN 要单独挡:它和任何数比较都是 False,夹取那两行放不住它,
    # 到 round() 才抛 ValueError —— 一个格式问题把整份报告弄挂。
    # 用 math.isnan 而不是 `v != v`:客户的代码扫描(SonarQube S1764)会把自比较报成缺陷,
    # 这条已经被 tests/test_code_scan_gate_units.py 钉住了。
    if math.isnan(v):
        v = 0.0
    v = 0.0 if v < 0 else (100.0 if v > 100 else v)
    n = int(round(v / 100.0 * width))
    return BAR_FULL * n + BAR_EMPTY * (width - n)


def share_lines(items: list, width: int = BAR_WIDTH) -> str:
    """[(名字, 比例), …] → 每行一条「名字 ████░░ 比例%」。

    名字右对齐到最长的那个,条形才对得齐 —— 对不齐的条形没法横向比较,
    那就失去了画它的意义。
    """
    rows = [(str(n), float(p)) for n, p in items if str(n)]
    if not rows:
        return ""
    pad = max(len(n) for n, _ in rows)
    out = []
    for name, pct in rows:
        out.append("%s  %s %5.1f%%" % (name.rjust(pad), bar(pct, width), pct))
    return "\n".join(out) + "\n"
