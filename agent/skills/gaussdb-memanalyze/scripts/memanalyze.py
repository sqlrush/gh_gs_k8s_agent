#!/usr/bin/env python3
"""memanalyze — analyse dynamic-memory spikes on openGauss / GaussDB.

Six layers, drilled top-down: which memory is high (L1) -> is it a leak or real
work (L2) -> which sessions (L3) -> which SQL (L4) -> which operator (L5) ->
is the configuration itself the problem (L6).

    memanalyze.py snapshot -c <conn>              # spike is happening now
    memanalyze.py history  -c <conn> [--top 20]   # spike is over (needs history tables)
    memanalyze.py watch    -c <conn> --interval 5 --count 12   # leak or spike?

Exit codes: 0 = script ran (memory problems or not), 1 = runtime error,
2 = connection/config error. A finding does NOT change the exit code — the
verdict is in stdout.

Views are discovered at runtime (probe.py), so the same script runs on both
openGauss and GaussDB. A layer that cannot produce data says why.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
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
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_float, as_int  # noqa: E402

import capability  # noqa: E402
import collectors  # noqa: E402
import probe  # noqa: E402
import report  # noqa: E402
import trend  # noqa: E402
import wlm  # noqa: E402
from model import DIM_CONTEXT, DIM_INSTANCE, DIM_SESSION, MemEvidence  # noqa: E402
from model import degraded  # noqa: E402
from thresholds import default_thresholds  # noqa: E402

_HIST_NOTE = "历史模式不可用：该视图是实时视图，不保留历史数据"


def _setup(runner):
    """Probe views + GUCs. Returns (catalog, capability)."""
    cat = probe.probe_views(runner)
    return cat, capability.assess(capability.read_gucs(runner), cat)


def _instance_mem(runner, cat) -> dict:
    """L1 memory map, or {} — L6 needs max_dynamic_memory from here, not pg_settings."""
    try:
        return collectors.instance_memory(runner, cat)
    except common.DBError:
        return {}


def run_snapshot(runner, conn: str, top: int) -> MemEvidence:
    th = default_thresholds()
    cat, cap = _setup(runner)
    mem = _instance_mem(runner, cat)
    dims = [
        collectors.collect_instance(runner, cat, th, top),
        collectors.collect_context(runner, cat, th, top),
        collectors.collect_session(runner, cat, th, top),
        wlm.collect_sql(runner, cat, cap, th, top),
        wlm.collect_operator(runner, cat, cap, th, top),
        collectors.collect_config(runner, cap, th, top, mem=mem),
    ]
    return MemEvidence(
        conn=conn, target=f"实时快照（Top {top}）", mode="snapshot",
        capability=cap, catalog=cat, dims=dims,
        findings=[f for d in dims for f in d.findings],
    )


def run_history(runner, conn: str, top: int) -> MemEvidence:
    th = default_thresholds()
    cat, cap = _setup(runner)
    dims = [
        degraded(DIM_INSTANCE, _HIST_NOTE),
        degraded(DIM_CONTEXT, _HIST_NOTE),
        degraded(DIM_SESSION, _HIST_NOTE),
        wlm.collect_sql(runner, cat, cap, th, top, historical=True),
        wlm.collect_operator(runner, cat, cap, th, top, historical=True),
        collectors.collect_config(runner, cap, th, top, mem=_instance_mem(runner, cat)),
    ]
    return MemEvidence(
        conn=conn, target=f"历史回溯（Top {top}）", mode="history",
        capability=cap, catalog=cat, dims=dims,
        findings=[f for d in dims for f in d.findings],
        notes=["L1/L2/L3 是实时视图，冲高过去后无法回溯；只有 WLM 历史表保留了"
               "当时的 SQL 与算子内存"],
    )


def run_watch(runner, conn: str, top: int, interval: int, count: int) -> MemEvidence:
    th = default_thresholds()
    cat, cap = _setup(runner)

    samples: list[float] = []
    notes: list[str] = []
    for i in range(count):
        if i:
            time.sleep(interval)
        try:
            mem = collectors.instance_memory(runner, cat)
            samples.append(mem.get("dynamic_used_memory", 0.0))
        except common.DBError as exc:
            notes.append(f"第 {i + 1} 次采样失败（已跳过）：{exc}")

    verdict, detail = trend.analyze(samples, th)
    tf = trend.finding(samples, th)

    dims = [
        collectors.collect_instance(runner, cat, th, top),
        collectors.collect_session(runner, cat, th, top),
        collectors.collect_config(runner, cap, th, top, mem=_instance_mem(runner, cat)),
    ]
    findings = [f for d in dims for f in d.findings]
    if tf is not None:
        findings.append(tf)

    notes.insert(0, f"趋势判定：{verdict} —— {detail}")
    notes.append("采样序列（dynamic_used_memory, MB）："
                 + ", ".join(f"{s:.0f}" for s in samples))

    return MemEvidence(
        conn=conn, target=f"采样 {len(samples)} 次 / 间隔 {interval}s",
        mode="watch", capability=cap, catalog=cat, dims=dims,
        findings=findings, notes=notes,
    )


_CMDS = ("snapshot", "history", "watch")


def _normalize(argv) -> list:
    """Default to `snapshot`, and let -c sit on either side of the subcommand.

    argparse binds a parent parser's options only *before* the subcommand, so
    `memanalyze.py snapshot -c og` would otherwise fail. Each subparser inherits
    the shared options instead (see `parents=`), and a bare invocation gets the
    default subcommand spliced in.
    """
    argv = list(argv)
    if argv and argv[0] in _CMDS:
        return argv
    if any(a in ("-h", "--help") for a in argv):
        return argv
    return ["snapshot"] + argv


def _parse_args(argv):
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(shared)
    shared.add_argument("--top", type=int, default=20, help="每层返回行数（默认 20）")
    shared.add_argument("--format", choices=["markdown", "json"], default="markdown")
    shared.add_argument("--timeout", type=int, default=60, help="查询超时（秒）")

    ap = argparse.ArgumentParser(
        prog="memanalyze.py",
        description="Analyse dynamic-memory spikes on openGauss / GaussDB "
                    "(默认子命令：snapshot)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("snapshot", parents=[shared], help="现场：内存正在高位（默认）")
    sub.add_parser("history", parents=[shared], help="事后：冲高已过去，读 WLM 历史表")
    w = sub.add_parser("watch", parents=[shared], help="持续采样，判定泄漏还是尖峰")
    w.add_argument("--interval", type=int, default=5, help="采样间隔秒（默认 5）")
    w.add_argument("--count", type=int, default=12, help="采样次数（默认 12）")

    return ap.parse_args(_normalize(argv if argv is not None else sys.argv[1:]))


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    cli.apply_session_arg(args)

    if args.cmd == "watch" and (args.interval < 1 or args.count < trend.MIN_SAMPLES):
        print(f"error: watch 需要 --interval >= 1 且 --count >= {trend.MIN_SAMPLES}",
              file=sys.stderr)
        return 1

    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "history":
            ev = run_history(runner, common.config.resolved_name(args.conn), args.top)
        elif args.cmd == "watch":
            ev = run_watch(runner, common.config.resolved_name(args.conn), args.top, args.interval, args.count)
        else:
            ev = run_snapshot(runner, common.config.resolved_name(args.conn), args.top)

        out = (report.render_json(ev) if args.format == "json"
               else report.render_markdown(ev))
        print(out, end="" if out.endswith("\n") else "\n")
        return 0
    except common.DBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
