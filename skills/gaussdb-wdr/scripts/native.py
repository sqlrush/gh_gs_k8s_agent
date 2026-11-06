"""Snapshot-window loading + best-effort native WDR留底 — port of native.go.

Snapshot ids are validated ints from --begin/--end, inlined into the SQL exactly
as the Go version did (fmt.Sprintf with %d). node/scope are escaped defensively.
"""
from __future__ import annotations

from model import NativeInfo, Options, Window
from util import summarize_err

import common  # resolved on sys.path by the entry script

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/wdr/ —— 两条路径共用同一份定义
# 取数失败只认这一个类型。换一种数据库访问方式时，改的是访问模块，
# 不是这里 —— 详见 common/grmp/errors.py。
from common import access  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_float, as_int, is_null  # noqa: E402



def sql_literal(s: str) -> str:
    """Escape a single-quoted SQL string literal."""
    return (s or "").replace("'", "''")


def load_window(runner, opt: Options) -> Window:
    """Validate the snapshot pair, resolve node name, read wdr-enabled GUC, and
    fetch the window's begin/end times + duration."""
    if opt.begin <= 0 or opt.end <= opt.begin:
        raise common.DBError(
            f"无效窗口：begin={opt.begin} end={opt.end}（end 必须 > begin > 0）")
    w = Window(begin_id=opt.begin, end_id=opt.end, scope=opt.scope or "node")

    try:
        rows = runner.run("wdr.wdr_enabled")
        enabled = rows[0]["enable_wdr_snapshot"] if rows else ""
        w.wdr_enabled = str(enabled or "").strip().lower() == "on"
    except access.QueryError:
        pass

    w.node = opt.node
    if not w.node:
        try:
            rows = runner.run("wdr.node_name")
            w.node = str(rows[0]["pgxc_node_name"] if rows else "").strip()
        except access.QueryError:
            pass

    try:
        rows = runner.run("wdr.window",
                          {"begin": int(opt.begin), "end": int(opt.end)})
    except access.QueryError as exc:
        raise common.DBError(
            f"加载快照窗口失败（snap {opt.begin}/{opt.end} 是否存在？run: wdr snaps）：{exc}")
    if not rows:
        raise common.DBError(
            f"加载快照窗口失败：snap {opt.begin}/{opt.end} 不存在（run: wdr snaps 查看可用快照）")
    w.begin_ts, w.end_ts = rows[0]["b_start"], rows[0]["e_start"]
    w.duration_min = as_int(rows[0]["dur"])
    return w


def generate_native(runner, opt: Options, w: Window) -> NativeInfo:
    """Call the native generate_wdr_report (read-only) for留底/审计. Failure is
    non-fatal — deterministic findings come from the self-computed delta."""
    # scope/node 仍走 sql_literal 转义：String 参数中间件不转义
    # （引号责任在脚本作者），这是这两个取值唯一的防线，迁移时不能丢。
    try:
        rows = runner.run("wdr.native_report", {
            "begin": int(opt.begin), "end": int(opt.end),
            "scope": sql_literal(w.scope), "node": sql_literal(w.node)})
    except access.QueryError as exc:
        return NativeInfo(generated=False,
                          note="generate_wdr_report 不可用或失败：" + summarize_err(exc))
    body = "".join((str(r["report_line"]) + "\n")
                   for r in rows if not is_null(r["report_line"]))
    ni = NativeInfo(generated=True, bytes=len(body.encode("utf-8")))
    if opt.save_html:
        try:
            with open(opt.save_html, "w", encoding="utf-8") as fh:
                fh.write(body)
            ni.saved_path = opt.save_html
        except OSError as exc:
            ni.note = "原生报告已生成但落盘失败：" + summarize_err(exc)
    return ni
