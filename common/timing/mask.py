"""详细版里的遮蔽:令牌、口令、会话句柄、IP 一律遮掉;SQL 与表名保留(否则看不出模型在做什么)。"""
from __future__ import annotations

import collections
import re

_RULES = [
    (re.compile(r"(--session[ =])([A-Za-z0-9]{6,})"), r"\1«会话句柄»", "会话句柄"),
    (re.compile(r"(?<![\d.])(?:\d{1,3}[.-]){3}\d{1,3}(?!\d)"), "«IP»", "IP"),
    (re.compile(r"\b([A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD))=(\S+)"), r"\1=«已遮»", "令牌 / 口令"),
    (re.compile(r"(?i)\b(token|api[_-]?key|appkey|password|passwd|secret)(\s*[:=]\s*)([^\s,'\"]{6,})"), r"\1\2«已遮»",
     "令牌 / 口令"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9+/=._-]{12,}"), r"\1 «已遮»", "令牌 / 口令"),
]


class Masker:
    def __init__(self) -> None:
        self.count = collections.Counter()

    def __call__(self, text: str, cap: int = 0) -> str:
        text = text or ""
        for rx, rep, label in _RULES:
            text, n = rx.subn(rep, text)
            if n:
                self.count[label] += n
        if cap and len(text) > cap:
            text = text[:cap] + "\n…(后面还有 %d 字,报告只保留前 %d 字)" % (len(text) - cap, cap)
        return text
