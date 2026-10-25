"""Agent-friendly Markdown primitives (port of internal/render/markdown.go).

Vendored per-skill: produces stable section headings that SKILL.md references.
"""
from __future__ import annotations


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
