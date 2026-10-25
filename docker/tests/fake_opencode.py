#!/usr/bin/env python3
"""测试用的假 opencode:`fake_opencode.py serve --hostname H --port P`。

只做槽位守护进程与管家会碰到的几件事,行为对齐 opencode 1.18.27(10-14 实机核对):
  · Basic 认证(用户名 opencode,口令取 OPENCODE_SERVER_PASSWORD),/global/health 也要;
  · 启动时在 $XDG_DATA_HOME/opencode/opencode.db 建库(WAL 模式)并写一行——像真 opencode 一样一直开着连接;
  · GET /session/status:$FAKE_OUT/busy 存在时报 ses_1 正忙;POST /session/ses_1/abort 记一笔;
  · 收到 SIGTERM 时再写一行(模拟最后一段对话)再退出。
把环境变量与工作目录写到 $FAKE_OUT/env.json,供测试核对守护进程交给它的身份。
"""
import base64
import http.server
import json
import os
import pathlib
import signal
import sqlite3
import sys

port = int(sys.argv[sys.argv.index("--port") + 1])
pw = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
out = pathlib.Path(os.environ["FAKE_OUT"])
out.mkdir(parents=True, exist_ok=True)
data = pathlib.Path(os.environ["XDG_DATA_HOME"]) / "opencode"
data.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(str(data / "opencode.db"), check_same_thread=False)
con.execute("pragma journal_mode=WAL")
con.execute("create table if not exists t (i integer)")
con.execute("insert into t values (1)")
con.commit()
keys = ("GSDB_USER_ID", "OPENCODE_SERVER_PASSWORD", "XDG_DATA_HOME", "XDG_STATE_HOME", "GSDB_HOME",
        "GSDB_REPORTS_DIR", "GSDB_KB_DIR", "SANDBOX_CONTROL_KEY", "GRMP_AUTH_TOKEN", "REPORTS_TOKEN", "HOME",
        "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "PYTHONPATH", "MODEL_API_KEY", "GRMP_SM2_PRIVATE_KEY", "MODEL_PROXY_URL",
        "GRMP_PROXY_SOCKET")
(out / "env.json").write_text(json.dumps(dict({k: os.environ.get(k) for k in keys}, cwd=os.getcwd(),
                                              pid=os.getpid())), encoding="utf-8")
_auth = "Basic " + base64.b64encode(("opencode:%s" % pw).encode()).decode()


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != _auth:
            return self._send(401, {})
        if self.path == "/global/health":
            return self._send(200, {"healthy": True})
        if self.path == "/session/status":
            return self._send(200, {"ses_1": {"type": "busy" if (out / "busy").exists() else "idle"}})
        return self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.headers.get("Authorization") != _auth:
            return self._send(401, {})
        if self.path == "/session/ses_1/abort":
            (out / "aborted").write_text("1", encoding="utf-8")
            return self._send(200, True)
        return self._send(404, {})


def _term(*_a):
    con.execute("insert into t values (2)")      # 最后一段对话:停之前才落库
    con.commit()
    con.close()
    (out / "terminated").write_text("1", encoding="utf-8")
    os._exit(0)


signal.signal(signal.SIGTERM, _term)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
