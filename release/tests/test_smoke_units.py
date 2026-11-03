"""冒烟验证:假网关(校验信任头、槽位准备中先 503、对话后报告与检索记录增加)。"""
from __future__ import annotations

import http.server
import json
import pathlib
import sys
import threading

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release.smoke import Smoke  # noqa: E402

TRUST = "t" * 24


class FakeGateway:
    def __init__(self, warmup=2, check_trust=True, kb_grows=True, chat_error=None, asks=False):
        self.warmup, self.check_trust, self.kb_grows, self.chat_error = warmup, check_trust, kb_grows, chat_error
        self.asks, self.aborted = asks, []
        self.reports, self.kb_lines, self.titles = 0, 1, []
        gw = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _send(self, code, obj=None, raw=None):
                body = raw if raw is not None else json.dumps(obj or {}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _go(self):
                if gw.check_trust and self.headers.get("X-Agent-Trust") != TRUST:
                    return self._send(403, {"error": "forbidden"})
                if gw.warmup > 0:
                    gw.warmup -= 1
                    return self._send(503, {"reason": "waiting"})
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if self.path == "/global/health":
                    return self._send(503, {"reason": "not_active"})     # 非交互路径:不分配槽位
                if self.path == "/session" and self.command == "GET":
                    return self._send(200, [])
                if self.path == "/session":
                    gw.titles.append(body["title"])
                    return self._send(200, {"id": "ses_%d" % len(gw.titles)})
                if self.path.endswith("/abort"):
                    gw.aborted.append(self.path)
                    return self._send(200, True)
                if self.path.endswith("/message") and self.command == "GET":
                    return self._send(200, [{"info": {"role": "assistant"}, "parts": [
                        {"type": "tool", "tool": "question", "state": {"status": "running"}}]}])
                if self.path.endswith("/message"):
                    text = body["parts"][0]["text"]
                    if gw.asks and "健康检查" in text:
                        import time as _t
                        _t.sleep(2)                       # 模型在等人回答:请求不返回
                        return self._send(200, {})
                    if "健康检查" in text:
                        gw.reports += 1
                    if "知识库" in text and gw.kb_grows:
                        gw.kb_lines += 1
                    return self._send(200, {"info": {"error": gw.chat_error}, "parts": [
                        {"type": "tool", "tool": "bash"}, {"type": "text", "text": "冒烟通过"}]})
                if self.path == "/reports/health/targets.json":
                    return self._send(200, [{"key": "og5", "label": "og5", "count": gw.reports}])
                if self.path == "/reports/kb/queries.jsonl":
                    return self._send(200, raw=b"".join(b'{"q":"x"}\n' for _ in range(gw.kb_lines)))
                self._send(404)
            do_GET = do_POST = _go

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def gw():
    g = FakeGateway()
    yield g
    g.close()


def _smoke(g, **kw):
    return Smoke(host="127.0.0.1", port=g.port, user="u9500", trust=TRUST, sleep=lambda _s: None, **kw)


def test_full_smoke_passes_through_warmup(gw):
    s = _smoke(gw, instance="og5")
    assert s.run("agent-v1.4.4")
    assert [c.name for c in s.results] == ["模拟登录", "真实对话", "只读诊断报告", "知识库检索"]
    assert all(c.ok for c in s.results) and "报告 0 → 1 份" in s.results[2].detail
    assert gw.titles[0] == "发版台冒烟 agent-v1.4.4", "冒烟会话标题带版本,留在冒烟工号的历史里可查"


def test_without_an_instance_the_report_check_is_skipped(gw):
    s = _smoke(gw)
    assert s.run("v") and [c.name for c in s.results] == ["模拟登录", "真实对话", "知识库检索"]


def test_a_gateway_that_lets_anyone_in_fails_the_login_check():
    g = FakeGateway(check_trust=False, warmup=0)
    try:
        s = _smoke(g)
        assert not s.run("v") and "网关没有在验证身份" in s.results[0].detail
    finally:
        g.close()


def test_a_model_error_fails_the_conversation():
    g = FakeGateway(warmup=0, chat_error={"name": "APIError", "data": {"message": "401"}})
    try:
        s = _smoke(g)
        assert not s.run("v") and not s.results[1].ok and "APIError" in s.results[1].detail
    finally:
        g.close()


def test_a_kb_query_that_leaves_no_trace_fails():
    g = FakeGateway(warmup=0, kb_grows=False)
    try:
        s = _smoke(g)
        assert not s.run("v") and s.results[-1].name == "知识库检索" and not s.results[-1].ok
    finally:
        g.close()


def test_a_wrong_trust_secret_never_logs_in():
    g = FakeGateway(warmup=0)
    try:
        s = Smoke(host="127.0.0.1", port=g.port, user="u9500", trust="wrong" * 5, sleep=lambda _s: None,
                  ready_timeout_s=0)
        assert not s.login().ok
    finally:
        g.close()


def test_a_model_waiting_for_the_user_is_named_and_the_session_aborted():
    g = FakeGateway(warmup=0, asks=True)
    try:
        s = _smoke(g, instance="og5", chat_timeout_s=1)
        assert not s.run("v")
        c = next(c for c in s.results if c.name == "只读诊断报告")
        assert not c.ok and "等人回答" in c.detail and "og5" in c.detail
        assert g.aborted and g.aborted[0].endswith("/abort")
    finally:
        g.close()
