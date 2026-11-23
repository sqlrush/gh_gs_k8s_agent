"""状态服务(Fuyao 运行模式,2026-11-17):沙箱挂不了 NAS,用户状态与知识库经它进出 NAS。

平台实测:OpenSandbox 的 flux 运行时不往下传 volumes,Flux 的沙箱对象没有卷字段,沙箱只有本地盘
(docs/specs/2026-11-17-fuyao-sandbox-design.md §2)。所以 NAS 只由这里(集群里的一个 Pod,挂 nas PVC)读写。

认人:只认网关签发的沙箱令牌(sbxtoken,控制密钥签名),令牌里的工号与种类决定能碰哪个目录 ——
诊断环境 users/<工号>/,导入环境 admins/<工号>/;知识库发布只认导入环境的令牌。没有登记表,不需要和网关同步状态。

规则沿用按需模式的 statesync(同一套「事故换来的」规则,只是写 NAS 的人换成了这里):
- 同一人同一时刻只有一台沙箱持有本人目录(NAS 上的 `.owner`,格式与 podctl 相同,按需 / 池化模式也认它);
- 载入时若 dirty 标记还在(上次非优雅退出),先把本地化的几个子目录快照到 `.unclean-<时间>/`;
- 回写只接受持有者的;删除只在沙箱载入成功过(包里声明 may_delete)时传播;库是整库一致性快照,写入后清掉旧 -wal / -shm;
- 读写 NAS 一律按文件描述符、逐级不跟随链接(本服务挂着全体用户的目录,不能被谁预埋的链接带到别人那里)。

接口(HTTP,`Authorization: Bearer <沙箱令牌>`):
  POST /v1/lease              {"action": "acquire" | "refresh" | "release"}
  GET  /v1/state              本人状态(tar.gz 流);同时打 dirty 标记
  PUT  /v1/state              增量回写(tar,含 .gsdb-meta/sync.json:keep、may_delete)
  POST /v1/state/finish       收尾:清 dirty 标记;{"release": true} 时同时放开独占
  GET  /v1/kb/manifest        知识库清单 {version, files: {相对路径: [大小, 修改时间纳秒]}}
  POST /v1/kb/files           {"paths": [...]} → 这些文件(tar.gz)
  POST /v1/kb/publish         导入环境发布(请求头 X-Gsdb-Kb-Base 带手上那份的版本;tar 含 .gsdb-meta/publish.json:keep)
  POST /v1/hello              报到:只记本沙箱的来源地址,不碰 NAS(沙箱每 5 秒一次)
  GET  /v1/where              这台沙箱最近一次请求的来源地址与距今秒数(网关恢复沙箱后用它找新地址)

运维统计(资源大盘,2026-11-18;**只认网关的运维签名**,请求头 X-Gsdb-Gateway-Sig,沙箱令牌调不通,见 opsstats.py):
  POST /v1/ops/requests       {"items": [{"u": 工号, "k": 环境, "n": 次数}]}  网关各副本每 30 秒报一次增量
  POST /v1/ops/fleet          {"running": n, "paused": n}                     清扫器每轮报一次沙箱台数
  GET  /v1/ops/stats          今天各工号请求次数、24 小时趋势(半小时一点)、NAS 用量
用法: statesvc.py --nas /nas [--listen 0.0.0.0:8090];控制密钥从环境变量 GSDB_CONTROL_KEY 读。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional, Tuple

import fdtree
import opsstats
import sbxtoken
import statesync
import treetar

SYNC_DIRS = tuple(statesync.LOCALIZED) + ("gdaa", "reports", "backup")
ROOTS = {"runtime": "users", "kb-import": "admins"}
KB = "kb"
KB_VERSION = ".kb-version"
KB_LOCK = ".lock"                       # agent/common/kb/lock.py
KB_DOCSETS = "docsets"                  # 官方文档集,kbstd Job 管;两种模板里都自带一份(随版本走),不经状态服务下发
OWNER, GEN = ".owner", ".gen"           # 与 docker/podctl.py 相同
LEASE_STALE_S = 90
MAX_FILE = 4 << 30
MAX_UPLOAD = 16 << 30
KB_EXCLUDE_RUNTIME = ("inbox", KB_DOCSETS)
KB_EXCLUDE_IMPORT = (KB_DOCSETS,)       # 导入环境首次启动不必拉一遍文档集(实测拉一遍要近两分钟)
HDR_KB_BASE = "X-Gsdb-Kb-Base"


class LimitedReader:
    """只读 Content-Length 那么多,读多了当成 EOF(防止客户端多塞)。"""

    def __init__(self, raw, n: int):
        self.raw, self.left = raw, n

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        data = self.raw.read(n)
        self.left -= len(data)
        return data

    def drain(self, limit: int = 64 << 20) -> bool:
        """提前拒绝时把没读的请求体读掉(客户端整个发完才读应答,不读完就关连接它会报管道断开)。
        剩下的超过 limit 就不读了,返回 False(调用方关连接)。"""
        if self.left > limit:
            return False
        while self.read(1 << 20):
            pass
        return True


def _jsonload(raw: Optional[bytes]) -> dict:
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


class Service:
    def __init__(self, nas: str, key: bytes, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, stale_s: int = LEASE_STALE_S):
        if len(key) < 16:
            raise ValueError("控制密钥至少 16 字节")
        self.nas_fd = fdtree.open_root(nas)
        self.key, self.log, self.clock, self.stale_s = key, log, clock, stale_s
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._kb_lock = threading.Lock()
        self._where: Dict[str, Tuple[str, float]] = {}       # 沙箱 → (最近一次请求的来源地址, 时刻)
        self.ops = opsstats.OpsStats(self.nas_fd, log=log, clock=clock)
        self.replay = sbxtoken.OpsReplay()

    # -- 沙箱现在在哪 ---------------------------------------------------------------------------------
    # 平台恢复暂停的沙箱后虚拟机换了地址,而 /endpoints 仍回旧地址(端到端实测)。沙箱里的后台同步每 5 秒报到一次
    # (/v1/hello,不碰 NAS),每个验签通过的请求都记下来源地址 —— 就是虚拟机现在的 Pod 网段地址(集群内不做源地址转换,实测)。
    # 网关恢复沙箱后只认恢复之后报上来的地址(gateway/fuyaorouter.py address()),绝不退回平台给的旧地址:
    # 旧地址可能已分给别人的沙箱,往那里探活就会送出本人口令(2026-11-18 修)。这张表只在内存里,重启后等下一次报到。

    def saw(self, sandbox_id: str, address: str) -> None:
        with self._guard:
            self._where[sandbox_id] = (address, self.clock())

    def where(self, sandbox_id: str) -> Optional[dict]:
        with self._guard:
            got = self._where.get(sandbox_id)
        if got is None:
            return None
        return {"address": got[0], "age": max(0.0, self.clock() - got[1])}

    # -- 小工具 -------------------------------------------------------------------------------------

    def lock_for(self, k: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(k, threading.Lock())

    def user_fd(self, c: sbxtoken.Claims) -> int:
        return fdtree.open_path(self.nas_fd, "%s/%s" % (ROOTS[c.kind], c.user_id), create=True, mode=0o700)

    def kb_fd(self) -> int:
        return fdtree.open_path(self.nas_fd, KB, create=True, mode=0o755)

    @staticmethod
    def holder(c: sbxtoken.Claims) -> str:
        return "sbx/%s" % c.sandbox_id

    def _owner(self, ufd: int) -> dict:
        try:
            return _jsonload(fdtree.read_small(ufd, OWNER))
        except fdtree.UnsafePath:
            return {}

    def holds(self, ufd: int, c: sbxtoken.Claims) -> bool:
        return self._owner(ufd).get("pod") == self.holder(c)

    # -- 独占 ---------------------------------------------------------------------------------------

    def lease(self, c: sbxtoken.Claims, action: str) -> Tuple[int, dict]:
        me = self.holder(c)
        with self.lock_for("%s/%s" % (c.kind, c.user_id)):
            ufd = self.user_fd(c)
            try:
                cur = self._owner(ufd)
                age = self.clock() - float(cur.get("ts") or 0)
                if action == "acquire":
                    if cur and cur.get("pod") != me and age < self.stale_s:
                        return 409, {"reason": "owner_busy", "holder": cur.get("pod"), "age": int(age)}
                    fdtree.write_small(ufd, OWNER, json.dumps({"pod": me, "ts": self.clock()}).encode())
                    fdtree.write_small(ufd, GEN, secrets.token_hex(16).encode())
                    took = cur.get("pod") if cur and cur.get("pod") != me else None
                    if took:
                        self.log("状态服务:%s 接管 %s 的过期独占(原持有 %s,%d 秒未刷新)" % (me, c.user_id, took, int(age)))
                    return 200, {"holder": me, "took_over": took}
                if action == "refresh":
                    if cur.get("pod") != me:
                        return 409, {"reason": "lost", "holder": cur.get("pod")}
                    fdtree.write_small(ufd, OWNER, json.dumps({"pod": me, "ts": self.clock()}).encode())
                    return 200, {"holder": me}
                if action == "release":
                    if cur.get("pod") == me:
                        fdtree.unlink(ufd, OWNER)
                    return 200, {"released": True}
                return 400, {"error": "action 只能是 acquire / refresh / release"}
            finally:
                os.close(ufd)

    # -- 状态 ---------------------------------------------------------------------------------------

    def prepare_load(self, c: sbxtoken.Claims) -> Tuple[Optional[int], Optional[Tuple[int, dict]]]:
        """载入前:核对持有者、必要时快照、打 dirty 标记。返回 (本人目录描述符, None) 或 (None, 错误应答)。
        描述符交给 write_state 打包并关闭。"""
        with self.lock_for("%s/%s" % (c.kind, c.user_id)):
            ufd = self.user_fd(c)
            try:
                if not self.holds(ufd, c):
                    os.close(ufd)
                    return None, (409, {"reason": "not_owner"})
                self._snapshot_if_dirty(ufd, c)
                fdtree.write_small(ufd, statesync.MARK_DIRTY, str(int(self.clock())).encode())
                return ufd, None
            except BaseException:
                os.close(ufd)
                raise

    def write_state(self, ufd: int, out) -> int:
        n = 0
        try:
            with tarfile.open(fileobj=out, mode="w|gz", compresslevel=1) as tar:
                for d in SYNC_DIRS:
                    dfd = fdtree.open_path(ufd, d, create=False)
                    if dfd is None:
                        continue
                    try:
                        n += len(treetar.pack(tar, dfd, d + "/"))
                    finally:
                        os.close(dfd)
        finally:
            os.close(ufd)
        return n

    def _snapshot_if_dirty(self, ufd: int, c: sbxtoken.Claims) -> None:
        try:
            dirty = fdtree.read_small(ufd, statesync.MARK_DIRTY) is not None
        except fdtree.UnsafePath:
            dirty = True
        if not dirty:
            return
        snap = statesync.SNAPSHOT_PREFIX + "%d" % int(self.clock())
        st = fdtree.Stats()
        for d in statesync.LOCALIZED:
            sfd = fdtree.open_path(ufd, d, create=False)
            if sfd is None:
                continue
            dfd = fdtree.open_path(ufd, "%s/%s" % (snap, d), create=True, mode=0o700)
            try:
                fdtree.copy_tree(sfd, dfd, stats=st, only_if_changed=False)
            finally:
                os.close(sfd)
                os.close(dfd)
        self.log("状态服务:⚠ %s 上次是非优雅退出,NAS 上那份已快照到 %s/(%d 个文件)" % (c.user_id, snap, st.files))

    def sync(self, c: sbxtoken.Claims, body) -> Tuple[int, dict]:
        with self.lock_for("%s/%s" % (c.kind, c.user_id)):
            ufd = self.user_fd(c)
            try:
                if not self.holds(ufd, c):
                    return 409, {"reason": "not_owner"}
                try:
                    res = treetar.unpack(body, ufd, allowed_top=lambda t: t in SYNC_DIRS,
                                         max_file=MAX_FILE, max_total=MAX_UPLOAD)
                except treetar.TarRejected as exc:
                    return 400, {"error": str(exc)}
                meta = res.meta.get("sync.json") or {}
                deleted = 0
                if meta.get("may_delete"):
                    keep = meta.get("keep") or {}
                    for d in SYNC_DIRS:
                        if d not in keep:
                            continue                        # 包里没声明这个目录:不动,宁可多留
                        dfd = fdtree.open_path(ufd, d, create=False)
                        if dfd is None:
                            continue
                        st = fdtree.Stats()
                        try:
                            fdtree.prune(dfd, set(keep[d]), stats=st, protect=statesync._protected)
                        finally:
                            os.close(dfd)
                        deleted += st.deleted
                        res.errors.extend(st.errors)
                return 200, {"written": len(res.written), "bytes": res.bytes, "deleted": deleted,
                             "skipped": res.skipped, "errors": res.errors[:5]}
            finally:
                os.close(ufd)

    def finish(self, c: sbxtoken.Claims, release: bool) -> Tuple[int, dict]:
        with self.lock_for("%s/%s" % (c.kind, c.user_id)):
            ufd = self.user_fd(c)
            try:
                if not self.holds(ufd, c):
                    return 409, {"reason": "not_owner"}
                fdtree.unlink(ufd, statesync.MARK_DIRTY)
                if release:
                    fdtree.unlink(ufd, OWNER)
                return 200, {"finished": True, "released": release}
            finally:
                os.close(ufd)

    # -- 知识库 -------------------------------------------------------------------------------------

    @staticmethod
    def _kb_skip_dir(kind: str) -> Callable[[str], bool]:
        if kind == "runtime":
            return lambda rel: rel in KB_EXCLUDE_RUNTIME
        return lambda rel: rel in KB_EXCLUDE_IMPORT

    @staticmethod
    def _kb_skip(rel: str) -> bool:
        return rel in (KB_LOCK, KB_VERSION)

    def kb_version(self, kfd: int) -> int:
        try:
            raw = fdtree.read_small(kfd, KB_VERSION)
        except fdtree.UnsafePath:
            return 0
        try:
            return int((raw or b"0").strip() or 0)
        except ValueError:
            return 0

    def kb_manifest(self, c: sbxtoken.Claims) -> dict:
        kfd = self.kb_fd()
        try:
            files = treetar.manifest(kfd, skip=self._kb_skip, skip_dir=self._kb_skip_dir(c.kind))
            return {"version": self.kb_version(kfd), "files": files}
        finally:
            os.close(kfd)

    def kb_files(self, c: sbxtoken.Claims, paths, out) -> None:
        wanted = set(p for p in paths if isinstance(p, str))
        kfd = self.kb_fd()
        try:
            with tarfile.open(fileobj=out, mode="w|gz", compresslevel=1) as tar:
                treetar.pack(tar, kfd, "", only=wanted, skip=self._kb_skip, skip_dir=self._kb_skip_dir(c.kind))
        finally:
            os.close(kfd)

    def kb_publish(self, c: sbxtoken.Claims, base: int, body) -> Tuple[int, dict]:
        """base:发布者手上那份的版本(请求头)。**先核对再写**:别的管理员已经先发布了就一个文件都不写。"""
        if c.kind != "kb-import":
            return 403, {"error": "只有知识库导入环境能发布知识库"}
        with self._kb_lock:
            kfd = self.kb_fd()
            try:
                cur = self.kb_version(kfd)
                if base != cur:
                    return 409, {"reason": "stale", "version": cur}
                try:
                    res = treetar.unpack(body, kfd, allowed_top=lambda t: t != KB_DOCSETS and t not in (KB_LOCK, KB_VERSION),
                                         max_file=MAX_FILE, max_total=MAX_UPLOAD, file_mode=0o644, dir_mode=0o755)
                except treetar.TarRejected as exc:
                    return 400, {"error": str(exc)}
                meta = res.meta.get("publish.json") or {}
                keep = set(meta.get("keep") or [])
                st = fdtree.Stats()
                fdtree.prune(kfd, keep | set(res.written), stats=st,
                             protect=lambda rel: rel.startswith(KB_DOCSETS + "/") or rel in (KB_LOCK, KB_VERSION))
                new = cur + 1
                fdtree.write_small(kfd, KB_VERSION, str(new).encode(), mode=0o644)
                self.log("状态服务:%s 发布知识库 v%d(写入 %d、删除 %d)" % (c.user_id, new, len(res.written), st.deleted))
                return 200, {"version": new, "written": len(res.written), "deleted": st.deleted}
            finally:
                os.close(kfd)


def handler(svc: Service):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"          # 流式响应靠关连接结束,不用分块
        server_version = "gsdb-statesvc/1"

        def _json(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- 运维统计:只认网关签名(控制密钥派生的另一把子密钥),沙箱令牌在这里无效 --------------------------

        def _ops_ok(self, body: bytes) -> bool:
            sig = self.headers.get(sbxtoken.OPS_HEADER) or ""
            if sbxtoken.ops_verify(svc.key, sig, self.command, self.path, body) and svc.replay.first(sig):
                return True
            self._json(401, {"error": "只接受网关签名的请求"})
            return False

        def _ops(self) -> None:
            if self.command == "GET" and self.path == "/v1/ops/stats":
                if self._ops_ok(b""):
                    self._json(200, svc.ops.snapshot())
                return
            if self.command == "POST" and self.path in ("/v1/ops/requests", "/v1/ops/fleet"):
                r = self._body(1 << 20)
                if r is None:
                    return
                raw = r.read()
                if not self._ops_ok(raw):
                    return
                d = _jsonload(raw)
                if self.path == "/v1/ops/requests":
                    return self._json(*svc.ops.add_requests(d.get("items")))
                return self._json(*svc.ops.set_fleet(d.get("running"), d.get("paused")))
            self._json(404, {"error": "没有这个接口"})

        def _claims(self) -> Optional[sbxtoken.Claims]:
            c = sbxtoken.verify(svc.key, sbxtoken.bearer(self.headers.get("Authorization")))
            if c is None:
                self._json(401, {"error": "沙箱令牌无效"})
            elif self.path != "/v1/where":                 # 网关来问地址时用的也是这台沙箱的令牌,那次不算
                svc.saw(c.sandbox_id, self.client_address[0])
            return c

        def _body(self, limit: int) -> Optional[LimitedReader]:
            try:
                n = int(self.headers.get("Content-Length") or -1)
            except ValueError:
                n = -1
            if n < 0:
                self._json(411, {"error": "要带 Content-Length"})
                return None
            if n > limit:
                self._json(413, {"error": "请求体超过上限"})
                return None
            return LimitedReader(self.rfile, n)

        def _small_json(self) -> Optional[dict]:
            r = self._body(1 << 20)
            if r is None:
                return None
            return _jsonload(r.read())

        def _stream(self, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()

        def do_GET(self):
            if self.path == "/healthz":
                return self._json(200, {"ok": True})
            if self.path.startswith("/v1/ops/"):
                return self._ops()
            c = self._claims()
            if c is None:
                return
            if self.path == "/v1/where":
                got = svc.where(c.sandbox_id)
                return self._json(200, got) if got else self._json(404, {"error": "还没收到过这台沙箱的请求"})
            try:
                if self.path == "/v1/state":
                    ufd, err = svc.prepare_load(c)
                    if err is not None:
                        return self._json(*err)
                    self._stream("application/gzip")
                    n = svc.write_state(ufd, self.wfile)
                    svc.log("状态服务:%s 的沙箱 %s 载入 %d 个文件" % (c.user_id, c.sandbox_id, n))
                    return
                if self.path == "/v1/kb/manifest":
                    return self._json(200, svc.kb_manifest(c))
                self._json(404, {"error": "没有这个接口"})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if self.path.startswith("/v1/ops/"):
                return self._ops()
            c = self._claims()
            if c is None:
                return
            if self.path == "/v1/hello":             # 只记来源地址(_claims 里),不碰 NAS:恢复后网关靠它尽快拿到新地址
                r = self._body(1 << 10)
                if r is not None:
                    r.drain()
                    self._json(200, {"ok": True})
                return
            if self.path == "/v1/lease":
                body = self._small_json()
                if body is not None:
                    self._json(*svc.lease(c, str(body.get("action") or "")))
                return
            if self.path == "/v1/state/finish":
                body = self._small_json()
                if body is not None:
                    self._json(*svc.finish(c, bool(body.get("release"))))
                return
            if self.path == "/v1/kb/files":
                body = self._small_json()
                if body is None:
                    return
                paths = body.get("paths") or []
                if not isinstance(paths, list):
                    return self._json(400, {"error": "paths 要是列表"})
                self._stream("application/gzip")
                try:
                    svc.kb_files(c, paths, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path == "/v1/kb/publish":
                try:
                    base = int(self.headers.get(HDR_KB_BASE) or "")
                except ValueError:
                    return self._json(400, {"error": "要带 %s(手上那份知识库的版本)" % HDR_KB_BASE})
                body = self._body(MAX_UPLOAD + (64 << 20))
                if body is not None:
                    got = svc.kb_publish(c, base, body)
                    if not body.drain():
                        self.close_connection = True
                    self._json(*got)
                return
            self._json(404, {"error": "没有这个接口"})

        def do_PUT(self):
            c = self._claims()
            if c is None:
                return
            if self.path != "/v1/state":
                return self._json(404, {"error": "没有这个接口"})
            body = self._body(MAX_UPLOAD + (64 << 20))
            if body is not None:
                got = svc.sync(c, body)
                if not body.drain():
                    self.close_connection = True
                self._json(*got)

        def log_message(self, fmt, *args):
            pass

    return H


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="状态服务(Fuyao 运行模式)")
    ap.add_argument("--nas", default="/nas")
    ap.add_argument("--listen", default="0.0.0.0:8090")
    a = ap.parse_args(argv)
    key = (os.environ.get("GSDB_CONTROL_KEY") or "").encode("utf-8")
    if len(key) < 16:
        print("缺 GSDB_CONTROL_KEY(Secret sandbox-control 的 KEY,与网关同一把),或不足 16 个字符", file=sys.stderr)
        return 2
    svc = Service(a.nas, key, log=lambda m: print(m, flush=True))
    svc.ops.start()
    host, port = a.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), handler(svc))
    srv.daemon_threads = True
    print("状态服务就绪:%s · NAS %s" % (a.listen, a.nas), flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
