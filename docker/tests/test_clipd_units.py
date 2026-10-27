"""网页剪藏接收(docker/clipd.py,导入容器 4095)。

要守的是:只收名单里网站的内容;图片按文件头认、SVG 不收、超限的跳过并写明原因而不是整单拒收;批次要么完整出现、要么不出现;
落下的批次 webclip(agent/common/kb)转得了 —— 两边对格式的约定只在这里被一起验证;口令不对一律 401。
"""
import base64
import email.utils
import json
import pathlib
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))
sys.path.insert(0, str(_ROOT / "agent"))

import clipd  # noqa: E402
from common.kb import webclip  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
URL = "https://wiki.corp/pages/viewpage.action?pageId=42"
BODY = "<p>" + "锁等待超过五分钟时先查 pg_locks 里 granted 为假的行,再沿阻塞链找到根会话,确认业务方同意后再处理。" * 4 + "</p>"
HTML = ('<html><head><meta charset="gbk"><title>锁等待处置手册</title></head><body><nav>首页 | 空间</nav>'
        '<div id="main-content" class="wiki-content"><h1>锁等待处置手册</h1>%s'
        '<p><img src="/download/attachments/42/lock.png" alt="阻塞链截图"></p></div></body></html>' % BODY)


def _data(raw: bytes, ctype: str = "image/png") -> str:
    return "data:%s;base64,%s" % (ctype, base64.b64encode(raw).decode())


def _s(tmp_path, origins="https://wiki.corp", mb=30) -> clipd.Settings:
    return clipd.Settings(kb=tmp_path / "kb", origins=clipd.allowed_origins(origins), max_bytes=mb << 20,
                          password="pw", user_id="u2001")


def _page(**kw):
    p = {"kind": "page", "url": URL, "title": "锁等待处置手册 - 运维空间", "fetched_at": "2026-10-27T10:30:00.000Z",
         "html": HTML, "images": [{"src": "https://wiki.corp/download/attachments/42/lock.png", "data": _data(PNG)}],
         "skipped": [{"src": "https://cdn.other/x.png", "reason": "跨站图片,读不到"}]}
    p.update(kw)
    return p


def test_a_page_clip_lands_as_a_complete_batch_that_webclip_converts(tmp_path):
    s = _s(tmp_path)
    out = clipd.store(s, _page())
    batch = webclip.batch_path(s.kb, out["batch"])          # 批次名合 webclip 的规矩,meta 在
    meta = json.loads((batch / "meta.json").read_text(encoding="utf-8"))
    assert meta["user_id"] == "u2001" and meta["url"] == URL and meta["encoding"] == "utf-8"
    assert meta["images"] == [{"src": "https://wiki.corp/download/attachments/42/lock.png", "file": "img-001.png",
                               "type": "image/png", "bytes": len(PNG)}]
    assert meta["skipped"] == [{"src": "https://cdn.other/x.png", "reason": "跨站图片,读不到"}]
    assert (batch / "images" / "img-001.png").read_bytes() == PNG
    snap = (batch / (out["batch"] + ".html")).read_bytes()
    assert snap.startswith(b"<!-- saved from url=(0049)" + URL.encode() + b" -->\n<html>"), "快照要记得自己的来源"
    assert not list((s.kb / "inbox" / "clips" / ".incoming").iterdir()), "临时目录要清空"
    res = webclip.convert(batch, s.kb)
    page = res.page_md.read_text(encoding="utf-8")
    assert "锁等待超过五分钟时先查 pg_locks" in page, "页面声明 gbk 但书签交来的是 utf-8:按声明解码会全是乱码"
    assert "![阻塞链截图](images/img-001.png)" in page and "首页 | 空间" not in page
    assert res.sha256 == meta["sha256"]


