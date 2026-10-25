"""Small formatting/severity helpers — port of health/util.go + shared bits of
xact.go (sev_by_duration), locks.go (escalate), repl.go (human_bytes)."""
from __future__ import annotations

from model import Severity


def f2(v: float) -> str:
    return f"{v:.2f}"


def i64(v: int) -> str:
    return str(int(v))


HINT_MARK = "\n提示:"   # common/grmp/hints.with_hint 追加的中文提示,整段保留
ERR_HEAD_MAX = 160


def summarize_err(exc: Exception) -> str:
    """Make a degrade note: truncate the raw error, keep the Chinese hint whole.

    GRMP 路径的报错前缀(status / task_id)本来就长,一刀切在 160 字会把
    「提示:…」切成半句——那段才是客户能照做的内容,不能截。
    """
    head, sep, hint = str(exc).partition(HINT_MARK)
    if len(head) > ERR_HEAD_MAX:
        head = head[:ERR_HEAD_MAX] + "…"
    return head + sep + hint


def trunc(s: str, n: int) -> str:
    """Shorten s for a table cell (newlines flattened, rune-safe)."""
    s = (s or "").replace("\n", " ")
    if len(s) > n:
        return s[:n] + "…"
    return s


def sev_by_duration(secs: float, notice: int, warn: int, crit: int) -> Severity:
    """Map an elapsed duration (seconds) to a severity band (xact & locks)."""
    if crit > 0 and secs >= crit:
        return Severity.CRITICAL
    if secs >= warn:
        return Severity.WARN
    if secs >= notice:
        return Severity.NOTICE
    return Severity.OK


def escalate(s: Severity) -> Severity:
    """Bump one severity band, capped at CRITICAL."""
    if s < Severity.CRITICAL:
        return Severity(int(s) + 1)
    return s


def human_bytes(b: int) -> str:
    """Format a byte count compactly (repl/schema)."""
    b = int(b)
    if b >= 1 << 30:
        return f"{b / (1 << 30):.1f}G"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.1f}M"
    if b >= 1 << 10:
        return f"{b / (1 << 10):.1f}K"
    return f"{b}B"
