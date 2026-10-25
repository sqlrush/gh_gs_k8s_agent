"""凭据代理(2026-10-23):中间件令牌、SM2 私钥、模型 Key 不进模型所在的容器。

模型在 opencode 的容器里能执行命令:令牌与私钥放在那个容器的环境变量里,`env` 一下就看得到;模型 Key 还明文写在
opencode.json 里。挡在前面的只有提示词。所以凭据挪到模型碰不到的地方,由这里在转发时加上:

    技能脚本 ──Unix 套接字──▶ ┐                      ┌─▶ GRMP:加 auth 令牌、Appkey / Timestamp / Signature、本人工号
                             ├── 凭据代理(本模块) ──┤
    opencode ──HTTP /model/*─▶ ┘                      └─▶ 模型服务:加 Authorization: Bearer <模型 Key>

代理在哪:按需模式是同一个 Pod 里的伴生容器 `credproxy`(`podctl serve-credproxy`);固定池化是 Pod 的管家,
每个槽位两个套接字(只挂给那个槽位,同 Pod 的别的槽位连不到):`credproxy.sock` 只转中间件,`model.sock` 只转模型,
槽位的 1 号进程把本槽位的模型端口中继到 `model.sock`。**中间件只走套接字、从不经 TCP**:同 Pod 的槽位共用 localhost,
别的槽位连得上我的模型端口 —— 它经那里只能用模型(全 Pod 同一个模型 Key),借不到我的中间件身份。

**只往自己配置的地方转发**:中间件地址来自本容器的 GRMP_API_HOST / GRMP_API_PORT,模型地址来自 MODEL_BASE_URL,
请求里给的 Host、路径前缀之外的东西一概不认 —— 否则模型能把带着令牌与签名的请求引到它自己的服务器上。
中间件只放行诊断接口那两个路径;工号按绑定的人强制写入(覆盖请求里自带的)。模型直接连代理,能做的也只是
「以本人身份调白名单脚本」「用模型」—— 与它经技能、经 opencode 本来就能做的一样,只是拿不到凭据本身。

只用标准库;签名与工号的口径复用技能里的 common.grmp.signing / userid(与直连时完全一致)。
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import pathlib
import socket
import socketserver
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional, Tuple

# 中间件接口路径 = 部署时配置的前缀(GRMP_PATH_PREFIX,与技能侧 common/grmp/paths.py 同一个变量)+ 这两个固定尾巴
GRMP_PREFIX_ENV = "GRMP_PATH_PREFIX"
GRMP_SUFFIXES = ("/common-operations", "/common-operations/invoke")
MODEL_PREFIX = "/model"
SOCKET_NAME = "credproxy.sock"         # 只转中间件
MODEL_SOCKET_NAME = "model.sock"        # 只转模型(固定池化:槽位的模型中继接这个)
GRMP_SOCKET_ENV = "GRMP_PROXY_SOCKET"   # 技能侧读这个变量走代理(gh_skill common/grmp/proxy.py)
MODEL_PORT = 4099                       # 按需模式:opencode 经 127.0.0.1:4099 访问模型
MAX_BODY = 32 * 1024 * 1024             # 一次对话的请求体(含历史与图片)的上限
GRMP_TIMEOUT = 150                      # 客户端默认 120 秒,这里略长,让客户端先超时、报得清楚
MODEL_CONNECT_TIMEOUT = 15
MODEL_READ_TIMEOUT = 900                # 模型的流式应答可能很久才出第一个字
_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
                  "transfer-encoding", "upgrade", "content-length", "host", "authorization"})

# 直连中间件,不认环境变量里的 HTTP_PROXY:带着令牌与签名的请求只去配置的地址
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 返回 (工号, GRMP 令牌);None = 当前没有人在用(固定池化的槽位不在用),一律拒绝
Binding = Callable[[], Optional[Tuple[str, str]]]


class ConfigError(ValueError):
    pass


def normalize_prefix(raw: Optional[str]) -> str:
    """与技能侧 common.grmp.paths.normalize 同一规则(单测核对两边一致):去空白与末尾 /,空串 = 没配。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if (not text.startswith("/") or any(ch.isspace() for ch in text) or "?" in text or "#" in text
            or "//" in text or "/../" in text + "/"):
        raise ConfigError("%s=%r 不是合法的路径前缀(以 / 开头,只写路径)" % (GRMP_PREFIX_ENV, text))
    return text.rstrip("/")


