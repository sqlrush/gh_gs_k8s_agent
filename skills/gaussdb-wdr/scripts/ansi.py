"""ANSI colors + CJK-aware box-drawing table — port of internal/probe/wdr/ansi.go."""
from __future__ import annotations

from model import Severity

ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"
ANSI_DIM = "\x1b[2m"
ANSI_GREEN = "\x1b[32m"
ANSI_YELLOW = "\x1b[33m"
ANSI_ORANGE = "\x1b[38;5;208m"
ANSI_RED = "\x1b[31m"


def colorize(s: str, code: str, enabled: bool) -> str:
    if not enabled or not code:
        return s
    return code + s + ANSI_RESET


def severity_color(s: Severity) -> str:
    return {Severity.CRITICAL: ANSI_RED, Severity.WARN: ANSI_ORANGE,
            Severity.NOTICE: ANSI_YELLOW}.get(s, ANSI_GREEN)


def _is_wide(o: int) -> bool:
    """Whether a codepoint occupies two terminal cells (CJK / fullwidth / emoji)."""
    return (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0x303E or 0x3041 <= o <= 0x33FF or
            0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xA000 <= o <= 0xA4CF or
            0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F or
            0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6 or 0x1F300 <= o <= 0x1FAFF or
            0x2600 <= o <= 0x27BF)


def display_width(s: str) -> int:
    """Terminal column width of s (CJK/emoji = 2)."""
    return sum(2 if _is_wide(ord(ch)) else 1 for ch in s)


def _pad_right(s: str, w: int) -> str:
    d = display_width(s)
    return s if d >= w else s + " " * (w - d)


def box_table(headers: list[str], rows: list[list[str]]) -> str:
    """Box-drawing table with CJK-aware column alignment. Cells must be plain
    text (no pre-applied ANSI codes)."""
    n = len(headers)
    width = [display_width(h) for h in headers]
    for row in rows:
        for i in range(min(n, len(row))):
            d = display_width(row[i])
            if d > width[i]:
                width[i] = d
    out = []

    def rule(left, mid, right):
        cells = ["─" * (width[i] + 2) for i in range(n)]
        out.append(left + mid.join(cells) + right)

    def line(cells):
        parts = []
        for i in range(n):
            c = cells[i] if i < len(cells) else ""
            parts.append(" " + _pad_right(c, width[i]) + " ")
        out.append("│" + "│".join(parts) + "│")

    rule("┌", "┬", "┐")
    line(headers)
    rule("├", "┼", "┤")
    for row in rows:
        line(row)
    rule("└", "┴", "┘")
    return "\n".join(out) + "\n"
