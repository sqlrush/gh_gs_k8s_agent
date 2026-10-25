"""L4 SQL / L5 operator collectors (WLM resource tracking).

Live and historical share one code path — only the view slot differs — because
`gs_wlm_session_statistics` and `gs_wlm_session_history` carry the same columns
under different names of the same shape. Column adaptation (probe.columns_expr)
absorbs whatever this version actually has.

When a layer is blind, it degrades *with the capability reason*: an empty
operator table would read as "no operator problems", which is exactly the lie
this skill exists to prevent.
"""
from __future__ import annotations

import common
from common import access  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_float, as_int  # noqa: E402
import probe
from model import (
    DIM_OPERATOR, DIM_SQL, Capability, Catalog, DimResult, Finding, Severity,
    degraded,
)
from thresholds import Thresholds
from util import f, human_mb, i64, trunc

_SQL_COLS = ("queryid", "query", "start_time", "duration", "estimate_memory",
             "max_peak_memory", "max_spill_size", "warning")

_OP_COLS = ("queryid", "plan_node_id", "plan_node_name", "duration",
            "estimated_rows", "tuple_processed", "max_peak_memory",
            "max_spill_size", "memory_skew_percent", "warning")

# A resolvable-but-empty WLM view is the trap this skill exists to avoid: the
# capability line says ✓, the table renders blank, and it reads as "no operator
# problems". So an empty layer spells out every prerequisite with *this instance's*
# value: a list of "possible causes" alone (the previous wording) left the reader
# unable to tell "a parameter is off" from "the kernel never fills these views" —
# faultlab 2026-10-24: S2/S6 spilled several GB and memanalyze never said why L4/L5
# were blank. On og5 (openGauss-lite 5.0.3, single node) every prerequisite is on and a
# 9-second spilling sort still leaves gs_wlm_session_statistics / operator_statistics /
# *_info at 0 rows: the kernel does not populate them.
_ON = frozenset({"on", "true", "1", "yes"})
_UNREAD = "?（没读到）"


def _prerequisites(gucs: dict, operator: bool, historical: bool) -> tuple:
    """(逐项「参数=本实例当前值」, 没满足的那几项)。没读到的标「?」,不当成 off —— 那是另一种不知道。"""
    items, unmet = [], []

    def need(name: str, ok, want: str) -> None:
        raw = str(gucs.get(name, "")).strip()
        items.append(f"{name}={raw or _UNREAD}")
        if raw and not ok(raw.lower()):
            unmet.append(f"{name}={raw}（需设为 {want}）")

    need("enable_resource_track", lambda v: v in _ON, "on")
    need("use_workload_manager", lambda v: v in _ON, "on")
    if operator:
        need("resource_track_level", lambda v: v == "operator", "operator")
    else:
        need("resource_track_level", lambda v: v in ("query", "operator"), "query 或 operator")
    if historical:
        need("enable_resource_record", lambda v: v in _ON, "on")
    items.append(f"resource_track_cost={gucs.get('resource_track_cost') or _UNREAD}（作业代价超过它才记）")
    if historical:
        items.append(f"resource_track_duration={gucs.get('resource_track_duration') or _UNREAD}"
                     "（秒；历史表只记跑够这么久的作业）")
    return items, unmet


def _empty_note(cap: Capability, operator: bool, historical: bool) -> str:
    what = "算子" if operator else "作业"
    items, unmet = _prerequisites(dict(cap.gucs), operator, historical)
    out = f"视图存在，但查不到{what}记录。这一层有数据的前置参数（本实例当前值）：{'、'.join(items)}。"
    if unmet:
        out += f"**没满足：{'；'.join(unmet)}** [需人工执行]——开了之后这一层才会有数据。"
    else:
        if historical:
            when = "历史表里没有跑够 resource_track_duration 的作业"
        else:
            when = "采样那一刻没有代价超过 resource_track_cost 的作业在跑（实时视图只显示正在运行的，冲高已结束时改用 history 子命令）"
        out += ("前置参数都已满足，视图仍是空的。两种可能：(1) %s；(2) 内核不填这几张视图 —— "
                "openGauss-lite 单机实测如此（og5 2026-10-24：S2/S6 落盘几 GB 期间、以及单独跑一条 9 秒的落盘排序时，"
                "gs_wlm_session_statistics / gs_wlm_operator_statistics / gs_wlm_session_info 都是 0 行）。"
                "分不清时按「这一层看不到」处理：落盘改看 gaussdb-wdr 的 WDR_TEMP_SPILL 与 Top SQL 的溢出列。" % when)
    return out + f"不能据此认为「{'算子层没有问题' if operator else '没有大内存 SQL'}」。"


