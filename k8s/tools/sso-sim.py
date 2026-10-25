#!/usr/bin/env python3
"""「模拟 SSO」:给每个请求加上工号头与信任凭据,再转给网关。没接 SSO 之前在现场测试用。

生产环境里是贵方 SSO / 反向代理认完人之后往请求里注入工号头与 `X-Agent-Trust`;
还没接 SSO 时浏览器加不了这些头,大盘与对话页一律 403。这个代理就站在 SSO 的位置上:
浏览器 → 本代理(加头)→ 网关(经 kubectl port-forward)。操作见 docs/现场测试-模拟SSO.md。

只监听 127.0.0.1(只有本机浏览器能用),只用 Python 标准库(3.8+)。流式响应(SSE,对话页靠它)逐块转发。
**只用于测试**:谁能连上监听端口,谁就是那个工号。用完即停。

用法: sso-sim.py <监听端口> <网关端口> <工号> <信任凭据文件> [角色,逗号分隔]
      角色给 kb-admin 时网关会另建知识库导入容器(取决于 gateway-config 的 KB_ADMIN_ROLES)。
      头名与 gateway-config 的 USER_HEADER / ROLES_HEADER 不同时,用环境变量
      SSO_USER_HEADER / SSO_ROLES_HEADER 指定成一样的。
      浏览器打开 http://127.0.0.1:<监听端口>/dash/health
"""
import http.client
import http.server
import os
import pathlib
import socketserver
import sys

HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
       "proxy-authorization", "proxy-authenticate"}
TRUST_HEADER = "X-Agent-Trust"          # 网关固定认这个名字,不可配
USER_HEADER = os.environ.get("SSO_USER_HEADER") or "X-Agent-User"
ROLES_HEADER = os.environ.get("SSO_ROLES_HEADER") or "X-Agent-Roles"
# 浏览器自己带的同名头一律丢掉:模拟的是「SSO 覆盖掉客户端传来的身份头」,不是透传
IDENTITY = {h.lower() for h in (USER_HEADER, TRUST_HEADER, ROLES_HEADER, "x-agent-user", "x-agent-roles")}


def make_handler(target: int, user: str, trust: str, roles: str = ""):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _proxy(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            conn = http.client.HTTPConnection("127.0.0.1", target, timeout=900)
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP and k.lower() not in IDENTITY and k.lower() != "host"}
            headers["Host"] = "127.0.0.1:%d" % target
            headers[USER_HEADER] = user
            headers[TRUST_HEADER] = trust
            if roles:
                headers[ROLES_HEADER] = roles
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
            except OSError as exc:
                msg = ("模拟 SSO 连不上网关 127.0.0.1:%d(%s)。port-forward 还在吗?" % (target, exc)).encode()
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            self.send_response(resp.status, resp.reason)
            stream = "text/event-stream" in (resp.getheader("Content-Type") or "")
            for k, v in resp.getheaders():
                if k.lower() in HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            # 1xx / 204 / 304 与 HEAD 按协议没有消息体:不加分块编码、不写结束块,否则多出的字节会被浏览器
            # 当成同一连接上下一个响应的开头(大盘第二次打开时静态文件全是 304 → 整页空白,2026-10-15)
            if self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200:
                self.end_headers()
                conn.close()
                return
            chunked = stream or resp.getheader("Content-Length") is None
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", resp.getheader("Content-Length"))
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(8192) if stream else resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk) if chunked else chunk)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _proxy

        def log_message(self, *_a):
            pass

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv):
    if len(argv) not in (4, 5):
        print("用法: sso-sim.py <监听端口> <网关端口> <工号> <信任凭据文件> [角色,逗号分隔]", file=sys.stderr)
        return 2
    listen, target, user = int(argv[0]), int(argv[1]), argv[2]
    roles = argv[4].strip() if len(argv) == 5 else ""
    trust = pathlib.Path(argv[3]).read_text(encoding="utf-8").strip()
    if not trust:
        print("信任凭据文件是空的:%s" % argv[3], file=sys.stderr)
        return 2
    print("模拟 SSO:http://127.0.0.1:%d → 网关 127.0.0.1:%d,工号 %s%s(头名 %s)"
          % (listen, target, user, ",角色 %s" % roles if roles else "", USER_HEADER), flush=True)
    Server(("127.0.0.1", listen), make_handler(target, user, trust, roles)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
