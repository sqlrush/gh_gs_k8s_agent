"""问一道题:拼好提问、新开会话、等答完(多轮题依次追问)、存下全部消息与回答。

每题一个目录 questions/<编号>/:prompt.txt(实际发出的话)、messages.json(会话全部消息,事后可重新评分)、
answer.md(最后一轮回答)、meta.json(状态、耗时、token、模型报错、导入题前后知识库是否变化)。
状态:ok 答完 | question 模型在等用户回答提问(不代答,问题算进回答) | timeout 超时 | env_error 环境问题(重试后仍失败)。
"""
from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import fixtures, kbswap, trajectory
from .opencode import Portal, PortalError

ENV_RETRIES = 2
MODEL_ERROR_HINTS = ("rate", "limit", "429", "overloaded", "timeout", "502", "503", "504", "APIError", "Connection")


@dataclass
class Ctx:
    run_id: str
    run_dir: pathlib.Path
    values: Dict[str, str]
    user: Portal
    admin: Optional[Portal]
    timeout_s: int
    faultlab_dir: pathlib.Path
    generated: pathlib.Path
    kb_nas: pathlib.Path
    kb_pod: str
    clip_sessions: Dict[str, str] = field(default_factory=dict)          # 题号 → 剪藏时导入容器开的会话
    fingerprint: Optional[Callable[[], Dict[str, str]]] = None
    log: Callable[[str], None] = print


def build_prompt(q: Mapping[str, Any], ctx: Ctx) -> tuple:
    text, missing = fixtures.render(str(q["ask"]), ctx.values)
    att = q.get("attach") or {}
    mode = att.get("mode")
    if mode == "paste":
        path = fixtures.resolve_file(str(att["file"]), ctx.faultlab_dir, ctx.generated)
        text += fixtures.paste_block(path, bool(att.get("strip_comments")))
    elif mode == "upload":
        src = ctx.generated / att["generated"] if att.get("generated") else \
            fixtures.resolve_file(str(att["file"]), ctx.faultlab_dir, ctx.generated)
        text += fixtures.place_upload(src, ctx.kb_nas, ctx.kb_pod, "acceptance-%s-%s" % (ctx.run_id, q["id"]))
    followups = []
    for f in q.get("followups") or []:
        t, m = fixtures.render(str(f), ctx.values)
        followups.append(t)
        missing += m
    return text, followups, missing


def _env_trouble(traj: trajectory.Trajectory) -> bool:
    return bool(traj.errors) and not traj.answer and any(
        h.lower() in " ".join(traj.errors).lower() for h in MODEL_ERROR_HINTS)


def _converse(portal: Portal, sid: str, prompts: List[str], timeout_s: int, first_sent: bool) -> tuple:
    status, pending = "ok", []
    for i, p in enumerate(prompts):
        if i > 0 or not first_sent:
            portal.prompt(sid, p)
        st = portal.wait(sid, timeout_s)
        if st == "question":
            pending = portal.questions(sid)
            portal.abort(sid)
            return "question", pending
        if st == "timeout":
            portal.abort(sid)
            return "timeout", pending
    return status, pending


def ask(q: Mapping[str, Any], ctx: Ctx) -> Dict[str, Any]:
    qid = str(q["id"])
    qdir = ctx.run_dir / "questions" / qid
    qdir.mkdir(parents=True, exist_ok=True)
    meta: Dict[str, Any] = {"id": qid, "env": q.get("env"), "phase": q.get("phase")}
    portal = ctx.admin if q.get("env") == "kb-import" else ctx.user
    try:
        prompt, followups, missing = build_prompt(q, ctx)
    except (OSError, fixtures.FixtureError) as e:
        return _save(qdir, dict(meta, status="env_error", error="夹具准备失败:%s" % e))
    if missing:
        return _save(qdir, dict(meta, status="env_error", error="占位符取不到值:%s" % "、".join(missing)))
    (qdir / "prompt.txt").write_text("\n\n---- 追问 ----\n\n".join([prompt] + followups), encoding="utf-8")
    before = ctx.fingerprint() if ctx.fingerprint and q.get("env") == "kb-import" else None
    for attempt in range(1, ENV_RETRIES + 2):
        t0 = time.time()
        try:
            clip_sid = ctx.clip_sessions.get(qid) if attempt == 1 else None
            sid = clip_sid or portal.new_session("验收 %s %s" % (ctx.run_id, qid))
            status, pending = _converse(portal, sid, [prompt] + followups, ctx.timeout_s, bool(clip_sid))
            msgs = portal.messages(sid)
        except PortalError as e:
            meta.update(status="env_error", error=str(e)[:300], attempts=attempt)
            ctx.log("  %s 第 %d 次:环境问题 %s" % (qid, attempt, str(e)[:120]))
            time.sleep(60 * attempt)
            continue
        traj = trajectory.parse(msgs, pending)
        (qdir / "messages.json").write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
        (qdir / "answer.md").write_text(traj.answer, encoding="utf-8")
        meta.update(session=sid, status=status, seconds=round(time.time() - t0, 1), attempts=attempt,
                    tokens=traj.tokens, model_s=traj.model_s, tool_s=traj.tool_s, errors=traj.errors,
                    tools=len(traj.tools), chars=len(traj.answer))
        if _env_trouble(traj) and attempt <= ENV_RETRIES:
            meta["status"] = "env_error"
            ctx.log("  %s 第 %d 次:模型服务报错 %s,重试" % (qid, attempt, traj.errors[-1][:100]))
            time.sleep(60 * attempt)
            continue
        if _env_trouble(traj):
            meta["status"] = "env_error"
        break
    if before is not None:
        diff = kbswap.changed(before, ctx.fingerprint())
        meta["kb_changed"] = diff[:100]
        meta["kb_unchanged"] = not kbswap.knowledge_changed(diff)
    ctx.log("  %s %s %.0fs %d 字 工具 %d 次" % (qid, meta.get("status"), meta.get("seconds") or 0,
                                           meta.get("chars") or 0, meta.get("tools") or 0))
    return _save(qdir, meta)


def _save(qdir: pathlib.Path, meta: Dict[str, Any]) -> Dict[str, Any]:
    (qdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta
