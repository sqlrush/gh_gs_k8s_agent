"""网页剪藏的网关这一半(gateway/kbclip.py + kbclip_pages.py)。

守的是:只给知识库管理员;名单为空 = 关闭;上传只认剪藏页自己发的(自定义头 + JSON + 同源),名单外网站的内容在网关就拦下、
不进导入容器;转给本人导入容器的 4095 并注入口令;成功后回会话地址并设上导入容器的目标;页面带 nonce CSP、不许被嵌套;
书签与页面脚本语法有效(本机有 node 时检查)。
"""
import dataclasses
import http.client
import json
import pathlib
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import config as gwconfig  # noqa: E402
from gateway import identity as idt     # noqa: E402
from gateway import kbclip, kbclip_pages, pods, server  # noqa: E402
from gateway.tests.test_pods_units import FakeKube      # noqa: E402

SECRET = "s" * 32
ADMIN_H = {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u2001", "X-Agent-Roles": "kb-admin"}
USER_H = {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u3001"}
UP_H = {"Content-Type": "application/json", "X-KB-Clip": "1", "Sec-Fetch-Site": "same-origin"}
PAGE = {"kind": "page", "url": "https://wiki.corp/x", "title": "t", "html": "<html></html>", "images": []}


class _Receiver(BaseHTTPRequestHandler):
    """假的导入容器剪藏接收:记下收到的,按 reply 回。"""
    protocol_version = "HTTP/1.1"
    got: list = []
    reply = (200, {"ok": True, "batch": "clip-20261027-103000-ab12", "session": "ses_abc", "url": "https://wiki.corp/x",
                   "bytes": 13})

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).got.append((self.path, self.headers.get("Authorization"), json.loads(body)))
        code, obj = type(self).reply
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def gw(monkeypatch):
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    _Receiver.got = []
    _Receiver.reply = (200, {"ok": True, "batch": "clip-20261027-103000-ab12", "session": "ses_abc",
                             "url": "https://wiki.corp/x", "bytes": 13})
    cfg = dataclasses.replace(gwconfig.load({"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t", "GATEWAY_TRUST_SECRET": SECRET,
                                             "GATEWAY_KB_CLIP_ORIGINS": "https://wiki.corp"}), port=0)
    mgr = pods.Manager(FakeKube(), namespace="ns", image_tag="t", pull_policy="IfNotPresent", templates=_ROOT / "k8s" / "templates")
    asked, touched, ready = [], [], []
    monkeypatch.setattr("gateway.proxy.Upstream.__init__",
                        lambda self, host, port=4096, connect_timeout=10.0:
                        (asked.append((host, port)), setattr(self, "host", "127.0.0.1"),
                         setattr(self, "port", up.server_address[1]), setattr(self, "connect_timeout", 2.0),
                         setattr(self, "retries", 1)) and None)
    monkeypatch.setattr(pods.Manager, "touch", lambda self, uid, kind=pods.KIND_RUNTIME, **kw: touched.append((uid, kind)))
    app = server.App(cfg, mgr, log=lambda _m: None)
    monkeypatch.setattr(app, "ensure_ready", lambda uid, kb_admin, kind=pods.KIND_RUNTIME: (ready.append((uid, kb_admin, kind)), "pw")[1])
    srv = server.serve(app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.socket.getsockname()[1], app, asked, touched, ready, up
    srv.shutdown(); srv.server_close(); up.shutdown(); up.server_close()


def _call(port, path, headers, method="GET", body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read().decode("utf-8"))
    c.close()
    return out


@pytest.mark.parametrize("path", ["/kb-import/clip", "/kb-import/clip/install", "/kb-import/clip/upload"])
def test_only_kb_admins_get_the_clip_pages(gw, path):
    port = gw[0]
    code, _h, body = _call(port, path, USER_H)
    assert code == 403 and "知识库管理员" in body
    assert _call(port, path, {"X-Agent-User": "u2001", "X-Agent-Roles": "kb-admin"})[0] == 403, "不可信来源"


@pytest.mark.parametrize("path, marker", [("/kb-import/clip/install", "拖到书签栏"), ("/kb-import/clip", "网页剪藏")])
def test_pages_carry_a_nonce_csp_and_cannot_be_framed(gw, path, marker):
    code, h, body = _call(gw[0], path, ADMIN_H)
    assert code == 200 and marker in body and h["Content-Type"].startswith("text/html")
    nonce = re.search(r"script-src 'nonce-([^']+)'", h["Content-Security-Policy"]).group(1)
    assert body.count('<script nonce="%s">' % nonce) == 1 and "unsafe-inline" not in h["Content-Security-Policy"].split("style-src")[0]
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"] and h["X-Frame-Options"] == "DENY"
    cfg = json.loads(re.search(r'<script type="application/json" id="cfg">(.*?)</script>', body).group(1))
    assert cfg["origins"] == ["https://wiki.corp"] and cfg["maxBytes"] == 30 << 20
    assert _call(gw[0], path, ADMIN_H)[1]["Content-Security-Policy"] != h["Content-Security-Policy"], "nonce 每次都要换"


def test_upload_is_forwarded_to_the_own_import_container_with_the_password(gw):
    port, _app, asked, touched, ready, _up = gw
    code, h, body = _call(port, "/kb-import/clip/upload", {**ADMIN_H, **UP_H}, "POST", json.dumps(PAGE))
    assert code == 200, body
    out = json.loads(body)
    assert out["open_url"] == server.LANDING + "/session/ses_abc" and out["batch"] == "clip-20261027-103000-ab12"
    assert "gsa_target=kb-import" in h["Set-Cookie"] and "HttpOnly" in h["Set-Cookie"]
    assert asked == [("kb-import-u2001.ns.svc", kbclip.CLIP_PORT)] and ready == [("u2001", True, pods.KIND_KB_IMPORT)]
    path, auth, payload = _Receiver.got[0]
    assert path == "/clip" and auth.startswith("Basic ") and payload == PAGE
    assert touched == [("u2001", pods.KIND_KB_IMPORT)], "剪藏算导入容器的活动,否则刚剪完它就可能被回收"


@pytest.mark.parametrize("headers, text", [
    ({"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}, "只接受剪藏页"),
    ({"Content-Type": "text/plain", "X-KB-Clip": "1"}, "只接受剪藏页"),
    ({**UP_H, "Sec-Fetch-Site": "cross-site"}, "只接受剪藏页"),
])
def test_upload_from_anywhere_but_the_clip_page_is_refused(gw, headers, text):
    code, _h, body = _call(gw[0], "/kb-import/clip/upload", {**ADMIN_H, **headers}, "POST", json.dumps(PAGE))
    assert code == 403 and text in json.loads(body)["error"] and _Receiver.got == []


def test_content_from_a_site_outside_the_list_never_reaches_the_container(gw):
    code, _h, body = _call(gw[0], "/kb-import/clip/upload", {**ADMIN_H, **UP_H}, "POST",
                           json.dumps(dict(PAGE, url="https://evil.example/x")))
    assert code == 403 and "https://evil.example" in json.loads(body)["error"]
    assert _Receiver.got == [] and gw[4] == [], "名单外的不该为它去起导入容器"
    assert _call(gw[0], "/kb-import/clip/upload", {**ADMIN_H, **UP_H}, "GET")[0] == 405
    assert _call(gw[0], "/kb-import/clip/upload", {**ADMIN_H, **UP_H}, "POST", "[1]")[0] == 400


def test_clipping_off_when_the_list_is_empty(gw):
    port, app = gw[0], gw[1]
    app.cfg = dataclasses.replace(app.cfg, kb_clip_origins=())
    code, _h, body = _call(port, "/kb-import/clip/upload", {**ADMIN_H, **UP_H}, "POST", json.dumps(PAGE))
    assert code == 403 and "KB_CLIP_ORIGINS" in json.loads(body)["error"]
    code, _h, body = _call(port, "/kb-import/clip/install", ADMIN_H)
    assert code == 200 and '"origins": []' in body, "安装页照样打开,页面上说明没开启"


def test_receiver_refusals_and_outages_come_back_in_plain_words(gw):
    port, _app, _a, touched, _r, up = gw
    _Receiver.reply = (413, {"error": "网页本身超过 30 MB 上限:请把页面拆开分别剪藏"})
    code, h, body = _call(port, "/kb-import/clip/upload", {**ADMIN_H, **UP_H}, "POST", json.dumps(PAGE))
    assert code == 413 and "拆开" in json.loads(body)["error"] and "Set-Cookie" not in h and touched == []
    _Receiver.reply = (200, {"ok": True, "batch": "clip-20261027-103000-ab12", "session": None})
    code, _h, body = _call(port, "/kb-import/clip/upload", {**ADMIN_H, **UP_H}, "POST", json.dumps(PAGE))
    assert code == 200 and json.loads(body)["open_url"] == "", "没开成会话:剪藏页改为提示用户自己说"
    up.shutdown(); up.server_close()
    code, _h, body = _call(port, "/kb-import/clip/upload", {**ADMIN_H, **UP_H}, "POST", json.dumps(PAGE))
    assert code == 502 and "剪藏接收服务" in json.loads(body)["error"]


def test_clip_routes_do_not_shadow_the_container_switch(gw):
    code, h, _b = _call(gw[0], "/kb-import/", ADMIN_H)
    assert code == 303 and h["Location"] == server.LANDING


def test_config_validates_the_origin_list_and_size():
    base = {"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t", "GATEWAY_TRUST_SECRET": SECRET}
    assert gwconfig.load(base).kb_clip_origins == () and gwconfig.load(base).kb_clip_max_mb == 30
    got = gwconfig.load(dict(base, GATEWAY_KB_CLIP_ORIGINS="https://Wiki.corp/ http://cf.corp:8090,https://wiki.corp"))
    assert got.kb_clip_origins == ("https://wiki.corp", "http://cf.corp:8090")
    for bad in ("wiki.corp", "https://wiki.corp/display", "ftp://wiki.corp", "https://u:p@wiki.corp"):
        with pytest.raises(gwconfig.ConfigError, match="KB_CLIP_ORIGINS"):
            gwconfig.load(dict(base, GATEWAY_KB_CLIP_ORIGINS=bad))
    with pytest.raises(gwconfig.ConfigError, match="最多 45"):
        gwconfig.load(dict(base, GATEWAY_KB_CLIP_MAX_MB="60"))


def test_page_config_cannot_break_out_of_its_script_tag():
    body = kbclip_pages.clip_page(["https://wiki.corp/</script><script>alert(1)//"], 1, "n").decode("utf-8")
    assert body.count("</script>") == 2 and "\\u003c/script>" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_bookmarklet_and_page_scripts_are_valid_javascript(tmp_path):
    """书签代码一个字符写错,用户点了就是没反应 —— 而单测只看 Python。本机有 node 时把三段脚本都过一遍语法检查。"""
    bm = (kbclip_pages.BOOKMARKLET.replace("__P__", json.dumps("https://agent.corp")).replace("__M__", "30")
          .replace("__ONE__", "5242880").replace("__ALL__", "31457280"))
    scripts = {"bookmarklet.js": bm}
    for name, page in (("install.js", kbclip_pages.install_page), ("clip.js", kbclip_pages.clip_page)):
        html = page(["https://wiki.corp"], 1 << 20, "n").decode("utf-8")
        scripts[name] = re.search(r'<script nonce="n">(.*?)</script>', html, re.S).group(1)
    for name, js in scripts.items():
        f = tmp_path / name
        f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, "%s 语法错误:%s" % (name, r.stderr)
    assert "__" not in bm and "location.origin===P" in bm
