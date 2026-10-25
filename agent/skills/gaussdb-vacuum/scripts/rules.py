"""触发线现算 + 四条手工清理判定 —— 纯函数，不连库。

四条规则互不短路：一张表可以同时命中 R1/R2/R3/R4，`evaluate()` 把它们分别
判一遍再拼进同一个列表，不是 elif 链 —— elif 会让一张「autovacuum 被关掉、
死元组比例同时也爆表」的表只报出排在前面的那一条，另一条真实存在的问题就
从报告里消失了。

R4 是本模块存在的核心理由：死元组能不能被回收，取决于有没有更老事务的
快照可能还用得到它们；只要这样的事务/复制槽存在，`VACUUM` 就是一条白费的
指令。所以 R4 命中时**不压制** R1/R2/R3 —— 那会把真实的膨胀问题藏起来 ——
而是给它们的 evidence 都缀上一句提示，让"建议 VACUUM"不至于误导人去跑一条
没用的命令（详见 skills/gaussdb-vacuum/SKILL.md 的「R4」一节）。

复制槽（replication_slot）这一路的 `xmin_age_s` 恒为空 —— 不是取数失败，是
`pg_replication_slots` 在这个内核上没有任何时间戳列（取数脚本那边逐列核对过
information_schema，故意把它留 NULL 而不是编造一个秒数）。
所以 R4 的判定绝不能依赖"xmin_age_s 是个可用的数字"，只依赖"oldest_xmin
里有没有条目"——一旦哪天改成依赖数值比较（哪怕只是 `if age:` 这种看似无害
的写法），复制槽这一整类阻塞源就会从判定里静默消失，而它照样在挡回收，
只是没人知道挡了多久。见 tests/test_vacuum_rules_units.py 里专门为这条防线
写的 test_r4_fires_on_a_replication_slot_with_no_usable_age。

取值一律走 common/grmp/values.py 的 is_null/as_bool/as_float —— 协议把 NULL
渲染成空串而不是 None，裸 `is None` 在这层协议下永远不成立；`or 0` 类写法
则会把「刚跑过（0 秒）」和「从没跑过（NULL）」这两个相反的事实混在一起。
"""
from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "common" / "finding.py").exists():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from common.finding import Finding, Severity  # noqa: E402
from common.grmp.values import as_bool, as_float, is_null  # noqa: E402

from thresholds import Thresholds, default_thresholds  # noqa: E402

__all__ = [
    "Thresholds", "default_thresholds", "trigger_line", "evaluate",
    "judge_tables",
]

DIMENSION = "Dead Tuples"

_GLOBAL_THRESHOLD_KEY = "autovacuum_vacuum_threshold"
_GLOBAL_SCALE_KEY = "autovacuum_vacuum_scale_factor"

_CODES = {
    "R1": "VACUUM_OVERDUE",
    "R2": "VACUUM_DISABLED",
    "R3": "VACUUM_DEAD_RATIO",
    "R4": "VACUUM_XMIN_BLOCKED",
}

_BLOCK_NOTE_TEMPLATE = (
    "注意：%s卡住了回收，现在跑 VACUUM 也回收不掉，先处理该事务/复制槽再说。"
)

_BLOCKER_LABELS = {
    "long_xact": "会话 %s 的长事务",
    "prepared_xact": "两阶段事务 %s",
    "replication_slot": "复制槽 %s",
}


def _parse_reloptions(reloptions) -> dict:
    """`'k=v,k2=v2'` -> `{'k': 'v', 'k2': 'v2'}`。空串/None 给空字典。"""
    if is_null(reloptions):
        return {}
    out = {}
    for item in str(reloptions).split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def trigger_line(reltuples: float, settings: dict, reloptions: str) -> float:
    """`threshold + scale_factor × reltuples`。

    全局值来自 `pg_settings`（`settings` 参数），逐表的 `reloptions` 里的同名
    项覆盖 —— 覆盖是逐项独立生效的：一张表可以只覆盖 scale_factor、
    threshold 仍然吃全局值（反之亦然）。
    """
    opts = _parse_reloptions(reloptions)
    threshold = as_float(
        opts.get(_GLOBAL_THRESHOLD_KEY, settings.get(_GLOBAL_THRESHOLD_KEY, 0)))
    scale = as_float(
        opts.get(_GLOBAL_SCALE_KEY, settings.get(_GLOBAL_SCALE_KEY, 0)))
    return threshold + scale * as_float(reltuples)


