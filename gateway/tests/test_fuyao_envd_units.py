"""envd 客户端(gateway/sandboxexec.py)对着真 HTTP 的替身测协议(2026-11-17)。

按平台实测:Connect 服务端流,信封是「1 字节标志 + 4 字节长度 + JSON」;stdout / stderr 是 base64;
退出码为 0 时 end 事件里没有 exitCode(proto3 省略零值);用户由 Basic 认证头的用户名指定;出错时最后一个信封带结束标志与 error。
"""
import base64
import json
import pathlib
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import sandboxexec as sx  # noqa: E402


def _env(flags, obj):
    data = json.dumps(obj).encode()
    return struct.pack(">BI", flags, len(data)) + data


class Envd:
    def __init__(self):
        self.seen = []
        self.script = []                 # 每次 Start 回的信封序列
        self.status = 200
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(204 if self.path == "/health" else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                flags, ln = struct.unpack(">BI", raw[:5])
                fake.seen.append({"path": self.path, "ctype": self.headers.get("Content-Type"),
                                  "auth": self.headers.get("Authorization"), "msg": json.loads(raw[5:5 + ln])})
                if fake.status != 200:
                    body = b'{"code":"unauthenticated"}'
                    self.send_response(fake.status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = b"".join(fake.script)
                self.send_response(200)
                self.send_header("Content-Type", "application/connect+json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def envd():
    e = Envd()
    yield e, sx.Envd(port=e.port)
    e.close()


def _out(text):
    return base64.b64encode(text.encode()).decode()


def test_a_command_runs_as_the_given_user_with_its_envs_and_output_is_collected(envd):
    fake, cli = envd
    fake.script = [_env(0, {"event": {"start": {"pid": 7}}}),
                   _env(0, {"event": {"data": {"stdout": _out("hello ")}}}),
                   _env(0, {"event": {"data": {"stdout": _out("world\n"), "stderr": _out("warn\n")}}}),
                   _env(0, {"event": {"end": {"exited": True, "status": "exit status 0"}}}),
                   _env(2, {})]
    r = cli.run("127.0.0.1", ["python3", "/opt/agent/podctl.py", "fuyao-start"], user="root",
                envs={"GSDB_USER_ID": "u1001"})
    assert r.ok and r.exit_code == 0 and r.stdout == "hello world\n" and r.stderr == "warn\n"
    s = fake.seen[0]
    assert s["path"] == "/process.Process/Start" and s["ctype"] == "application/connect+json"
    assert s["auth"] == "Basic " + base64.b64encode(b"root:").decode()
    assert s["msg"] == {"process": {"cmd": "python3", "args": ["/opt/agent/podctl.py", "fuyao-start"],
                                    "envs": {"GSDB_USER_ID": "u1001"}}}


def test_a_nonzero_exit_code_is_reported(envd):
    fake, cli = envd
    fake.script = [_env(0, {"event": {"end": {"exitCode": 3, "exited": True}}}), _env(2, {})]
    r = cli.run("127.0.0.1", ["false"])
    assert not r.ok and r.exit_code == 3


@pytest.mark.parametrize("script,status,needle", [
    ([_env(2, {"error": {"code": "internal", "message": "boom"}})], 200, "boom"),
    ([_env(0, {"event": {"start": {"pid": 1}}}), _env(2, {})], 200, "没有给出进程结束事件"),
    ([], 401, "HTTP 401"),
])
def test_protocol_failures_raise(envd, script, status, needle):
    fake, cli = envd
    fake.script, fake.status = script, status
    with pytest.raises(sx.ExecError) as ei:
        cli.run("127.0.0.1", ["true"])
    assert needle in str(ei.value)


def test_health_and_unreachable(envd):
    _fake, cli = envd
    assert cli.healthy("127.0.0.1") is True
    dead = sx.Envd(port=1, connect_timeout_s=1)
    assert dead.healthy("127.0.0.1") is False
    with pytest.raises(sx.ExecError):
        dead.run("127.0.0.1", ["true"], timeout_s=2)


def test_env_from_drops_empty_values():
    assert sx.env_from({"A": "1", "B": "", "C": None, "D": 2}) == {"A": "1", "D": "2"}
