#!/usr/bin/env python3
"""waitevent — 多窗口 DB time 分解 + 等待事件下钻（OpenGauss/GaussDB，只读）。

回答"最近几个采样窗口里 DB time 都花到哪了"：合并两个数据源——
`waitevent.instance_time`（`snap_global_instance_time` 十项时间模型的
窗口增量）与 `waitevent.events`（`snap_global_wait_events` 下钻到
event 级的窗口增量，时间模型里没有"锁"，这部分只能从这里补）。

**渲染上的核心约束：DB time 九项一律平铺，不画包含树。**
`tools/probe_dbtime_containment.py` 实测过：EXECUTION_TIME+PARSE_TIME+
PLAN_TIME+REWRITE_TIME<=DB_TIME 在 5 个窗口全部成立，但 CPU_TIME+
DATA_IO_TIME+NET_SEND_TIME<=EXECUTION_TIME 在全部 5 个窗口都不成立（超出
15%~24%）。一真一假时画树最危险——树暗示"总量减子项=剩余"，读者会去做
这个减法，在不成立的那一半上得到负数或无意义的数字，且没有任何报错提示。
所以 `dbtime.breakdown()` 把九项平铺返回，这里渲染成一张单层表格（没有
标题嵌套、没有缩进层级），并把 `Breakdown.note` 原样带进输出，提示读者
不要在项之间相加或相减。

**跨实例重启的窗口只报"数据不可用"，不算、不列任何百分比。**
`waitevent.instance_time` 的增量能算出负数，重启一眼可见；但
`waitevent.events` 继承自 `wdr.waits` 的 `HAVING SUM(e.wt-b.wt) > 0`，
重启窗口里这个谓词会把负增量行悄悄过滤掉——那一侧只是"行变少了"，看不出
重启。所以本模块对重启窗口**整节跳过等待事件**，不把"行变少"渲染成
"没有等待"。

Usage:
    waitevent.py -c <conn> [--snapshots 6] [--begin ID --end ID] [--format json] [--timeout N]
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/waitevent/*.yaml —— 两条路径共用同一份定义

import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
from common import kbsection  # noqa: E402
from common.finding import findings_to_json  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_int  # noqa: E402

import dbtime  # noqa: E402
import render  # noqa: E402

SKILL = "gaussdb-waitevent"
DEFAULT_SNAPSHOTS = 6

SNAPSHOTS_SCRIPT = "wdr.snapshots"       # 复用 wdr 的，不新写
WINDOW_SCRIPT = "wdr.window"             # 复用
TIME_SCRIPT = "waitevent.instance_time"  # 本 skill 新增
EVENTS_SCRIPT = "waitevent.events"       # 本 skill 新增

EVENTS_TOP = 20


@dataclass(frozen=True)
class Window:
    begin: int
    end: int
    label: str
    breakdown: object          # dbtime.Breakdown
    events: list
    seconds: Optional[float] = None    # 窗口时长;取不到时 None(不做空闲判断,不猜)


@dataclass(frozen=True)
class WaitReport:
    windows: list = field(default_factory=list)
    findings: list = field(default_factory=list)


def _pick_snapshots(runner, snapshots: int) -> list:
    """取最近 N 个快照 id，升序。不足 2 个就抛 —— 算不出窗口。"""
    rows = runner.run(SNAPSHOTS_SCRIPT, {"limit": int(snapshots)})
    ids = sorted(as_int(r["snapshot_id"]) for r in rows)
    if len(ids) < 2:
        raise ValueError(
            "至少需要 2 个快照才能算出一个窗口，当前只有 %d 个。"
            "确认 WDR 快照已开启（见 gaussdb-wdr）。" % len(ids))
    return ids[-int(snapshots):]


def collect(runner, snapshots: int = DEFAULT_SNAPSHOTS,
            begin: int = 0, end: int = 0) -> WaitReport:
    """按窗口取时间模型增量 + 等待事件明细，逐窗口判定，汇总成一份报告。

    每个窗口独立判定（`dbtime.judge_dbtime`），不跨窗口合并阈值——
    这样才能分清"持续存在的问题"与"某个窗口内的一次性尖峰"。
    """
    if begin and end:
        pairs = [(int(begin), int(end))]
    else:
        ids = _pick_snapshots(runner, snapshots)
        pairs = list(zip(ids, ids[1:]))     # 相邻两两成对
    windows, findings = [], []
    for b, e in pairs:
        time_rows = runner.run(TIME_SCRIPT, {"b": b, "e": e})
        event_rows = runner.run(EVENTS_SCRIPT, {"b": b, "e": e, "top": EVENTS_TOP})
        bd = dbtime.breakdown(time_rows)
        win = runner.run(WINDOW_SCRIPT, {"begin": b, "end": e})
        label = ("%s → %s" % (win[0]["b_start"], win[0]["e_start"])) if win else "%d→%d" % (b, e)
        secs = _window_seconds(win)
        windows.append(Window(begin=b, end=e, label=label,
                              breakdown=bd, events=event_rows, seconds=secs))
        findings.extend(dbtime.judge_dbtime(bd, event_rows, secs))
    return WaitReport(windows=windows, findings=findings)


def _window_seconds(win) -> Optional[float]:
    """wdr.window 的 dur 是分钟(四舍五入)。0 或取不到 → None:不到半分钟的窗口算不出可靠的平均活跃会话。"""
    try:
        minutes = as_int(win[0].get("dur")) if win else 0
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    return minutes * 60.0 if minutes > 0 else None


# ---------------------------------------------------------------------------
# 渲染 —— 纯函数，不连库
# ---------------------------------------------------------------------------

def _pct(ratio: float) -> str:
    return "%.1f%%" % (ratio * 100)


def _items_table(bd) -> str:
    """DB time 九项**平铺**成一张单层表格 —— 不加标题嵌套、不加缩进，
    任何一项都不比另一项更"高一级"。表格本身没有父子关系可读，这是
    避免让读者把 CPU/IO/NET 读成 EXECUTION_TIME 子项的关键。
    """
    if not bd.items:
        return "本窗口 DB_TIME=0，没有可分解的 DB time。\n"
    rows = [[name, str(delta), _pct(share)] for name, delta, share in bd.items]
    return render.table(["Item（平铺，互不隶属）", "Delta (us)", "Share of DB_TIME"], rows)


def _events_block(events: list) -> str:
    """等待事件按 wait_class 分组，下钻到 event。这是等待事件自身真实存在的
    二级结构（与被否定的 DB time 包含树无关），所以允许分组展示。
    """
    out = ["### 等待事件（按 wait_class 分组，下钻到 event）\n"]
    if not events:
        out.append(
            "本窗口没有等待事件耗时数据 —— 已排除 STATUS/NONE（会话空等客户端"
            "命令的空闲时间），且 `waitevent.events` 只返回增量>0 的行；"
            "真的没有锁/轻量锁等待时也会呈现为空，这是合法结果，不代表取数出错。\n")
        return "\n".join(out)
    by_class: dict = {}
    for row in events:
        cls = row.get("wait_class") or "(未知)"
        by_class.setdefault(cls, []).append(row)
    for cls in sorted(by_class):
        class_rows = sorted(by_class[cls], key=lambda r: -as_int(r.get("wait_us")))
        rows = [[r.get("event", ""), str(as_int(r.get("waits"))),
                 str(as_int(r.get("wait_us")))] for r in class_rows]
        out.append("#### %s\n" % cls)
        out.append(render.table(["Event", "Waits", "Wait (us)"], rows))
    return "\n".join(out)


def _findings_block(findings: list, idle: bool = False) -> str:
    if not findings and idle:
        # 空闲窗口没判 —— 不能写「未发现越过阈值的问题」,那读起来是「查过了、没问题」
        return "### Findings（本窗口）\n\n本窗口几乎空闲，没有做比例判定（见上）。\n"
    if not findings:
        return "### Findings（本窗口）\n\n未发现越过阈值的问题。\n"
    rows = [[f.severity.label(), f.code, f.metric, f.value, f.threshold] for f in findings]
    out = ["### Findings（本窗口）\n",
           render.table(["级别", "代码", "指标", "实测值", "阈值"], rows), ""]
    for f in findings:
        out.append("- **%s**（%s）：%s" % (f.code, f.severity.label(), f.evidence))
    return "\n".join(out) + "\n"


def _window_block(win: Window) -> str:
    bd = win.breakdown
    out = ["## 窗口 %s（快照 %d → %d）\n" % (win.label, win.begin, win.end)]

    if bd.restarted:
        # 只报不可用——不算、不列任何百分比。等待事件那一侧的 HAVING 谓词
        # 会在重启窗口里悄悄滤掉负增量行，"行变少了"不等于"没有等待"，
        # 所以这里整节跳过等待事件，不把不可信的数据当正常结果展示。
        out.append("**该窗口跨越了实例重启，数据不可用。**\n")
        out.append("> %s\n" % bd.note)
        out.append(
            "等待事件明细在此类窗口里同样不可信（`waitevent.events` 的 "
            "HAVING 谓词会把负增量行悄悄过滤掉，行数变少不代表没有等待），"
            "本节跳过，不展示。\n")
        return "\n".join(out)

    out.append("DB_TIME = %d us\n" % bd.db_time_us)
    out.append("> %s\n" % bd.note)
    # 哪些比例没判、为什么(空闲窗口 / 自相矛盾)—— 放在表格前,先说哪些数不能信
    g = dbtime.gate(bd, win.events, win.seconds)
    for note in g.notes:
        out.append("> %s\n" % note)
    out.append(_items_table(bd))
    out.append(_events_block(win.events))
    # judge_dbtime 是纯函数：用本窗口已经取到的 bd/events 原样重算一遍，
    # 只为把"是哪个窗口触发的"这条信息带进渲染——不重新取数，结果与
    # collect() 汇总进 WaitReport.findings 的那份完全一致。
    out.append(_findings_block(dbtime.judge_dbtime(bd, win.events, win.seconds), idle=g.idle))
    return "\n".join(out)


def render_markdown(rep: WaitReport) -> str:
    parts = ["# DB Time 分解与等待事件下钻\n"]
    if not rep.windows:
        parts.append("没有可用窗口。\n")
    else:
        parts.extend(_window_block(w) for w in rep.windows)
    parts.append(
        "\n> 多个窗口逐个列出，不合并成单一均值——用来区分"
        "「持续存在的问题」与「某个窗口内的一次性尖峰」。\n"
        "> 锁的详细堵塞关系（谁堵了谁、阻塞链的根在哪）见 `gaussdb-lockwait`，"
        "本报告只给锁/轻量锁耗时占比。\n")
    # 客户知识库对这些发现怎么说(各窗口的发现合在一起查)。只在 markdown 里查:health 用 --format json
    # 拉本脚本,它会对汇总后的发现统一检索,JSON 里再查就是同一份发现查两遍(见 common/kbsection.py)。
    parts.append(kbsection.markdown(rep.findings))
    return "\n".join(parts)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="waitevent.py",
                                 description="多窗口 DB time 分解 + 等待事件下钻（只读）")
    ap.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    ap.add_argument("--snapshots", type=int, default=DEFAULT_SNAPSHOTS, help="取最近几个快照")
    ap.add_argument("--begin", type=int, default=0, help="起始快照 ID（与 --end 一起给）")
    ap.add_argument("--end", type=int, default=0, help="结束快照 ID")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--timeout", type=int, default=None)
    args = ap.parse_args(argv)
    cli.apply_session_arg(args)

    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        rep = collect(runner, snapshots=args.snapshots, begin=args.begin, end=args.end)
    # ValueError 在这里既覆盖"快照不足两个"（_pick_snapshots），也覆盖
    # dbtime.breakdown() 发现的数据缺项/空值——两者都是"取到的数据不能用
    # 来算窗口"，同一类失败，同一个退出码。
    except (common.DBError, access.QueryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.format == "json":
            print(findings_to_json(rep.findings, skill=SKILL))
        else:
            print(render_markdown(rep), end="")
        return 0
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
