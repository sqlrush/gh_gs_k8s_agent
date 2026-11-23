"""report:把上一次运行的时间线整理成一份能贴进测试记录的文字。

**为什么需要它:** 跑完之后真正要用的信息只有几样 —— 故障窗口的起止时刻(拿去给 WDR 对窗口)、
每个场景的起止时刻(拿去对 skill 的输出)、快照号、哪些场景自证失败。这些散在几十行
时间线里,每次都人工挑一遍太容易挑错。
"""
from __future__ import annotations

import json
import pathlib
from typing import Dict, List, Optional

from . import config as C
from .db import LabError

TIMELINE = C.STATE_DIR / "timeline.jsonl"


def _load(path: Optional[str] = None) -> List[Dict]:
    p = pathlib.Path(path).expanduser() if path else TIMELINE
    if not p.exists():
        raise LabError("找不到时间线 %s —— 先跑一次 run" % p)
    rows: List[Dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue            # 坏行跳过:时间线是边跑边写的,最后一行可能被截断
    if not rows:
        raise LabError("%s 里没有可读的记录" % p)
    return rows


def render(path: Optional[str] = None) -> str:
    rows = _load(path)
    out: List[str] = ["# faultlab 运行报告", ""]

    snaps = [r for r in rows if r["event"] == "WDR 快照"]
    starts = {r["event"][2:]: r for r in rows if r["event"].startswith("起 ")}
    stops = {r["event"][2:]: r for r in rows if r["event"].startswith("停 ")}

    head = rows[0]
    w0 = next((r for r in rows if r["event"] == "故障窗口开始"), None)
    w1 = next((r for r in rows if r["event"] == "故障窗口结束"), None)
    origin = w0["offset_s"] if w0 else 0        # 排期表的 0 秒 = 故障窗口开始那一刻
    out += ["## 这次跑了什么", "- 开始:%s(%s)" % (head["wall"], head.get("detail", "")),
            "- **故障窗口(数据库时间,拿这一段去对 WDR)**:%s → %s" % (
                (w0.get("db_time") or w0["wall"]) if w0 else "?",
                (w1.get("db_time") or w1["wall"]) if w1 else "?"),
            "- WDR 快照:%s" % ", ".join(
                s["detail"].split("snap_id=")[-1] for s in snaps if "snap_id=" in s["detail"]),
            ""]

    out += ["## 各场景的起止(数据库时间,拿这个去对 WDR 窗口与 skill 输出)", "",
            "| 场景 | 起(库时间) | 止(库时间) | 窗口内第几秒 | 做的是什么 |", "|---|---|---|---|---|"]
    for code, r in sorted(starts.items()):
        end = stops.get(code)
        out.append("| %s | %s | %s | +%ds | %s |" % (
            code, r.get("db_time") or r["wall"],
            (end.get("db_time") or end["wall"]) if end else "(到窗口结束)",
            r["offset_s"] - origin, r.get("detail", "")[:60]))
    out.append("")

    probes = [r for r in rows if r["event"].strip() and r["event"].startswith("  ")
              and "自证失败" in str(r.get("detail", ""))]
    if probes:
        out += ["## 自证失败的场景(这些要查:是故障没造出来,还是观测语句有问题)", ""]
        out += ["- %s:%s" % (r["event"].strip(), r["detail"]) for r in probes]
        out.append("")

    errs = [r for r in rows if r["event"] in ("场景起不来", "出错的会话", "WDR 快照失败")]
    if errs:
        out += ["## 出错", ""] + ["- %s %s" % (r["event"], r.get("detail", "")) for r in errs] + [""]

    tail = [r for r in rows if r["event"] in ("清理", "快照号", "死锁", "慢 SQL 完成次数")]
    if tail:
        out += ["## 收尾", ""] + ["- **%s**:%s" % (r["event"], r.get("detail", "")) for r in tail]
    return "\n".join(out)