def test_images_are_judged_by_their_bytes_and_bad_ones_are_skipped_not_fatal(tmp_path):
    s = _s(tmp_path)
    imgs = [{"src": "https://wiki.corp/a.svg", "data": _data(SVG, "image/png")},       # 声明是 png,实际是 svg
            {"src": "https://wiki.corp/b.jpg", "data": _data(JPG, "image/png")},       # 声明错了也按文件头认
            {"src": "https://wiki.corp/c.png", "data": "data:image/png;base64,@@@"},
            {"src": "https://wiki.corp/b.jpg", "data": _data(JPG)},                    # 重复的只收一次
            {"src": "https://wiki.corp/d.png", "data": _data(b"\x89PNG\r\n\x1a\n" + b"\0" * (clipd.MAX_IMAGE_BYTES + 1))}]
    out = clipd.store(s, _page(images=imgs, skipped=[]))
    meta = json.loads((s.kb / "inbox" / "clips" / out["batch"] / "meta.json").read_text(encoding="utf-8"))
    assert [(m["src"], m["file"], m["type"]) for m in meta["images"]] == [("https://wiki.corp/b.jpg", "img-001.jpg", "image/jpeg")]
    reasons = {x["src"]: x["reason"] for x in meta["skipped"]}
    assert "不是认得的图片" in reasons["https://wiki.corp/a.svg"]
    assert "base64" in reasons["https://wiki.corp/c.png"] and "单张超过" in reasons["https://wiki.corp/d.png"]


def test_image_count_and_total_size_limits_skip_the_rest(tmp_path):
    s = _s(tmp_path, mb=1)
    many = [{"src": "https://wiki.corp/%d.png" % i, "data": _data(PNG)} for i in range(clipd.MAX_IMAGES + 2)]
    out = clipd.store(s, _page(images=many, skipped=[]))
    assert out["images"] == clipd.MAX_IMAGES and out["skipped"] == 2
    big = [{"src": "https://wiki.corp/big%d.png" % i, "data": _data(b"\x89PNG\r\n\x1a\n" + b"\0" * 400_000)} for i in range(4)]
    out = clipd.store(s, _page(images=big, skipped=[]))
    assert out["images"] == 2 and out["skipped"] == 2, "总大小 1 MB:放得下两张,后两张写明「总大小超过上限」"


@pytest.mark.parametrize("payload, code, text", [
    (_page(url="https://evil.example/x"), 403, "不在允许剪藏的名单里"),
    (_page(url="javascript:alert(1)"), 400, r"不是 http\(s\)"),
    (_page(html="就是一段字"), 400, "没有收到网页内容"),
    (_page(html=None), 400, "没有收到网页内容"),
    ({"kind": "url", "url": URL}, 400, "kind 只能是"),
])
def test_bad_page_clips_are_rejected_with_a_reason(tmp_path, payload, code, text):
    with pytest.raises(clipd.ClipRejected, match=text) as exc:
        clipd.store(_s(tmp_path), payload)
    assert exc.value.code == code
    assert webclip.list_batches(tmp_path / "kb") == []


def test_disabled_oversized_and_backlogged_clips_are_refused(tmp_path):
    with pytest.raises(clipd.ClipRejected, match="未开启") as exc:
        clipd.store(_s(tmp_path, origins=""), _page())
    assert exc.value.code == 403
    with pytest.raises(clipd.ClipRejected, match="1 MB 上限") as exc:
        clipd.store(_s(tmp_path, mb=1), _page(html="<html>" + "x" * (1 << 20) + "</html>"))
    assert exc.value.code == 413
    root = tmp_path / "kb" / "inbox" / "clips"
    for i in range(clipd.MAX_PENDING):
        (root / ("clip-20261027-000000-%04d" % i)).mkdir(parents=True)
    with pytest.raises(clipd.ClipRejected, match="clip clean") as exc:
        clipd.store(_s(tmp_path), _page())
    assert exc.value.code == 429


