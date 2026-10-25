"""GUC probing: which layers can produce data, and — when they cannot — why.

A layer that is blind must say *why* it is blind, naming the GUC and its target
value. Rendering an empty operator table because `resource_track_level = query`
would read as "no operator problems", which is a lie. Same discipline as the
hypopg `provides_session` guard in sqltune.
"""
from __future__ import annotations

import common
from common import access  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_float, as_int  # noqa: E402
from model import Capability, Catalog

GUCS = (
    "enable_resource_track",
    "resource_track_level",
    "resource_track_cost",
    "enable_resource_record",
    "memory_tracking_mode",
    "max_process_memory",
    "max_dynamic_memory",
    "work_mem",
    "maintenance_work_mem",
    "max_connections",
    "shared_buffers",
)

_ON = frozenset({"on", "true", "1", "yes"})

# GUC names are our own constants, never user input — safe to inline.
_GUC_Q = (
    "SELECT name, setting FROM pg_settings WHERE name IN ("
    + ", ".join("'" + g + "'" for g in GUCS)
    + ")"
)


def read_gucs(runner) -> dict:
    """Read the memory-relevant GUCs. Missing ones are simply absent."""
    try:
        rows = runner.run("memanalyze.gucs")
    except access.QueryError:
        return {}
    return {str(r["name"]): str(r["setting"]) for r in rows}


def _on(gucs: dict, name: str, default: str = "on") -> bool:
    return str(gucs.get(name, default)).strip().lower() in _ON


def assess(gucs: dict, catalog: Catalog) -> Capability:
    """Decide layer availability from the GUCs and the probed catalog (pure)."""
    reasons: dict[str, str] = {}

    track_on = _on(gucs, "enable_resource_track", "on")
    level = str(gucs.get("resource_track_level", "query")).strip().lower()
    # 没读到(白名单脚本是旧版没取它,或本内核没有这个参数)不等于 off:旧版 memanalyze.gucs 不取
    # enable_resource_record,这里按 off 处理,报告就写「= off(需设为 on)」—— og5 上它实际是 on。
    # 读不到就照常去查历史表,查出来是空的再由 wlm.py 把前置参数逐个摆出来。
    record_on = _on(gucs, "enable_resource_record", "on")

    track_off_reason = (
        f"enable_resource_track = {gucs.get('enable_resource_track', 'off')}"
        f"（需设为 on）[需人工执行]"
    )

    # --- L4: SQL-level memory ------------------------------------------------
    sql_available = True
    if not track_on:
        sql_available = False
        reasons["L4"] = track_off_reason
    elif not catalog.has("wlm_session"):
        sql_available = False
        reasons["L4"] = catalog.reason("wlm_session")

    # --- L5: operator-level memory -------------------------------------------
    operator_available = True
    if not track_on:
        operator_available = False
        reasons["L5"] = track_off_reason
    elif level != "operator":
        operator_available = False
        reasons["L5"] = (
            f"resource_track_level = {level}（需设为 operator，否则算子级内存不采集）"
            f"[需人工执行]"
        )
    elif not catalog.has("wlm_operator"):
        operator_available = False
        reasons["L5"] = catalog.reason("wlm_operator")

    # --- history -------------------------------------------------------------
    history_available = True
    if not record_on:
        history_available = False
        reasons["history"] = (
            f"enable_resource_record = {gucs.get('enable_resource_record', 'off')}"
            f"（需设为 on，否则 gs_wlm_*_info 历史表无数据）[需人工执行]"
        )
    elif not catalog.has("wlm_session_hist"):
        history_available = False
        reasons["history"] = catalog.reason("wlm_session_hist")

    # --- L2: memory contexts -------------------------------------------------
    context_available = catalog.has("session_ctx") or catalog.has("thread_ctx")
    if not context_available:
        reasons["L2"] = catalog.reason("session_ctx")

    return Capability(
        gucs=gucs,
        sql_available=sql_available,
        operator_available=operator_available,
        history_available=history_available,
        context_available=context_available,
        reasons=reasons,
    )
