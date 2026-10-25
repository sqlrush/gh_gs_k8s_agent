#!/usr/bin/env python3
"""本机验证用的小反向代理:给每个请求加上 Basic 认证头,转发到 Pod 的 opencode serve(经 port-forward)。

headless 浏览器不方便带认证头,而 opencode 开了基本认证后连静态资源都要口令。
支持流式响应(SSE)。只在本机用,不进镜像。用法: auth-proxy.py <listen_port> <target_port> <user> <口令文件>
"""
import base64
import http.client
import http.server
import pathlib
import socketserver
import sys

LISTEN, TARGET, USER, PWFILE = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
AUTH = "Basic " + base64.b64encode(("%s:%s" % (USER, pathlib.Path(PWFILE).read_text().strip())).encode()).decode()
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate"}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        conn = http.client.HTTPConnection("127.0.0.1", TARGET, timeout=900)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "host"}
        headers["Authorization"] = AUTH
        headers["Host"] = "127.0.0.1:%d" % TARGET
        conn.request(self.command, self.path, body=body, headers=headers)
        resp = conn.getresponse()
        self.send_response(resp.status, resp.reason)
        chunked = False
        for k, v in resp.getheaders():
            if k.lower() in HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        if resp.getheader("Content-Length") is not None and "text/event-stream" not in (resp.getheader("Content-Type") or ""):
            self.send_header("Content-Length", resp.getheader("Content-Length"))
        else:
            chunked = True
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _proxy

    def log_message(self, *_a):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    Server(("127.0.0.1", LISTEN), Handler).serve_forever()
