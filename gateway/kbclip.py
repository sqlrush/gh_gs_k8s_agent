"""网页剪藏的网关这一半(2026-10-27,知识库导入)。

    GET  /kb-import/clip/install   书签安装页:把「导入知识库」拖到书签栏
    GET  /kb-import/clip           剪藏页:书签打开它、postMessage 交来已登录页面的 DOM 与图片;也可拖入 .mhtml
    POST /kb-import/clip/upload    剪藏页同源上传 → 本人导入容器的剪藏接收(4095,docker/clipd.py)
                                   → 落进知识库收件目录,并在导入环境里新开会话发出「导入剪藏」

**只给知识库管理员**(与 /kb-import 同一套身份校验);名单 KB_CLIP_ORIGINS 为空 = 剪藏关闭。
上传只认剪藏页自己发的请求:要带 X-KB-Clip 头且是 JSON —— 跨站页面这样发会先触发预检,网关不答预检,浏览器就不发;
浏览器带了 Sec-Fetch-Site 的,还要是 same-origin。网关不访问任何内网网站,网页内容只经用户的浏览器进来。
"""
from __future__ import annotations

import json
import secrets
from typing import Optional
from urllib.parse import urlsplit

from . import identity as idt
from . import kbclip_pages as pages
from . import pods, proxy
from .diagnose import NotReady

CLIP_PORT = 4095            # 导入容器里的剪藏接收(k8s/templates/kb-import.yaml)
PATHS = frozenset({pages.CLIP_PATH, pages.INSTALL_PATH, pages.UPLOAD_PATH})
# 剪藏完落到导入环境里那个新会话:opencode 网页的会话地址是 /<base64url(工作目录)>/session/<会话号>
# (前半段就是 server.LANDING,测试核对两处一致)
SESSION_URL = "/L2RhdGEvc3RhdGUvd29ya3NwYWNl/session/%s"
TARGET_COOKIE = "gsa_target=kb-import; Path=/; HttpOnly; SameSite=Lax"
OFF = "网页剪藏还没有开启:请平台在 agent-config 里配 KB_CLIP_ORIGINS(允许剪藏的内网网站),重启网关后再用"


def handles(path: str) -> bool:
    return path.rstrip("/") in PATHS


def handle(h, who: idt.Identity, path: str) -> None:
    """h 是 server._Handler;身份已经校验过(代理入口那一层)。"""
    path = path.rstrip("/")
    if not h.app.is_kb_admin(who):
        return h._text(403, "您不是知识库管理员,网页剪藏只对知识库管理员开放。")
    cfg = h.app.cfg
    if path == pages.UPLOAD_PATH:
        return _upload(h, who)
    if h.command not in ("GET", "HEAD"):
        return h._text(405, "这个地址只能打开,不能提交")
    nonce = secrets.token_urlsafe(16)
    render = pages.install_page if path == pages.INSTALL_PATH else pages.clip_page
    body = render(cfg.kb_clip_origins, cfg.kb_clip_max_mb * 1024 * 1024, nonce)
    h.send_response(200)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    for k, v in pages.headers(nonce).items():
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    if h.command == "GET":
        h.wfile.write(body)


def _reject(h, code: int, msg: str) -> None:
    return h._json(code, {"error": msg})


