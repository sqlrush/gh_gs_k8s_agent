"""固定池化的管家容器(POOL_MODE=steward,2026-10-23):一个 Pod 里 8 个槽位的载入、回写、腾挪,控制接口 4098。

管家是 Pod 里唯一挂着全部用户目录(NAS users/)和全部槽位盘的容器;模型只在槽位里执行命令,碰不到这里。
每个槽位的状态机见 poolslot.py。这里做的是 Pod 一级的事:

  · **Pod 级 CAS**:bind / resume 带上网关以为的「在用人数」(expect_in_use),对不上回 409 pod_changed ——
    两个网关副本不会把两个新人同时塞进同一个「空」Pod(16 人以内各自独占靠这一条)。
  · **控制接口签名**(poolauth):请求与应答都签;GRMP 令牌在请求体里是密文。管家重启的空档里同 Pod 的槽位
    抢了 4098 也拿不到任何东西;管家起来时发现 4098 被某个槽位的 uid 占着,就让那个槽位腾挪(按 boot 发 exit)。
  · **监视**:每 2 秒读各槽位回执(会话忙、重启过没有)与 /proc/net/tcp(端口上是不是本人),外来监听者定位到肇事槽位。
  · **停止信号**:Pod 被删时所有在用的槽位并行转保留(停 opencode、最终快照、完整写回、释放独占)。

接口(除 /healthz 外都要签名;应答带 X-Pool-Resp-Sig):
  GET  /pool/status                                       全部槽位的状态(不含秘密)
  POST /pool/bind    {slot, user_id, grmp_token, expect_in_use, container_id}
  POST /pool/resume  {slot, user_id, grmp_token, expect_in_use, container_id}
  POST /pool/retain  {slot, user_id}
  POST /pool/evict   {slot, container_id}                 container_id:腾挪前的(k8s 给的)
  POST /pool/verify  {slot, container_id}                 container_id:重启后的
  POST /pool/touch   {activity: [[slot, user_id, ts], …]} 网关每 30 秒报一次交互活动
  POST /pool/disable {slot, reason}
  POST /pool/reserve {slot, user_id, seconds}             满员时为等待者预留腾出来的槽位(最长 600 秒)

凭据代理(docker/credproxy.py):中间件令牌(按人,绑定 / 恢复时由网关加密交来)、SM2 私钥、模型 Key 只在管家这里,
每个槽位一个 Unix 套接字转发 —— 槽位里没有任何凭据。
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import signal
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import credproxy
import fdtree
import netcheck
import poolauth
import poolslot
import poolsync
import slotrun

MAX_BODY = 8192
SLOT_UID_BASE = 2000
STEWARD_PORT = 4098

Reply = Tuple[int, dict]


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    key: bytes
    pod: str
    slots: int = 8
    users_dir: pathlib.Path = pathlib.Path("/pool/users")
    slots_dir: pathlib.Path = pathlib.Path("/pool/slots")      # <i>/ctl、<i>/state
    priv_dir: pathlib.Path = pathlib.Path("/pool/priv")
    port: int = STEWARD_PORT
    monitor_seconds: float = 2.0
    settings: Optional[poolslot.Settings] = None
    slot_ports: Callable[[int], Tuple[int, int]] = slotrun.slot_ports
    slot_model_port: Callable[[int], int] = slotrun.slot_model_port
    slot_uid: Callable[[int], int] = lambda i: SLOT_UID_BASE + i
    proc_files: Sequence[str] = netcheck.PROC_FILES

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        key = env.get("SANDBOX_CONTROL_KEY", "")
        if len(key) < 16:
            raise ConfigError("缺少 SANDBOX_CONTROL_KEY 或不足 16 个字符(共享 Secret sandbox-control,只注入管家与网关)")
        pod = env.get("POD_NAME") or socket.gethostname()
        try:
            slots = int(env.get("SLOTS_PER_POD", "8"))
        except ValueError:
            raise ConfigError("SLOTS_PER_POD=%r 不是整数" % env.get("SLOTS_PER_POD")) from None
        if not 1 <= slots <= slotrun.MAX_SLOT + 1:
            raise ConfigError("SLOTS_PER_POD=%d 越界(1–%d)" % (slots, slotrun.MAX_SLOT + 1))

        def num(name: str, default: str) -> float:
            try:
                return float(env.get(name, default))
            except ValueError:
                raise ConfigError("%s=%r 不是数字" % (name, env.get(name))) from None
        settings = poolslot.Settings(pod=pod, sync_seconds=num("STATE_SYNC_SECONDS", "60"),
                                     ready_seconds=num("POOL_READY_SECONDS", "60"),
                                     stop_wait_seconds=num("POOL_STOP_WAIT_SECONDS", "40"),
                                     evict_timeout_seconds=num("POOL_EVICT_TIMEOUT_SECONDS", "360"))
        return cls(key=key.encode("utf-8"), pod=pod, slots=slots, settings=settings)


def make_verdict(files: Sequence[str]) -> Callable[[int, int], str]:
    """端口上是不是本人在听。没有 /proc/net/tcp(开发机)时给 unavailable,调用方当作通过。"""
    present = any(os.path.exists(f) for f in files)

    def verdict(port: int, uid: int) -> str:
        return netcheck.verdict(port, uid, files) if present else "unavailable"
    return verdict


class Steward:
    def __init__(self, cfg: Config, log: Callable[[str], None] = print,
                 verdict: Optional[Callable[[int, int], str]] = None,
                 upstreams: Optional[credproxy.Upstreams] = None):
        self.cfg, self.log = cfg, log
        self.upstreams = upstreams          # 凭据代理的去处(中间件、模型);None = 不开(测试)
        self._proxies: list = []
        self.settings = cfg.settings or poolslot.Settings(pod=cfg.pod)
        self.lock = threading.Lock()        # Pod 级:CAS 与状态认领
        self.verdict = verdict or make_verdict(cfg.proc_files)
        self.started = time.time()
        self.stop = threading.Event()
        self.users_fd = fdtree.open_root(cfg.users_dir)
        cfg.priv_dir.mkdir(parents=True, exist_ok=True)
        self.slots: List[poolslot.Slot] = []
        for i in range(cfg.slots):
            base = cfg.slots_dir / str(i)
            self.slots.append(poolslot.Slot(i, base / "ctl", base / "state", cfg.priv_dir, self.users_fd,
                                            self.settings, cfg.slot_ports(i), cfg.slot_uid(i), self.verdict, log,
                                            model_port=cfg.slot_model_port(i)))
        self._uid_to_slot = {cfg.slot_uid(i): i for i in range(cfg.slots)}

    # -- 启动与后台 -----------------------------------------------------------------------

    def start(self) -> None:
        if self.upstreams is not None:
            self.start_credproxies()
        for s in self.slots:
            threading.Thread(target=self._slot_loop, args=(s,), name="slot-%d" % s.i, daemon=True).start()
        threading.Thread(target=self._monitor_loop, name="monitor", daemon=True).start()

    def start_credproxies(self) -> None:
        """每个槽位两个凭据代理套接字(<槽位>/sock/,只挂给那个槽位):credproxy.sock 只转中间件,用本人令牌、
        本人工号,槽位不在用时一律拒绝;model.sock 只转模型,加模型 Key,槽位的模型中继接它。
        分开是因为模型端口在 localhost 上,同 Pod 的别的槽位连得到 —— 那条路上绝不能有中间件。"""
        for s in self.slots:
            d = self.cfg.slots_dir / str(s.i) / "sock"
            try:
                d.mkdir(parents=True, exist_ok=True)
                proxy = credproxy.Proxy(self.upstreams, s.credentials, log=self.log)
                self._proxies.append(credproxy.serve_unix(d / credproxy.SOCKET_NAME, proxy))
                self._proxies.append(credproxy.serve_unix(d / credproxy.MODEL_SOCKET_NAME, proxy,
                                                          grmp=False, model=True))
            except OSError as exc:
                self.log("⚠ 槽位 %d 的凭据代理开不起来(%s):这个槽位调不了中间件与模型" % (s.i, exc))

    def _slot_loop(self, s: poolslot.Slot) -> None:
        try:
            s.recover()
        except Exception as exc:                # noqa: BLE001  一个槽位恢复出错不能拖垮其他槽位
            self.log("槽位 %d 恢复出错:%s" % (s.i, exc))
        while not self.stop.wait(1.0):
            try:
                if s.state == poolslot.BOOTING:
                    s.settle_booting()
                else:
                    s.periodic()
            except Exception as exc:            # noqa: BLE001
                self.log("槽位 %d 后台出错:%s" % (s.i, exc))

    def _monitor_loop(self) -> None:
        while not self.stop.wait(self.cfg.monitor_seconds):
            self.monitor_once()

    def monitor_once(self) -> None:
        offenders: Dict[int, int] = {}
        for s in self.slots:
            try:
                for uid in s.observe():
                    j = self._uid_to_slot.get(uid)
                    if j is not None and j != s.i:
                        offenders[j] = s.i
            except Exception as exc:            # noqa: BLE001
                self.log("监视槽位 %d 出错:%s" % (s.i, exc))
        for s in self.slots:
            was, s.offender = s.offender, s.i in offenders
            if s.offender and not was:
                self.log("⚠ 槽位 %d 在槽位 %d 的端口上监听(冒充):应尽快腾挪(告警)" % (s.i, offenders[s.i]))

    # -- 状态 -------------------------------------------------------------------------------

    def in_use(self) -> int:
        return sum(1 for s in self.slots if s.state in poolslot.IN_USE)

    def status(self) -> dict:
        slots = [s.status() for s in self.slots]
        return {"pod": self.cfg.pod, "started": self.started, "in_use": self.in_use(),
                "free": sum(1 for x in slots if x["state"] == poolslot.FREE), "slots": slots}

    # -- 指令 -------------------------------------------------------------------------------

    def _slot(self, body: dict) -> Optional[poolslot.Slot]:
        i = body.get("slot")
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(self.slots):
            return None
        return self.slots[i]

    @staticmethod
    def _user(body: dict) -> str:
        u = body.get("user_id")
        u = u.strip().lower() if isinstance(u, str) else ""
        return u if poolsync.USER_ID_RE.match(u) else ""

    def _grmp(self, body: dict) -> Optional[str]:
        """GRMP 令牌只收密文;没给 = 空串(沿用不带令牌的部署),给了解不开 = None。"""
        tok = body.get("grmp_token")
        if tok in (None, ""):
            return ""
        return poolauth.unseal(self.cfg.key, tok)

    def _claim(self, body: dict, verb: str) -> "tuple[Optional[poolslot.Slot], str, str, Optional[Reply]]":
        s, user, grmp = self._slot(body), self._user(body), self._grmp(body)
        if s is None:
            return None, "", "", (400, {"reason": "bad_slot"})
        if not user:
            return None, "", "", (400, {"reason": "bad_user_id", "message": "工号格式不对"})
        if grmp is None:
            return None, "", "", (400, {"reason": "bad_token", "message": "GRMP 令牌要用 poolauth.seal 加密"})
        cid = body.get("container_id") if isinstance(body.get("container_id"), str) else ""
        with self.lock:
            if s.state == poolslot.ACTIVE and s.user == user:
                return None, "", "", (200, dict(s.creds(), idempotent=True))
            expect = body.get("expect_in_use")
            if isinstance(expect, bool) or not isinstance(expect, int) or expect != self.in_use():
                return None, "", "", (409, {"reason": "pod_changed", "in_use": self.in_use(), "status": self.status()})
            refused = s.begin_bind(user, cid) if verb == "bind" else s.begin_resume(user, cid)
        if refused is not None:
            return None, "", "", refused
        return s, user, grmp, None

    def bind(self, body: dict) -> Reply:
        s, _user, grmp, early = self._claim(body, "bind")
        return early if early is not None else s.finish_bind(grmp)

    def resume(self, body: dict) -> Reply:
        s, _user, grmp, early = self._claim(body, "resume")
        return early if early is not None else s.finish_resume(grmp)

    def retain(self, body: dict) -> Reply:
        s, user = self._slot(body), self._user(body)
        if s is None or not user:
            return 400, {"reason": "bad_request"}
        with self.lock:
            refused = s.begin_retain(user)
        return refused if refused is not None else s.finish_retain()

    def evict(self, body: dict) -> Reply:
        s = self._slot(body)
        if s is None:
            return 400, {"reason": "bad_slot"}
        cid = body.get("container_id") if isinstance(body.get("container_id"), str) else ""
        with self.lock:
            return s.begin_evict(cid)

    def verify(self, body: dict) -> Reply:
        s = self._slot(body)
        cid = body.get("container_id")
        if s is None or not isinstance(cid, str):
            return 400, {"reason": "bad_request"}
        return s.verify(cid)

    def disable(self, body: dict) -> Reply:
        s = self._slot(body)
        if s is None:
            return 400, {"reason": "bad_slot"}
        reason = body.get("reason") if isinstance(body.get("reason"), str) else ""
        with self.lock:
            return s.disable(reason[:200])

    def reserve(self, body: dict) -> Reply:
        s, user = self._slot(body), self._user(body)
        secs = body.get("seconds")
        if s is None or not user or isinstance(secs, bool) or not isinstance(secs, (int, float)) or not 0 < secs <= 600:
            return 400, {"reason": "bad_request"}
        with self.lock:
            return s.reserve(user, float(secs))

    def touch(self, body: dict) -> Reply:
        items = body.get("activity")
        if not isinstance(items, list) or len(items) > 4 * len(self.slots):
            return 400, {"reason": "bad_request"}
        n = 0
        for it in items:
            if (isinstance(it, list) and len(it) == 3 and isinstance(it[0], int) and not isinstance(it[0], bool)
                    and 0 <= it[0] < len(self.slots) and isinstance(it[1], str)
                    and isinstance(it[2], (int, float)) and not isinstance(it[2], bool)):
                self.slots[it[0]].touch(it[1].strip().lower(), min(float(it[2]), time.time()))
                n += 1
        return 200, {"touched": n}

    # -- 停止 -------------------------------------------------------------------------------

    def on_sigterm(self) -> None:
        """Pod 被删:所有在用的槽位并行转保留。正在绑定 / 恢复的等它做完(op 锁)再收尾。"""
        self.stop.set()

        def one(s: poolslot.Slot) -> None:
            with s.op:
                if s.state in (poolslot.ACTIVE, poolslot.RETAINING, poolslot.BINDING, poolslot.RESUMING):
                    self.log("收到停止信号:槽位 %d(%s)转保留" % (s.i, s.user))
                    s._finish_retain_locked()
        threads = [threading.Thread(target=one, args=(s,), daemon=True) for s in self.slots]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def close(self) -> None:
        for srv in self._proxies:
            srv.shutdown()
            srv.server_close()
        for s in self.slots:
            s.close()
        os.close(self.users_fd)


# --- HTTP ------------------------------------------------------------------------------------

_ROUTES = {"/pool/bind": "bind", "/pool/resume": "resume", "/pool/retain": "retain", "/pool/evict": "evict",
           "/pool/verify": "verify", "/pool/touch": "touch", "/pool/disable": "disable", "/pool/reserve": "reserve"}


def make_server(st: Steward, host: str = "0.0.0.0", port: Optional[int] = None) -> http.server.ThreadingHTTPServer:
    key = st.cfg.key
    verifier = poolauth.Verifier(key, st.cfg.pod)
    vlock = threading.Lock()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def _send(self, code: int, obj: dict, nonce: str = "") -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if nonce:
                self.send_header(poolauth.HDR_RESP_SIG, poolauth.sign_response(key, st.cfg.pod, nonce, code, body))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Optional[bytes]:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n < 0 or n > MAX_BODY:
                return None
            return self.rfile.read(n) if n else b""

        def _authed(self, body: bytes) -> str:
            with vlock:
                return verifier.check(self.command, self.path, self.headers, body) or ""

        def do_GET(self) -> None:
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            nonce = self._authed(b"")
            if not nonce:
                return self._send(401, {})
            if self.path == "/pool/status":
                return self._send(200, st.status(), nonce)
            return self._send(404, {}, nonce)

        def do_POST(self) -> None:
            raw = self._body()
            if raw is None:
                self.close_connection = True
                return self._send(413, {"reason": "bad_request"})
            nonce = self._authed(raw)
            if not nonce:
                return self._send(401, {})
            name = _ROUTES.get(self.path)
            if name is None:
                return self._send(404, {}, nonce)
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._send(400, {"reason": "bad_request"}, nonce)
            if not isinstance(body, dict):
                return self._send(400, {"reason": "bad_request"}, nonce)
            try:
                code, out = getattr(st, name)(body)
            except Exception as exc:            # noqa: BLE001  接口不能因为一个请求挂掉
                st.log("处理 %s 出错:%s" % (self.path, exc))
                code, out = 500, {"reason": "internal", "message": str(exc)}
            return self._send(code, out, nonce)

    srv = http.server.ThreadingHTTPServer((host, st.cfg.port if port is None else port), H)
    srv.daemon_threads = True
    return srv


def bind_server(st: Steward, attempts: int = 30, wait_s: float = 2.0) -> http.server.ThreadingHTTPServer:
    """绑 4098。被占着时看是谁:槽位的 uid → 那个槽位在冒充管家,按它可信的 boot 发 exit 让它重启清盘。"""
    for n in range(attempts):
        try:
            return make_server(st)
        except OSError as exc:
            who = [x.uid for x in netcheck.listeners(st.cfg.port, st.cfg.proc_files)]
            st.log("⚠ 控制端口 %d 绑不上(%s),监听者 uid:%s(告警)" % (st.cfg.port, exc, who or "未知"))
            for uid in who:
                i = st._uid_to_slot.get(uid)
                if i is not None and n == 0:
                    s = st.slots[i]
                    st.log("⚠ 槽位 %d 占了管家的端口:让它腾挪(%s 最近一个同步周期的改动丢失)" % (i, s.user))
                    with s.op:
                        s.force_evict("squatted_steward_port")
            time.sleep(wait_s)
    raise OSError("控制端口 %d 一直绑不上" % st.cfg.port)


def serve(env: Mapping[str, str] = os.environ) -> int:
    try:
        cfg = Config.from_env(env)
    except ConfigError as exc:
        print("配置错误: %s" % exc, flush=True)
        return 2
    os.umask(0o007)                         # 载入、库自检产生的文件要让槽位(同组)读写得到
    try:
        ups = credproxy.Upstreams.from_env(env)
    except credproxy.ConfigError as exc:
        print("配置错误: %s" % exc, flush=True)
        return 2
    if not ups.model_base:
        print("配置错误: 管家没有 MODEL_BASE_URL / MODEL_API_KEY —— 模型 Key 在管家这里(凭据代理),"
              "槽位里没有;请检查清单里管家的 envFrom agent-config 与 Secret model-api", flush=True)
        return 2
    st = Steward(cfg, log=lambda m: print(m, flush=True), upstreams=ups)
    srv = bind_server(st)
    term = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_a: term.set())
    signal.signal(signal.SIGINT, lambda *_a: term.set())
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    st.start()
    print("管家就绪:Pod %s,%d 个槽位,控制接口 :%d" % (cfg.pod, cfg.slots, cfg.port), flush=True)
    term.wait()
    st.on_sigterm()                         # 收尾期间接口仍在服务(网关可查状态)
    srv.shutdown()
    return 0