def _dead_ratio(table: dict) -> float:
    live = as_float(table.get("n_live_tup"))
    dead = as_float(table.get("n_dead_tup"))
    denom = live + dead
    if denom <= 0:
        return 0.0
    return dead / denom


def _table_autovac_enabled(table: dict) -> bool:
    """`reloptions` 里的 `autovacuum_enabled=false` 优先，其次看列本身的值。"""
    opts = _parse_reloptions(table.get("reloptions", ""))
    raw = opts.get("autovacuum_enabled")
    if raw is not None and not is_null(raw):
        return as_bool(raw)
    val = table.get("autovac_enabled", True)
    if is_null(val):
        return True
    return as_bool(val)


def _blocker_label(x: dict) -> str:
    source = x.get("source", "?")
    identifier = x.get("identifier", "?")
    template = _BLOCKER_LABELS.get(source, "%s %%s" % source)
    return template % identifier


def _blocker_age_text(x: dict) -> str:
    age = x.get("xmin_age_s")
    if is_null(age):
        return "年龄未知（这一路没有时间戳列，算不出挡了多久，但它照样在挡回收）"
    return "已持续 %.0f 秒" % as_float(age)


def _block_note(oldest_xmin: list) -> str:
    labels = "、".join(_blocker_label(x) for x in oldest_xmin)
    return _BLOCK_NOTE_TEMPLATE % labels


def evaluate(table: dict, settings: dict, oldest_xmin: list,
             th: Thresholds) -> list:
    """返回这张表命中的规则码列表。四条规则各自独立判定，互不短路。"""
    hits = []

    reltuples = table.get("reltuples", table.get("n_live_tup", 0))
    trigger = trigger_line(reltuples, settings, table.get("reloptions", ""))
    n_dead = as_float(table.get("n_dead_tup"))

    # R1：死元组过了触发线，且 autovacuum 没跟上（从没服务过，或服务完已经很久）
    over_line = n_dead > trigger
    last_autovac_age = table.get("last_autovacuum_age_s")
    overdue = (is_null(last_autovac_age)
               or as_float(last_autovac_age) > th.autovac_overdue_s)
    if over_line and overdue:
        hits.append("R1")

    # R2：表级把 autovacuum 整个关掉了 —— 不管死元组堆多少、不看触发线，单独判
    if not _table_autovac_enabled(table):
        hits.append("R2")

    # R3：死元组比例高，且表大到值得管（小表比例再高也没有半夜处理的价值）
    ratio = _dead_ratio(table)
    table_bytes = as_float(table.get("table_bytes"))
    if ratio >= th.dead_ratio_warn and table_bytes >= th.min_table_bytes:
        hits.append("R3")

    # R4：有更老的事务/复制槽挡着回收 —— 只看 oldest_xmin 里有没有条目，
    #     绝不看 xmin_age_s 是不是个可用的数字（复制槽那一路恒为空）。
    #     n_dead > 0 是这张表本身有没有东西可回收的门槛：没有死元组，
    #     「回收被挡住」对这张表就无从谈起。
    if oldest_xmin and n_dead > 0:
        hits.append("R4")

    return hits


def _tname(table: dict) -> str:
    """证据开头写上是哪张表:两张表都命中同一条规则时,文字一模一样看起来像重复(2026-09-23 大盘上撞到)。"""
    return "%s.%s：" % (table.get("schema") or "?", table.get("table") or "?")