# 列对应照注册脚本的 SELECT 列表(memanalyze.wlm_sql / _hist:… estimate_memory, NULL AS used_memory,
# max_peak_memory, average_peak_memory, spill_info;算子:… NULL AS estimate_memory, NULL AS memory_used,
# max_peak_memory, average_peak_memory, NULL AS spill_size, warning)。原来的取法把 used_memory(恒 NULL)当峰值、
# max_peak_memory 当下盘量、spill_info 当 GaussDB 告警、算子的 average_peak_memory 当下盘量 —— history 模式
# 一能用(2026-10-24 修好 enable_resource_record 的读取之后),og5 上 20 条 SQL 各报一条「GaussDB 自带告警:None」,
# 「下盘」一列其实是峰值内存。下盘量只有脚本取了 max_spill_size 才判;现行白名单只有 spill_info(是否下盘)。
_SPILL_FLAG = {"none": "否", "all": "是（All）", "partial": "部分（Partial）"}
_NO_SPILL_SIZE_NOTE = ("「下盘」一列只有是否下盘（spill_info）：注册脚本没取下盘量（max_spill_size），"
                       "所以不按下盘量判 MEM_SQL_SPILL；要看落盘量用 gaussdb-wdr 的 Top SQL 溢出列。")


def _opt(r: dict, key: str):
    """脚本没取这一列(或值为 NULL)返回 None —— 与 0 是两回事:0 是「没有」,None 是「不知道」。"""
    v = r.get(key)
    return None if v is None or str(v).strip() == "" else f(v)


def _spill_flag(raw) -> str:
    v = str(raw or "").strip()
    return _SPILL_FLAG.get(v.lower(), v or "—")


def _warning(r: dict) -> str:
    w = str(r.get("warning") or "").strip()
    return "" if w.lower() in ("none", "null") else w


def _order_by(vi, preferred: str) -> str:
    """ORDER BY only on a column this view really has — otherwise the query
    would fail on the dialects that lack it."""
    return f" ORDER BY {preferred} DESC NULLS LAST" if probe.has_col(vi, preferred) else ""


# --------------------------------------------------------------------------
# L4 — SQL level
# --------------------------------------------------------------------------
def collect_sql(runner, cat: Catalog, cap: Capability, th: Thresholds, top: int,
                historical: bool = False) -> DimResult:
    if historical and not cap.history_available:
        return degraded(DIM_SQL, cap.reasons.get("history", "历史数据不可用"))
    if not historical and not cap.sql_available:
        return degraded(DIM_SQL, cap.reasons.get("L4", "SQL 级资源跟踪不可用"))

    vi = cat.get("wlm_session_hist" if historical else "wlm_session")
    if not vi.available:
        return degraded(DIM_SQL, vi.reason)

    try:
        rows = runner.run("memanalyze.wlm_sql_hist" if historical
                          else "memanalyze.wlm_sql", {"limit": int(top)})
    except access.QueryError as exc:
        from util import summarize_err
        return degraded(DIM_SQL, summarize_err(exc))

    d = DimResult(dimension=DIM_SQL, available=True,
                  headers=["query_id", "耗时(ms)", "估算内存", "峰值内存",
                           "下盘", "warning", "SQL"])

    sized = False
    for r in rows:
        qid = str(r["queryid"] or "")
        query = str(r["query"] or "")
        duration = i64(r["duration"])
        est_mb, peak_mb = f(r["estimate_memory"]), f(r["max_peak_memory"])
        spill_mb = _opt(r, "max_spill_size")
        sized = sized or spill_mb is not None
        warning = _warning(r)

        d.rows.append([qid, str(duration), human_mb(est_mb), human_mb(peak_mb),
                       human_mb(spill_mb) if spill_mb is not None else _spill_flag(r.get("spill_info")),
                       trunc(warning, 30), trunc(query, 60)])

        if peak_mb >= th.session_hog_mb:
            d.findings.append(Finding(
                DIM_SQL, "MEM_SQL_HOG", Severity.WARN, "SQL 峰值内存",
                human_mb(peak_mb), f">={human_mb(th.session_hog_mb)}",
                f"query_id {qid} 峰值 {human_mb(peak_mb)}，SQL：{trunc(query, 80)}"))

        if spill_mb is not None and spill_mb >= th.spill_warn_mb:
            d.findings.append(Finding(
                DIM_SQL, "MEM_SQL_SPILL", Severity.WARN, "SQL 下盘量",
                human_mb(spill_mb), f">={human_mb(th.spill_warn_mb)}",
                f"query_id {qid} 下盘 {human_mb(spill_mb)}，说明 work_mem 不足以容纳"
                f"排序/哈希，算子被迫落盘（先定位到具体算子再调 work_mem）"))

        if est_mb > 0 and peak_mb / est_mb >= th.estimate_dev_ratio:
            d.findings.append(Finding(
                DIM_SQL, "MEM_SQL_ESTIMATE_OFF", Severity.WARN, "内存估算偏差",
                f"{peak_mb / est_mb:.1f}×", f">={th.estimate_dev_ratio:.0f}×",
                f"query_id {qid} 估算 {human_mb(est_mb)} 实际 {human_mb(peak_mb)}，"
                f"优化器估算严重偏低——通常是统计信息过期或行数估算错误"))

        if warning.strip():
            d.findings.append(Finding(
                DIM_SQL, "MEM_SQL_WARNING", Severity.NOTICE, "GaussDB 自带告警",
                trunc(warning, 60), "非空",
                f"query_id {qid} 的 warning 字段：{trunc(warning, 120)}"))

    if not d.rows:
        d.headline = "视图可用，但无 SQL 级数据"
        d.note = _empty_note(cap, operator=False, historical=historical)
    else:
        d.headline = f"峰值内存最高 SQL：query_id {d.rows[0][0]}（{d.rows[0][3]}）"
        if not sized:
            d.note = _NO_SPILL_SIZE_NOTE
    return d


