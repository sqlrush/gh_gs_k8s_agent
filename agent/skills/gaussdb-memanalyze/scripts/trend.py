"""Trend verdict for watch mode (pure functions).

Distinguishing a leak from a spike is a numeric judgement, so the script makes
it — not the LLM:

    leak   memory climbed and stayed up            -> something is not releasing
    spike  memory climbed then fell back           -> one big query, already over
    flat   never rose meaningfully                 -> no incident in this window
"""
from __future__ import annotations

from model import DIM_INSTANCE, Finding, Severity
from thresholds import Thresholds
from util import pct

LEAK = "leak"
SPIKE = "spike"
FLAT = "flat"
INSUFFICIENT = "insufficient"

MIN_SAMPLES = 3


def analyze(samples, th: Thresholds) -> tuple[str, str]:
    """Classify a series of memory readings (MB). Returns (verdict, detail)."""
    xs = [float(s) for s in samples]
    if len(xs) < MIN_SAMPLES:
        return INSUFFICIENT, (
            f"仅 {len(xs)} 个采样点，至少需要 {MIN_SAMPLES} 个才能判定趋势"
        )

    first, last, peak = xs[0], xs[-1], max(xs)
    peak_growth = pct(peak - first, first) if first else 0.0
    drop_from_peak = pct(peak - last, peak) if peak else 0.0
    net_growth = pct(last - first, first) if first else 0.0

    detail = (
        f"起始 {first:.0f} MB → 峰值 {peak:.0f} MB → 末次 {last:.0f} MB"
        f"（峰值增长 {peak_growth:.1f}%，自峰值回落 {drop_from_peak:.1f}%）"
    )

    if peak_growth < th.trend_flat_pct:
        return FLAT, detail
    if drop_from_peak >= th.trend_fallback_pct:
        return SPIKE, detail
    if net_growth >= th.trend_flat_pct:
        return LEAK, detail
    return FLAT, detail


def finding(samples, th: Thresholds):
    """The Finding a trend verdict warrants, or None when nothing happened."""
    verdict, detail = analyze(samples, th)
    if verdict in (FLAT, INSUFFICIENT):
        return None

    xs = [float(s) for s in samples]
    net_growth = pct(xs[-1] - xs[0], xs[0]) if xs[0] else 0.0

    if verdict == LEAK:
        sev = (Severity.CRITICAL if net_growth >= th.trend_leak_critical_pct
               else Severity.WARN)
        return Finding(
            dimension=DIM_INSTANCE, code="MEM_TREND_LEAK", severity=sev,
            metric="动态内存趋势", value=f"净增长 {net_growth:.1f}%",
            threshold=f">{th.trend_flat_pct:.0f}% 且未回落",
            evidence=detail + "；内存持续上升且未回落，疑似泄漏或缓存不释放",
        )

    return Finding(
        dimension=DIM_INSTANCE, code="MEM_TREND_SPIKE", severity=Severity.NOTICE,
        metric="动态内存趋势", value=f"峰值后回落",
        threshold=f"自峰值回落 >{th.trend_fallback_pct:.0f}%",
        evidence=detail + "；尖峰后已回落，指向单次大查询而非泄漏",
    )
