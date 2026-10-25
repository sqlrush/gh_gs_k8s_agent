#!/usr/bin/env python3
"""wdr — read-only OpenGauss/GaussDB WDR snapshot interpretation.

Port of internal/probe/wdr/* + internal/cli/wdr.go. Three subcommands:

  snaps      list WDR snapshots + preflight enable_wdr_snapshot (read-only)
  collect    self-compute snapshot-delta evidence across 7 dimensions +
             deterministic findings; best-effort留底 native generate_wdr_report
  render     no-DB: render the final report from evidence JSON + LLM interp JSON,
             after a mechanical anchoring recheck of the interp vs the evidence

NEVER creates snapshots, NEVER executes changes.

Usage:
    wdr.py snaps    -c <conn> [--limit 20] [--from 'YYYY-MM-DD HH:MM' [--to 'YYYY-MM-DD HH:MM']]
    wdr.py collect  -c <conn> --begin <id> --end <id> [--top 10] [--scope node|cluster] [--format json]
    wdr.py render   --evidence ev.json --interp interp.json [--format md|ansi]
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
from common import reports  # noqa: E402
from collectors import collect_evidence  # noqa: E402
from interp import load_evidence, load_interp  # noqa: E402
from finalreport import render_report  # noqa: E402
import followup  # noqa: E402
from model import DIM_TOPSQL, Options  # noqa: E402
from report import render_evidence, render_evidence_json  # noqa: E402
from snaps import snaps as run_snaps  # noqa: E402
from thresholds import default_thresholds  # noqa: E402


def _cmd_snaps(args) -> int:
    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        print(run_snaps(runner, args.limit, args.t_from, args.t_to), end="")
        return 0
    except common.DBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_collect(args) -> int:
    if args.begin <= 0 or args.end <= 0 or args.end <= args.begin:
        print(f"error: --begin/--end required with end>begin (run: wdr.py snaps -c {args.conn})",
              file=sys.stderr)
        return 1
    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        opt = Options(begin=args.begin, end=args.end, scope=args.scope, node=args.node,
                      top=args.top,
                      save_html=args.save_html or default_native_path(common.config.resolved_name(args.conn)),
                      thresholds=default_thresholds())
        ev = collect_evidence(runner, opt)
        ev.conn = common.config.resolved_name(args.conn)
        if args.format == "json":
            print(render_evidence_json(ev))
        else:
            print(render_evidence(ev), end="")
        # 存档在输出之后:大盘读 latest.json 与上一份做窗口对比;失败只 warn
        saved = reports.archive("wdr", _archive_payload(ev, args.save_html), instance=ev.conn)
        # 链接直接筛在**本次分析的窗口**上:用户说「生成这段时间的报告」时,
        # 点开就该是那一段,而不是全部历史再让他自己去顶栏选一次。
        # 快照时间形如 `2026-10-17 10:46:29`,大盘的时间段参数要 `2026-10-17T10:46`。
        reports.announce("wdr", saved, ev.conn, span=_dash_span(ev.window))
        _announce_followup(ev)
        return 0
    except common.DBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_render(args) -> int:
    if not args.evidence or not args.interp:
        print("error: --evidence 与 --interp 均必填", file=sys.stderr)
        return 1
    try:
        ev = load_evidence(args.evidence)
        in_ = load_interp(args.interp)
        report = render_report(ev, in_, fmt=args.format, no_color=args.no_color)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.out:
        try:
            pathlib.Path(args.out).write_text(report, encoding="utf-8")
        except OSError as exc:
            print(f"error: 写报告文件失败：{exc}", file=sys.stderr)
            return 1
        print(f"report written to {args.out}", file=sys.stderr)
        return 0
    print(report, end="")
    return 0


def _archive_payload(ev, explicit_path: str) -> dict:
    """给大盘存档的那份。显式 --save-html 时原生报告落在用户指定处(常是 /tmp),大盘的
    「下载 HTML →」按文件名去报告目录找会 404 —— 所以在报告目录里再留一份副本,存档指向它。
    屏幕上的输出不动:用户要的那个路径照样是他要的。复制失败只影响大盘那个链接,不报错。"""
    d = ev.to_dict()
    src = ev.native.saved_path
    if not (explicit_path and ev.native.generated and src):
        return d
    dst = default_native_path(ev.conn)
    if not dst:
        return d
    try:
        shutil.copyfile(src, dst)
    except OSError:
        d["native"].pop("saved_path", None)      # 宁可大盘不给链接,也不给一个 404 的链接
        return d
    d["native"]["saved_path"] = dst
    return d


def default_native_path(instance: str, now: Optional[str] = None) -> str:
    """原生 WDR 报告的默认落点:本人 reports/wdr/<实例键>/<ts>.native.html,大盘上「下载 HTML →」
    链接的就是 /reports/wdr/<实例键>/<文件名>。没设 GSDB_REPORTS_DIR 时返回空串 —— 与现在一样
    不落盘。目录在这里先建好:native.py 落盘失败只会把失败写进 note,不会自己建目录。"""
    base = reports.reports_dir()
    if base is None:
        return ""
    d = base / "wdr" / reports.instance_key(instance)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    return str(d / (reports.utc_stamp(now) + ".native.html"))


def _announce_followup(ev) -> None:
    """在**标准错误**给出下一步要执行的检查。

    为什么走标准错误、而不是写进报告:见 `followup.py` 的模块说明。一句话是 ——
    报告是给用户看的最终输出,把 `python3 …/explain.py --sql-id 123` 写进去,
    用户拿到的是一行命令而不是执行计划,而模型渲染完报告就收工了,那条命令也不会被执行。
    这里在 collect 阶段就交代清楚,模型还没写 interp.json,正好拿去跑、把结论写进来。
    """
    stale = followup.staleness_s(getattr(ev.window, "end_ts", ""))
    fresh = stale is not None and stale <= followup.STALE_AFTER_S
    top = next((d for d in (ev.dims or []) if d.dimension == DIM_TOPSQL), None)
    steps = followup.plan(ev.findings, ev.window,
                          top_sql_headers=getattr(top, "headers", []) if top else [],
                          top_sql_rows=getattr(top, "rows", []) if top else [],
                          fresh=fresh)
    text = followup.render_plan(steps, stale,
                                manual=followup.manual_notes(ev.findings))
    if text:
        print(text, end="", file=sys.stderr)


def _dash_span(w) -> tuple:
    """快照窗口 → 大盘时间段参数。取不到就返回空,announce 会照常只给普通链接。

    大盘的 WDR 页按**快照时间**筛(lib/range.js),所以这里给的就是窗口两端的快照时间;
    为了把边界上的那一份也包含进去,两头各放宽一分钟 —— 不放宽的话,窗口起点正好等于
    筛选起点时,浮点/秒级取整偶尔会把这一份自己筛掉。
    """
    import datetime as _dt

    def fmt(ts, delta):
        # 快照时间**两种长度都会出现**:`2026-10-17 10:46:29` 与 `2026-10-17 10:46`
        # (wdr.window 这条脚本按分钟给)。只认带秒的那种会一个都解析不出来、
        # 链接静默地不带时间段 —— 第一版就是这样,日志里什么都看不到。
        t = str(ts or "").strip()
        for pat, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
            if len(t) < n:
                continue
            try:
                d = _dt.datetime.strptime(t[:n], pat) + _dt.timedelta(minutes=delta)
                return d.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                continue
        return ""
    a, b = fmt(getattr(w, "begin_ts", ""), -1), fmt(getattr(w, "end_ts", ""), 1)
    return (a, b) if a and b else ()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="wdr.py",
                                 description="Read-only WDR snapshot interpretation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("snaps", help="list WDR snapshots + preflight")
    ps.add_argument("-c", "--conn", default="")
    cli.add_session_arg(ps)
    ps.add_argument("--limit", type=int, default=20, help="列出最近 N 个快照")
    ps.add_argument("--from", dest="t_from", default="", metavar="'YYYY-MM-DD HH:MM'",
                    help="按时间段找窗口:开始时间(数据库所在时区)")
    ps.add_argument("--to", dest="t_to", default="", metavar="'YYYY-MM-DD HH:MM'",
                    help="按时间段找窗口:结束时间;不给 = 到最新快照")
    ps.add_argument("--timeout", type=int, default=None)

    pc = sub.add_parser("collect", help="collect snapshot-delta evidence + findings")
    pc.add_argument("-c", "--conn", default="")
    cli.add_session_arg(pc)
    pc.add_argument("--begin", type=int, default=0, help="begin snapshot id (required)")
    pc.add_argument("--end", type=int, default=0, help="end snapshot id (required, > begin)")
    pc.add_argument("--scope", default="node", help="cluster | node")
    pc.add_argument("--node", default="", help="node name (empty → auto-detect)")
    pc.add_argument("--top", type=int, default=10, help="各 Top 列表条数")
    pc.add_argument("--save-html", dest="save_html", default="",
                    help="落盘原生 WDR 原文到此路径（审计，默认不落）")
    pc.add_argument("--format", choices=["markdown", "json"], default="markdown")
    pc.add_argument("--timeout", type=int, default=None)

    pr = sub.add_parser("render", help="render final report from evidence + interp (no DB)")
    pr.add_argument("--evidence", default="", help="collect --format json 产出的证据 JSON 路径")
    pr.add_argument("--interp", default="", help="LLM 产出的 interp.json 路径")
    pr.add_argument("--format", choices=["md", "ansi"], default="md")
    pr.add_argument("--no-color", dest="no_color", action="store_true", help="ansi 模式去掉颜色")
    pr.add_argument("--out", default="", help="落盘到文件（默认打印到 stdout）")

    args = ap.parse_args(argv)

    cli.apply_session_arg(args)
    if args.cmd == "snaps":
        return _cmd_snaps(args)
    if args.cmd == "collect":
        return _cmd_collect(args)
    return _cmd_render(args)


if __name__ == "__main__":
    raise SystemExit(main())
