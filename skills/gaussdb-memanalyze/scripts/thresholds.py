"""memanalyze thresholds (immutable; every deterministic finding cites one)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    # L1 instance: dynamic memory used, as a % of max_dynamic_memory
    dyn_notice_pct: float = 70.0
    dyn_warn_pct: float = 80.0
    dyn_critical_pct: float = 90.0
    # Peak reached this % while current usage is back under dyn_notice_pct:
    # the spike already happened and is over. Saying nothing would hide it.
    peak_fallback_pct: float = 80.0
    # other_used_memory as a % of process_used_memory (unmanaged / 3rd-party)
    other_pct_notice: float = 20.0

    # L2 contexts
    context_pct_warn: float = 15.0      # one context's share of all context memory
    context_frag_pct: float = 50.0      # free/total ratio -> fragmentation
    context_frag_min_mb: float = 100.0  # ignore fragmentation on tiny contexts

    # L3 sessions
    session_hog_mb: float = 1024.0      # one session's peak memory
    idle_xact_mem_mb: float = 256.0     # idle-in-transaction still holding memory

    # L4 SQL / L5 operator
    spill_warn_mb: float = 1024.0       # spilled to disk -> work_mem too small
    estimate_dev_ratio: float = 10.0    # actual peak / estimated memory
    rows_dev_ratio: float = 10.0        # tuple_processed / estimated_rows
    skew_warn_pct: float = 50.0         # memory_skew_percent across DNs

    # watch / trend
    trend_flat_pct: float = 10.0        # peak growth below this -> flat, no verdict
    trend_fallback_pct: float = 30.0    # drop from peak above this -> spike
    trend_leak_critical_pct: float = 100.0   # doubled over the window -> CRITICAL


def default_thresholds() -> Thresholds:
    return Thresholds()
