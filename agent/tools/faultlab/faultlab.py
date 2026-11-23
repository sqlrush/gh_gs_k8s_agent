#!/usr/bin/env python3
"""faultlab —— 在 openGauss 测试库上复现典型性能问题,用来检验这套诊断 skill。

一句话:**一条命令造出 34 项典型问题、持续 20 分钟、再一条命令清干净**,可以反复跑。
造的是慢 SQL、锁等待、死锁、VACUUM、轻量锁五类;每一项都对着某个 skill 的某条判定去设计,
对应关系在 `expected.yaml`,用法与原理在 `docs/test/og故障复现手册.md`。

用法(Mac 上,要用装了 psycopg2 的解释器):

    P=~/p2venv/bin/python
    $P tools/faultlab/faultlab.py check          # 环境自检(只读)
    $P tools/faultlab/faultlab.py prepare        # 建用户与数据(一次就够)
    $P tools/faultlab/faultlab.py calibrate      # 校准慢 SQL 的规模(换机器后跑一次)
    $P tools/faultlab/faultlab.py run            # 造故障,20 分钟
    $P tools/faultlab/faultlab.py run --only lock,deadlock --window 600
    $P tools/faultlab/faultlab.py run --codes K5,V6 --window 180 --quiet 0   # 只验这两项,挪到开头跑
    $P tools/faultlab/faultlab.py status         # 另开一个终端:现在哪些现象看得到
    $P tools/faultlab/faultlab.py cleanup        # 收尾(run 正常结束会自己做)
    $P tools/faultlab/faultlab.py purge          # 连数据和用户一起删
    $P tools/faultlab/faultlab.py expected       # 核对 expected.yaml 的格式,数 actual 还剩几项没填(不连库)

**安全边界:** 只动目标库里模式 `fl` 下的对象与用户 `fl_app`;实例级参数一个都不改;
动手前核对实例指纹(端口 + version()),对不上就拒绝执行。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lab import config as C                      # noqa: E402
from lab.db import LabError                      # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 1


def _print_block(title: str, data) -> None:
    print("\n== %s" % title)
    if isinstance(data, dict):
        for k, v in data.items():
            print("  %-12s %s" % (k, v))
    else:
        print("  %s" % data)


def cmd_check(args: argparse.Namespace) -> int:
    from lab import bootstrap
    _print_block("环境自检", bootstrap.check())
    _print_block("排期(吃 CPU 的会话峰值)", C.cpu_budget(_pick(args)))
    return EXIT_OK


def cmd_prepare(args: argparse.Namespace) -> int:
    from lab import bootstrap, prepare
    print("== 建专用用户与模式")
    info = bootstrap.ensure_user()
    print("  %s %s(口令在 %s,0600)" % (info["user"], info["action"], C.STATE_DIR / "fl_app.pw"))
    bootstrap.ensure_schema()
    print("== 造数据(og5 上约 1 分钟)")
    _print_block("完成", prepare.run())
    return EXIT_OK


def cmd_calibrate(args: argparse.Namespace) -> int:
    from lab import calibrate
    _print_block("慢 SQL 规模校准", calibrate.run(target_s=args.target))
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    from lab import runner
    return runner.run(scenarios=_pick(args), window_s=args.window, quiet=args.quiet)


def cmd_status(args: argparse.Namespace) -> int:
    from lab import status
    return status.show(watch=args.watch)


def cmd_cleanup(args: argparse.Namespace) -> int:
    from lab import cleanup
    _print_block("清理", cleanup.run())
    return EXIT_OK


def cmd_purge(args: argparse.Namespace) -> int:
    from lab import cleanup
    if not args.yes:
        print("purge 会删掉模式 %s 下的全部数据与用户 %s。确认请加 --yes。" % (C.SCHEMA, C.LAB_USER))
        return EXIT_USAGE
    _print_block("已删除", cleanup.purge())
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    from lab import report
    print(report.render(path=args.file))
    return EXIT_OK


def cmd_expected(args: argparse.Namespace) -> int:
    from lab import expected
    text, ok = expected.render(path=args.file or None)
    print(text)
    return EXIT_OK if ok else EXIT_FAILED


def _split(text: str) -> Tuple[str, ...]:
    return tuple(x.strip() for x in (text or "").split(",") if x.strip())


def _pick(args: argparse.Namespace) -> Tuple[C.Scenario, ...]:
    """--only 按分类挑;--codes 按编号挑,并整体前移到窗口开头(验证单项时不用陪跑十几分钟)。"""
    groups, codes = _split(getattr(args, "only", "")), _split(getattr(args, "codes", ""))
    if groups and codes:
        raise LabError("--only 与 --codes 只能给一个")
    try:
        if codes:
            return C.rebase(C.by_codes(codes))
        return C.by_group(groups)
    except ValueError as exc:
        raise LabError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="faultlab.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    for name, fn, help_ in (("check", cmd_check, "环境自检(只读)"),
                            ("prepare", cmd_prepare, "建专用用户与数据")):
        sp = add(name, fn, help_)
        sp.add_argument("--only", default="", help="只看这几类:%s" % ",".join(C.GROUPS))

    sp = add("calibrate", cmd_calibrate, "校准慢 SQL 规模")
    sp.add_argument("--target", type=int, default=12 * 60, help="单条慢 SQL 的目标耗时(秒),默认 720")

    sp = add("run", cmd_run, "造故障")
    sp.add_argument("--only", default="", help="只跑这几类:%s" % ",".join(C.GROUPS))
    sp.add_argument("--codes", default="",
                    help="只跑这几项(如 K5,V6),整体前移到窗口第 5 秒起跑;与 --only 二选一")
    sp.add_argument("--window", type=int, default=C.WINDOW_S, help="故障窗口秒数,默认 1200")
    sp.add_argument("--quiet", type=int, default=C.BASELINE_QUIET_S, help="开始前的静默基线秒数")

    sp = add("status", cmd_status, "现在哪些现象看得到")
    sp.add_argument("--watch", type=int, default=0, help="每 N 秒刷新一次(0 = 只看一次)")

    add("cleanup", cmd_cleanup, "收尾清理")

    sp = add("purge", cmd_purge, "连数据与用户一起删")
    sp.add_argument("--yes", action="store_true", help="确认删除")

    sp = add("report", cmd_report, "把上一次运行的时间线整理成报告")
    sp.add_argument("--file", default="", help="时间线文件,默认 ~/.faultlab/timeline.jsonl")

    sp = add("expected", cmd_expected, "核对预期发现矩阵的格式与 actual 填写进度(不连库)")
    sp.add_argument("--file", default="", help="矩阵文件,默认 tools/faultlab/expected.yaml")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or EXIT_OK)
    except LabError as exc:
        print("\n[faultlab] %s" % exc, file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\n[faultlab] 被打断 —— 正在清理", file=sys.stderr)
        from lab import cleanup
        print(json.dumps(cleanup.run(), ensure_ascii=False, indent=2), file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
