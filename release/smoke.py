"""冒烟验证:经网关走用户的真实路径(设计 §7)。

1. 模拟登录(GET /session):不带信任头必须 403;带上(X-Agent-User + X-Agent-Trust)200。槽位刚在准备时网关回 503「正在为您准备环境」,带退避重试。
2. 一次真实对话(经模型):默认一句不碰数据库的问题;给了冒烟实例时,问对它做一次只读健康检查(全链路:模型、技能、白名单、中间件),
   并核对 /reports/health/targets.json 里该实例的份数增加。
3. 知识库检索:请它查一次知识库,核对 /reports/kb/queries.jsonl 多了一行。
信任凭据由调用方从 Secret gateway-auth 现读传入,只在内存里。
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

ASK = "这是发布后的冒烟验证。不要调用任何工具、不要连接数据库。请只回复四个字:冒烟通过。"
# 冒烟要验的是链路,不是模型怎么规划:明说按别名登录、不要反问(11-03 演练:同一句话模型一次按别名登录跑通,一次反问要 IP)
HEALTH = ("对实例 {inst} 做一次只读健康检查,只列最严重的 3 个问题。实例别名已配置,用 gaussdb-login 按别名 {inst} 登录;"
          "这是自动化的发布验证,不要向我提问,遇到问题直接说明原因。")
KB = "用知识库查一下「死锁」相关的条目,只列标题。不要连接数据库。"


class SmokeError(Exception):
    pass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Smoke:
    host: str
    port: int
    user: str
    trust: str
    user_header: str = "X-Agent-User"
    instance: str = ""
    chat_timeout_s: float = 240
    ready_timeout_s: float = 180
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = lambda _m: None
    results: List[Check] = field(default_factory=list)

    # -- 传输 -------------------------------------------------------------------------------------

    def _req(self, method: str, path: str, body: Optional[dict] = None, trust: bool = True,
             timeout: float = 60) -> Tuple[int, bytes]:
        h = {self.user_header: self.user, "Accept": "application/json"}
        if trust:
            h["X-Agent-Trust"] = self.trust
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            h["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def _ready(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 60) -> Tuple[int, bytes]:
        """网关在准备槽位时回 503 / 504:等一会儿再试。"""
        end, wait = self.clock() + self.ready_timeout_s, 2.0
        while True:
            code, data = self._req(method, path, body, timeout=timeout)
            if code not in (502, 503, 504) or self.clock() >= end:
                return code, data
            self.sleep(wait)
            wait = min(wait * 1.5, 10)

    def _record(self, name: str, ok: bool, detail: str, t0: float) -> Check:
        c = Check(name, ok, detail, round(self.clock() - t0, 1))
        self.results.append(c)
        self.log("%s %s:%s" % ("✓" if ok else "✕", name, detail))
        return c

    # -- 各项 -------------------------------------------------------------------------------------

    def login(self) -> Check:
        t0 = self.clock()
        # 用 GET /session(列会话)而不是 /global/health:后者是网关的「非交互」路径(标签页探活不算在用),
        # 用户没有在用的槽位时网关不会为它分配,只回「有操作时自动恢复」(2026-11-03 真环境冒烟踩到)
        bare, _ = self._req("GET", "/session", trust=False)
        if bare != 403:
            return self._record("模拟登录", False, "不带信任头应当被拒(403),实际 %d:网关没有在验证身份" % bare, t0)
        code, data = self._ready("GET", "/session")
        ok = code == 200
        return self._record("模拟登录", ok, "%s → %d%s" % (self.user, code, "" if ok else ":" + data[:120].decode("utf-8", "replace")), t0)

    def chat(self, text: str, title: str) -> Tuple[str, List[str], str]:
        code, data = self._ready("POST", "/session", {"title": title})
        if code != 200:
            raise SmokeError("建会话失败(%d):%s" % (code, data[:200].decode("utf-8", "replace")))
        sid = json.loads(data)["id"]
        try:
            code, data = self._req("POST", "/session/%s/message" % urllib.parse.quote(sid),
                                   {"parts": [{"type": "text", "text": text}]}, timeout=self.chat_timeout_s)
        except OSError:                             # 超时:看看模型是不是在等人回答,然后中止这个会话
            raise SmokeError(self._why_stuck(sid))
        if code != 200:
            raise SmokeError("对话失败(%d):%s" % (code, data[:200].decode("utf-8", "replace")))
        doc = json.loads(data or b"{}")
        parts = doc.get("parts") or []
        reply = " ".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        tools = [str(p.get("tool")) for p in parts if p.get("type") == "tool"]
        err = (doc.get("info") or {}).get("error")
        return reply, tools, json.dumps(err, ensure_ascii=False)[:200] if err else ""

    def _why_stuck(self, sid: str) -> str:
        q = urllib.parse.quote(sid)
        why = "%d 秒没有回答" % self.chat_timeout_s
        try:
            code, data = self._req("GET", "/session/%s/message" % q, timeout=30)
            parts = [p for m in json.loads(data or b"[]") for p in m.get("parts") or []] if code == 200 else []
            waiting = [p for p in parts if p.get("type") == "tool" and p.get("tool") == "question"
                       and (p.get("state") or {}).get("status") == "running"]
            if waiting:
                why = ("模型在向用户提问、等人回答(常见原因:实例 %s 的别名没配、冒烟工号连不上这个实例);已中止该会话" % (self.instance or "?"))
            self._req("POST", "/session/%s/abort" % q, {}, timeout=30)
        except (OSError, ValueError):
            pass
        return why

    def conversation(self, version: str) -> Check:
        t0 = self.clock()
        try:
            reply, _tools, err = self.chat(ASK, "发版台冒烟 %s" % version)
        except (SmokeError, OSError, ValueError) as exc:
            return self._record("真实对话", False, str(exc), t0)
        ok = bool(reply) and not err
        return self._record("真实对话", ok, ("回答:%s" % reply[:60]) if ok else ("没有回答" + (",错误 " + err if err else "")), t0)

    def _count(self, path: str, key: Optional[str] = None) -> int:
        code, data = self._req("GET", path)
        if code == 404:
            return 0
        if code != 200:
            raise SmokeError("读 %s 失败(%d)" % (path, code))
        if path.endswith(".jsonl"):
            return sum(1 for line in data.splitlines() if line.strip())
        items = json.loads(data or b"[]")
        return sum(int(i.get("count") or 0) for i in items if key is None or i.get("label") == key or i.get("key") == key)

    def health_report(self, version: str) -> Optional[Check]:
        if not self.instance:
            return None
        t0 = self.clock()
        try:
            before = self._count("/reports/health/targets.json")
            reply, tools, err = self.chat(HEALTH.format(inst=self.instance), "发版台冒烟 %s 健康检查" % version)
            after = self._count("/reports/health/targets.json")
        except (SmokeError, OSError, ValueError) as exc:
            return self._record("只读诊断报告", False, str(exc), t0)
        ok = after > before and not err
        return self._record("只读诊断报告", ok, "报告 %d → %d 份%s" % (before, after, (",错误 " + err) if err else ""), t0)

    def kb_query(self, version: str) -> Check:
        t0 = self.clock()
        try:
            before = self._count("/reports/kb/queries.jsonl")
            _reply, _tools, err = self.chat(KB, "发版台冒烟 %s 知识库" % version)
            after = self._count("/reports/kb/queries.jsonl")
        except (SmokeError, OSError, ValueError) as exc:
            return self._record("知识库检索", False, str(exc), t0)
        ok = after > before and not err
        return self._record("知识库检索", ok, "检索记录 %d → %d 行" % (before, after), t0)

    def run(self, version: str) -> bool:
        if not self.login().ok:
            return False
        steps = [self.conversation(version), self.health_report(version), self.kb_query(version)]
        return all(c.ok for c in steps if c is not None)
