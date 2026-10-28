"""真值说明:把检查点的 truth(lab: / kb: / env: / tool: / fixture: / turn:)翻成给裁判看的一段话。"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Dict, List, Mapping, Optional

import yaml

KB_FIELD_LINES = 4
MAX_ITEM_CHARS = 1500


def _kb_item(kb: pathlib.Path, kid: str) -> str:
    if kid.startswith("PD-") and "-" in kid[3:]:
        code = kid.split("-")[1]
        f = kb / "docsets" / code / "items" / ("%s.md" % kid)
        return f.read_text(encoding="utf-8") if f.is_file() else ""
    if kid.startswith("GS-"):
        for f in sorted((kb / "rules").glob("*.yaml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or []
            rows = data if isinstance(data, list) else (data.get("rules") or data.get("items") or [])
            for r in rows:
                if isinstance(r, dict) and r.get("id") == kid:
                    return yaml.safe_dump(r, allow_unicode=True, sort_keys=False)
        return ""
    for f in kb.rglob("*.md"):
        if "docsets" in f.parts:
            continue
        text = f.read_text(encoding="utf-8")
        if re.search(r"^id:\s*%s\s*$" % re.escape(kid), text, re.M):
            return text
    return ""


def kb_text(kb: Optional[pathlib.Path], ref: str) -> str:
    if ref == "none":
        return "验收知识库里没有这方面的条目(题目设计如此)"
    if ref == "unchanged":
        return "导入题只能答到写库前的确认清单,知识库条目不该有任何变化"
    if kb is None:
        return "知识库条目 %s" % ref
    kid, _, fld = ref.partition(".")
    text = _kb_item(kb, kid)
    if not text:
        return "知识库条目 %s(本机副本里没找到原文)" % kid
    if fld:
        lines = text.splitlines()
        hits = [i for i, l in enumerate(lines) if fld in l]
        if hits:
            i = hits[0]
            return "%s 中「%s」:%s" % (kid, fld, " ".join(lines[i:i + KB_FIELD_LINES]).strip()[:600])
    return "%s 原文:%s" % (kid, re.sub(r"\n{2,}", "\n", text).strip()[:MAX_ITEM_CHARS])


def lab_text(facts: Mapping[str, Any], ref: str) -> str:
    code, _, key = ref.partition(".")
    exp = (facts.get("expected") or {}).get(code) or {}
    parts = ["faultlab 场景 %s:%s" % (code, exp.get("title") or "")]
    if exp.get("expect"):
        parts.append("预期:%s" % json.dumps(exp["expect"], ensure_ascii=False)[:600])
    probe = (facts.get("probes") or {}).get(code)
    if probe:
        parts.append("本轮自证:%s" % probe[:300])
    static = (facts.get("static") or {}).get(code) or {}
    if static:
        parts.append("设计事实:%s" % "、".join("%s=%s" % kv for kv in static.items()))
    pid = (facts.get("pids") or {}).get(code)
    if pid:
        parts.append("根会话 pid=%s" % pid)
    sid = (facts.get("sql_ids") or {}).get(code)
    if sid:
        parts.append("主语句 unique_sql_id=%s" % sid)
    if code in (facts.get("failed_codes") or []):
        parts.append("**本轮这个场景没起来**,按「不适用」对待")
    return ";".join(parts)


def env_text(facts: Mapping[str, Any], key: str) -> str:
    env = facts.get("env") or {}
    if key == "window":
        return "问的时段 %s 到 %s(faultlab 故障窗口)" % (facts.get("start"), facts.get("end"))
    if key == "conn":
        return "取样时:max_connections=%s,真实会话数=%s" % (env.get("max_connections"), env.get("real_sessions"))
    if key == "snaps":
        return "快照 %s 到 %s,对应故障窗口 %s 到 %s" % (facts.get("snap_begin"), facts.get("snap_end"),
                                                   facts.get("start"), facts.get("end"))
    if key == "retention":
        return "保留期设置:WDR 快照 %s 天(wdr_snapshot_retention_days),语句历史 %s(track_stmt_retention_time)," \
               "活动会话采样 %s 天(asp_retention_days);问的时刻是 %s" % (
                   env.get("wdr_snapshot_retention_days"), env.get("track_stmt_retention_time"),
                   env.get("asp_retention_days"), facts.get("old"))
    if key == "instance.version":
        return "实例版本:%s" % env.get("version")
    return ""


def describe(truth: str, facts: Mapping[str, Any], kb: Optional[pathlib.Path], earlier: List[str]) -> str:
    if not truth:
        return ""
    prefix, _, body = truth.partition(":")
    refs = [r.strip() for r in body.split(",") if r.strip()]
    if prefix == "lab":
        return " | ".join(lab_text(facts, r) for r in refs)
    if prefix == "kb":
        return " | ".join(kb_text(kb, r) for r in refs)
    if prefix == "env":
        return env_text(facts, body)
    if prefix == "tool":
        return "以会话里 %s 的输出为准(见「工具调用与输出」);它没被调用就看回答里的数字有没有出处" % body
    if prefix == "fixture":
        f = (facts.get("fixtures") or {}).get(body) or {}
        return "夹具事实:%s" % f.get("text", body)
    if prefix == "turn":
        return "「第 2 条」以第 1 轮回答里的编号为准(第 1 轮回答见前文)" if earlier else ""
    return ""
