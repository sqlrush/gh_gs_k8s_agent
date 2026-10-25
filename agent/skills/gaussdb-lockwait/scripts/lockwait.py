#!/usr/bin/env python3
"""lockwait — 锁堵塞分析：谁挡了谁、挡在什么锁上、挡了多久、根源是谁。

只读。生成的 kill 语句**只输出文本，不执行**。

Usage:
    lockwait.py -c <conn> [--limit 20] [--format json] [--timeout 30]
    lockwait.py -c <conn> --from 'YYYY-MM-DD HH:MM[:SS]' [--to …]   # 时段回看(aspchain.py)

**此刻模式只能在堵塞发生时抓。** openGauss 的 statement_history 只记
lock_wait_time 总量，不记当时是谁在阻塞。事后追查「某段时间谁挡了谁」走 --from/--to：
读活跃会话采样（gs_asp，每个样本带 block_sessionid），粒度约 10 秒、受保留天数限制，
见 aspchain.py。（2026-10-21 之前这里写「事后做不到」——那时只看了 statement_history。）

**本文件与 task-7-brief.md 的三处对照（详见 task-7-report.md）：**

1. lockwait.pairs 按 locktag 自连接会把与 waiter 请求模式并不冲突的
   holder 也配进来（无辜旁观者）。这里用 common.lockmodes.conflicts()
   过滤；矩阵不认识的模式不当成「不冲突」丢掉——那等于用一个猜测冒充判定，
   而现场可能正被这一对堵着。保留该行，渲染时明确标「互斥关系未知」。
2. waiter_wait_s / holder_xact_age_s 现在可能是 NULL。**协议把 NULL
   渲染成空串，不是 Python None**（common/grmp/serialize.py 的
   render_cell：value is None 时走 settings.null_text，默认就是空串；
   common/grmp/values.py 的 is_null() 正是为这件事存在的）。这里一律用
   is_null() 判断"未知"，不用 `is None`，也不用 `or 默认值`（后者会把
   真正的 0 也吞成"未知"）。未知时长不折成 0：那会让本该最先报出来的
   一行（被遗弃的预备/2PC 事务，持锁时间不可知）在严重度上显得无害，
   与 pairs.yaml 把它排到结果最前面的用意正好相反。
3. recovery.py 生成的函数已是两参数、会话感知的
   pg_cancel_session(pid, sessionid) / pg_terminate_session(pid, sessionid)。
   本文件调用 recovery.kill_for() 时继承这一点，不重新引入单参数形式。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
from common.finding import Finding, Severity, findings_to_json  # noqa: E402
from common.grmp.values import as_float, as_int, is_null  # noqa: E402
from common.lockmodes import conflicts  # noqa: E402
import aspchain  # noqa: E402
import chain  # noqa: E402
import deadlocklog  # noqa: E402
import deadlockreport  # noqa: E402
import locktypes  # noqa: E402
import recovery  # noqa: E402
import render  # noqa: E402

SKILL = "gaussdb-lockwait"
DIM = "Locks"

WAIT_WARN_S = 5.0
WAIT_CRIT_S = 60.0

PAIRS_SCRIPT = "lockwait.pairs"
CHAIN_SCRIPT = "lockwait.chain"


@dataclass(frozen=True)
class LockReport:
    pairs: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    roots: dict = field(default_factory=dict)
    deadlocks: list = field(default_factory=list)
    findings: list = field(default_factory=list)


def _mode_conflict(holder_mode, waiter_mode):
    """三态判定：True/False/None（None＝矩阵不认识这个模式，判不出）。"""
    try:
        return conflicts(holder_mode, waiter_mode)
    except KeyError:
        return None


def _filter_conflicting(pairs: list) -> list:
    """按 8 级锁矩阵过滤掉「无辜旁观者」holder。

    lockwait.pairs 在同一 locktag 上把 waiter 与**每一个**已授予的 holder
    配对，其中只有模式互斥的那些才是真正挡住 waiter 的会话；其余只是碰巧
    在同一把锁上、模式并不冲突的旁观者，不该出现在报告里（会被误当成
    「有 N 个会话阻塞」）。

    矩阵不认识的模式（conflicts() 抛 KeyError）不当「不冲突」处理——那是
    冒充一个我们根本没做出的判定，而现场可能正被这一对堵着。这种行的
    唯一正确处理是**保留**，交给渲染层明确标注「互斥关系未知」，让人自己
    判断，而不是让它悄悄从报告里消失。
    """
    kept = []
    for p in pairs:
        verdict = _mode_conflict(p.get("holder_mode"), p.get("waiter_mode"))
        if verdict is not False:   # True 或 None（未知）都保留
            kept.append(p)
    return kept


def collect(runner, limit: int) -> LockReport:
    # 注意：LIMIT 在 SQL 里先于这里的冲突过滤生效——lockwait.pairs 先按
    # limit 截断原始行，_filter_conflicting() 再从截断后的结果里剔除旁观
    # 者。所以 --limit 20 之后拿到的真冲突对可能少于 20 条（截断掉的那些
    # 原始行里如果混了旁观者，真冲突的名额被占掉了）。不是本次要改的行为。
    raw_pairs = runner.run(PAIRS_SCRIPT, {"limit": int(limit)})
    pairs = _filter_conflicting(raw_pairs)
    raw_edges = runner.run(CHAIN_SCRIPT, {})
    edges = [(as_int(e["sessionid"]), as_int(e["block_sessionid"]))
             for e in raw_edges]
    roots = chain.roots(edges)
    deadlocks = chain.cycles(edges)
    findings = _judge(pairs, deadlocks)
    return LockReport(pairs=pairs, edges=edges, roots=roots,
                      deadlocks=deadlocks, findings=findings)


def _wait_seconds(p: dict):
    """取一对的等待秒数。未知（NULL，协议里是空串）原样返回 None，
    不折成 0——0 与「取不到」是两个不同的事实。"""
    raw = p.get("waiter_wait_s")
    return None if is_null(raw) else as_float(raw)


def _judge(pairs: list, deadlocks: list) -> list:
    """阈值判定 —— 确定性的，LLM 不得更改。"""
    out = []
    if deadlocks:
        out.append(Finding(
            DIM, "LOCK_DEADLOCK", Severity.CRITICAL, "死锁环数",
            str(len(deadlocks)), ">0",
            "环上的会话：" + "；".join(
                ", ".join(str(s) for s in ring) for ring in deadlocks)))
    if not pairs:
        return out

    waits = [_wait_seconds(p) for p in pairs]
    known = [w for w in waits if w is not None]
    unknown_count = len(waits) - len(known)
    longest_known = max(known) if known else None

    # 未知时长不当 0：0 意味着"确认过、几乎没等"，未知意味着"等了多久
    # 不知道，可能是被遗弃的预备/2PC 事务，锁可能一直挂着"。与
    # pairs.yaml 的 ORDER BY waiter_wait_s DESC NULLS FIRST 同一立场——
    # 未知在严重度上至少与已知最长值一样紧急，归到最高档，绝不能因为
    # "凑不出一个数字"就显得比已知的长等待更无害。
    if unknown_count or (longest_known is not None and longest_known >= WAIT_CRIT_S):
        code, sev = "LOCK_WAIT_LONG", Severity.CRITICAL
        thr = ">=%.0fs 或时长未知" % WAIT_CRIT_S
        if unknown_count:
            value = ("未知（%d/%d 条等待时长取不到，很可能是被遗弃的预备/2PC "
                     "事务持锁，持续时间不可知；已知最长 %s）"
                     % (unknown_count, len(pairs),
                        "%.1fs" % longest_known if longest_known is not None else "无"))
        else:
            value = "%.1fs" % longest_known
    elif longest_known is not None and longest_known >= WAIT_WARN_S:
        code, sev = "LOCK_WAIT", Severity.WARN
        thr = ">=%.0fs" % WAIT_WARN_S
        value = "%.1fs" % longest_known
    else:
        code, sev = "LOCK_BLOCKED", Severity.NOTICE
        thr = ">0"
        value = "%.1fs" % longest_known

    out.append(Finding(
        DIM, code, sev, "最长锁等待", value, thr,
        "%d 个会话被阻塞；最久的一条等在 %s 上"
        % (len(pairs), pairs[0].get("lock_object") or pairs[0].get("locktype"))))

    for p in pairs:
        if str(p.get("holder_state") or "").startswith("idle in transaction"):
            age_raw = p.get("holder_xact_age_s")
            age_display = "未知" if is_null(age_raw) else age_raw
            out.append(Finding(
                DIM, "LOCK_ROOT_IDLE_XACT", Severity.WARN, "空闲事务持锁",
                "会话 %s" % p.get("holder_sessionid"), "不应长期持有",
                "状态 %s，事务已持续 %s 秒，仍持有 %s"
                % (p.get("holder_state"), age_display, p.get("holder_mode"))))
            break
    return out


def _wait_display(p: dict) -> str:
    raw = p.get("waiter_wait_s")
    return "未知" if is_null(raw) else str(raw)


def kb_section(findings) -> str:
    """「客户知识库参照」:按每条 finding 查客户知识库(脚本层接入,与 health 同款)。

    知识库不存在 / 没配 / 连不上,小节只剩「> 知识库未接入(原因)」——报告照常,不许静默省略。
    """
    try:
        from common.kb import query as kbquery
    except ImportError as exc:                       # 旧安装没有 common/kb
        return f"## 客户知识库参照\n> 知识库未接入(common/kb 未安装:{exc})\n\n"
    return kbquery.section_for(list(findings))[0]


def render_markdown(rep: LockReport) -> str:
    if not rep.pairs:
        # **空结果要明说。** 空白会被读成「这项没查」。
        if rep.deadlocks:
            # pairs 与 chain 是两条独立查询，中间有时间差：理论上可能查到
            # 死锁环、却拿不到对应的持有者/等待者明细（状态在两次查询之间
            # 变了）。这时不能说"当前无锁等待"——那与死锁环矛盾。
            return ("# 锁堵塞分析\n\n检测到死锁环，但未能取得对应的持有者/"
                    "等待者明细（两次查询之间状态发生了变化）。环上的会话："
                    + "；".join(" → ".join(str(s) for s in ring) + " → …回到起点"
                                for ring in rep.deadlocks) + "\n\n" + kb_section(rep.findings))
        return ("# 锁堵塞分析\n\n当前无锁等待 —— 查询正常返回，"
                "没有任何会话在等锁。\n\n" + kb_section(rep.findings))

    waiter_ids = sorted({as_int(p.get("waiter_sessionid")) for p in rep.pairs})
    deepest = max((chain.depth(rep.edges, sid) for sid in waiter_ids), default=0)
    unknown_mode_count = sum(
        1 for p in rep.pairs
        if _mode_conflict(p.get("holder_mode"), p.get("waiter_mode")) is None)
    root_groups = chain.blocked_by_root(rep.edges)

    out = ["# 锁堵塞分析\n",
           "共 %d 条阻塞链、%d 对阻塞关系，涉及 %d 个等待会话，阻塞链最深 %d 层。\n"
           % (len(root_groups), len(rep.pairs), len(waiter_ids), deepest)]
    if unknown_mode_count:
        out.append("> %d 对的锁模式不在已知的 8 级矩阵里，互斥关系无法判定，"
                   "已保留在明细中并单独标注，未按「不冲突」丢弃。\n"
                   % unknown_mode_count)
    if rep.deadlocks:
        out.append("> **检测到死锁**：" + "；".join(
            " → ".join(str(s) for s in ring) + " → …回到起点"
            for ring in rep.deadlocks) + "\n")
    # 客户知识库对这些锁发现怎么说——放在明细之前,处置建议(含下面的快速恢复语句)以它为首选依据。
    out.append(kb_section(rep.findings))

    body = []
    for p in rep.pairs:
        body.append([
            str(p.get("waiter_sessionid")), str(p.get("holder_sessionid")),
            "%s ← %s" % (p.get("waiter_mode"), p.get("holder_mode")),
            "%s %s" % (p.get("locktype"), p.get("lock_object") or ""),
            _wait_display(p),
            str(rep.roots.get(as_int(p.get("waiter_sessionid")), "")),
            render.truncate(p.get("holder_query") or "", 60),
        ])
    out.append("## 阻塞明细\n")
    out.append(render.table(
        ["等待会话", "持有会话", "模式对(waiter←holder)", "锁对象",
         "等待秒", "根阻塞会话", "持有者正在执行"], body))

    out.append("\n## 互斥关系\n")
    for p in rep.pairs[:5]:
        # 按锁类型说:8 级矩阵与「典型语句」只适用于表锁,事务号 / 行 / 咨询锁各有各的意思(见 locktypes.py)
        try:
            reason, detail = locktypes.explain(p.get("locktype"), p.get("holder_mode"),
                                               p.get("waiter_mode"), p.get("waiter_query") or "")
        except KeyError as exc:
            reason = "互斥关系未知：%s" % exc
            detail = "（未知锁模式，无法给出典型语句）"
        out.append("- 会话 %s ← %s：%s\n  %s"
                   % (p.get("waiter_sessionid"), p.get("holder_sessionid"),
                      reason, detail))

    out.append("\n## 阻塞链与根\n")
    if not root_groups:
        out.append("没有可归纳的阻塞链（`lockwait.chain` 未给出可用的边，"
                   "「根阻塞会话」栏留空）。\n")
    else:
        for root, blocked in root_groups.items():
            out.append("- 根会话 %s 最终挡住 %d 个会话：%s"
                       % (root, len(blocked),
                          ", ".join(str(s) for s in blocked)))
    out.append("")

    out.append("\n" + _render_recovery_section(rep))
    return "\n".join(out)


def _direct_blockers(edges: list) -> dict:
    """(waiter, blocker) 边表 → waiter → 它被记录到的**全部**直接阻塞者。

    与 `chain._blocker_of()` 只留第一条边（`setdefault`）的差别是有意的，
    因为两者回答的不是同一个问题：chain 要给出"根是谁"这**一个**答案，
    只能挑一条边；这里要回答的是"处理掉根会话之后，这个 holder 会不会
    跟着解开"——那是一句会写进恢复语句小节的**肯定断言**，只要有任何
    一条被记录下来的等待边不通向那个根，这句断言就不成立。在这里跟着
    丢掉第二条边，只会让断言看起来成立。
    """
    out: dict = {}
    for waiter, blocker in edges:
        bucket = out.setdefault(waiter, [])
        if blocker not in bucket:
            bucket.append(blocker)
    return out


def _released_by_killing(blockers: dict, node: int, target: int) -> bool:
    """只凭阻塞链数据判断：处理掉 `target` 之后，`node` 是否**必定**不再
    卡在等待上（因而它持有的锁会随之释放）。

    True 的充要条件是：从 node 出发，**每一条**被记录下来的等待边最终都
    落到 target 上。两种情况一律返回 False，都是"数据没说"而不是"数据说
    了不"：

      - node 根本没被记录成等待者（`blockers` 里没有它）——chain 没说它
        在等谁，就不能替它断言"它会跟着解开"。**这正是 review 给出的那
        个复现**：一个 waiter 同时被 200 和 201 独立挡着，chain 只记下
        200；201 自己不在等任何人，杀掉 200 之后它照样挡着。
      - 顺着边绕回了已经走过的会话（环）却没碰到 target——环里的会话互相
        等着，处理 target 解不开它们。

    不用递归：真实的阻塞链很短，但报告不能因为一条畸形的长链就抛
    RecursionError。
    """
    stack = [(node, frozenset())]
    while stack:
        cur, visiting = stack.pop()
        if cur == target:
            continue                      # 这条分支落到 target 上了
        if cur in visiting:
            return False                  # 绕回来了，没碰到 target
        waiting_on = blockers.get(cur)
        if not waiting_on:
            return False                  # chain 没说它在等谁
        deeper = visiting | {cur}
        for nxt in waiting_on:
            stack.append((nxt, deeper))
    return True


def _classify_pairs(rep: LockReport) -> dict:
    """把 `rep.pairs` 里**每一对**都归到唯一一类——这是一个全函数
    （total function）：并集覆盖全部 pairs，各类互不重叠，任何一对都不
    会因为落在两类判据的缝隙里而消失。

    前几轮的教训：先是只分 confirmed/unconfirmed 两类，遗漏了"根已确认
    但没出现在 pairs 里"的第三种情况；后来在渲染层用 `kills`/`reasons`
    两个聚合状态去判断该不该提示，又在"两条链混在一份报告里、一条成功
    生成语句、另一条恰好落进第三类"时失效——聚合判断只关心"整体有没有
    话可说"，看不见"某一条具体的 pair 有没有被照顾到"。于是把判断挪到
    最细的粒度：**每一对**必属其一，渲染层只管照着这些类如实转述，不再
    做任何整体性的"还有没有话说"式推断。

    round 4 补上的是另一半：不但"不能少说"，而且"不能多说"。上一版把
    `intermediate` 的判据写成"这个 waiter 的根**恰好**也出现在某一对的
    holder 列里"，从来没有验证过**这一对自己的 holder** 与那个根之间有
    任何关系——于是会对一个独立的共同阻塞者说"处理掉根就顺带解决了它"，
    而事实是杀掉根之后它照样挡着。根源是两条查询的不对称：
    `lockwait.pairs`（`pg_locks` 自连接）一个 waiter 可以有好几个真冲突
    的 holder（一条 DDL 等 AccessExclusive、被好几个 AccessShare 读者
    同时挡住，是最平常的现场），而 `lockwait.chain`
    （`pg_thread_wait_status`）每个等待者只记一个阻塞者。现在这句"已被
    覆盖"要过 `_released_by_killing()` 那一关：数据连不上就不许说。

    五类（对每一对 (waiter, holder)）：

      root          holder 本身就是（某条链的）确认根——是 kill 语句的
                    直接对象。
      data_gap      chain 里完全没有这个 waiter 的数据（waiter_sid 不在
                    rep.roots 里）——不知道这个 holder 是不是根。
      coblocker     chain 给出了这个 waiter 的根，但**没有任何数据**表明
                    这个 holder 会随着那个根一起解开——它是一个独立的
                    共同阻塞者（或者只是 chain 没覆盖到它），必须当成
                    "还没被处理的阻塞者"单独摆出来，绝不能算进已覆盖。
      intermediate  chain 给出了这个 waiter 的根，且数据可以确认这个
                    holder 自己也在（传递地）等那个根，那个根又**确实**
                    作为某一对的 holder 出现在了 rep.pairs 里——这个
                    holder 是链条中间节点，处理根时会一并解开，真正该
                    处理的根有另一对负责生成语句/说明。
      orphan_root   同上，但那个根**没有**作为任何一对的 holder 出现在
                    rep.pairs 里——根已确认，却没有材料（pid/状态/
                    query……）生成语句，需要人工继续查。

    `lockwait.pairs` 与 `lockwait.chain` 是两条**独立、不同视图**上的
    查询，覆盖面不保证一致：data_gap、coblocker、orphan_root 都是这种
    不一致的直接后果，三者的共同点是"我们不知道"，处理方式一律是如实
    说出不知道什么，而不是挑一个看起来合理的说法填上去。
    """
    root_ids = set(rep.roots.values())
    holder_sids_in_pairs = {as_int(p.get("holder_sessionid")) for p in rep.pairs}
    blockers = _direct_blockers(rep.edges)

    buckets: dict = {"root": [], "data_gap": [], "coblocker": [],
                     "intermediate": [], "orphan_root": []}
    for p in rep.pairs:
        waiter_sid = as_int(p.get("waiter_sessionid"))
        holder_sid = as_int(p.get("holder_sessionid"))
        if holder_sid in root_ids:
            buckets["root"].append(p)
            continue
        if waiter_sid not in rep.roots:
            buckets["data_gap"].append(p)
            continue
        root_sid = rep.roots[waiter_sid]
        if not _released_by_killing(blockers, holder_sid, root_sid):
            buckets["coblocker"].append(p)
        elif root_sid in holder_sids_in_pairs:
            buckets["intermediate"].append(p)
        else:
            buckets["orphan_root"].append(p)
    return buckets


def _dedup_by_holder(pairs: list) -> list:
    """按 holder 去重——生成 kill 语句只需要一条，同一个根挡了几个
    waiter 不该重复杀。"""
    seen, out = set(), []
    for p in pairs:
        h = as_int(p.get("holder_sessionid"))
        if h not in seen:
            seen.add(h)
            out.append(p)
    return out


def _dedup_by_waiter_and_holder(pairs: list) -> list:
    """按 (waiter, holder) 这一对去重——枚举"缺口"类文本的单位是
    「对阻塞关系」，按 holder 单独折叠会让两个不同 waiter 撞上同一个
    holder 时被压成一条，数字和枚举都比实际少。"""
    seen, out = set(), []
    for p in pairs:
        key = (as_int(p.get("waiter_sessionid")), as_int(p.get("holder_sessionid")))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _waiters_by_holder(pairs: list) -> dict:
    """holder_sessionid -> 被它挡住的 waiter_sessionid 列表（保留原始
    字符串、去重、按出现顺序）。用来在 kill 语句/失败说明旁边标注
    "这条语句预计解除了哪些会话"——`recovery.kill_for()` 只知道 holder，
    不知道被它挡住的是谁，这段追加要在 lockwait.py 这一层做。"""
    out: dict = {}
    for p in pairs:
        h = as_int(p.get("holder_sessionid"))
        w = p.get("waiter_sessionid")
        bucket = out.setdefault(h, [])
        if w not in bucket:
            bucket.append(w)
    return out


def _kill_statements(root_holders: list):
    """给已确认的根 holder 生成 kill 语句。

    recovery.kill_for() 内部是裸 `int(holder.get("holder_pid") or 0)`——
    在 brief 起草时输入还是假想的，可以不管；这里接的是真实查询结果，
    协议把所有列值都渲染成字符串。两类输入都不能悄悄变成一个编造的 0：

      - 非数字字符串（取数异常留下的脏值）：as_int() 会抛，这里接住。
      - NULL（协议里是空串 ""，见 common.grmp.values.is_null()）：
        as_int() 对空串/None 的默认行为是返回 0，而不是抛——那正是
        本函数要避免的"悄悄编一个 0"，所以在调用 as_int() 之前先用
        is_null() 单独挡一道，NULL 一律算失败，不让它混进 as_int()
        的默认值路径。

    两种情况都不生成语句，把原因摆出来让人工核对，而不是生成一条看着
    正常、其实指向假会话（pid=0）的 kill 语句。
    """
    kills, failures = [], []
    for p in root_holders:
        pid_raw = p.get("holder_pid")
        sid_raw = p.get("holder_sessionid")
        if is_null(pid_raw) or is_null(sid_raw):
            failures.append((p, "holder_pid/holder_sessionid 缺失（NULL），"
                                "拒绝用默认值 0 顶替去生成 kill 语句"))
            continue
        try:
            safe = dict(p)
            safe["holder_pid"] = as_int(pid_raw)
            safe["holder_sessionid"] = as_int(sid_raw)
        except (ValueError, TypeError) as exc:
            failures.append((p, str(exc)))
            continue
        kills.append(recovery.kill_for(safe))
    return kills, failures


def _render_recovery_section(rep: LockReport) -> str:
    """渲染「## 快速恢复语句」整段。

    **两条不变量（这是本函数存在的唯一理由）：**

      1. `rep.pairs` 里的每一对，要么被一条已生成的 kill 语句覆盖，要么
         有一句写明的理由说明为什么没有。不允许任何一对因为"恰好还有
         别的对生成了语句/给出了理由"就被聚合状态判断悄悄放过。
      2. **没有任何一对可以在数据没有真正建立起连接的情况下被说成"已被
         别处覆盖"。** 少说一句是沉默，说错一句是在读的人正准备结束故障
         的那一节里给他一个错的肯定 —— 后者更糟。判据交给
         `_released_by_killing()`，连不上就归 coblocker 段如实说明。

    这是本函数第三次因为同一类问题被改：
      round 1：只判断"有没有 unconfirmed"，漏了"confirmed 但全部生成
        失败"这个分支。
      round 2：把裸"无"堵死了，但用 `kills`/`reasons` 两个**聚合**布尔
        状态去决定要不要打印某一类说明——两条独立的阻塞链混在一份报告
        里、一条成功生成了语句时，`kills` 非空，另一条恰好落进"根已
        确认但不在 pairs 里"这个兜底分支就被跳过了，因为兜底分支的
        触发条件写的是 `if not kills and not reasons`，只要**别的**pair
        让 kills 非空，这一对就静默消失。
      round 3：不再用任何聚合状态做判断。`_classify_pairs()`
        把每一对都放进四个互斥的桶（root / data_gap / intermediate /
        orphan_root），下面对四个桶**各自独立**地渲染说明——桶 A 是否
        非空只取决于桶 A 里有没有 pair，与桶 B 是否非空无关。这样不管
        一份报告里同时出现多少条独立的阻塞链、各自处于什么状态，每一对
        都有自己对应的一句话，不会因为报告里别的部分"看起来正常"就被
        捎带着忽略。
      round 4（本次）：前三轮都在补"不能少说"，这轮补的是"不能多说"。
        `intermediate` 那一桶对读者说的是一句肯定断言（"不用单独管，
        处理掉根就顺带解开了"），而它的判据只看"这个 waiter 的根恰好
        也出现在某个 holder 列里"，从来没有验证过**这一对自己的
        holder** 与那个根有任何关系 —— 一个 waiter 被两个会话独立挡住、
        chain 只跟踪了其中一个时，另一个会被说成"已覆盖"，而杀掉根它
        照样挡着。现在这句话要过 `_released_by_killing()` 那一关，过不
        了的归 coblocker，当成还没被处理的独立阻塞者摆出来。
    """
    if not rep.pairs:
        return recovery.render_kills([])   # 真正的"无"：没有阻塞对

    buckets = _classify_pairs(rep)
    root_pairs = _dedup_by_holder(buckets["root"])
    waiters_by_holder = _waiters_by_holder(buckets["root"])
    kills, kill_failures = _kill_statements(root_pairs)

    if kills:
        section = recovery.render_kills(kills)
        notes = []
        for k in kills:
            waiters = waiters_by_holder.get(k.target_sessionid, [])
            if waiters:
                # 措辞按**这一对**来写，不写成"解除了 X 的阻塞"：同一个
                # waiter 完全可以同时被好几个会话独立挡着（pg_locks 里
                # 一个 waiter 可以配出多个真冲突的 holder），这条语句只
                # 解除它自己那一份。别的那几份在本节其余条目里各自有话。
                notes.append(
                    "> 会话 %s 的这条语句解除的是**它自己**挡住的这几个会话："
                    "%s（这些会话若同时还被别的会话挡着，那部分见本节其余说明）"
                    % (k.target_sessionid, "、".join(str(w) for w in waiters)))
        if notes:
            section += "\n" + "\n".join(notes)
    else:
        section = ("## 快速恢复语句\n\n"
                   "> **未生成任何 kill 语句**——原因见下，不代表当前"
                   "无需处理：\n")

    if kill_failures:
        lines = []
        for p, reason in kill_failures:
            h = as_int(p.get("holder_sessionid"))
            waiters = waiters_by_holder.get(h) or [p.get("waiter_sessionid")]
            lines.append("> - 会话 %s（挡住 %s）：%s"
                         % (h, "、".join(str(w) for w in waiters), reason))
        # 块之间空一行：markdown 里连续的 `>` 行会并成同一段，"已覆盖"
        # 与"没被覆盖"两段挤在一起时，读的人很容易只看见前一句。
        section += "\n\n" + (
            "> **%d 个根 holder 的 kill 语句未能生成**（pid/sessionid "
            "缺失或不是合法数字，为避免编造数据已跳过，请人工核对该会话）：\n"
            % len(kill_failures) + "\n".join(lines))

    coblocker = _dedup_by_waiter_and_holder(buckets["coblocker"])
    if coblocker:
        lines = []
        for p in coblocker:
            w = p.get("waiter_sessionid")
            h = p.get("holder_sessionid")
            r = rep.roots[as_int(w)]
            lines.append(
                "> - 会话 %s ← %s（阻塞链数据只说会话 %s 在等会话 %s，"
                "没有任何一条边说会话 %s 也在等会话 %s）" % (w, h, w, r, h, r))
        # 块之间空一行：markdown 里连续的 `>` 行会并成同一段，"已覆盖"
        # 与"没被覆盖"两段挤在一起时，读的人很容易只看见前一句。
        section += "\n\n" + (
            "> **%d 对阻塞关系没有被上面任何一条语句覆盖 —— 这些持有者是"
            "独立的阻塞者，要各自单独处理**：`lockwait.pairs`（`pg_locks`）"
            "显示它们与等待者的锁模式确实互斥，等待者必须等它们释放；但"
            "`lockwait.chain`（`pg_thread_wait_status`）里没有任何数据表明"
            "它们自己在等对应的根会话 —— 那张视图每个等待者只记**一个**"
            "阻塞者，而同一个等待者在 `pg_locks` 里可以被好几个会话同时"
            "挡着（一条 DDL 等 AccessExclusive、被好几个 AccessShare 读者"
            "一起挡住是最平常的现场），链路数据里只会留下其中一个。**所以"
            "不能说处理掉根会话就顺带解决了它们**；也不给它们凭猜测生成 "
            "kill 语句（它们自己可能同样在等别人，杀了不解堵）。请逐条确认"
            "这个会话在等谁、要不要单独处理：\n" % len(coblocker)
            + "\n".join(lines))

    data_gap = _dedup_by_waiter_and_holder(buckets["data_gap"])
    if data_gap:
        # 块之间空一行：markdown 里连续的 `>` 行会并成同一段，"已覆盖"
        # 与"没被覆盖"两段挤在一起时，读的人很容易只看见前一句。
        section += "\n\n" + (
            "> **%d 对阻塞关系因阻塞链数据缺失，未能确认根 holder，"
            "未生成对应 kill 语句**（`lockwait.pairs` 与 `lockwait.chain` "
            "是两条独立查询，覆盖面可能不一致；不能因为 chain 没提到就"
            "当作「不是根」处理，也不能猜它是根去生成语句）：\n"
            % len(data_gap)
            + "\n".join(
                "> - 会话 %s ← %s（%s %s）"
                % (p.get("waiter_sessionid"), p.get("holder_sessionid"),
                   p.get("locktype"), p.get("lock_object") or "")
                for p in data_gap))

    intermediate = _dedup_by_waiter_and_holder(buckets["intermediate"])
    if intermediate:
        # 只有走到这里的对才允许说"已经被别处覆盖" —— 判据是
        # _released_by_killing()：阻塞链数据能确认这个持有者自己也在等
        # 那个根，处理根之后它必定跟着解开。连不上的一律进上面的
        # coblocker 段，当成还没被处理的独立阻塞者摆出来。
        lines = []
        for p in intermediate:
            w = p.get("waiter_sessionid")
            h = p.get("holder_sessionid")
            r = rep.roots[as_int(w)]
            lines.append(
                "> - 会话 %s ← %s：阻塞链数据可确认会话 %s 自己也在（传递地）"
                "等会话 %s，处理根会话 %s 之后它会一并解开" % (w, h, h, r, r))
        # 块之间空一行：markdown 里连续的 `>` 行会并成同一段，"已覆盖"
        # 与"没被覆盖"两段挤在一起时，读的人很容易只看见前一句。
        section += "\n\n" + (
            "> %d 对是阻塞链的中间节点，**已被针对根会话的处理覆盖**，因此"
            "不单独生成语句（杀中间节点不解堵）；根会话本身的语句或说明见"
            "本节其他条目：\n" % len(intermediate)
            + "\n".join(lines))

    orphan_root = _dedup_by_waiter_and_holder(buckets["orphan_root"])
    if orphan_root:
        # 块之间空一行：markdown 里连续的 `>` 行会并成同一段，"已覆盖"
        # 与"没被覆盖"两段挤在一起时，读的人很容易只看见前一句。
        section += "\n\n" + (
            "> **%d 对阻塞关系的根会话已被阻塞链数据确认，但该根未出现在"
            "「阻塞明细」的持有者列里**（它可能持有的是链条上游、不在"
            "本次 pairs 明细范围内的另一把锁），没有材料生成 kill 语句，"
            "需人工进一步排查：\n" % len(orphan_root)
            + "\n".join(
                "> - 会话 %s ← %s（根是会话 %s，未出现在明细持有者列里）"
                % (p.get("waiter_sessionid"), p.get("holder_sessionid"),
                   rep.roots[as_int(p.get("waiter_sessionid"))])
                for p in orphan_root))

    return section


def _read_log(path: str) -> str:
    """`-` = 标准输入(用户贴进对话的日志段走 heredoc);否则是文件。按 UTF-8 读,坏字节替换掉不报错。"""
    if path == "-":
        return sys.stdin.read(deadlocklog.MAX_BYTES + 1)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(deadlocklog.MAX_BYTES + 1)


def run_deadlock_log(path: str, fmt: str) -> int:
    """死锁日志解析:不连库,只读用户给的日志文本(见 deadlocklog.py 为什么这样做)。"""
    try:
        text = _read_log(path)
    except OSError as exc:
        print("error: 读不了日志 %s:%s" % (path, exc), file=sys.stderr)
        return 2
    parsed = deadlocklog.parse(text)
    if fmt == "json":
        print(json.dumps(deadlockreport.to_dict(parsed), ensure_ascii=False, indent=2))
    else:
        print(deadlockreport.render_markdown(parsed), end="")
    return 0


def _span_ts(text: str, what: str) -> str:
    """'YYYY-MM-DD HH:MM[:SS]' → 'YYYY-MM-DD HH:MM:SS';格式不对抛 ValueError(白名单的 DateTime 参数只认这一种)。"""
    t = " ".join((text or "").replace("T", " ").split())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(t, fmt).strftime(aspchain.TS_FMT)
        except ValueError:
            continue
    raise ValueError("%s %r 格式不对,要 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS(数据库所在时区)" % (what, text))


def run_span(runner, args) -> int:
    """时段回看:某段时间里谁挡了谁。终点不给 = 到数据库此刻。"""
    try:
        begin = _span_ts(args.from_, "--from")
        end = _span_ts(args.to, "--to") if args.to else ""
        if not end:
            st = aspchain.parse_status(runner.run(aspchain.STATUS_SCRIPT,
                                                  {"begin_time": begin, "end_time": begin}))
            end = st.db_now or datetime.now().strftime(aspchain.TS_FMT)
        if end <= begin:
            raise ValueError("终点 %s 不晚于起点 %s" % (end, begin))
        rep = aspchain.collect(runner, begin, end)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (common.DBError, access.QueryError) as exc:
        # 客户环境没导入新增的三条白名单时落到这里:说清是缺脚本,不是「没有锁等待」
        print(f"error: 时段锁回溯取数失败({exc})。需要白名单里有 {aspchain.STATUS_SCRIPT} / "
              f"{aspchain.PAIRS_SCRIPT} / {aspchain.SESSIONS_SCRIPT} 三条脚本", file=sys.stderr)
        return 2
    findings = aspchain.judge(rep, DIM, WAIT_WARN_S, WAIT_CRIT_S)
    if args.format == "json":
        d = json.loads(findings_to_json(findings, skill=SKILL))
        d["asp"] = aspchain.to_dict(rep)
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    conn = common.config.resolved_name(args.conn)
    out = aspchain.render_markdown(rep, findings, conn, args.limit)
    if findings:
        out += kb_section(findings)
    print(out, end="")
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="lockwait.py",
                                 description="锁堵塞分析（只读；kill 语句只生成不执行）")
    ap.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    ap.add_argument("--limit", type=int, default=20, help="阻塞明细条数上限")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--timeout", type=int, default=None)
    # 死锁明细只在数据库日志里(次数之外数据库不留),本工具读不到日志 —— 由用户把日志段给过来
    ap.add_argument("--deadlock-log", default="", metavar="文件|-",
                    help="解析数据库日志里的死锁段(含 deadlock detected 的那几段);- 表示从标准输入读。不连库")
    # 时段回看:读活跃会话采样(gs_asp),不生成 kill 语句(见 aspchain.py)
    ap.add_argument("--from", dest="from_", default="", metavar="'YYYY-MM-DD HH:MM'",
                    help="时段回看起点(数据库所在时区);给了就不看此刻,改读活跃会话采样")
    ap.add_argument("--to", default="", metavar="'YYYY-MM-DD HH:MM'", help="时段回看终点;不给 = 到此刻")
    args = ap.parse_args(argv)
    if args.deadlock_log:
        return run_deadlock_log(args.deadlock_log, args.format)
    if args.to and not args.from_:
        ap.error("--to 要和 --from 一起给")
    cli.apply_session_arg(args)

    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.from_:
        return run_span(runner, args)
    try:
        rep = collect(runner, args.limit)
    except (common.DBError, access.QueryError) as exc:
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