def _mhtml(loc=URL) -> bytes:
    return ("From: <Saved by Blink>\r\nSnapshot-Content-Location: %s\r\nSubject: =?utf-8?B?%s?=\r\nDate: %s\r\n"
            "MIME-Version: 1.0\r\nContent-Type: multipart/related; type=\"text/html\"; boundary=\"B\"\r\n\r\n"
            "--B\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: quoted-printable\r\nContent-Location: %s\r\n\r\n"
            "<html><body><article><h1>t</h1></article></body></html>\r\n--B--\r\n"
            % (loc, base64.b64encode("锁等待处置手册".encode()).decode(), email.utils.format_datetime(
                email.utils.parsedate_to_datetime("Tue, 27 Oct 2026 10:30:00 +0800")), loc)).encode()


def test_a_dropped_mhtml_keeps_its_url_title_and_date(tmp_path):
    s = _s(tmp_path)
    out = clipd.store(s, {"kind": "file", "file_name": "C:\\Users\\a\\锁等待.MHTML", "data": base64.b64encode(_mhtml()).decode()})
    batch = s.kb / "inbox" / "clips" / out["batch"]
    meta = json.loads((batch / "meta.json").read_text(encoding="utf-8"))
    assert meta["file_name"] == "锁等待.mhtml" and meta["url"] == URL and meta["title"] == "锁等待处置手册"
    assert meta["fetched_at"].startswith("2026-10-27T10:30:00") and "encoding" not in meta
    assert (batch / (out["batch"] + ".mhtml")).read_bytes() == _mhtml()


@pytest.mark.parametrize("name, raw, text", [
    ("a.pdf", b"%PDF-1.7", "只收 .mhtml"),
    ("a.mhtml", b"<html>not mime</html>", "不是「网页,单个文件」"),
    ("a.html", b"\x89PNG\r\n", "不是网页文件"),
    ("a.html", b"", "文件是空的"),
])
def test_dropped_files_must_be_what_their_suffix_says(tmp_path, name, raw, text):
    with pytest.raises(clipd.ClipRejected, match=text):
        clipd.store(_s(tmp_path), {"kind": "file", "file_name": name, "data": base64.b64encode(raw).decode()})


def test_a_url_cannot_close_the_source_comment_early(tmp_path):
    out = clipd.store(_s(tmp_path), _page(url="https://wiki.corp/a?x=--><script>alert(1)</script>"))
    snap = (tmp_path / "kb" / "inbox" / "clips" / out["batch"] / (out["batch"] + ".html")).read_text(encoding="utf-8")
    assert snap.split("\n", 1)[0].count("-->") == 1 and "%2D%2D>" in snap.split("\n", 1)[0]


def test_a_saved_html_file_gives_its_url_from_the_saved_from_comment(tmp_path):
    raw = b"<!-- saved from url=(0042)https://wiki.corp/display/OPS/lock -->\n<html><body>x</body></html>"
    out = clipd.store(_s(tmp_path), {"kind": "file", "file_name": "lock.htm", "data": base64.b64encode(raw).decode()})
    assert out["url"] == "https://wiki.corp/display/OPS/lock"


def test_a_failed_write_leaves_no_half_batch(tmp_path, monkeypatch):
    s = _s(tmp_path)
    real = pathlib.Path.write_bytes

    def boom(self, data):
        if self.parent.name == "images":
            raise OSError(28, "No space left on device")
        return real(self, data)
    monkeypatch.setattr(pathlib.Path, "write_bytes", boom)
    with pytest.raises(OSError):
        clipd.store(s, _page())
    root = s.kb / "inbox" / "clips"
    assert [p.name for p in root.iterdir()] == [".incoming"] and not list((root / ".incoming").iterdir())
    assert webclip.list_batches(s.kb) == []