def _r1_finding(table: dict, trigger: float, th: Thresholds,
                note: str) -> Finding:
    last_autovac_age = table.get("last_autovacuum_age_s")
    if is_null(last_autovac_age):
        history = "从未被 autovacuum 服务过（last_autovacuum 为 NULL）"
    else:
        history = "距上次 autovacuum 已过 %.0f 秒（超过过期阈值 %.0f 秒）" % (
            as_float(last_autovac_age), th.autovac_overdue_s)
    n_dead = as_float(table.get("n_dead_tup"))
    evidence = _tname(table) + "死元组 %d 已超过触发线 %.0f；%s。" % (int(n_dead), trigger, history)
    if note:
        evidence += note
    return Finding(
        dimension=DIMENSION, code=_CODES["R1"], severity=Severity.WARN,
        metric="死元组数 / 触发线", value="%d" % int(n_dead),
        threshold="%.0f" % trigger, evidence=evidence,
    )


def _r2_finding(table: dict, note: str) -> Finding:
    evidence = (_tname(table) + "reloptions 里 autovacuum_enabled=false，autovacuum 永远不会"
                "处理这张表，不管死元组堆多少、触发线过没过。")
    if note:
        evidence += note
    return Finding(
        dimension=DIMENSION, code=_CODES["R2"], severity=Severity.WARN,
        metric="autovacuum_enabled", value="false", threshold="true",
        evidence=evidence,
    )


def _r3_finding(table: dict, th: Thresholds, note: str) -> Finding:
    ratio = _dead_ratio(table)
    table_bytes = as_float(table.get("table_bytes"))
    sev = Severity.CRITICAL if ratio >= th.dead_ratio_crit else Severity.WARN
    evidence = _tname(table) + ("死元组比例 %.1f%%（活 %d / 死 %d），表大小 %.0f MB —— "
                "比例过警戒线 %.0f%% 且表过门槛 %.0f MB。" % (
                    ratio * 100,
                    int(as_float(table.get("n_live_tup"))),
                    int(as_float(table.get("n_dead_tup"))),
                    table_bytes / (1024 * 1024),
                    th.dead_ratio_warn * 100,
                    th.min_table_bytes / (1024 * 1024)))
    if note:
        evidence += note
    return Finding(
        dimension=DIMENSION, code=_CODES["R3"], severity=sev,
        metric="死元组比例", value="%.1f%%" % (ratio * 100),
        threshold="warn>=%.0f%% / crit>=%.0f%%" % (
            th.dead_ratio_warn * 100, th.dead_ratio_crit * 100),
        evidence=evidence,
    )


def _r4_finding(oldest_xmin: list) -> Finding:
    parts = ["%s：%s" % (_blocker_label(x), _blocker_age_text(x))
             for x in oldest_xmin]
    detail = "；".join(parts)
    evidence = ("存在更老的事务/复制槽挡着回收：%s。这些快照可能还用得到这些"
                "死元组，回收不掉——现在跑 VACUUM 也没用，先处理这些事务/"
                "复制槽。" % detail)
    return Finding(
        dimension=DIMENSION, code=_CODES["R4"], severity=Severity.WARN,
        metric="阻塞回收的事务/复制槽数", value=str(len(oldest_xmin)),
        threshold="0（存在即命中）", evidence=evidence,
    )


def judge_tables(tables: list, settings: dict, oldest_xmin: list,
                  th: Thresholds) -> list:
    """把每张表命中的规则码转成 Finding。R4 命中时给 R1/R2/R3 的 evidence 加一句提示。"""
    out = []
    for table in tables:
        hits = evaluate(table, settings, oldest_xmin, th)
        if not hits:
            continue
        reltuples = table.get("reltuples", table.get("n_live_tup", 0))
        trigger = trigger_line(reltuples, settings, table.get("reloptions", ""))
        note = _block_note(oldest_xmin) if "R4" in hits else ""
        for code in hits:
            if code == "R1":
                out.append(_r1_finding(table, trigger, th, note))
            elif code == "R2":
                out.append(_r2_finding(table, note))
            elif code == "R3":
                out.append(_r3_finding(table, th, note))
            elif code == "R4":
                out.append(_r4_finding(oldest_xmin))
    return out
