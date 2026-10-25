#!/usr/bin/env python3
"""health — read-only OpenGauss/GaussDB health check, 8 local dimensions +
3 sub-skill dimensions aggregated via subprocess.

Port of internal/probe/health/* + internal/cli/health.go. Runs every selected
read-only collector, assembles an evidence pack, and derives deterministic,
threshold-based findings (4 severity bands: 🟢健康/🟡关注/🟠告警/🔴严重).
Per-collector failures degrade (dimension marked unavailable) instead of
aborting. NEVER executes any fix (no kill/VACUUM/DDL/DML).

**waits/lwlock/bloat/locks 不再是本地 collector。** 它们现在由
gaussdb-waitevent / gaussdb-vacuum / gaussdb-lockwait 三个子 skill 各自采集，
health 通过 aggregate.py 以子进程调用、汇总它们的 findings —— 见 collectors.py
顶部的说明。这四个名字在 --include/--exclude 里继续有效，只是改成路由到
对应子 skill（见 `_sub_skill_in_scope` / `SUB_SKILL_DIMS`），保证已有命令行
不因为这次内部重组而失效。

Usage:
    health.py -c <conn> [--include dims] [--exclude dims] [--top 10] [--format json]
    本地维度: overview,slowsql,xact,conn,logs,repl,schema,concurrency
    路由维度（本次采集，来自子 skill）: waits,lwlock -> gaussdb-waitevent；
                                       bloat -> gaussdb-vacuum；
                                       locks -> gaussdb-lockwait

退出码：
    0 报告完整（本地维度 + 所有纳入范围的子 skill 都采集到）
    1 参数/形状被拒绝，或渲染层出错
    2 连接建立失败，没有产出报告
    3 报告已经打印，但至少一个纳入范围的子 skill 采集失败——3 是附加信息，
      不是"没有报告"；报告本身照常完整打印，顶部会点名哪个子 skill 失败、
      为什么。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

import aggregate  # noqa: E402
import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
from common import reports  # noqa: E402
import collectors  # noqa: E402
import counters as counters_mod  # noqa: E402
from model import HealthEvidence, Severity, worst  # noqa: E402
from report import health_dict, render_health, render_health_json  # noqa: E402,F401
from thresholds import Thresholds, default_thresholds  # noqa: E402
import window as window_mod  # noqa: E402

# 四个退役的本地维度名，现在路由到对应子 skill 而不是本地 collector。
# waits 和 lwlock 出自同一份 gaussdb-waitevent 报告（同一次 DB time 分解的
# 两个切面：锁等待占比 / 轻量锁等待占比），没法只取其中一半 —— exclude 任
# 一个就整份跳过那次子进程调用，include 任一个就整份触发。
SUB_SKILL_DIMS: dict = {
    "gaussdb-lockwait": ("locks",),
    "gaussdb-waitevent": ("waits", "lwlock"),
    "gaussdb-vacuum": ("bloat",),
}


def _sub_skill_in_scope(skill: str, inc: set, exc: set) -> bool:
    """--include/--exclude 对子 skill 的过滤，语义与本地 8 个维度一致：
    exc 优先于 inc；inc 为空视为"没有限定，谁都算在内"。"""
    dims = SUB_SKILL_DIMS.get(skill, ())
    if any(d in exc for d in dims):
        return False
    if inc and not any(d in inc for d in dims):
        return False
    return True


def _exit_code(sub_results) -> int:
    """0 = 全部纳入范围的子 skill 都采集到；3 = 报告已打印，但至少一个失败。

    **只看 ok，不看 findings 是否为空。** ok=False 时 findings 必然是空列表
    （aggregate.py 的契约），跟"确实没查出风险"的 ok=True 在列表长度上长得
    一样——唯一能分辨两者的字段是 ok。这里特意不写 `if not r.findings`：
    那样会把一次真实的采集失败和一次干净的体检结果混为一谈，恰恰是
    Task 18/19 要防的静默失效。
    """
    if any(not r.ok for r in sub_results):
        return 3
    return 0


def run_health(runner, include: list[str], exclude: list[str], top: int,
               th: Thresholds, sub_results: list = ()) -> HealthEvidence:
    """Run every selected collector (read-only) and assemble the evidence pack.
    Per-collector failures degrade (available=False); collectors never raise.

    sub_results：已经按 scope 过滤过的 aggregate.SubSkillResult 列表（locks/
    waits/lwlock/bloat 四个维度现在从这里来，不再是本地 collector）。只并入
    `r.ok is True` 的 findings —— 判断依据是 ok，不是 findings 是否为空。
    """
    if top <= 0:
        top = 10
    ev = HealthEvidence()
    inc = set(include)
    exc = set(exclude)
    for key, fn in collectors.registry():
        if inc and key not in inc:
            continue
        if key in exc:
            continue
        d = fn(runner, th, top)
        ev.dims.append(d)
        ev.findings.extend(d.findings)
    for r in sub_results:
        if r.ok:
            ev.findings.extend(r.findings)
    # Stable sort by severity desc (matches Go sort.SliceStable).
    ev.findings.sort(key=lambda f: int(f.severity), reverse=True)
    ev.overall = worst([f.severity for f in ev.findings])
    return ev


def annotate_counters(ev: HealthEvidence, conn: str, full_run: bool) -> str:
    """把累计计数器换算成「距上次新增」,并据此修正判定。返回一句说明(空 = 没做)。

    **为什么必须做这件事。** 死锁数、回滚数都是**自上次统计重置以来**的累计值。
    一旦发生过一次死锁,`deadlocks` 就永远 > 0,健康检查从此每次都报「死锁累计数 24」。
    报了三个月,其中可能两个月零新增 —— 看报告的人分不出「正在发生」和「历史上发生过」,
    久了把这条当背景噪音,真的新增时也看不见了。

    有基线时:
      · 累计值照样显示(回答「历史上有没有」);
      · **判定改按增量**(回答「现在有没有」)—— 零新增时把死锁这条降级成信息行,
        并写明「累计 N 次,距上次巡检没有新增」;
      · 回滚率也按增量算 —— 累计回滚率会被历史摊平。

    只有完整巡检才更新基线(与存档同一个条件):局部巡检(--include)拿到的计数器是一样的,
    但如果它也更新基线,下一次完整巡检的「距上次」就被这次局部运行截断了。

    **给不出增量时,原因必须进报告**(维度表一行 + DEADLOCKS 那条的证据)。faultlab 那轮
    (2026-10-24)死锁 80 → 96,报告只有累计值、一句没说为什么 —— 原因其实算出来了,
    只是从没写进报告。
    """
    dim = next((d for d in ev.dims if d.raw.get("deadlocks") is not None), None)
    if dim is None:
        return ""
    now = {k: int(dim.raw.get(k, 0) or 0) for k in counters_mod.TRACKED}
    path = counters_mod.baseline_path(reports.reports_dir(), reports.instance_key(conn))
    before, since, why = counters_mod.load_baseline(path)
    res = counters_mod.compute(now, before, since)
    saved = full_run and counters_mod.save(path, reports.utc_stamp(), now)
    note = res.note if before else why + _baseline_outcome(path, full_run, saved)

    dl = res.items.get("deadlocks")
    if dl is not None and dl.delta is not None:
        _deadlocks_by_delta(ev, dim, dl)
    elif dl is not None and dl.now > 0:
        # 有基线却给不出:要么统计被重置过(note 已经说了),要么老基线里没记这一项
        _deadlocks_without_delta(ev, dim, note if (not before or res.reset)
                                 else "上一次巡检的基线里没有死锁计数")
    rb = counters_mod.rollback_pct(res, "delta")
    if rb is not None:
        dim.rows.append(["回滚率 距上次", "%.1f%%" % rb])
    return note


def _baseline_outcome(path, full_run: bool, saved: bool) -> str:
    """没有基线时,这次留没留下 —— 告诉读报告的人下次能不能给出增量。"""
    if path is None:
        return ""
    if not full_run:
        return "；局部巡检（--include/--exclude）不留基线"
    if saved:
        return "；本次已留基线，下次完整巡检可给出「距上次新增」"
    return "；本次基线也没存下来（存档目录不可写）"


def _replace_deadlock_finding(ev: HealthEvidence, dim, **changes) -> None:
    """Finding 是 frozen dataclass:**不能改字段,只能换一条新的**。
    (第一版直接赋值,报 "cannot assign to field 'metric'",而它发生在
     「确实有新增死锁」这条路径上 —— 正是最该报出来的时候整份报告挂掉。
     仓库的不可变规范本来也要求重建而不是原地改。)"""
    def _swap(f):
        return dataclasses.replace(f, **changes) if f.code == "DEADLOCKS" else f
    dim.findings[:] = [_swap(f) for f in dim.findings]
    ev.findings[:] = [_swap(f) for f in ev.findings]


def _deadlocks_by_delta(ev: HealthEvidence, dim, dl) -> None:
    dim.rows.append(["deadlocks 距上次新增", "%d（累计 %d）" % (dl.delta, dl.now)])
    # 判定按增量来:零新增就不该再占着一条发现
    if dl.delta <= 0:
        dim.findings[:] = [f for f in dim.findings if f.code != "DEADLOCKS"]
        ev.findings[:] = [f for f in ev.findings if f.code != "DEADLOCKS"]
        dim.rows.append(["deadlocks 判定", "累计 %d 次，但距上次巡检没有新增" % dl.now])
        return
    _replace_deadlock_finding(ev, dim, metric="死锁（距上次新增）",
                              value="%d（累计 %d）" % (dl.delta, dl.now), threshold=">0")


def _deadlocks_without_delta(ev: HealthEvidence, dim, reason: str) -> None:
    """给不出增量:值仍是累计值(不编增量),但要写明这是只增不减的累计值、为什么给不出。"""
    dim.rows.append(["deadlocks 距上次新增", "给不出：" + reason])
    prefix = ("累计值（自上次统计重置以来只增不减，不会自己清零），分不出是正在发生还是早就发生过；"
              "这次给不出「距上次新增」：%s。" % reason)
    found = next((f for f in dim.findings if f.code == "DEADLOCKS"), None)
    if found is not None:
        _replace_deadlock_finding(ev, dim, evidence=prefix + found.evidence)


def _split_dim_list(s: str) -> list[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def _run_window(runner, args) -> int:
    """时段体检。能回看的维度两端相减;瞬时维度终点是此刻时给当前值,否则明列回看不了。"""
    try:
        cov, _ck = window_mod.plan(runner, args.from_, args.to, args.window)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    conn = common.config.resolved_name(args.conn)
    if cov.start is None:
        print("# 时段体检 — %s\n\n口径：%s\n" % (conn, cov.note))
        return 0
    timeout = (args.timeout if args.timeout is not None
               else access.DEFAULT_SKILL_TIMEOUT_SECONDS)
    ev = window_mod.collect(runner, cov)
    ev.dims.append(window_mod.topsql_dim(cov, args.top, aggregate.script_path("gaussdb-topsql"),
                                         timeout, args.conn))
    subs = []
    if cov.end.kind == "live":
        # 终点是此刻:瞬时维度给当前值,标明是当前值 —— 它们回答「现在怎么样」,
        # 不回答「那段时间里怎么样」
        subs = [aggregate.run_sub_skill(s, args.conn, timeout)
                for s in ("gaussdb-lockwait", "gaussdb-vacuum")]
        cur = run_health(runner, ["conn", "xact", "repl", "storage"], [], args.top,
                         default_thresholds(), sub_results=subs)
        for d in cur.dims:
            d.headline = "【当前值，不是时段内的】" + (d.headline or "")
        ev.dims += cur.dims
        sub_findings = [f for r in subs if r.ok for f in r.findings]
    else:
        # 锁阻塞从活跃会话采样回看(lockwait --from/--to);其余瞬时维度回看不了
        ev.dims.append(window_mod.lock_dim(cov, aggregate.script_path("gaussdb-lockwait"),
                                           timeout, args.conn))
        ev.dims += window_mod.dims_unavailable_instant()
        sub_findings = []
    window_mod.finish(ev)
    ev.findings += sub_findings
    ev.findings.sort(key=lambda f: int(f.severity), reverse=True)
    ev.overall = worst([f.severity for f in ev.findings])
    ev.conn = conn
    d = health_dict(ev, sub_results=subs)
    d["mode"] = "span"
    d["span_note"] = cov.note
    # 存档索引按它筛时间段(common/reports._window_summary)
    d["window"] = {"begin_ts": cov.start.at.strftime("%Y-%m-%d %H:%M:%S"),
                   "end_ts": cov.end.at.strftime("%Y-%m-%d %H:%M:%S")}
    if args.format == "json":
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        out = render_health(ev, sub_results=subs,
                            no_sub_note="" if cov.end.kind == "live" else window_mod.PAST_SUB_NOTE)
        print(out.replace("# Health Evidence — ", "# 时段体检 — ", 1), end="")
    # latest=False:大盘默认显示的「最近一次巡检」仍是此刻巡检,时段报告经 run= 直达(见 reports.archive)
    saved = reports.archive("health", d, name="%s.span" % reports.utc_stamp(), instance=conn, latest=False)
    reports.announce("health", saved, conn,
                     span=(cov.start.at.strftime("%Y-%m-%dT%H:%M"), cov.end.at.strftime("%Y-%m-%dT%H:%M")))
    return _exit_code(subs)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="health.py",
        description="Read-only health check: 8 local dimensions + 3 sub-skill "
                    "dimensions aggregated via subprocess (deterministic findings)")
    ap.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    ap.add_argument("--include", default="",
                    help="只采集这些维度(逗号分隔)。本地: overview,slowsql,xact,conn,logs,"
                         "repl,schema,concurrency；路由到子 skill: waits,lwlock,locks,bloat")
    ap.add_argument("--exclude", default="", help="排除这些维度（同上，含四个路由名）")
    ap.add_argument("--top", type=int, default=10, help="各 Top 列表条数")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--timeout", type=int, default=None)
    # 时段体检:回看「某个时间段 / 最近 N 分钟」。不带这三个 = 原来的「此刻」巡检。
    ap.add_argument("--from", dest="from_", default="", metavar="'YYYY-MM-DD HH:MM'",
                    help="时段体检起点(数据库所在时区)。窗口取包住这段时间的快照 / 此刻,并说明实际评估的是哪一段")
    ap.add_argument("--to", default="", metavar="'YYYY-MM-DD HH:MM'", help="时段体检终点;不给 = 到此刻")
    ap.add_argument("--window", default="", metavar="10m", help="时段体检:最近这么久(10m / 2h / 1d)")
    args = ap.parse_args(argv)
    cli.apply_session_arg(args)

    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.from_ or args.to or args.window:
        if args.include or args.exclude:
            print("error: 时段体检不支持 --include/--exclude", file=sys.stderr)
            return 2
        return _run_window(runner, args)

    inc_list = _split_dim_list(args.include)
    exc_list = _split_dim_list(args.exclude)
    inc_set, exc_set = set(inc_list), set(exc_list)
    timeout = (args.timeout if args.timeout is not None
              else access.DEFAULT_SKILL_TIMEOUT_SECONDS)

    try:
        # 只有真的有子 skill 维度在范围内时才起子进程——用户只要本地维度
        # （比如 --include overview）时不必白跑三个子进程。
        if any(_sub_skill_in_scope(s, inc_set, exc_set) for s in aggregate.SUB_SKILLS):
            all_sub_results = aggregate.collect_all(args.conn, timeout)
        else:
            all_sub_results = []
        sub_results = [r for r in all_sub_results
                       if _sub_skill_in_scope(r.skill, inc_set, exc_set)]

        ev = run_health(runner, inc_list, exc_list, args.top, default_thresholds(),
                        sub_results=sub_results)
        ev.conn = common.config.resolved_name(args.conn)
        # 累计计数器换算成「距上次新增」。必须在生成 d / 渲染**之前**做 ——
        # 它会改动判定(零新增时撤掉 DEADLOCKS 那条发现)。
        cnote = annotate_counters(ev, ev.conn, full_run=not inc_list and not exc_list)
        if cnote:
            ev.overall = worst([f.severity for f in ev.findings])
        d = health_dict(ev, sub_results=sub_results)
        if args.format == "json":
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            print(render_health(ev, sub_results=sub_results), end="")
        # 存档在输出之后:大盘读 latest.json;失败只 warn,不影响这次的输出与退出码。
        # 只有完整巡检才存:--include/--exclude 的局部运行(深挖时常见)存进去会成为「最近一次」,
        # 大盘只剩那一两张卡、历史里多一次假巡检(2026-09-23 user 反馈)。
        if not inc_list and not exc_list:
            saved = reports.archive("health", d, instance=d.get("conn", ""))   # 按实例分目录:大盘按库看
            reports.announce("health", saved, d.get("conn", ""))
        # 报告已经打印完——3 是附加信息，不是替代输出。exit code 只看 ok，
        # 见 _exit_code 的说明。
        return _exit_code(sub_results)
    except common.DBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:          # 渲染/协议层的失败也要清楚报出来
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
