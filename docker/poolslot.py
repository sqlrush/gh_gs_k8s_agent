"""固定池化:管家一侧的一个槽位 —— 状态机、载入 / 回写、握手、腾挪核验(2026-10-23)。

    空闲 ─bind─▶ 绑定中 ─就绪─▶ 在用 ─retain─▶ 转保留中 ─写回完成─▶ 保留 ─resume─▶ 恢复中 ─就绪─▶ 在用
     ▲                                                                  │ evict(写回干净才行)
     └── verify(containerID 变了 + 新 boot 握手 + 盘是空的) ◀── 清理中 ◀─┘   清理中 ─超时─▶ 停用

**信任从哪里来。** 槽位里的模型与槽位 1 号进程同 uid,回执与 boot.json 它都能改。所以:
  · rec.boot 只在「刚由可信的重启产生」时更新 —— 新 Pod 的第一次握手、腾挪核验通过时。之后发给槽位的每条指令
    都带它,只有那个 1 号进程认。回执里出现别的 boot(真重启或伪造)一律按「槽位重启过」处理:释放独占、
    按 rec.boot 发 exit、进清理中,等网关拿 k8s 的 containerID 来核验。
  · 网关给的 containerID(签名请求)是根:清理中 → 空闲,containerID 必须与腾挪时记下的不同。
  · 「保留」的槽位里没有模型的进程(停止时 kill(-1)),它的盘只属于这个人;写回不干净的保留槽位**继续持有独占**
    (这个人到别处登录会被挡回这里,用的是最新的文件),也不许腾挪。

**版本令牌(gen)。** 每次取得独占都换新(poolsync.gen_rotate,按需模式的 Pod 同样换)。转保留时记下当时的令牌
与 NAS 主库指纹;回来时两样都对得上才直接用槽位里的文件,对不上 = 这个人在别处用过 → 409 stale,网关腾挪后重新绑定。

**NAS 上的数据不见了(2026-10-24)。** 回来时 NAS 上的令牌**不存在**(不是变成了别的值),主库**不存在或还是
转保留时记下的那一份**(指纹一致),不判 stale:用保留副本完整写回 NAS,写回成功才补上新令牌、照常恢复
(_restore_nas)。原来的做法是判 stale → 腾挪清盘 → 从 NAS 重新载入 —— 把唯一完整的那份清掉,NAS 上被删的
跟着丢(全删了就是空库启动)。测试环境的 NAS 曾放在 Mac 的 /tmp 下,macOS 每天 0 点删掉三天没碰过的文件,
保留超过三天的人回来就是这样;令牌写在绑定 / 恢复时、库写在会话结束时,会话跨过午夜时令牌会比库早一晚被删。
客户现场误删、备份恢复不全也是同一个样子。
为什么这不会是「在别处用过」:干净的转保留会释放独占;之后任何人取得独占(别的槽位 bind / resume,按需模式的
Pod)都先写一个新令牌 —— 令牌不存在,就说明这期间没人拿到过独占;库被别人写过,指纹一定会变(写库都是原子改名,
inode 换新;原地写也会改纳秒修改时间),所以「库还是记下的那份」也说明没人写过它。极端情况是别人用过、而他写的
令牌与库后来也被外部清掉了:那时他那段的主库已经不在,保留副本是还能拿到的最新一份;恢复时也只写不删。

所有对 NAS 与槽位盘的读写都经 poolsync / fdtree(按描述符、不跟随链接)。只在绑定载入之后、opencode 启动之前
(槽位空闲、没有模型进程)按路径用 SQLite 做库自检。
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import secrets
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import fdtree
import podctl
import poolsync

FREE, BINDING, ACTIVE, RETAINING, RETAINED = "free", "binding", "active", "retaining", "retained"
RESUMING, EVICTING, DISABLED, BOOTING = "resuming", "evicting", "disabled", "booting"
IN_USE = (BINDING, ACTIVE, RESUMING)
HOLDS_OWNER = (BINDING, ACTIVE, RESUMING, RETAINING)
RUN_FILE, SNAP_FILE, HELLO_FILE = "run.json", "snap.json", "hello.json"
ACK_FILE, BOOT_FILE = "ack.json", "boot.json"
CTL_MODE = 0o640
_POOL_ALLOWED = {BOOT_FILE, ACK_FILE}

Reply = Tuple[int, dict]


@dataclass
class Settings:
    pod: str
    sync_seconds: float = 60.0
    ready_seconds: float = 60.0
    stop_wait_seconds: float = 40.0
    snap_wait_seconds: float = 20.0
    hello_seconds: float = 10.0
    owner_refresh_seconds: float = 30.0
    evict_timeout_seconds: float = 360.0     # kubelet 重启退避最长 5 分钟(P0 摸底 S2)
    host: str = "127.0.0.1"


def _blank(boot: str = "", container_id: str = "") -> dict:
    return {"state": BOOTING, "user": None, "since": None, "password": None, "reports_token": None,
            "boot": boot, "container_id": container_id, "gen": "", "fingerprint": None, "dirty": False,
            "stale": False, "bound_at": None, "retained_at": None, "last_active": None, "evict": None,
            "error": "", "snap_seq": 0, "quiet": False, "reserved_for": None, "reserved_until": 0.0,
            "restore": False,       # 正在用保留副本恢复 NAS、还没写回成功(见 _restore_nas)
            "grmp_token": ""}       # 本人的中间件令牌:只在管家这里(凭据代理用),不进槽位、不进 status


class Slot:
    def __init__(self, index: int, ctl_dir: pathlib.Path, state_dir: pathlib.Path, priv_dir: pathlib.Path,
                 users_fd: int, settings: Settings, ports: Tuple[int, int], uid: int,
                 verdict: Callable[[int, int], str], log: Callable[[str], None] = print, model_port: int = 0):
        self.i = index
        self.ctl_dir, self.state_dir, self.priv = ctl_dir, state_dir, priv_dir / ("slot-%d.json" % index)
        self.users_fd, self.s, self.ports, self.uid = users_fd, settings, ports, uid
        # 要核对监听者的端口:对话、报告,加上模型中继(槽位重启的空档里被同 Pod 的别人抢到,opencode 会把对话发给它)
        self.watch: Tuple[int, ...] = tuple(ports) + ((model_port,) if model_port else ())
        self.verdict, self.log = verdict, log
        self.holder = "%s/%d" % (settings.pod, index)
        self.lock = threading.RLock()       # rec 的读写与持久化(短)
        self.op = threading.Lock()          # 本槽位的长操作逐个来
        self.state_fd = fdtree.open_root(state_dir)
        self.ufd: Optional[int] = None
        self.snap_seen: Dict[str, poolsync.Ident] = {}
        self.listener = "unknown"
        self.offender = False               # 本槽位的 uid 在别的槽位端口上监听
        self.busy, self.busy_at = False, 0.0
        self.last_sync_at: Optional[float] = None
        self.last_sync: Optional[dict] = None
        self.next_sync = 0.0
        self.next_owner = 0.0
        self.rec = self._load_rec()

    # -- 记录 ------------------------------------------------------------------------

    def _load_rec(self) -> dict:
        try:
            data = json.loads(self.priv.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _blank()
        rec = _blank()
        rec.update({k: data.get(k, v) for k, v in rec.items()})
        return rec

    def _persist(self) -> None:
        podctl._write_private(self.priv, json.dumps(self.rec, ensure_ascii=False))

    def _set(self, **fields) -> None:
        with self.lock:
            self.rec = dict(self.rec, **fields)
            self._persist()

    @property
    def state(self) -> str:
        return self.rec["state"]

    @property
    def user(self) -> Optional[str]:
        return self.rec["user"]

    def status(self) -> dict:
        """给网关看的样子。**不含任何秘密**(口令、报告令牌、GRMP 令牌)。"""
        r = self.rec
        ev = r.get("evict") or {}
        return {"slot": self.i, "state": r["state"], "user_id": r["user"], "since": r["since"],
                "bound_at": r["bound_at"], "retained_at": r["retained_at"], "last_active": r["last_active"],
                "busy": self.busy, "busy_at": self.busy_at, "dirty": r["dirty"], "stale": r["stale"],
                "listener": self.listener, "offender": self.offender, "boot": r["boot"],
                "container_id": r["container_id"], "evicting_since": ev.get("at"), "error": r["error"],
                "reserved_for": r.get("reserved_for"), "reserved_until": r.get("reserved_until") or 0.0,
                "ports": {"opencode": self.ports[0], "reports": self.ports[1]},
                "last_sync_at": self.last_sync_at, "last_sync": self.last_sync}

    def creds(self) -> dict:
        r = self.rec
        return {"slot": self.i, "state": r["state"], "user_id": r["user"],
                "opencode": {"port": self.ports[0], "username": "opencode", "password": r["password"]},
                "reports": {"port": self.ports[1], "token": r["reports_token"]}}

    # -- ctl 与回执 -----------------------------------------------------------------------

    def _write_ctl(self, name: str, obj: dict) -> None:
        path = self.ctl_dir / name
        tmp = self.ctl_dir / (".%s.%s.part" % (name, secrets.token_hex(4)))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, CTL_MODE)
        try:
            os.write(fd, json.dumps(obj, ensure_ascii=False).encode("utf-8"))
            os.fchmod(fd, CTL_MODE)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def _write_run(self, state: str) -> None:
        r = self.rec
        body = {"state": state, "since": r["since"], "user_id": r["user"], "password": r["password"],
                "reports_token": r["reports_token"], "boot": r["boot"]}
        self._write_ctl(RUN_FILE, body)

    def credentials(self) -> Optional[Tuple[str, str]]:
        """给凭据代理:(工号, 本人的中间件令牌)。只有在用的槽位才有 —— 保留 / 空闲 / 清理中的一律不转发。"""
        r = self.rec
        if r["state"] != ACTIVE or not r["user"]:
            return None
        return r["user"], r.get("grmp_token") or ""

    def _read_pool(self, name: str) -> dict:
        """槽位写的 .pool/ 下的小文件。**不可信**:按描述符读、限大小,读不出来就当没有。"""
        try:
            d = fdtree.open_path(self.state_fd, ".pool", create=False)
        except (fdtree.UnsafePath, OSError):
            return {}
        if d is None:
            return {}
        try:
            raw = fdtree.read_small(d, name, limit=65536)
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (fdtree.UnsafePath, UnicodeDecodeError, ValueError, OSError):
            data = {}
        finally:
            os.close(d)
        return data if isinstance(data, dict) else {}

    def ack(self) -> dict:
        return self._read_pool(ACK_FILE)

    def _same_boot(self) -> bool:
        """槽位还是记录里那个 1 号进程吗。读不到回执也算「不是」:清盘时 .pool/ 会先被删掉。
        回写之前、传播删除之前都要问 —— 重启过的槽位盘是空的,照它回写会把 NAS 上的历史删光。"""
        return bool(self.rec["boot"]) and self.ack().get("boot") == self.rec["boot"]

    @staticmethod
    def _wait(cond: Callable[[], bool], seconds: float, step: float = 0.1) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            if cond():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(step)

    def handshake(self, seconds: Optional[float] = None) -> Optional[dict]:
        """写一个新 nonce,等回执带回它。返回回执(含 boot);超时 None。"""
        nonce = secrets.token_hex(12)
        self._write_ctl(HELLO_FILE, {"nonce": nonce})
        got: dict = {}

        def answered() -> bool:
            a = self.ack()
            if a.get("hello") == nonce and isinstance(a.get("boot"), str) and a["boot"]:
                got.update(a)
                return True
            return False
        return got if self._wait(answered, self.s.hello_seconds if seconds is None else seconds) else None

    def disk_is_clean(self) -> bool:
        """槽位盘上只剩空的顶层目录与 .pool/ 下的 boot.json、ack.json(及其写到一半的临时名)。"""
        try:
            for rel, _dfd, name, _st in fdtree.walk(self.state_fd):
                if rel == ".pool/" + name and (name in _POOL_ALLOWED or (name.startswith(".") and name.endswith(".part"))):
                    continue
                return False
        except (OSError, fdtree.UnsafePath):
            return False
        return True

    # -- 就绪 -----------------------------------------------------------------------------

    def _listener_ok(self) -> bool:
        return all(self.verdict(p, self.uid) in ("ok", "unavailable") for p in self.watch)

    def _http_ok(self, port: int, header: str) -> bool:
        path = "/global/health" if port == self.ports[0] else "/whoami.json"
        req = urllib.request.Request("http://%s:%d%s" % (self.s.host, port, path))
        req.add_header("Authorization", header)
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except OSError:
            return False

    def ready(self) -> bool:
        """opencode 与报告服务都应答,且各端口(含模型中继)上的监听者都是本槽位的 uid(不是同 Pod 的冒充者)。"""
        r = self.rec
        basic = "Basic " + base64.b64encode(("opencode:%s" % r["password"]).encode()).decode()
        return (self._http_ok(self.ports[0], basic) and self._http_ok(self.ports[1], "Bearer " + r["reports_token"])
                and self._listener_ok())

    # -- 绑定 -----------------------------------------------------------------------------

    def begin_bind(self, user: str, container_id: str) -> Optional[Reply]:
        """在管家的 Pod 锁里调用:核对状态、取独占、换版本令牌。返回 None 表示可以继续 finish_bind。"""
        with self.lock:
            if self.state != FREE:
                return 409, {"reason": "state", "state": self.state}
            if self._reserved_for_other(user):
                return 409, {"reason": "reserved", "state": self.state}
            try:
                ufd = poolsync.open_user(self.users_fd, user)
            except (poolsync.PoolError, ValueError, OSError) as exc:
                reason = getattr(exc, "reason", "user_dir")
                return 500, {"reason": reason, "message": str(exc), "state": self.state}
            ok, holder, retry = poolsync.owner_acquire(ufd, self.holder)
            if not ok:
                os.close(ufd)
                return 409, {"reason": "owner_busy", "holder": holder, "retry_after": retry, "state": self.state}
            gen = poolsync.gen_rotate(ufd)
            self.ufd = ufd
            self.snap_seen = {}
            # 核验时记下的 containerID 是管家自己确认过的,优先于这次请求带来的(网关的视图可能过期)
            self._set(state=BINDING, user=user, gen=gen, container_id=self.rec["container_id"] or container_id,
                      dirty=False, stale=False, error="", bound_at=None, retained_at=None, since=None, quiet=False,
                      reserved_for=None, reserved_until=0.0, restore=False)
        return None

    def finish_bind(self, grmp_token: str) -> Reply:
        with self.op:
            phase = "load"
            try:
                moved, unclean = poolsync.load(self.ufd, self.state_fd, log=self.log, modes=poolsync.SLOT_MODES)
                check = poolsync.selfcheck(self.state_dir, self.ufd, self.state_fd, log=self.log,
                                           modes=poolsync.SLOT_MODES)
                poolsync.mark_dirty(self.ufd)
                phase = "start"
                self._start_run(grmp_token)
            except Exception as exc:            # noqa: BLE001  任何意外都要落到确定的状态,不能卡在「绑定中」
                return self._fail_start(phase, str(exc))
            if not self._wait(self.ready, self.s.ready_seconds, step=0.2):
                return self._fail_start("ready", self._not_ready_reason())
            loaded = dict(moved.to_json(), unclean_last_time=unclean, selfcheck=check)
            self._activate()
            self.log("槽位 %d 绑定 %s 完成:%s" % (self.i, self.user, loaded))
            return 200, dict(self.creds(), loaded=loaded)

    def _start_run(self, grmp_token: str) -> None:
        # 令牌留在管家(私有目录 0600,管家重启后接着用);槽位拿到的只有口令与报告令牌
        self._set(password=secrets.token_urlsafe(24), reports_token=secrets.token_urlsafe(24), since=time.time(),
                  grmp_token=grmp_token)
        self._write_run("run")

    def _activate(self) -> None:
        now = time.time()
        self.next_sync = time.monotonic() + self.s.sync_seconds
        self.next_owner = time.monotonic() + self.s.owner_refresh_seconds
        self._set(state=ACTIVE, bound_at=now, last_active=now, error="")

    def _not_ready_reason(self) -> str:
        bad = [p for p in self.watch if self.verdict(p, self.uid) not in ("ok", "unavailable", "down")]
        if bad:
            return "port_squatted:" + ",".join("%d=%s" % (p, self.verdict(p, self.uid)) for p in bad)
        return "opencode_not_ready"

    def _fail_start(self, phase: str, message: str) -> Reply:
        """载入阶段失败:盘上是半份数据、opencode 没起过 → 释放独占、腾挪(清盘)。
        启动阶段失败:opencode 可能已经写过 → 按转保留收尾(停止、写回、释放)。"""
        self.log("槽位 %d 为 %s 启动失败(%s):%s" % (self.i, self.user, phase, message))
        if phase == "load":
            self._release_owner()
            self._begin_evict(self.rec["container_id"], error="load_failed")
            return 500, {"reason": "load_failed", "message": message, "state": self.state}
        code = 502 if message.startswith("port_squatted") else 504
        self._finish_retain_locked(error=message)
        return code, {"reason": message.split(":", 1)[0], "message": message, "state": self.state}

    # -- 转保留 ---------------------------------------------------------------------------

    def begin_retain(self, user: str) -> Optional[Reply]:
        with self.lock:
            if self.state != ACTIVE:
                return 409, {"reason": "state", "state": self.state}
            if user != self.user:
                return 409, {"reason": "user_mismatch", "state": self.state}
            self._set(state=RETAINING)
        return None

    def finish_retain(self) -> Reply:
        with self.op:
            return self._finish_retain_locked()

    def _finish_retain_locked(self, error: str = "") -> Reply:
        """停 opencode → 等最终回执 → 完整写回 → 退出备份 → 干净才清 dirty、记指纹、释放独占。"""
        since = self.rec["since"]
        if since is None:
            # 还没起过 opencode:盘上至多是半份载入。**绝不能回写**(带删除传播的回写会把 NAS 上还没载入的部分删掉)
            self._release_owner()
            self._begin_evict(self.rec["container_id"], error=error or "load_interrupted")
            return 500, {"reason": "load_interrupted", "state": self.state}
        self._set(state=RETAINING)
        ack: dict = {}
        self._write_run("stop")

        def final() -> bool:
            a = self.ack()
            if a.get("final") and a.get("since") == since and a.get("boot") == self.rec["boot"]:
                ack.update(a)
                return True
            return False
        confirmed = self._wait(lambda: final() or not self._same_boot(), self.s.stop_wait_seconds, step=0.2)
        if not self._same_boot():
            # 等它停下的时候槽位重启了:盘已清空,最近一个同步周期之后的改动丢了。不能当「写回不干净」持有独占
            self._on_new_boot(str(self.ack().get("boot") or ""))
            return 500, {"reason": "restarted", "state": self.state}
        moved, name = self._sync_all(final=True)
        clean = confirmed and not moved.errors
        if clean:
            try:
                poolsync.clear_dirty(self.ufd)
                fp = poolsync.db_fingerprint(self.ufd)
                self._release_owner()
                self._set(fingerprint=list(fp) if fp else None, dirty=False)
            except OSError as exc:
                moved.errors.append("释放独占:%s" % exc)
                clean = False
        if not clean:
            why = "stop_not_confirmed" if not confirmed else "; ".join(moved.errors[:3])
            self.log("槽位 %d 转保留不干净(%s):继续持有 %s 的独占,稍后重试写回" % (self.i, why, self.user))
        self._set(state=RETAINED, retained_at=time.time(), dirty=not clean, quiet=confirmed, grmp_token="",
                  error=error or ("" if clean else "not_synced"))
        self.busy = False
        return 200, {"slot": self.i, "state": RETAINED, "user_id": self.user, "dirty": not clean,
                     "aborted_running": bool(ack.get("aborted_running")), "synced": moved.to_json(), "backup": name}

    def _sync_all(self, final: bool, may_delete: bool = True) -> "tuple[poolsync.Moved, Optional[str]]":
        name = None
        if self.ufd is None:
            return poolsync.Moved(errors=["没有打开本人的 NAS 目录"]), None
        if not self._same_boot():
            return poolsync.Moved(errors=["槽位重启过(盘已清空),不回写"]), None
        try:
            moved = poolsync.sync(self.ufd, self.state_fd, self.snap_seen, may_delete=may_delete,
                                  modes=poolsync.NAS_MODES, still_valid=self._same_boot)
            if final:
                name = poolsync.backup(self.ufd, self.state_fd, modes=poolsync.NAS_MODES)
        except Exception as exc:                # noqa: BLE001  写回失败只记下来,不能让收尾中断
            moved = poolsync.Moved(errors=["写回:%s" % exc])
        self.last_sync_at = time.time()
        self.last_sync = moved.to_json()
        return moved, name

    def _release_owner(self) -> None:
        if self.ufd is None:
            return
        try:
            poolsync.owner_release(self.ufd, self.holder)
        finally:
            os.close(self.ufd)
            self.ufd = None

    # -- 恢复 -----------------------------------------------------------------------------

    def begin_resume(self, user: str, container_id: str) -> Optional[Reply]:
        with self.lock:
            if self.state != RETAINED:
                return 409, {"reason": "state", "state": self.state}
            if user != self.user:
                return 409, {"reason": "user_mismatch", "state": self.state}
            if self.rec["stale"]:
                return 409, {"reason": "stale", "state": self.state}
            if container_id and self.rec["container_id"] and container_id != self.rec["container_id"]:
                return 409, {"reason": "restarted", "state": self.state}
            self._set(state=RESUMING, quiet=False)
        return None

    def finish_resume(self, grmp_token: str) -> Reply:
        with self.op:
            refused = self._reacquire()
            if refused is not None:
                return refused
            try:
                self._set(gen=poolsync.gen_rotate(self.ufd))
                poolsync.mark_dirty(self.ufd)
                self._start_run(grmp_token)
            except Exception as exc:            # noqa: BLE001
                return self._fail_start("start", str(exc))
            if not self._wait(self.ready, self.s.ready_seconds, step=0.2):
                return self._fail_start("ready", self._not_ready_reason())
            self._activate()
            self.log("槽位 %d 恢复 %s 完成" % (self.i, self.user))
            return 200, dict(self.creds(), resumed=True)

    def _reacquire(self) -> Optional[Reply]:
        """回到保留槽位前:重新取独占,核对版本令牌与主库指纹。写回不干净的保留槽位一直持有独占,直接用
        (正在用保留副本恢复 NAS 的,先把恢复写完)。NAS 上令牌不存在、主库不存在或还是记下的那份 → 用保留副本恢复,
        不判 stale。"""
        if self.rec["dirty"] and self.ufd is not None and poolsync.owner_refresh(self.ufd, self.holder):
            return self._restore_nas() if self.rec["restore"] else None
        try:
            self.ufd = poolsync.open_user(self.users_fd, self.user)
        except (poolsync.PoolError, ValueError, OSError) as exc:
            self._set(state=RETAINED)
            return 500, {"reason": getattr(exc, "reason", "user_dir"), "message": str(exc), "state": RETAINED}
        ok, holder, retry = poolsync.owner_acquire(self.ufd, self.holder)
        if not ok:
            os.close(self.ufd)
            self.ufd = None
            self._set(state=RETAINED, stale=True)       # 他正在别处用:这份保留副本迟早过期,交给网关腾挪
            return 409, {"reason": "owner_busy", "holder": holder, "retry_after": retry, "state": RETAINED}
        # 令牌不存在、主库不存在或还是转保留时那一份 = NAS 上的数据(部分)不见了,不是「在别处用过」(论证见模块说明)。
        # 还要是转保留时那块盘(槽位重启过盘就清空了,没有副本可用,照旧判 stale)
        fp = poolsync.db_fingerprint(self.ufd)
        intact = fp is not None and list(fp) == self.rec["fingerprint"]
        if (self.rec["gen"] and poolsync.gen_missing(self.ufd) and (intact or poolsync.db_missing(self.ufd))
                and self._same_boot()):
            return self._restore_nas()
        if poolsync.gen_read(self.ufd) != self.rec["gen"] or (list(fp) if fp else None) != self.rec["fingerprint"]:
            self._release_owner()
            self._set(state=RETAINED, stale=True)
            self.log("槽位 %d:%s 在别处用过,保留副本已过期" % (self.i, self.user))
            return 409, {"reason": "stale", "state": RETAINED}
        return None

    def _restore_nas(self) -> Optional[Reply]:
        """NAS 上这个人的数据不见了:用保留副本完整写回 NAS。持有独占时调用。

        · 只写不删(may_delete=False):NAS 上还剩下的东西一样不动;
        · 库快照全部重搬(snap_seen 清空):记下的「已搬过」对的是被删之前的 NAS;
        · 写回成功才返回 None,由 finish_resume 换上新令牌 —— **写回成功之前不补令牌**:令牌在、数据不全的 NAS
          会被当成一份完好的;
        · 写回失败按「写回不干净的保留」走:继续持有独占、记 dirty / not_synced,监视线程稍后重试(_retry_retain),
          这次恢复先不放行(503,网关稍后重试)。
        """
        if not self.rec["restore"]:
            self.log("⚠ 槽位 %d:%s 在 NAS 上的数据不见了(版本令牌不存在,主库不存在或未被改动),用保留副本恢复" % (self.i, self.user))
        self._set(restore=True)
        self.snap_seen = {}
        try:
            poolsync.mark_dirty(self.ufd)       # 恢复写到一半管家没了:下一个载入的人看得到「上次没写完」
        except OSError as exc:
            self.log("槽位 %d 标记 dirty 失败:%s" % (self.i, exc))
        moved, name = self._sync_all(final=True, may_delete=False)
        if moved.errors:
            self.log("⚠ 槽位 %d:%s 用保留副本恢复 NAS 时写回失败(%s),继续持有独占、稍后再试"
                     % (self.i, self.user, moved.errors[0]))
            self._set(state=RETAINED, dirty=True, error="not_synced")
            return 503, {"reason": "not_synced", "state": RETAINED, "retry_after": int(self.s.sync_seconds)}
        self._set(restore=False, dirty=False, error="")
        self.log("槽位 %d:%s 已用保留副本写回 NAS:%s,退出备份 %s" % (self.i, self.user, moved.to_json(), name))
        return None

    # -- 腾挪与核验 ---------------------------------------------------------------------------

    def begin_evict(self, container_id: str) -> Reply:
        with self.lock:
            if self.state not in (RETAINED, FREE, DISABLED):
                return 409, {"reason": "state", "state": self.state}
            if self.rec["dirty"]:
                return 409, {"reason": "not_synced", "state": self.state}
            self._begin_evict(container_id or self.rec["container_id"])
        return 202, {"slot": self.i, "state": EVICTING}

    def _begin_evict(self, container_id: str, error: str = "") -> None:
        """发 exit(按可信的 rec.boot,只有那个 1 号进程认)→ 清理中。调用前独占应已释放。"""
        if self.ufd is not None:
            os.close(self.ufd)
            self.ufd = None
        self.busy = False
        self._set(state=EVICTING, evict={"boot": self.rec["boot"], "container_id": container_id or "",
                                         "at": time.time()}, error=error, grmp_token="")
        try:
            self._write_ctl(RUN_FILE, {"state": "exit", "boot": self.rec["boot"]})
        except OSError as exc:
            self.log("槽位 %d 写 exit 指令失败:%s" % (self.i, exc))

    def force_evict(self, error: str) -> None:
        """不经网关的腾挪(槽位冒充管家时):放掉本槽位可能持有的独占,按可信的 boot 发 exit。调用方持有 op。"""
        if self.state in HOLDS_OWNER or self.rec["dirty"]:
            try:
                if self.ufd is None and self.user:
                    self.ufd = poolsync.open_user(self.users_fd, self.user)
                self._release_owner()
            except (poolsync.PoolError, ValueError, OSError) as exc:
                self.log("槽位 %d 释放独占失败:%s" % (self.i, exc))
        self._set(dirty=False)
        self._begin_evict(self.rec["container_id"], error=error)

    def verify(self, container_id: str) -> Reply:
        with self.lock:
            ev = self.rec.get("evict") or {}
            if self.state not in (EVICTING, DISABLED) or not ev:
                return 409, {"reason": "state", "state": self.state}
            if not container_id or container_id == ev.get("container_id"):
                return 409, {"reason": "not_restarted", "state": self.state}
        with self.op:
            # 再核对一次:两个网关副本会同时来核验同一个槽位,先到的那个可能已经核验完、甚至有人绑上了
            if self.state not in (EVICTING, DISABLED) or (self.rec.get("evict") or {}) != ev:
                return 409, {"reason": "state", "state": self.state}
            ack = self.handshake()
            if ack is None:
                return 409, {"reason": "no_hello", "state": self.state}
            boot = ack["boot"]
            rec = self._read_pool(BOOT_FILE)
            wiped = rec.get("wiped") if isinstance(rec.get("wiped"), dict) else {}
            if boot == ev.get("boot") or rec.get("boot") != boot:
                return 409, {"reason": "same_boot", "state": self.state}
            if rec.get("state_empty") is not True or wiped.get("errors"):
                return 409, {"reason": "not_wiped", "state": self.state}
            if not self.disk_is_clean():
                return 409, {"reason": "disk_not_empty", "state": self.state}
            if not self._listener_ok():
                return 409, {"reason": "listener", "state": self.state}
            with self.lock:
                self.rec = dict(_blank(boot=boot, container_id=container_id), state=FREE, quiet=True,
                                **self._reservation())      # 为等待者腾出来的槽位,核验后预留照旧
                self._persist()
            self.snap_seen = {}
            self.offender = False
        self.log("槽位 %d 腾挪核验通过:boot %s,已空闲" % (self.i, boot))
        return 200, {"slot": self.i, "state": FREE}

    def settle_booting(self) -> None:
        """没有记录的槽位(新 Pod):第一次握手的 boot 就是可信的(还没有任何人用过)。盘不干净就腾挪。"""
        with self.op:
            ack = self.handshake()
            if ack is None:
                return
            rec = self._read_pool(BOOT_FILE)
            if rec.get("boot") == ack["boot"] and rec.get("state_empty") is True and self.disk_is_clean():
                self._set(**dict(_blank(boot=ack["boot"]), state=FREE, quiet=True))
                self.log("槽位 %d 就绪:boot %s,空闲" % (self.i, ack["boot"]))
                return
            self._set(boot=ack["boot"])
            self._begin_evict("", error="not_clean_at_start")

    # -- 预留(满员时为等待者腾出来的槽位,所有网关副本都从 status 看到) ------------------------------

    def _reserved_for_other(self, user: str) -> bool:
        r = self.rec
        return bool(r.get("reserved_for")) and r["reserved_for"] != user and float(r.get("reserved_until") or 0) > time.time()

    def _reservation(self) -> dict:
        return {"reserved_for": self.rec.get("reserved_for"), "reserved_until": self.rec.get("reserved_until") or 0.0}

    def reserve(self, user: str, seconds: float) -> Reply:
        with self.lock:
            if self._reserved_for_other(user):
                return 409, {"reason": "reserved", "state": self.state}
            self._set(reserved_for=user, reserved_until=time.time() + seconds)
        return 200, {"slot": self.i, "reserved_for": user, "reserved_until": self.rec["reserved_until"]}

    def disable(self, reason: str) -> Reply:
        with self.lock:
            if self.state in IN_USE or self.state == RETAINING:
                return 409, {"reason": "state", "state": self.state}
            self._set(state=DISABLED, error=reason or "disabled")
        self.log("槽位 %d 停用:%s" % (self.i, reason))
        return 200, {"slot": self.i, "state": DISABLED}

    def touch(self, user: str, ts: float) -> None:
        with self.lock:
            if self.state == ACTIVE and user == self.user and ts > (self.rec["last_active"] or 0):
                self.rec = dict(self.rec, last_active=ts)   # 每 30 秒一批,不逐条落盘;下次状态迁移时一起写

    # -- 周期性工作(管家给每个槽位一个线程,每秒一轮) -----------------------------------------

    def periodic(self) -> None:
        """在用 → 刷新独占、定期回写;写回不干净的保留 → 重试;清理中 → 超时停用。有长操作在跑就跳过这一轮。"""
        if not self.op.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            st, dirty = self.state, self.rec["dirty"]
            if st in (ACTIVE, RETAINED) and self.rec["boot"] and not self._same_boot():
                self._on_new_boot(str(self.ack().get("boot") or ""))     # 监视线程拿不到锁时这里接住
                return
            if (st == ACTIVE or (st == RETAINED and dirty)) and now >= self.next_owner:
                self.next_owner = now + self.s.owner_refresh_seconds
                if not self._refresh_owner():
                    return
            if st == ACTIVE and now >= self.next_sync:
                self.next_sync = now + self.s.sync_seconds
                self.sync_once()
            elif st == RETAINED and dirty and now >= self.next_sync:
                self.next_sync = now + self.s.sync_seconds
                self._retry_retain()
            elif st == EVICTING and time.time() - float((self.rec.get("evict") or {}).get("at") or 0) > \
                    self.s.evict_timeout_seconds:
                self._set(state=DISABLED, error="evict_timeout")
                self.log("⚠ 槽位 %d 腾挪 %d 秒内没核验通过,已停用(告警):检查该容器的重启与日志"
                         % (self.i, self.s.evict_timeout_seconds))
        finally:
            self.op.release()

    def _refresh_owner(self) -> bool:
        if self.ufd is None:
            try:
                self.ufd = poolsync.open_user(self.users_fd, self.user)
            except (poolsync.PoolError, ValueError, OSError) as exc:
                self.log("槽位 %d 打不开 users/%s:%s" % (self.i, self.user, exc))
                return True             # NAS 暂时不可用:下一轮再试,不当成被接管
        if poolsync.owner_refresh(self.ufd, self.holder):
            return True
        self._lost_owner()
        return False

    def _lost_owner(self) -> None:
        """独占被别人接管(本管家停顿超过 90 秒):再写就是两边互相覆盖。停下、不再回写,这份副本作废。"""
        self.log("⚠ 槽位 %d:%s 的独占被别处接管,停止并不再回写(最近一个同步周期的改动丢失)" % (self.i, self.user))
        if self.rec["since"] is not None:
            try:
                self._write_run("stop")
            except OSError:
                pass
        if self.ufd is not None:
            os.close(self.ufd)
            self.ufd = None
        self._set(state=RETAINED, stale=True, dirty=False, error="owner_lost", retained_at=time.time(), grmp_token="")

    def sync_once(self) -> dict:
        seq = int(self.rec["snap_seq"] or 0) + 1
        self._set(snap_seq=seq)
        boot = self.rec["boot"]
        self._write_ctl(SNAP_FILE, {"seq": seq, "boot": boot})

        def snapped() -> bool:
            a = self.ack()
            return a.get("boot") == boot and isinstance(a.get("snap_seq"), int) and a["snap_seq"] >= seq
        confirmed = self._wait(lambda: snapped() or not self._same_boot(), self.s.snap_wait_seconds, step=0.2)
        if not self._same_boot():
            self._on_new_boot(str(self.ack().get("boot") or ""))
            return {"errors": 1, "restarted": True}
        moved, _name = self._sync_all(final=False)
        self.last_sync = dict(moved.to_json(), snapshot_confirmed=confirmed)
        if moved.errors:
            self.log("槽位 %d 定期回写有错:%s" % (self.i, "; ".join(moved.errors[:3])))
        return self.last_sync

    def _retry_retain(self) -> None:
        a = self.ack()
        if not (a.get("final") and a.get("since") == self.rec["since"] and a.get("boot") == self.rec["boot"]):
            if self.rec["since"] is not None:
                self._write_run("stop")
            return
        restoring = bool(self.rec["restore"])          # 用保留副本恢复 NAS 时只写不删(见 _restore_nas)
        moved, _name = self._sync_all(final=True, may_delete=not restoring)
        if moved.errors:
            self.log("⚠ 槽位 %d:%s 的写回仍失败(%s),继续持有独占、稍后再试" % (self.i, self.user, moved.errors[0]))
            return
        try:
            if restoring:                               # 写回成功了才补令牌,补的就是保留副本下次回来要对的那个
                self._set(gen=poolsync.gen_rotate(self.ufd), restore=False)
            poolsync.clear_dirty(self.ufd)
            fp = poolsync.db_fingerprint(self.ufd)
            self._release_owner()
        except OSError as exc:
            self.log("槽位 %d 释放独占失败:%s" % (self.i, exc))
            return
        self._set(dirty=False, quiet=True, error="", fingerprint=list(fp) if fp else None)
        self.log("槽位 %d:%s 的写回补上了,已释放独占" % (self.i, self.user))

    # -- 监视(管家的监视线程,每轮所有槽位) ------------------------------------------------

    def observe(self) -> list:
        """读回执与监听者。返回在本槽位端口上监听的外来 uid(交给管家定位肇事槽位)。"""
        a = self.ack()
        if self.state == ACTIVE:
            self.busy = bool(a.get("busy"))
            try:
                self.busy_at = float(a.get("busy_at") or 0)
            except (TypeError, ValueError):
                self.busy_at = 0.0
        vs = [self.verdict(p, self.uid) for p in self.watch]
        foreign = [int(v.split(":", 1)[1]) for v in vs if v.startswith("foreign:")]
        listener = "foreign:%d" % foreign[0] if foreign else ("ok" if all(v in ("ok", "unavailable") for v in vs)
                                                              else "down")
        if listener != self.listener and foreign:
            self.log("⚠ 槽位 %d 的端口上有外来监听者(uid %d):停止转发、告警" % (self.i, foreign[0]))
        self.listener = listener
        boot = a.get("boot")
        if (isinstance(boot, str) and boot and self.rec["boot"] and boot != self.rec["boot"]
                and self.state not in (EVICTING, DISABLED, BOOTING) and self.op.acquire(blocking=False)):
            try:
                self._on_new_boot(boot)
            finally:
                self.op.release()
        return foreign

    def _on_new_boot(self, boot: str) -> None:
        """回执里出现了不是 rec.boot 的启动编号。安静的槽位(空闲,或已确认停下的保留)里没有模型进程,
        只可能是 1 号进程真的重启过:盘已清空,核对后直接接受。其余情况按腾挪处理,等网关核验 containerID。"""
        st, quiet = self.state, bool(self.rec["quiet"])
        lost = st in HOLDS_OWNER or self.rec["dirty"]
        if lost:
            self.log("⚠ 槽位 %d 重启过(%s,%s):%s 最近一个同步周期的改动丢失,下次登录从 NAS 载入"
                     % (self.i, st, self.rec["boot"], self.user))
        if self.ufd is not None:
            try:
                self._release_owner()
            except OSError:
                pass
        if quiet and st in (FREE, RETAINED):
            rec = self._read_pool(BOOT_FILE)
            if rec.get("boot") == boot and rec.get("state_empty") is True and self.disk_is_clean():
                self._set(**dict(_blank(boot=boot, container_id=self.rec["container_id"]), state=FREE, quiet=True,
                                 **self._reservation()))
                self.log("槽位 %d 重启后已清盘,空闲(boot %s)" % (self.i, boot))
                return
        self._begin_evict(self.rec["container_id"], error="restarted")

    # -- 管家重启后接着做 -----------------------------------------------------------------

    def recover(self) -> None:
        with self.op:
            st = self.state
            if st == BOOTING:
                return
            if st not in (BINDING, RESUMING, RETAINING, ACTIVE) and not (st == RETAINED and self.rec["dirty"]):
                return
            try:
                self.ufd = poolsync.open_user(self.users_fd, self.user)
            except (poolsync.PoolError, ValueError, OSError) as exc:
                self.log("管家重启:槽位 %d 打不开 users/%s(%s)" % (self.i, self.user, exc))
                self._set(state=RETAINED, dirty=True, error="nas_unavailable")
                return
            if not poolsync.owner_refresh(self.ufd, self.holder):
                self._lost_owner()
                return
            if st == ACTIVE:
                self.next_sync = time.monotonic() + self.s.sync_seconds
                self.next_owner = time.monotonic() + self.s.owner_refresh_seconds
                self.log("管家重启:槽位 %d 恢复为在用(%s),继续定期回写" % (self.i, self.user))
            elif st == BINDING and self.rec["since"] is None:
                self.log("管家重启:槽位 %d 载入到一半,释放独占、腾挪" % self.i)
                self._release_owner()
                self._begin_evict(self.rec["container_id"], error="load_interrupted")
            elif st in (BINDING, RESUMING, RETAINING):
                self.log("管家重启:槽位 %d 处在「%s」,按转保留收尾" % (self.i, st))
                self._finish_retain_locked()

    def close(self) -> None:
        for fd in (self.ufd, self.state_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.ufd = None
