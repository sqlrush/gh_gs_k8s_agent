"""Small formatting helpers — port of internal/probe/wdr/util.go."""
from __future__ import annotations


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


def mib(byts: int) -> str:
    """Format a byte count as a human MiB string."""
    return f2(float(byts) / (1 << 20)) + "MiB"
