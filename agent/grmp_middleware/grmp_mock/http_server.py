"""HTTP 外壳 —— 只负责收发字节，协议逻辑全在 server.App 里。

只绑 127.0.0.1。本组件刻意采用文本替换而非绑定变量（为复现客户行为），
对外暴露等于把一个已知的注入面挂到网上。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple

from .server import App

BIND_HOST = "127.0.0.1"
MAX_BODY_BYTES = 1 << 20  # 1 MiB，足够任何诊断请求；超限直接拒绝而不是截断


def make_handler(app: App, quiet: bool = False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json;charset=UTF-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> Tuple[bytes, bool]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return b"", False
            if length < 0 or length > MAX_BODY_BYTES:
                return b"", False
            return self.rfile.read(length), True

        def _dispatch(self, method: str) -> None:
            body, ok = self._read_body()
            if not ok:
                self._respond(413, {"error": "request body too large or malformed"})
                return
            headers = {k: v for k, v in self.headers.items()}
            try:
                status, payload = app.handle(method, self.path, headers, body)
            except Exception as exc:  # 兜底：绝不把异常变成空响应
                status, payload = 500, {
                    "code": "1",
                    "msg": "grmp-mock 内部错误：%s" % exc,
                }
            self._respond(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def log_message(self, fmt, *args) -> None:
            if not quiet:
                super().log_message(fmt, *args)

    return Handler


def serve(app: App, port: int, quiet: bool = False) -> ThreadingHTTPServer:
    """建好服务器并返回（不自动启动，便于测试里在线程中跑）。"""
    return ThreadingHTTPServer((BIND_HOST, port), make_handler(app, quiet))
