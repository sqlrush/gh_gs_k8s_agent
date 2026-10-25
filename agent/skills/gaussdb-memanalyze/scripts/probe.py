"""Runtime view discovery — the reason this skill runs on both openGauss and GaussDB.

The memory views differ by product, by version, and between centralised and
distributed deployments, in *both* their names and their column sets. So we
hardcode nothing: each slot lists candidate views in preference order, we ask
the catalog which of them exist and what columns they really have, and the
collectors build their SELECTs from that.

`select()` and `columns_expr()` are pure — "given what exists, which view and
which columns" is decided without touching a database, and is unit tested.
"""
from __future__ import annotations

from typing import Mapping

import common
from common import access  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_float, as_int  # noqa: E402
from model import Catalog, ViewInfo

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/memanalyze/ —— 两条路径共用同一份定义

# Candidate views per slot, most-preferred first.
CANDIDATES: Mapping[str, tuple] = {
    # L1 — instance-wide memory breakdown (memorytype / memorymbytes rows)
    "instance": ("gs_total_memory_detail", "pv_total_memory_detail",
                 "dbe_perf.global_memory_node_detail"),
    # L2 — memory contexts
    "thread_ctx": ("gs_thread_memory_context", "pv_thread_memory_context"),
    "session_ctx": ("gs_session_memory_detail", "pv_session_memory_detail"),
    "shared_ctx": ("gs_shared_memory_detail", "pv_shared_memory_detail"),
    # L3 — per-session memory, plus what each session is running
    "session_mem": ("dbe_perf.session_memory", "gs_session_memory"),
    "activity": ("pg_stat_activity",),
    # L4 / L5 — WLM resource tracking, live and historical
    "wlm_session": ("gs_wlm_session_statistics", "pgxc_wlm_session_statistics"),
    "wlm_operator": ("gs_wlm_operator_statistics", "pgxc_wlm_operator_statistics"),
    "wlm_session_hist": ("gs_wlm_session_history", "gs_wlm_session_info"),
    "wlm_operator_hist": ("gs_wlm_operator_history", "gs_wlm_operator_info"),
}

SLOTS = tuple(CANDIDATES)

# Which schemas an unqualified candidate may live in.
_SCHEMAS = ("pg_catalog", "public", "dbe_perf")




_SCHEMA_LIST = ", ".join("'" + s.replace("'", "''") + "'" for s in _SCHEMAS)



#_COLS_BARE = f"""
#SELECT a.attname::text
#FROM pg_class c
#JOIN pg_namespace n ON n.oid = c.relnamespace
#JOIN pg_attribute a ON a.attrelid = c.oid
#WHERE c.relname = %s AND n.nspname IN ({', '.join("'" + s + "'" for s in _SCHEMAS)})
#  AND a.attnum > 0 AND NOT a.attisdropped
#ORDER BY a.attnum"""


def select(slot: str, existing: Mapping[str, tuple]) -> ViewInfo:
    """Pick the highest-priority candidate that actually exists (pure)."""
    for cand in CANDIDATES[slot]:
        cols = existing.get(cand)
        if cols:
            return ViewInfo(name=cand, columns=tuple(cols), available=True)
    return ViewInfo(
        available=False,
        reason="候选视图均不存在：" + "、".join(CANDIDATES[slot]),
    )


def columns_expr(vi: ViewInfo, wanted: tuple) -> str:
    """Build a SELECT list over `wanted`, substituting NULL for columns this
    view does not have (pure).

    Dialect columns come and go — `gs_wlm_operator_history.warning` is absent on
    some versions. Without this, one missing column would fail the whole query
    and blind the layer.
    """
    have = {c.lower() for c in vi.columns}
    return ", ".join(w if w.lower() in have else f"NULL AS {w}" for w in wanted)


def has_col(vi: ViewInfo, col: str) -> bool:
    return col.lower() in {c.lower() for c in vi.columns}


def _columns_of(runner, cand: str) -> tuple:
    """Real column list of one candidate, or () if it does not exist here."""
    if "." in cand:
        schema, rel = cand.split(".", 1)
        rows = runner.run("memanalyze.cols_qualified",
                          {"schema": schema, "relname": rel})
    else:
        rows = runner.run("memanalyze.cols_bare",
                          {"relname": cand, "schemas": _SCHEMA_LIST})
    return tuple(str(r["attname"]) for r in rows)


def probe_views(runner) -> Catalog:
    """Discover which memory views this instance offers.

    Raises DBError only if the catalog itself is unreadable — that means the
    connection is unusable, which is fatal (a missing *memory* view is not).
    """
    existing: dict[str, tuple] = {}
    for slot in SLOTS:
        for cand in CANDIDATES[slot]:
            if cand in existing:
                continue
            cols = _columns_of(runner, cand)
            if cols:
                existing[cand] = cols
    return Catalog(views={slot: select(slot, existing) for slot in SLOTS})


def probe_views_safe(runner) -> tuple:
    """probe_views, but turn a catalog failure into (empty Catalog, reason)."""
    try:
        return probe_views(runner), ""
    except common.DBError as exc:
        return Catalog(), str(exc)
