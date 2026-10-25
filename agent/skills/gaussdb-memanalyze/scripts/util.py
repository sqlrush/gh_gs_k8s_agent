"""Formatting and coercion helpers (pure)."""
from __future__ import annotations


def f(x, default: float = 0.0) -> float:
    """Coerce a possibly-None numeric (Decimal / float / str) to float."""
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def i64(x, default: int = 0) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def f2(x) -> str:
    return f"{f(x):.2f}"


def pct(part, whole) -> float:
    """Percentage, 0.0 when the denominator is zero (never raises)."""
    w = f(whole)
    if w == 0:
        return 0.0
    return f(part) / w * 100.0


def human_mb(mb) -> str:
    """MB-valued number -> human string. GaussDB memory views report MB."""
    v = f(mb)
    if abs(v) >= 1024:
        return f"{v / 1024:.2f} GB"
    return f"{int(v)} MB"


def trunc(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)] + "…"


HINT_MARK = "\n提示:"   # common/grmp/hints.with_hint 追加的中文提示,整段保留


def summarize_err(exc) -> str:
    """First line of a DBError, trimmed — enough to say why a layer went blind.

    只取第一行会把 with_hint 追加在第二行的「提示:…」整段丢掉,那段才是客户能照做的内容:
    原文照旧只留第一行、截 200 字,提示原样跟在后面。
    """
    text = str(exc).strip()
    head, sep, hint = text.partition(HINT_MARK)
    first = head.strip().splitlines()[0] if head.strip() else "unknown error"
    return trunc(first, 200) + sep + hint