def _skills_common(env: Mapping[str, str]):
    """复用技能里的签名与工号模块(镜像里装在 opencode 的技能目录下)。"""
    root = env.get("AGENT_SKILLS_DIR") or "/opt/agent/config/opencode/skills"
    if root not in sys.path:
        sys.path.insert(0, root)
    from common.grmp import signing, userid      # noqa: WPS433  技能目录在运行时才知道
    return signing, userid


@dataclass(frozen=True)
class Upstreams:
    grmp_base: str          # 空 = 不提供中间件转发
    model_base: str         # 空 = 不提供模型转发
    model_key: str
    env: Mapping[str, str]  # 签名与工号的旋钮(GRMP_APPKEY、GRMP_SIGN_*、GRMP_SM2_PRIVATE_KEY、GRMP_USER_ID_*)
    grmp_prefix: str = ""   # 中间件接口路径前缀;空 = 没配,中间件请求一律报「没配」

    @property
    def grmp_paths(self) -> frozenset:
        return frozenset(self.grmp_prefix + s for s in GRMP_SUFFIXES) if self.grmp_prefix else frozenset()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Upstreams":
        host = (env.get("GRMP_API_HOST") or "").strip()
        grmp = ""
        if host:
            scheme = (env.get("GRMP_API_SCHEME") or "http").strip().lower()
            if scheme not in ("http", "https"):
                raise ConfigError("GRMP_API_SCHEME=%r 只能是 http / https" % scheme)
            try:
                port = int(env.get("GRMP_API_PORT") or 8080)
            except ValueError:
                raise ConfigError("GRMP_API_PORT=%r 不是端口号" % env.get("GRMP_API_PORT")) from None
            grmp = "%s://%s:%d" % (scheme, host, port)
        model = (env.get("MODEL_BASE_URL") or "").strip().rstrip("/")
        if model and urllib.parse.urlsplit(model).scheme not in ("http", "https"):
            raise ConfigError("MODEL_BASE_URL=%r 要以 http:// 或 https:// 开头" % model)
        key = env.get("MODEL_API_KEY") or ""
        if model and not key:
            raise ConfigError("有 MODEL_BASE_URL 却没有 MODEL_API_KEY(模型 Key 由平台以 Secret model-api 注入代理)")
        return cls(grmp_base=grmp, model_base=model, model_key=key, env=dict(env),
                   grmp_prefix=normalize_prefix(env.get(GRMP_PREFIX_ENV)))