def _upload(h, who: idt.Identity) -> None:
    cfg = h.app.cfg
    if h.command != "POST":
        return _reject(h, 405, "只收 POST")
    if not cfg.kb_clip_origins:
        return _reject(h, 403, OFF)
    site = (h.headers.get("Sec-Fetch-Site") or "same-origin").lower()
    if (h.headers.get("X-KB-Clip") != "1" or site != "same-origin"
            or not (h.headers.get("Content-Type") or "").lower().startswith("application/json")):
        return _reject(h, 403, "只接受剪藏页自己发来的上传")
    if int(h.headers.get("Content-Length") or 0) <= 0:
        return _reject(h, 400, "请求体是空的")
    body = h._read_body()
    if body is None:
        return                                  # _read_body 已经回了 413
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return _reject(h, 400, "请求体不是 JSON 对象")
    if payload.get("kind") == "page":
        origin = _origin(str(payload.get("url") or ""))
        if origin not in cfg.kb_clip_origins:
            return _reject(h, 403, "来源网站 %s 不在允许剪藏的名单里(KB_CLIP_ORIGINS):请平台把它加进 agent-config"
                           % (origin or "(不是 http(s) 地址)"))
    password = _ready(h, who)
    if password is None:
        return
    host = "%s-%s.%s.svc" % (pods.KIND_KB_IMPORT, who.user_id, cfg.namespace)
    headers = [("Content-Type", "application/json"), ("Authorization", proxy.basic_auth(password)),
               ("X-Forwarded-User", who.user_id)]
    try:
        resp = proxy.Upstream(host, CLIP_PORT).send("POST", "/clip", headers, body)
        data = resp.read()
        status = resp.status
        resp.close()
    except OSError as exc:
        h.app.log("剪藏:转给 %s 的导入容器失败:%s" % (who.user_id, exc))
        return _reject(h, 502, "连不上您导入环境里的剪藏接收服务,请稍后再试;一直这样请联系平台(导入镜像要 v1.4.1 及以上,"
                               "升级后导入环境要重建一次)")
    try:
        out = json.loads(data)
        if not isinstance(out, dict):
            raise ValueError
    except ValueError:
        h.app.log("剪藏:%s 的剪藏接收回了 %d,内容不是 JSON" % (who.user_id, status))
        return _reject(h, 502, "导入环境的剪藏接收回的内容看不懂,请联系平台")
    if status != 200:
        h.app.log("剪藏:%s 被导入容器拒收(%d):%s" % (who.user_id, status, out.get("error")))
        return h._json(status if 400 <= status < 600 else 502, {"error": str(out.get("error") or "导入环境拒收")})
    h.app.mgr.touch(who.user_id, kind=pods.KIND_KB_IMPORT)
    sid = str(out.get("session") or "")
    out["open_url"] = (SESSION_URL % sid) if sid else ""
    h.app.log("剪藏:%s → %s(%s,%s 字节)" % (who.user_id, out.get("batch"), _origin(str(out.get("url") or "")) or "文件",
                                         out.get("bytes")))
    raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
    h.send_response(200)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    # 打开那个会话要进导入容器:和访问 /kb-import 一样设上目标
    h.send_header("Set-Cookie", TARGET_COOKIE)
    h.send_header("Content-Length", str(len(raw)))
    h.end_headers()
    h.wfile.write(raw)


def _ready(h, who: idt.Identity) -> Optional[str]:
    """保证本人的导入容器在跑,返回口令;不行就回一个剪藏页看得懂的 JSON 错误并返回 None。"""
    try:
        return h.app.ensure_ready(who.user_id, True, pods.KIND_KB_IMPORT)
    except pods.PrerequisiteMissing as exc:
        h.app.log("剪藏:为 %s 准备导入环境失败(前置条件):%s" % (who.user_id, exc))
        _reject(h, 503, "您的导入环境尚未开通,请联系管理员。")
    except NotReady as exc:
        h.app.log("剪藏:%s 的导入环境未就绪:%s" % (who.user_id, exc.detail))
        why = ("(%s)" % exc.category) if exc.category else ""
        if exc.permanent:
            _reject(h, 503, "导入环境启动失败%s,等待不会好转,请联系管理员处理。" % why)
        else:
            _reject(h, 504, "导入环境还在启动%s,请稍后再点一次「导入」。" % why)
    except Exception as exc:                    # noqa: BLE001
        h.app.log("剪藏:为 %s 准备导入环境失败:%s" % (who.user_id, exc))
        _reject(h, 502, "导入环境准备失败,请联系管理员。")
    return None


def _origin(url: str) -> str:
    s = urlsplit(url)
    return ("%s://%s" % (s.scheme, s.netloc)).lower() if s.scheme in ("http", "https") and s.netloc else ""