# --------------------------------------------------------------------------
# L5 — operator level
# --------------------------------------------------------------------------
def collect_operator(runner, cat: Catalog, cap: Capability, th: Thresholds, top: int,
                     historical: bool = False) -> DimResult:
    if historical and not cap.history_available:
        return degraded(DIM_OPERATOR, cap.reasons.get("history", "历史数据不可用"))
    if not cap.operator_available:
        return degraded(DIM_OPERATOR, cap.reasons.get("L5", "算子级资源跟踪不可用"))

    vi = cat.get("wlm_operator_hist" if historical else "wlm_operator")
    if not vi.available:
        return degraded(DIM_OPERATOR, vi.reason)

    try:
        rows = runner.run("memanalyze.wlm_operator_hist" if historical
                          else "memanalyze.wlm_operator", {"limit": int(top)})
    except access.QueryError as exc:
        from util import summarize_err
        return degraded(DIM_OPERATOR, summarize_err(exc))

    d = DimResult(dimension=DIM_OPERATOR, available=True,
                  headers=["query_id", "node_id", "算子", "耗时(ms)", "估算行",
                           "实际行", "峰值内存", "下盘", "倾斜%"])

    for r in rows:
        qid, node_id, node_name = (str(r["queryid"] or ""),
                                   str(r["plan_node_id"] or ""),
                                   str(r["plan_node_name"] or ""))
        duration = i64(r["duration"])
        # 行数 / 下盘量 / 倾斜:现行白名单脚本都没取(见上方列对应的说明),取到了才判
        est_rows, act_rows = _opt(r, "estimated_rows"), _opt(r, "tuple_processed")
        peak_mb, spill_mb = f(r["max_peak_memory"]), _opt(r, "max_spill_size")
        skew = _opt(r, "memory_skew_percent")
        where = f"query_id {qid} 的算子 #{node_id} {node_name}"

        d.rows.append([qid, node_id, trunc(node_name, 28), str(duration),
                       "—" if est_rows is None else str(i64(est_rows)),
                       "—" if act_rows is None else str(i64(act_rows)), human_mb(peak_mb),
                       "—" if spill_mb is None else human_mb(spill_mb),
                       "—" if skew is None else f"{skew:.0f}"])

        if peak_mb >= th.session_hog_mb:
            d.findings.append(Finding(
                DIM_OPERATOR, "MEM_OP_HOG", Severity.WARN, "算子峰值内存",
                human_mb(peak_mb), f">={human_mb(th.session_hog_mb)}",
                f"{where} 峰值 {human_mb(peak_mb)} —— 内存就消耗在这个算子上"))

        if spill_mb is not None and spill_mb >= th.spill_warn_mb:
            d.findings.append(Finding(
                DIM_OPERATOR, "MEM_OP_SPILL", Severity.WARN, "算子下盘量",
                human_mb(spill_mb), f">={human_mb(th.spill_warn_mb)}",
                f"{where} 下盘 {human_mb(spill_mb)}，work_mem 不足以容纳该算子"))

        if est_rows and act_rows is not None and act_rows / est_rows >= th.rows_dev_ratio:
            d.findings.append(Finding(
                DIM_OPERATOR, "MEM_OP_ROWS_OFF", Severity.WARN, "行数估算偏差",
                f"{act_rows / est_rows:.1f}×", f">={th.rows_dev_ratio:.0f}×",
                f"{where} 估算 {i64(est_rows)} 行、实际 {i64(act_rows)} 行，"
                f"低估导致内存分配不足（根因通常是统计信息过期）"))

        if skew is not None and skew >= th.skew_warn_pct:
            d.findings.append(Finding(
                DIM_OPERATOR, "MEM_OP_SKEW", Severity.WARN, "内存倾斜",
                f"{skew:.0f}%", f">={th.skew_warn_pct:.0f}%",
                f"{where} 各 DN 内存分配倾斜 {skew:.0f}%，指向数据分布不均"))

    if not d.rows:
        d.headline = "视图可用，但无算子级数据"
        d.note = _empty_note(cap, operator=True, historical=historical)
    else:
        d.headline = (f"峰值内存最高算子：{d.rows[0][2]}（{d.rows[0][6]}，query_id "
                      f"{d.rows[0][0]}）")
    return d