class Proxy:
    def __init__(self, ups: Upstreams, binding: Binding, log: Callable[[str], None] = print):
        self.ups, self.binding, self.log = ups, binding, log
        self._signer = None
        self._sign_error = ""
        if ups.grmp_base:
            signing, self._userid = _skills_common(ups.env)
            try:
                settings = signing.settings_from(ups.env, None, load_secret=_no_secret_file)
                self._signer = signing.RequestSigner(settings) if settings else None
            except Exception as exc:            # noqa: BLE001  签名配错只让中间件那一路报错,不让代理起不来
                self._sign_error = str(exc)
                log("凭据代理:中间件签名配置有误,中间件请求将一律报错:%s" % exc)

    # -- 中间件 ------------------------------------------------------------------------------

    def grmp(self, path: str, body: bytes) -> Tuple[int, bytes]:
        def fail(code: int, msg: str) -> Tuple[int, bytes]:
            return code, json.dumps({"code": str(code), "msg": msg}, ensure_ascii=False).encode("utf-8")
        if not self.ups.grmp_base:
            return fail(503, "凭据代理没有配置中间件地址(GRMP_API_HOST)")
        if not self.ups.grmp_prefix:
            return fail(503, "凭据代理没有配置中间件接口路径前缀(GRMP_PATH_PREFIX,部署时在 agent-config 里填)")
        if path not in self.ups.grmp_paths:
            return fail(404, "凭据代理只转发 GRMP 诊断接口")
        bound = self.binding()
        if bound is None:
            return fail(403, "当前没有在用的人(槽位未绑定),不转发")
        user, token = bound
        if not token:
            return fail(503, "没有拿到本人的中间件令牌(Secret grmp-token-<工号>)")
        if self._sign_error:
            return fail(500, "中间件签名配置有误:%s" % self._sign_error)
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return fail(400, "报文不是 JSON")
        if not isinstance(payload, dict):
            return fail(400, "报文不是 JSON 对象")
        try:
            uid = self._userid.settings_from(dict(self.ups.env, GSDB_USER_ID=user))
        except Exception as exc:                # noqa: BLE001
            return fail(500, "工号配置有误:%s" % exc)
        headers = {"auth": token, "Content-Type": "application/json"}
        if self._signer is not None:
            headers.update(self._signer.headers(path))
        headers = self._userid.apply_headers(uid, headers)          # 覆盖:工号只认绑定的人
        payload = self._userid.apply_payload(uid, payload)
        req = urllib.request.Request(self.ups.grmp_base + path, method="POST", headers=headers,
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        try:
            with _DIRECT.open(req, timeout=GRMP_TIMEOUT) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(64 * 1024)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            return fail(502, "连不上中间件 %s:%s" % (self.ups.grmp_base, exc))

    # -- 模型 ------------------------------------------------------------------------------

    def model_request(self, method: str, sub: str, headers, body: bytes):
        """发到模型服务,返回 http.client 的响应(调用方流式转发并关闭)。sub 是 /model 之后的部分(含查询串)。"""
        u = urllib.parse.urlsplit(self.ups.model_base)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        kw = {"context": ssl.create_default_context()} if u.scheme == "https" else {}
        conn = cls(u.hostname, u.port, timeout=MODEL_CONNECT_TIMEOUT, **kw)
        out = {k: v for k, v in headers if k.lower() not in _HOP}
        out["Authorization"] = "Bearer " + self.ups.model_key
        out["Content-Length"] = str(len(body))
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(MODEL_READ_TIMEOUT)
        conn.request(method, u.path.rstrip("/") + sub, body=body or None, headers=out)
        return conn, conn.getresponse()


def _no_secret_file(_name: str) -> str:
    """容器里不读凭据文件:私钥只来自本容器的环境变量(Secret grmp-sm2)。"""
    from common import credential
    raise credential.CredentialError("容器部署只从环境变量 GRMP_SM2_PRIVATE_KEY 读私钥")


# --- HTTP ------------------------------------------------------------------------------------

def make_handler(proxy: Proxy, grmp: bool = True, model: bool = True):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def address_string(self):
            return "local"

        def _body(self) -> Optional[bytes]:
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                return self._chunked()
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n < 0 or n > MAX_BODY:
                return None
            return self.rfile.read(n) if n else b""

        def _chunked(self) -> Optional[bytes]:
            out = bytearray()
            while True:
                line = self.rfile.readline(1024)
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    return None
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(out)
                if len(out) + size > MAX_BODY:
                    return None
                out += self.rfile.read(size)
                self.rfile.readline(4)

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _model_error(self, code: int, msg: str) -> None:
            # 与 OpenAI 兼容接口的错误形状一致,opencode 能把原因显示给用户
            self._send(code, json.dumps({"error": {"message": msg, "type": "credproxy"}}, ensure_ascii=False)
                       .encode("utf-8"))

        def _dispatch(self) -> None:
            path = self.path
            body = self._body()
            if body is None:
                self.close_connection = True
                return self._send(413, b'{"msg": "request too large"}')
            if model and (path == MODEL_PREFIX or path.startswith(MODEL_PREFIX + "/")):
                return self._model(path[len(MODEL_PREFIX):] or "/", body)
            if grmp and self.command == "POST" and path.split("?", 1)[0].endswith(GRMP_SUFFIXES):
                code, out = proxy.grmp(path.split("?", 1)[0], body)
                return self._send(code, out)
            self._send(404, b'{"msg": "not found"}')

        def _model(self, sub: str, body: bytes) -> None:
            if not proxy.ups.model_base:
                return self._model_error(503, "凭据代理没有配置模型地址(MODEL_BASE_URL)")
            if self.command not in ("GET", "POST"):
                return self._model_error(405, "只转发 GET / POST")
            try:
                conn, resp = proxy.model_request(self.command, sub, self.headers.items(), body)
            except (OSError, http.client.HTTPException) as exc:
                return self._model_error(502, "凭据代理连不上模型服务:%s" % exc)
            try:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in _HOP:
                        self.send_header(k, v)
                if resp.status in (204, 304):
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read1(8192)            # 流式应答:有多少转多少,不攒
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (OSError, http.client.HTTPException):
                self.close_connection = True            # 任一头断了:这条连接不再复用
            finally:
                resp.close()
                conn.close()

        do_GET = do_POST = _dispatch

        def do_PUT(self):
            self._send(405, b'{"msg": "method not allowed"}')

        do_DELETE = do_PATCH = do_HEAD = do_PUT

    return H


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class _TCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve_unix(path: pathlib.Path, proxy: Proxy, mode: int = 0o660, grmp: bool = True, model: bool = False):
    """在 path 上开 Unix 套接字(先删掉旧的),权限 mode(同组可连:固定池化下槽位与管家同组)。
    默认只转中间件;模型套接字另开一个(model=True, grmp=False),两者不混:能被中继到的那个绝不带中间件。"""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    srv = _UnixServer(str(path), make_handler(proxy, grmp=grmp, model=model))
    os.chmod(str(path), mode)
    threading.Thread(target=srv.serve_forever, name="credproxy-unix", daemon=True).start()
    return srv


def serve_tcp(port: int, proxy: Proxy, host: str = "127.0.0.1"):
    srv = _TCPServer((host, port), make_handler(proxy, grmp=False))
    threading.Thread(target=srv.serve_forever, name="credproxy-tcp", daemon=True).start()
    return srv


# --- 中继(固定池化的槽位:本槽位的模型端口 → 管家的套接字) -------------------------------------------

def relay(listen_port: int, target: str, log: Callable[[str], None] = print, host: str = "127.0.0.1"):
    """在 host:listen_port 收 TCP 连接,逐字节转到 Unix 套接字 target。不看内容、不加任何东西(凭据在管家那边加)。
    由槽位的 1 号进程开着:它活着端口就被占着,同 Pod 的别的槽位抢不走。返回监听的套接字。"""
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind((host, listen_port))
    ls.listen(64)

    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def serve() -> None:
        while True:
            try:
                client, _addr = ls.accept()
            except OSError:
                return                              # 监听套接字被关了
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                up.connect(target)
            except OSError as exc:
                log("模型中继:连不上凭据代理 %s(%s)" % (target, exc))
                up.close()
                client.close()
                continue
            for a, b in ((client, up), (up, client)):
                threading.Thread(target=pump, args=(a, b), daemon=True).start()
    threading.Thread(target=serve, name="model-relay", daemon=True).start()
    return ls


# --- 按需模式的伴生容器 ------------------------------------------------------------------------------

def main(env: Mapping[str, str] = os.environ) -> int:
    """`podctl serve-credproxy`:本人的令牌、SM2 私钥、模型 Key 只在这个容器里。"""
    try:
        ups = Upstreams.from_env(env)
    except ConfigError as exc:
        print("凭据代理配置错误:%s" % exc, flush=True)
        return 2
    user, token = (env.get("GSDB_USER_ID") or "").strip(), env.get("GRMP_AUTH_TOKEN") or ""
    proxy = Proxy(ups, lambda: (user, token) if user else None, log=lambda m: print(m, flush=True))
    sock_dir = pathlib.Path(env.get("CREDPROXY_DIR") or "/run/credproxy")
    sock_dir.mkdir(parents=True, exist_ok=True)
    serve_unix(sock_dir / SOCKET_NAME, proxy)
    port = int(env.get("CREDPROXY_MODEL_PORT") or MODEL_PORT)
    if ups.model_base:
        serve_tcp(port, proxy)
    print("凭据代理就绪:工号 %s,中间件 %s,模型 %s(127.0.0.1:%d)"
          % (user or "(未绑定)", "已配置" if ups.grmp_base else "未配置", "已配置" if ups.model_base else "未配置", port),
          flush=True)
    threading.Event().wait()
    return 0
