"""经模拟 SSO / 网关访问本人的 opencode:建会话、发问、等答完、取消息。

接口与前端用的是同一套(docs/api/opencode-1.18.27-openapi.json):
  POST /session                       建会话
  POST /session/{id}/prompt_async     发一句,立即返回(答的过程在后台)
  GET  /session/status                忙着的会话 → {type: busy | retry};答完的不在里面
  GET  /question                      模型用提问工具发的问题(等用户选)
  POST /session/{id}/abort            停下
  GET  /session/{id}/message          全部消息(回答、工具调用与输出、token)
导入环境的请求带 Cookie gsa_target=kb-import,网关据此转给本人的导入容器。
网关在环境起来之前回 503(等待页),这里按 Retry-After 退避重试。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

RETRY_STATUS = (502, 503, 504)


class PortalError(Exception):
    """网关 / opencode 一直不可用:这道题记「环境问题」。"""


class Portal:
    def __init__(self, base: str, kb_import: bool = False, attempts: int = 8,
                 sleep: Callable[[float], None] = time.sleep):
        self.base = base.rstrip("/")
        self.headers = {"Cookie": "gsa_target=kb-import"} if kb_import else {}
        self.attempts = attempts
        self.sleep = sleep

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 60) -> Any:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = dict(self.headers, **({"Content-Type": "application/json"} if data is not None else {}))
        last = ""
        for i in range(self.attempts):
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as e:
                last = "HTTP %d %s" % (e.code, e.read(300).decode("utf-8", "replace").strip()[:120])
                if e.code not in RETRY_STATUS:
                    raise PortalError("%s %s:%s" % (method, path, last)) from e
                wait = float(e.headers.get("Retry-After") or 10)
            except (urllib.error.URLError, OSError, ValueError) as e:
                last, wait = str(e), 10.0
            self.sleep(min(60.0, wait * (1 + i / 2)))
        raise PortalError("%s %s 重试 %d 次仍失败:%s" % (method, path, self.attempts, last))

    def new_session(self, title: str) -> str:
        return str(self.call("POST", "/session", {"title": title})["id"])

    def prompt(self, sid: str, text: str) -> None:
        self.call("POST", "/session/%s/prompt_async" % sid, {"parts": [{"type": "text", "text": text}]})

    def busy(self, sid: str) -> bool:
        st = self.call("GET", "/session/status") or {}
        return (st.get(sid) or {}).get("type") in ("busy", "retry")

    def questions(self, sid: str) -> List[Dict[str, Any]]:
        return [q for q in (self.call("GET", "/question") or []) if q.get("sessionID") == sid]

    def abort(self, sid: str) -> None:
        try:
            self.call("POST", "/session/%s/abort" % sid, {})
        except PortalError:
            pass

    def messages(self, sid: str) -> List[Dict[str, Any]]:
        return self.call("GET", "/session/%s/message" % sid, timeout=120) or []

    def wait(self, sid: str, timeout_s: float, poll_s: float = 10.0, clock: Callable[[], float] = time.time) -> str:
        """等这一轮结束:idle 答完了 | question 模型在等用户回答提问 | timeout 超时。"""
        t0 = clock()
        self.sleep(min(poll_s, 5.0))
        while True:
            if self.questions(sid):
                return "question"
            if not self.busy(sid):
                return "idle"
            if clock() - t0 > timeout_s:
                return "timeout"
            self.sleep(poll_s)
