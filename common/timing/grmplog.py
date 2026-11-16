"""读 GRMP 计时日志(common/grmp/timing.py 写的 logs/grmp-timing.log 与滚动出的 .1),并把每条对到会话里的工具调用。"""
from __future__ import annotations

import json
import pathlib
from typing import List, Optional

from ..grmp import timing

SLACK_MS = 300      # 记录与工具调用起止的容差:两边各取各的时间,差几毫秒到几十毫秒是常态


def default_path() -> pathlib.Path:
    return timing.log_path()


def load(path: Optional[str]) -> List[dict]:
    """按时间排好的记录;每条加上 start / end(毫秒)。文件不在就返回空表(v1.4.14 之前没有这份日志)。"""
    base = pathlib.Path(path).expanduser() if path else default_path()
    out = []
    for f in (base.with_name(base.name + ".1"), base):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
                start = int(float(rec["ts"]) * 1000)
                rec.update(start=start, end=start + int(rec.get("ms") or 0))
            except (ValueError, KeyError, TypeError):
                continue
            out.append(rec)
    return sorted(out, key=lambda r: r["start"])


def _belongs(rec: dict, cmd: str) -> bool:
    """调用方是技能脚本时,工具命令里要出现这个脚本名(并行的几个工具调用据此分开);调用方不是技能脚本就只看时间。"""
    caller = str(rec.get("caller") or "")
    if "/" not in caller:
        return True
    return caller.rsplit("/", 1)[1] in cmd


def assign(records: List[dict], tools: List[dict]) -> None:
    """把记录分给时间窗口能套住它的工具调用(tool:abs_s / abs_e 毫秒、cmd 命令原文);每条记录只分一次。
    结果写进 tool["grmp_raw"]。"""
    taken = set()
    for t in sorted(tools, key=lambda t: t["abs_s"]):
        t["grmp_raw"] = []
        for k, rec in enumerate(records):
            if k in taken or rec["start"] < t["abs_s"] - SLACK_MS:
                continue
            if rec["start"] > t["abs_e"] + SLACK_MS:
                break
            if rec["end"] <= t["abs_e"] + SLACK_MS and _belongs(rec, t["cmd_raw"]):
                t["grmp_raw"].append(rec)
                taken.add(k)
