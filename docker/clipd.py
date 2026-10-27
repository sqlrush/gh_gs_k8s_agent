"""知识库网页剪藏的接收服务(只在导入容器,端口 4095,2026-10-27)。

管理员在已登录的内网网页上点书签「导入知识库」→ 剪藏页(网关提供)→ 网关按工号转到本人导入容器的这个端口:

    POST /clip   {"kind": "page", "url", "title", "fetched_at", "html",
                  "images": [{"src", "data": "data:image/png;base64,…"}], "skipped": [{"src", "reason"}]}
                 {"kind": "file", "file_name": "xx.mhtml", "data": "<base64>"}     书签用不了时拖入的「网页,单个文件」
    GET  /healthz

落成剪藏批次 <kb>/inbox/clips/<批次>/(meta.json、<批次>.html 或 .mhtml、images/),格式与 agent/common/kb/webclip.py
的约定一致(转 md 在那边);然后在本容器的 opencode 里新开一个会话,发出「导入剪藏 clips/<批次>」,把会话号回给网关。

**只收,不解析正文、不访问任何网址**:HTML 原样存;图片按文件头认类型(不信后缀与声明的类型),SVG 不收(能带脚本,
模型也看不了);大小、张数都有上限。鉴权与 opencode 同一个口令(Basic opencode:<OPENCODE_SERVER_PASSWORD>,由网关注入)——
网络策略只放行网关到这个端口,口令是第二道。纯标准库。
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import email.parser
import email.policy
import email.utils
import hashlib
import hmac
import http.client
import http.server
import json
import os
import pathlib
import re
import secrets
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

CLIPS = ("inbox", "clips")           # <kb>/inbox/clips,与 webclip.CLIPS 一致
META = "meta.json"
INCOMING = ".incoming"               # 先在这里写全,再整目录改名进 clips/:clip list 永远看不到写了一半的批次
DEFAULT_PORT = 4095
DEFAULT_MAX_MB, MAX_MB_CEILING = 30, 45     # 网关单请求上限 64 MB,图片按 base64 传要多三分之一
MAX_IMAGES = 30
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PENDING = 200                    # 收件目录里的剪藏批次上限:堆到这么多说明没人在处理
FILE_SUFFIXES = (".mhtml", ".mht", ".html", ".htm")
_BATCH_RE = re.compile(r"clip-[0-9A-Za-z-]{6,64}")
_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png", ".png"), (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
                (b"GIF87a", "image/gif", ".gif"), (b"GIF89a", "image/gif", ".gif"), (b"BM", "image/bmp", ".bmp"))


class ClipRejected(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Settings:
    kb: pathlib.Path
    origins: Tuple[str, ...]
    max_bytes: int
    password: str
    user_id: str = ""
    opencode_port: int = 4096
    workspace: str = "/data/state/workspace"


def origin_of(url: str) -> str:
    s = urlsplit(url)
    return ("%s://%s" % (s.scheme, s.netloc)).lower() if s.scheme in ("http", "https") and s.netloc else ""


def allowed_origins(raw: str) -> Tuple[str, ...]:
    """KB_CLIP_ORIGINS(逗号或空白分隔)→ 源列表;与 webclip.allowed_origins 同一规则(测试核对)。"""
    out: List[str] = []
    for item in re.split(r"[,\s]+", raw or ""):
        o = origin_of(item.strip().rstrip("/"))
        if o and o not in out:
            out.append(o)
    return tuple(out)


def max_mb(raw: str) -> int:
    try:
        n = int((raw or "").strip() or DEFAULT_MAX_MB)
    except ValueError:
        return DEFAULT_MAX_MB
    return min(max(n, 1), MAX_MB_CEILING)


def settings_from(env: Mapping[str, str]) -> Settings:
    return Settings(kb=pathlib.Path(env.get("GSDB_KB_DIR") or "/nas/kb"),
                    origins=allowed_origins(env.get("KB_CLIP_ORIGINS", "")),
                    max_bytes=max_mb(env.get("KB_CLIP_MAX_MB", "")) * 1024 * 1024,
                    password=env.get("OPENCODE_SERVER_PASSWORD", ""),
                    user_id=env.get("GSDB_USER_ID", ""),
                    opencode_port=int(env.get("OPENCODE_PORT") or 4096),
                    workspace=env.get("WORKSPACE") or "/data/state/workspace")


def sniff_image(data: bytes) -> Optional[Tuple[str, str]]:
    """按文件头认图片 → (类型, 后缀);认不出(含 SVG)返回 None。"""
    for magic, ctype, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ctype, ext
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _b64(raw: str) -> bytes:
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("base64 解不开") from exc


def _data_url(s: str) -> bytes:
    m = re.fullmatch(r"data:[\w.+/-]*(?:;[\w=.-]+)*;base64,(.*)", s or "", re.S)
    if not m:
        raise ValueError("不是 base64 的 data: 地址")
    return _b64(m.group(1))


def _list(v: object) -> list:
    return v if isinstance(v, list) else []


def _clean(s: object, limit: int) -> str:
    return _CTRL_RE.sub(" ", str(s or "")).strip()[:limit]


def _iso(raw: object, fallback: str) -> str:
    s = _clean(raw, 40)
    try:
        _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s
    except ValueError:
        return fallback


def new_batch_name(now: Optional[_dt.datetime] = None) -> str:
    return "clip-%s-%s" % ((now or _dt.datetime.now()).strftime("%Y%m%d-%H%M%S"), secrets.token_hex(2))


def _root(s: Settings) -> pathlib.Path:
    return s.kb.joinpath(*CLIPS)


def _pending(root: pathlib.Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for p in root.iterdir() if p.is_dir() and _BATCH_RE.fullmatch(p.name))


def _images(items: object, budget: int) -> Tuple[List[Tuple[str, bytes, str, str]], List[Dict[str, str]]]:
    """书签取回的图片 → [(src, 字节, 类型, 后缀)] 与跳过清单。超限、认不出的跳过并写明原因,不整单拒收。"""
    kept: List[Tuple[str, bytes, str, str]] = []
    skipped: List[Dict[str, str]] = []
    seen = set()
    for im in _list(items):
        if not isinstance(im, dict):
            continue
        src = _clean(im.get("src"), 2000)
        if not src or src in seen:
            continue
        seen.add(src)
        why = ""
        try:
            data = _data_url(str(im.get("data") or ""))
        except ValueError as exc:
            data, why = b"", str(exc)
        kind = sniff_image(data) if data else None
        if not why and len(kept) >= MAX_IMAGES:
            why = "超过 %d 张上限" % MAX_IMAGES
        elif not why and len(data) > MAX_IMAGE_BYTES:
            why = "单张超过 %d MB" % (MAX_IMAGE_BYTES // 1024 // 1024)
        elif not why and kind is None:
            why = "不是认得的图片(只收 png / jpg / gif / webp / bmp)"
        elif not why and len(data) > budget:
            why = "总大小超过上限"
        if why:
            skipped.append({"src": src, "reason": why})
            continue
        budget -= len(data)
        kept.append((src, data, kind[0], kind[1]))
    return kept, skipped


def _file_info(name: str, raw: bytes) -> Tuple[str, str, str]:
    """拖入的文件 → (url, 标题, 抓取时间),尽力而为;内容与后缀不符就拒。"""
    head = raw[:65536]
    if name.endswith((".mhtml", ".mht")):
        if not re.search(rb"(?im)^(mime-version:|content-type:\s*multipart/related)", head[:8192]):
            raise ClipRejected(400, "%s 不是「网页,单个文件」格式(.mhtml):请用浏览器「另存为 → 网页,单个文件」重新保存" % name)
        hdr = email.parser.BytesHeaderParser(policy=email.policy.default).parsebytes(head)
        url = str(hdr.get("Snapshot-Content-Location") or "")
        when = ""
        try:
            when = email.utils.parsedate_to_datetime(str(hdr.get("Date") or "")).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            pass
        return _clean(url, 2000), _clean(hdr.get("Subject"), 300), when
    if not head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        raise ClipRejected(400, "%s 不是网页文件" % name)
    m = re.search(rb"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->", head[:4096], re.I)
    return (_clean(m.group(1).decode("ascii", "ignore"), 2000) if m else ""), "", ""


def store(s: Settings, payload: Mapping[str, object], now: Optional[_dt.datetime] = None) -> Dict[str, object]:
    """校验 + 落盘,返回批次摘要。不合格抛 ClipRejected(带 HTTP 状态码与说给用户听的原因)。"""
    if not s.origins:
        raise ClipRejected(403, "网页剪藏未开启:平台要在 agent-config 里配 KB_CLIP_ORIGINS(允许剪藏的内网网站)")
    kind = payload.get("kind")
    if kind not in ("page", "file"):
        raise ClipRejected(400, "kind 只能是 page(书签剪藏)或 file(拖入的 .mhtml / .html)")
    root = _root(s)
    if _pending(root) >= MAX_PENDING:
        raise ClipRejected(429, "收件目录里已有 %d 个剪藏批次:先处理掉(kb.py clip list / clip clean)再剪藏" % MAX_PENDING)
    now = now or _dt.datetime.now().astimezone()
    received = now.isoformat(timespec="seconds")
    batch = new_batch_name(now)
    meta: Dict[str, object] = {"batch": batch, "kind": kind, "user_id": s.user_id, "received_at": received}
    images: List[Tuple[str, bytes, str, str]] = []
    if kind == "page":
        url = _clean(payload.get("url"), 2000)
        origin = origin_of(url)
        if not origin:
            raise ClipRejected(400, "网页地址不是 http(s):%s" % (url or "(空)"))
        if origin not in s.origins:
            raise ClipRejected(403, "来源网站 %s 不在允许剪藏的名单里(KB_CLIP_ORIGINS):请平台把它加进 agent-config" % origin)
        html = payload.get("html")
        if not isinstance(html, str) or not html.lstrip().startswith("<"):
            raise ClipRejected(400, "没有收到网页内容")
        # 快照开头记下原网页地址(浏览器「另存为」的同一种写法):快照作为 L3 证据挂到条目上之后,剪藏批次会被清掉,
        # 只有它还记得这份网页是从哪来的。地址里的 "--" 转义掉,免得提前结束注释
        mark = "<!-- saved from url=(%04d)%s -->\n" % (len(url), url.replace("--", "%2D%2D"))
        raw = mark.encode("utf-8") + html.encode("utf-8")
        if len(raw) > s.max_bytes:
            raise ClipRejected(413, "网页本身超过 %d MB 上限:请把页面拆开分别剪藏" % (s.max_bytes // 1024 // 1024))
        images, skipped = _images(payload.get("images"), s.max_bytes - len(raw))
        client_skipped = [{"src": _clean(x.get("src"), 2000), "reason": _clean(x.get("reason"), 200)}
                          for x in _list(payload.get("skipped"))[:200] if isinstance(x, dict)]
        snapshot = batch + ".html"
        # 书签交来的是 JS 字符串,存成 utf-8;页面里 <meta charset=gbk> 之类的声明已经不作数(webclip 按 encoding 解码)
        meta.update(url=url, title=_clean(payload.get("title"), 300), fetched_at=_iso(payload.get("fetched_at"), received),
                    encoding="utf-8", source="bookmarklet", skipped=client_skipped + skipped)
    else:
        name = pathlib.PurePosixPath(_clean(payload.get("file_name"), 200).replace("\\", "/")).name.lower()
        suffix = pathlib.PurePosixPath(name).suffix
        if suffix not in FILE_SUFFIXES:
            raise ClipRejected(400, "只收 .mhtml / .mht / .html / .htm(浏览器「另存为 → 网页,单个文件」)")
        try:
            raw = _b64(str(payload.get("data") or ""))
        except ValueError:
            raise ClipRejected(400, "文件内容没传全(base64 解不开)")
        if not raw:
            raise ClipRejected(400, "文件是空的")
        if len(raw) > s.max_bytes:
            raise ClipRejected(413, "文件超过 %d MB 上限" % (s.max_bytes // 1024 // 1024))
        url, title, when = _file_info(name, raw)
        snapshot = batch + suffix
        meta.update(url=url, title=title, fetched_at=when or received, file_name=name, source="file", skipped=[])
    meta.update(snapshot=snapshot, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                images=[{"src": src, "file": "img-%03d%s" % (k, ext), "type": ctype, "bytes": len(data)}
                        for k, (src, data, ctype, ext) in enumerate(images, 1)])
    _write(root, batch, snapshot, raw, images, meta)
    return {"batch": batch, "title": meta.get("title") or "", "url": meta.get("url") or "", "bytes": len(raw),
            "images": len(images), "skipped": len(meta.get("skipped") or [])}


def _write(root: pathlib.Path, batch: str, snapshot: str, raw: bytes,
           images: List[Tuple[str, bytes, str, str]], meta: Dict[str, object]) -> None:
    tmp = root / INCOMING / batch
    tmp.mkdir(parents=True)
    try:
        (tmp / snapshot).write_bytes(raw)
        if images:
            (tmp / "images").mkdir()
            for k, (_src, data, _ctype, ext) in enumerate(images, 1):
                (tmp / "images" / ("img-%03d%s" % (k, ext))).write_bytes(data)
        (tmp / META).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.rename(tmp, root / batch)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def prompt_text(summary: Mapping[str, object]) -> str:
    what = "「%s」" % summary["title"] if summary.get("title") else ""
    where = " %s" % summary["url"] if summary.get("url") else ""
    return ("导入剪藏 clips/%s(网页%s%s;按 gaussdb-kb-import 的「网页剪藏」流程处理:先 kb.py clip ingest %s)"
            % (summary["batch"], what, where, summary["batch"]))


def start_import(s: Settings, summary: Mapping[str, object], log: Callable[[str], None] = print) -> Optional[str]:
    """在本容器的 opencode 里新开会话并发出导入指令,返回会话号;失败返回 None(批次已经落盘,用户可以自己说)。"""
    auth = "Basic " + base64.b64encode(("opencode:%s" % s.password).encode("utf-8")).decode("ascii")

    def call(path: str, body: Dict[str, object]) -> Dict[str, object]:
        conn = http.client.HTTPConnection("127.0.0.1", s.opencode_port, timeout=20)
        try:
            conn.request("POST", path, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                         {"Authorization": auth, "Content-Type": "application/json",
                          "x-opencode-directory": s.workspace})
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        if resp.status >= 300:
            raise OSError("opencode %s 返回 %d" % (path, resp.status))
        return json.loads(data) if data.strip() else {}

    title = "导入剪藏:%s" % (summary.get("title") or summary.get("url") or summary["batch"])
    try:
        sid = str(call("/session", {"title": title[:80]}).get("id") or "")
        if not _SESSION_RE.fullmatch(sid):
            raise ValueError("opencode 没回会话号")
        call("/session/%s/prompt_async" % sid, {"parts": [{"type": "text", "text": prompt_text(summary)}]})
        return sid
    except (OSError, ValueError) as exc:
        log("剪藏 %s 已落盘,但没能自动发起导入:%s" % (summary["batch"], exc))
        return None


def make_server(s: Settings, port: int, log: Callable[[str], None] = print,
                importer: Callable[..., Optional[str]] = start_import) -> http.server.ThreadingHTTPServer:
    want = ("Basic " + base64.b64encode(("opencode:%s" % s.password).encode("utf-8")).decode("ascii")).encode("utf-8")
    cap = s.max_bytes * 4 // 3 + 2 * 1024 * 1024        # base64 膨胀 + JSON 外壳

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code: int, obj: Dict[str, object]) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/healthz":
                return self._json(200, {"ok": True, "enabled": bool(s.origins)})
            self._json(404, {"error": "没有这个接口"})

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/clip":
                return self._json(404, {"error": "没有这个接口"})
            if not hmac.compare_digest((self.headers.get("Authorization") or "").encode("utf-8"), want):
                return self._json(401, {"error": "口令不对"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > cap:
                self.close_connection = True
                return self._json(413 if n else 400, {"error": "剪藏内容超过 %d MB 上限" % (s.max_bytes // 1024 // 1024)
                                                      if n else "请求体是空的"})
            try:
                payload = json.loads(self.rfile.read(n))
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "请求体不是 JSON 对象"})
            try:
                summary = store(s, payload)
            except ClipRejected as exc:
                log("剪藏被拒(%d):%s" % (exc.code, exc))
                return self._json(exc.code, {"error": str(exc)})
            except OSError as exc:
                log("剪藏落盘失败:%s" % exc)
                return self._json(500, {"error": "写不进知识库收件目录:%s" % exc.strerror})
            log("剪藏 %s:%s %d 字节,图片 %d 张,跳过 %d" % (summary["batch"], origin_of(str(summary["url"])) or "文件",
                                                     summary["bytes"], summary["images"], summary["skipped"]))
            self._json(200, dict(summary, ok=True, session=importer(s, summary, log)))

        def _not_allowed(self):
            self._json(405, {"error": "只收 POST /clip"})

        do_PUT = do_DELETE = do_PATCH = _not_allowed

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    return srv


def main(port: int = DEFAULT_PORT, env: Mapping[str, str] = os.environ) -> int:
    s = settings_from(env)
    if not s.password:
        print("剪藏接收不启动:没有 OPENCODE_SERVER_PASSWORD", file=sys.stderr)
        return 2
    print("剪藏接收: 端口 %d,允许的网站 %s,上限 %d MB" % (port, "、".join(s.origins) or "(未开启)",
                                                  s.max_bytes // 1024 // 1024))
    make_server(s, port, log=lambda m: print(m, flush=True)).serve_forever()
    return 0
