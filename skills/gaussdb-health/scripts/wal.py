"""WAL 维度:xlog 生成速度与写入来源(2026-11-05 加)。

行方原话里的例子:「大量大字段写入导致 xlog 膨胀迅速 —— 这不是内核异常,是业务异常导致的内核指标报警」。
此前 health 只有 checkpoint 请求占比(间接信号),测不到 xlog 每秒生成多少,更说不出是谁在写。这一维:

  1. 隔 th.wal_sample_s 秒采两次 xlog 位置(health.xlog_position),差值 / 时间 = 每秒生成量;
  2. 同一段时间里各表写了多少行(health.table_writes 两次之差),大字段(TOAST)写入折回所属业务表;
  3. 累计写入行数最多的业务 SQL(health.write_sql,统计重置以来;非业务 SQL 按 common/syssql.py 排除);
  4. 此刻正在执行写入的会话(health.write_sessions):表级计数要等语句结束才累加,跑了很久的批量写入 / 存储过程
     在采样期间看不到表增量,要靠这一路说出是谁在写;
  5. 库支持时(GaussDB 有 pg_stat_activity.xlog_quantity)再给出当前事务 xlog 用量最大的会话。

GaussDB 没有按语句记的 xlog 字节数,表与 SQL 两路用的是写入行数 —— 证据里如实写「按写入行数」。
采样期间一行都没写的(写入已经停了),写「尚未定位到引起它的业务」并指向时段分析,不编来源。
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

from model import DIM_WAL, DimResult, Finding, Severity, degraded
from thresholds import Thresholds
from util import human_bytes, i64, summarize_err, trunc

import common  # noqa: F401,E402  (定位 common/ 由 collectors 完成)
from common import access  # noqa: E402
from common import syssql  # noqa: E402
from common.grmp.values import as_bool, as_float, as_int, is_null  # noqa: E402

_sleep = time.sleep            # 单测里换掉,不真等
TABLE_LIMIT = 200              # 两次采样都要覆盖到正在写的表:按最近变化时间取前 200 张
_MB = 1 << 20


def lsn_bytes(lsn: str) -> Optional[int]:
    """'1B7/3DC7BAF0' → 字节位置(高 32 位 / 低 32 位,与 pg_xlog_location_diff 同一算法)。"""
    try:
        hi, lo = str(lsn).strip().split("/")
        return (int(hi, 16) << 32) + int(lo, 16)
    except (ValueError, AttributeError):
        return None


def _seconds(at: str) -> Optional[float]:
    import datetime as _dt
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(str(at).strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


def _table_key(r: dict) -> Tuple[str, str]:
    return (str(r.get("schema") or ""), str(r.get("table") or ""))


def table_deltas(before: List[dict], after: List[dict]) -> List[dict]:
    """两次采样之差,按业务表汇总:普通行写入 + 大字段(TOAST)写入分开记。只出现在后一次的表按全量算增量为 0
    (前一次没取到就不知道起点,不能把累计值当增量)。"""
    def index(rows):
        out: Dict[Tuple[Tuple[str, str], bool], dict] = {}
        for r in rows or []:
            out[(_table_key(r), as_bool(r.get("is_toast")))] = r
        return out
    b, a = index(before), index(after)
    agg: Dict[Tuple[str, str], dict] = {}
    for key, ra in a.items():
        rb = b.get(key)
        if rb is None:
            continue
        (tkey, toast) = key
        d = {k: max(0, as_int(ra.get(k)) - as_int(rb.get(k))) for k in ("n_tup_ins", "n_tup_upd", "n_tup_del")}
        e = agg.setdefault(tkey, {"schema": tkey[0], "table": tkey[1], "ins": 0, "upd": 0, "del": 0,
                                  "toast_rows": 0, "toast_bytes": None})
        if toast:
            e["toast_rows"] += d["n_tup_ins"] + d["n_tup_upd"]
            if not is_null(ra.get("toast_bytes")):
                e["toast_bytes"] = as_int(ra.get("toast_bytes"))
        else:
            e["ins"] += d["n_tup_ins"]
            e["upd"] += d["n_tup_upd"]
            e["del"] += d["n_tup_del"]
    rows = [e for e in agg.values() if e["ins"] + e["upd"] + e["del"] + e["toast_rows"] > 0]
    return sorted(rows, key=lambda e: e["ins"] + e["upd"] + e["del"] + e["toast_rows"], reverse=True)


def _write_sql(runner, top: int) -> Tuple[list, str]:
    try:
        fetched = runner.run("health.write_sql", {"limit": top + syssql.SCREEN_MARGIN})
    except access.QueryError as exc:
        return [], "写入最多的 SQL 取不到:%s" % summarize_err(exc)
    screening = syssql.screen(fetched, want=top, sql_id=lambda r: r["unique_sql_id"], text=lambda r: r["query"],
                              shown_chars=80, full_text=syssql.full_text_lookup(runner),
                              seconds=lambda r: as_float(r.get("total_sec")), cache=syssql.verdict_cache())
    return screening.kept, ""


def _sessions(runner, top: int) -> Tuple[list, str]:
    try:
        return runner.run("health.xlog_by_session", {"limit": top}), ""
    except access.QueryError as exc:
        msg = str(exc)
        if "xlog_quantity" in msg:
            return [], "本库没有按会话的 xlog 用量(openGauss 的 pg_stat_activity 无 xlog_quantity 列),写入来源看表与 SQL 两路"
        return [], "按会话的 xlog 用量取不到:%s" % summarize_err(exc)


def _writers(runner, top: int) -> list:
    try:
        return runner.run("health.write_sessions", {"limit": top})
    except access.QueryError:
        return []


def _rate_severity(mb_s: float, th: Thresholds) -> Tuple[Severity, str]:
    if mb_s >= th.wal_rate_warn_mb:
        return Severity.WARN, ">=%.0f MB/s" % th.wal_rate_warn_mb
    if mb_s >= th.wal_rate_notice_mb:
        return Severity.NOTICE, ">=%.0f MB/s" % th.wal_rate_notice_mb
    return Severity.OK, ""


def _source_text(tables: List[dict], sqls: list, sessions: list, writers: list = ()) -> str:
    """证据里的「写入来源」一句:会话 > 表 > SQL,都没有就明说没定位到。"""
    parts = []
    if writers and not sessions:
        w = writers[0]
        age = as_float(w.get("query_age_s"))
        parts.append("正在执行写入的会话 %s%s(已执行 %.0f 秒:%s)" % (
            w.get("sessionid"), ("（应用 %s）" % w["application_name"]) if w.get("application_name") else "",
            age, trunc(w.get("query") or "", 40)))
    if sessions:
        s = sessions[0]
        parts.append("当前事务 xlog 用量最大的是会话 %s%s(%s,sql_id %s)" % (
            s.get("sessionid"), ("（应用 %s）" % s["application_name"]) if s.get("application_name") else "",
            human_bytes(as_int(s.get("xlog_quantity"))), s.get("unique_sql_id") or "-"))
    if tables:
        t = tables[0]
        lob = ("，其中大字段写入 %s 行" % i64(t["toast_rows"])) if t["toast_rows"] else ""
        parts.append("采样期间写入最多的表是 %s.%s(插入 %s、更新 %s、删除 %s%s)" % (
            t["schema"], t["table"], i64(t["ins"]), i64(t["upd"]), i64(t["del"]), lob))
    if sqls:
        q = sqls[0]
        parts.append("累计写入行数最多的 SQL 是 sql_id %s(%s 行,%s)" % (
            q.get("unique_sql_id"), i64(as_int(q.get("rows_written"))), trunc(q.get("query") or "", 40)))
    if not parts:
        return ("尚未定位到引起它的业务:采样期间没有表级写入增量(写入可能已经停了)—— "
                "过去一段时间的写入来源用 gaussdb-wdr 同一时段的报告看")
    return ";".join(parts)


def collect_wal(runner, th: Thresholds, top: int) -> DimResult:
    try:
        p1 = runner.run("health.xlog_position", {})
        t1 = runner.run("health.table_writes", {"limit": TABLE_LIMIT})
        _sleep(th.wal_sample_s)
        p2 = runner.run("health.xlog_position", {})
        t2 = runner.run("health.table_writes", {"limit": TABLE_LIMIT})
    except access.QueryError as exc:
        msg = str(exc)
        if "recovery" in msg.lower() or "恢复" in msg:
            return degraded(DIM_WAL, "备机上没有 xlog 写入(恢复中),这一维在主库上看")
        return degraded(DIM_WAL, summarize_err(exc))
    b1 = lsn_bytes(p1[0]["lsn"]) if p1 else None
    b2 = lsn_bytes(p2[0]["lsn"]) if p2 else None
    s1 = _seconds(p1[0]["at"]) if p1 else None
    s2 = _seconds(p2[0]["at"]) if p2 else None
    if None in (b1, b2, s1, s2) or s2 <= s1:
        return degraded(DIM_WAL, "xlog 位置解析不出来,算不出生成速度")
    mb_s = max(0, b2 - b1) / _MB / (s2 - s1)
    tables = table_deltas(t1, t2)[:top]
    sqls, sql_note = _write_sql(runner, min(top, 5))
    sessions, sess_note = _sessions(runner, min(top, 5))
    writers = _writers(runner, min(top, 5))

    d = DimResult(dimension=DIM_WAL, available=True, headers=["来源", "对象", "写入", "大字段(TOAST)", "说明"])
    d.rows.append(["xlog", "每秒生成", "%.2f MB/s" % mb_s, "", "采样 %.1f 秒" % (s2 - s1)])
    for t in tables[:5]:
        d.rows.append(["表(采样期间)", "%s.%s" % (t["schema"], t["table"]),
                       "插入 %s / 更新 %s / 删除 %s" % (i64(t["ins"]), i64(t["upd"]), i64(t["del"])),
                       ("%s 行%s" % (i64(t["toast_rows"]), ("，共 " + human_bytes(t["toast_bytes"]))
                                     if t["toast_bytes"] else "")) if t["toast_rows"] else "", ""])
    for q in sqls[:5]:
        d.rows.append(["SQL(累计)", str(q.get("unique_sql_id")),
                       "%s 行 / 调用 %s 次" % (i64(as_int(q.get("rows_written"))), i64(as_int(q.get("calls")))),
                       "", trunc(q.get("query") or "", 50)])
    for w in writers[:5]:
        d.rows.append(["会话(正在写)", "%s%s" % (w.get("sessionid"),
                                              ("（%s）" % w["application_name"]) if w.get("application_name") else ""),
                       "已执行 %.0f 秒" % as_float(w.get("query_age_s")), "", trunc(w.get("query") or "", 50)])
    for s in sessions[:5]:
        d.rows.append(["会话(当前事务)", "%s%s" % (s.get("sessionid"),
                                                ("（%s）" % s["application_name"]) if s.get("application_name") else ""),
                       human_bytes(as_int(s.get("xlog_quantity"))), "", trunc(s.get("query") or "", 50)])
    notes = [n for n in (sql_note, sess_note) if n]
    if notes:
        d.note = ";".join(notes)
    sev, thr = _rate_severity(mb_s, th)
    source = _source_text(tables, sqls, sessions, writers)
    if sev != Severity.OK:
        d.findings.append(Finding(
            DIM_WAL, "WAL_RATE_HIGH", sev, "xlog 每秒生成量", "%.2f MB/s" % mb_s, thr,
            "xlog 涨得快多半是业务写入引起的(批量写入、大字段写入、频繁小事务提交),不是数据库本身的问题。"
            "%s。处置在应用侧:批量写入分批提交、控制大字段写入(压缩、拆表、挪到文件存储)、移到业务低峰;"
            "同时由运维确认归档与磁盘余量跟得上。" % source))
    d.headline = "xlog %.2f MB/s;%s" % (mb_s, source if tables or sqls or sessions or writers else "采样期间无表级写入")
    d.raw = {"xlog_mb_s": mb_s}
    return d
