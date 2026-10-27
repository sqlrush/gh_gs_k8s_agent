#!/usr/bin/env python3
"""网页剪藏端到端用的「需要登录的内网 Wiki」(只在本机跑,内容全是虚构的)。

    python3 scripts/k8s/mock-wiki.py <端口>          账号 ops / 口令 ops-wiki(只为演示登录这一步)

· 没登录访问任何页面 → 302 到 /login.action;登录后发 HttpOnly 会话 cookie(书签取页面、取图片都靠它)。
· /display/OPS/lock-incident:Confluence 式的事件复盘页 —— 正文在 #main-content.wiki-content,带导航、面包屑、侧栏、
  评论区、页脚(都该被去掉);有数据表格、带语言的代码块、提示框、一张**同源**截图(要登录才取得到)和一张**跨源**图片
  (书签读不到,应写明跳过)。
· /download/attachments/42/lock-chain.png:截图本体,没登录 401。
"""
import http.cookies
import http.server
import secrets
import struct
import sys
import urllib.parse
import zlib

USER, PASSWORD = "ops", "ops-wiki"
SESSIONS = set()

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>批量清算作业超时(锁等待)事件复盘 - 运维空间 - 内部 Wiki</title></head><body>
<div id="header"><nav class="aui-header"><a href="/">内部 Wiki</a> | <a href="/spaces">空间</a> | <a href="/people">人员</a>
<form class="search"><input name="q" placeholder="搜索"></form></nav></div>
<ol id="breadcrumbs"><li>运维空间</li><li>事件复盘</li><li>2026</li></ol>
<div id="sidebar" class="ia-secondary-sidebar"><h3>页面树</h3><ul><li>事件复盘</li><li>值班手册</li><li>变更日历</li></ul></div>
<div id="main" class="aui-page-panel"><div id="main-content" class="wiki-content">
<h1>批量清算作业超时(锁等待)事件复盘</h1>
<table class="confluenceTable"><tr><th>发生时间</th><td>2026-09-18 02:10 ~ 02:55</td></tr>
<tr><th>系统</th><td>批量清算系统(数据库 GaussDB 主备)</td></tr><tr><th>影响</th><td>日终批量延迟 45 分钟完成</td></tr>
<tr><th>级别</th><td>S2</td></tr></table>
<h2>现象</h2>
<p>02:10 起日终清算批量的「账户余额汇总」步骤卡住,作业平台 30 分钟超时告警;数据库 CPU 不高,活跃会话从 40 涨到 180,
大量会话 wait_event 为 transactionid。</p>
<p><img src="/download/attachments/42/lock-chain.png" alt="阻塞链截图"></p>
<h2>排查过程</h2>
<ol><li>查 pg_stat_activity:等待会话都在更新 acct_balance 表;</li>
<li>查 pg_locks 中 granted = f 的行,沿阻塞链找到根会话 pid 4100,状态 idle in transaction,已持续 52 分钟;</li>
<li>根会话来自运维人员手工执行的数据修复脚本,执行完 UPDATE 后忘记提交,终端一直开着。</li></ol>
<div class="code panel pdl"><div class="codeContent panelContent pdl">
<pre class="syntaxhighlighter-pre" data-syntaxhighlighter-params="brush: sql; gutter: false">SELECT l.pid, a.state, now() - a.xact_start AS xact_age, a.query
  FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
 WHERE NOT l.granted;</pre></div></div>
<h2>根因</h2>
<p>手工数据修复会话长时间持有 acct_balance 的行锁未提交(idle in transaction),批量更新同一批账户时全部排队等锁。</p>
<h2>处置</h2>
<ul><li>02:48 与脚本执行人确认后,pg_terminate_backend(4100) 结束根会话,事务回滚;</li>
<li>02:55 批量自动续跑完成;修复脚本改在白天窗口重新执行并当场提交。</li></ul>
<div class="confluence-information-macro confluence-information-macro-warning"><div class="confluence-information-macro-body">
<p>结束会话前必须先确认根会话的业务归属,不能直接批量 kill 等待者。</p></div></div>
<h2>后续改进</h2>
<ul><li>生产库设置 idle_in_transaction_session_timeout = 10min;</li>
<li>手工 DML 一律走变更平台,不在交互终端里直接执行。</li></ul>
<p><img src="http://127.0.0.1:%(other)d/logo.png" alt="外部图床的示意图"></p>
</div></div>
<div id="comments-section" class="comments"><h3>评论(2)</h3><p>张三:学到了,我们组也排查一下。</p><p>李四:+1</p></div>
<div id="likes-and-labels-container" class="like-button">赞 12</div>
<footer id="footer">内部 Wiki · 技术支持请联系 IT 服务台</footer>
</body></html>"""

LOGIN = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>登录 - 内部 Wiki</title></head><body>
<h1>请登录</h1><form method="post" action="/login.action"><input type="hidden" name="next" value="%s">
<p>账号 <input name="user" id="user"></p><p>口令 <input name="password" id="password" type="password"></p>
<p><button id="login" type="submit">登录</button></p></form></body></html>"""


def png(w: int = 240, h: int = 90) -> bytes:
    """一张小 PNG(灰底黑条,示意阻塞链);不依赖 PIL。"""
    rows = b""
    for y in range(h):
        row = bytearray(b"\x00")
        for x in range(w):
            v = 30 if (20 < y < 34 and 10 < x < 200) or (50 < y < 64 and 40 < x < 230) else 235
            row += bytes((v, v, v))
        rows += bytes(row)

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


IMAGE = png()


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _logged_in(self) -> bool:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return False
        return "JSESSIONID" in jar and jar["JSESSIONID"].value in SESSIONS

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/login.action":
            nxt = urllib.parse.parse_qs(self.path.split("?", 1)[1]).get("next", ["/"])[0] if "?" in self.path else "/"
            return self._send(200, (LOGIN % nxt.replace('"', "")).encode("utf-8"))
        if not self._logged_in():
            if path.startswith("/download/"):
                return self._send(401, b"login required", "text/plain")
            return self._send(302, b"", extra=[("Location", "/login.action?next=" + urllib.parse.quote(self.path))])
        if path == "/download/attachments/42/lock-chain.png":
            return self._send(200, IMAGE, "image/png")
        if path in ("/", "/display/OPS/lock-incident"):
            return self._send(200, (PAGE % {"other": self.server.server_address[1] + 1}).encode("utf-8"))
        self._send(404, "<html><body><h1>页面不存在</h1></body></html>".encode("utf-8"))

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/login.action":
            return self._send(405, b"")
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
        if form.get("user", [""])[0] != USER or form.get("password", [""])[0] != PASSWORD:
            return self._send(403, "账号或口令不对".encode("utf-8"), "text/plain; charset=utf-8")
        sid = secrets.token_hex(16)
        SESSIONS.add(sid)
        nxt = form.get("next", ["/"])[0]
        self._send(302, b"", extra=[("Location", nxt if nxt.startswith("/") else "/"),
                                    ("Set-Cookie", "JSESSIONID=%s; Path=/; HttpOnly; SameSite=Lax" % sid)])


class Other(http.server.BaseHTTPRequestHandler):
    """另一个源上的图片:书签不该去取它(跨源)。"""
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(IMAGE)))
        self.end_headers()
        self.wfile.write(IMAGE)


if __name__ == "__main__":
    import threading
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18300
    other = http.server.ThreadingHTTPServer(("127.0.0.1", port + 1), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    print("模拟 Wiki:http://127.0.0.1:%d(账号 %s);跨源图片 http://127.0.0.1:%d" % (port, USER, port + 1), flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