def test_origin_rules_match_webclip_and_settings_clamp():
    raw = "https://Wiki.corp/, http://cf.corp:8090 https://wiki.corp ,,"
    assert list(clipd.allowed_origins(raw)) == webclip.allowed_origins(raw) == ["https://wiki.corp", "http://cf.corp:8090"]
    assert clipd.max_mb("") == 30 and clipd.max_mb("abc") == 30 and clipd.max_mb("0") == 1 and clipd.max_mb("999") == 45
    s = clipd.settings_from({"KB_CLIP_ORIGINS": "https://wiki.corp", "OPENCODE_SERVER_PASSWORD": "pw", "GSDB_KB_DIR": "/nas/kb",
                             "WORKSPACE": "/data/state/workspace"})
    assert s.origins == ("https://wiki.corp",) and s.max_bytes == 30 << 20 and s.kb == pathlib.Path("/nas/kb")


# ---------------------------------------------------------------- HTTP 与自动发起导入

class _FakeOpencode(BaseHTTPRequestHandler):
    seen: list = []
    fail = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, self.headers.get("Authorization"), self.headers.get("x-opencode-directory"), body))
        if type(self).fail:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps({"id": "ses_abc123"}).encode() if self.path == "/session" else b""
        self.send_response(200 if data else 204)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def opencode():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpencode)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _FakeOpencode.seen, _FakeOpencode.fail = [], False
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_start_import_opens_a_session_and_sends_the_import_instruction(tmp_path, opencode):
    import dataclasses
    s = dataclasses.replace(_s(tmp_path), opencode_port=opencode)
    sid = clipd.start_import(s, {"batch": "clip-20261027-103000-ab12", "title": "锁等待处置手册", "url": URL})
    assert sid == "ses_abc123"
    (p1, auth, wd, b1), (p2, _a, _w, b2) = _FakeOpencode.seen
    assert p1 == "/session" and b1["title"] == "导入剪藏:锁等待处置手册"
    assert auth == "Basic " + base64.b64encode(b"opencode:pw").decode() and wd == "/data/state/workspace"
    assert p2 == "/session/ses_abc123/prompt_async"
    text = b2["parts"][0]["text"]
    assert text.startswith("导入剪藏 clips/clip-20261027-103000-ab12") and "kb.py clip ingest clip-20261027-103000-ab12" in text
    _FakeOpencode.fail = True
    logs = []
    assert clipd.start_import(s, {"batch": "clip-20261027-103000-ab12", "title": "", "url": ""}, logs.append) is None
    assert "没能自动发起导入" in logs[0]


@pytest.fixture()
def receiver(tmp_path):
    calls = []
    srv = clipd.make_server(_s(tmp_path, mb=1), 0, log=lambda _m: None,
                            importer=lambda s, summary, log: (calls.append(summary["batch"]), "ses_x")[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1], calls
    srv.shutdown()
    srv.server_close()


def _post(base, path, body, auth="Basic " + base64.b64encode(b"opencode:pw").decode(), method="POST"):
    r = urllib.request.Request(base + path, data=body, method=method,
                               headers={"Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_receiver_needs_the_password_and_answers_with_the_batch_and_session(receiver):
    base, calls = receiver
    body = json.dumps(_page()).encode()
    assert _post(base, "/clip", body, auth="Basic eA==")[0] == 401
    assert _post(base, "/clip", body, method="PUT")[0] == 405
    assert _post(base, "/other", body)[0] == 404
    code, out = _post(base, "/clip", body)
    assert code == 200 and out["ok"] is True and out["session"] == "ses_x" and calls == [out["batch"]]
    assert out["images"] == 1 and out["title"] == "锁等待处置手册 - 运维空间"
    code, out = _post(base, "/clip", json.dumps(_page(url="https://evil.example/")).encode())
    assert code == 403 and "不在允许剪藏的名单里" in out["error"] and len(calls) == 1
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", int(base.rsplit(":", 1)[1]), timeout=10)
    c.putrequest("POST", "/clip")
    c.putheader("Authorization", "Basic " + base64.b64encode(b"opencode:pw").decode())
    c.putheader("Content-Length", str(10 ** 9))
    c.endheaders()
    assert c.getresponse().status == 413, "超过上限的请求体不读就拒"
    c.close()
    assert _post(base, "/clip", b"[1]")[0] == 400
