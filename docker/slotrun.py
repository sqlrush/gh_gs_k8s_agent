"""固定池化槽位容器的 1 号进程(POOL_MODE=slot,2026-10-23)。在 poolrun.Supervisor 之上加槽位的事。

一个 Pod 里有 8 个槽位容器(uid 2000+i,同组 1000)和一个管家(uid 1000)。槽位里跑模型能执行命令,
所以这里每一条都是在防「上一个人」或「同 Pod 的别人」:

  · **启动先清盘**:容器重启后 /data、/tmp、/dev/shm 与状态盘上的文件都还在(P0 摸底 S7),上一个人的
    `.pth` / `.gitconfig` / opencode 配置会在新进程里被执行。所以本进程(用 `python3 -E -s` 起,
    不读环境里的 PYTHON* 与用户目录的 site-packages)第一件事就是清空这些盘,再从镜像拷配置、渲染、
    建好自己名下的顶层目录(技能每次运行都 chmod 0700 自己的目录,属主不是本槽位就 EPERM,P0 摸底 S1)。
  · **指令按 boot 编号认**:每次启动生成一个随机 boot 编号写进 `.pool/boot.json`,管家写的 run.json /
    snap.json 里的 boot 对不上就不理 —— 新容器绝不会按上一个人的指令起 opencode。
  · **端口不留空档**:不跑 opencode 时自己占着对话与报告两个端口(连上来的回 503),同 Pod 别的槽位
    抢不走(P0 摸底 S3)。opencode 起来到开始监听之间的约 2.5 秒仍是空档,由管家按 uid 核对监听者兜底。
  · **停止时杀光**:停 opencode 后,1 号进程向容器里所有别的进程发 SIGKILL(模型 nohup 起的后台命令),
    再出最终快照 —— 保留态的盘上不会再有人写。
  · **载入后接管属主**:管家载入的文件属主是 uid 1000,技能 chmod 自己的目录会 EPERM;起 opencode 之前
    复制成自己的(fdtree.adopt,库文件只需组读写、不复制)。
  · **回写前规整权限**:给自己名下的文件加组读(fdtree.grant_group),管家才读得到。
  · **按人的凭证**:对话口令、报告令牌只来自本次 run.json,每次绑定 / 恢复都不同;中间件令牌、SM2 私钥、模型 Key
    根本不进槽位 —— 在管家的凭据代理里(docker/credproxy.py),本槽位的模型端口由 1 号进程中继过去。

本进程手里没有比本人更大的权限:没有控制密钥、看不到别人的目录。模型与它同 uid,能改回执、能给它发
SIGTERM —— 都只伤到本人;管家判断「腾挪完成」靠的是 k8s 给的 containerID 变化,不靠这里写的任何文件。
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import shutil
import signal
import socket
import sys
import time
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

import credproxy
import fdtree
import podctl
import poolrun

BOOT_FILE = ".pool/boot.json"
HELLO_FILE = "hello.json"
SHARED_FILE_MODE = 0o640
SLOT_DIR_MODE = 0o2770
TOP_DIRS: Tuple[str, ...] = poolrun.POOL_LOCALIZED + ("xdg-data/opencode", "xdg-state/opencode", "gdaa",
                                                     ".pool", poolrun.SNAP_DIR)
PORT_BASE = 4100
PORT_STEP = 10
MAX_SLOT = 63
BUSY_EVERY_S = 5.0
_NONCE_MAX = 128
_PLACEHOLDER_REPLY = (b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                      b"Content-Length: 30\r\nConnection: close\r\n\r\n{\"reason\": \"slot_not_running\"}")
# 凭据(中间件令牌、SM2 私钥、模型 Key)一律不进槽位:它们在管家那里,经凭据代理加到请求上(docker/credproxy.py)
_SCRUB_ENV = ("GRMP_AUTH_TOKEN", "GRMP_SM2_PRIVATE_KEY", "MODEL_API_KEY", "OPENCODE_SERVER_PASSWORD", "REPORTS_TOKEN",
              "SANDBOX_CONTROL_KEY", "POOL_CONTROL_KEY", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE")


def slot_ports(slot: int) -> Tuple[int, int]:
    """槽位 i 的对话端口与报告端口(4100+10i、4101+10i)。管家与网关按同一公式算。"""
    return PORT_BASE + PORT_STEP * slot, PORT_BASE + PORT_STEP * slot + 1


def slot_model_port(slot: int) -> int:
    """槽位 i 的模型中继端口(127.0.0.1:4102+10i):opencode 经它访问模型,1 号进程转给管家的凭据代理。"""
    return PORT_BASE + PORT_STEP * slot + 2


@dataclass
class SlotConfig:
    slot: int
    run: poolrun.Config
    data: pathlib.Path = pathlib.Path("/data")
    agent: pathlib.Path = pathlib.Path("/opt/agent")
    wipe: Tuple[pathlib.Path, ...] = (pathlib.Path("/data"), pathlib.Path("/tmp"), pathlib.Path("/dev/shm"))
    busy_every_s: float = BUSY_EVERY_S
    credproxy_dir: pathlib.Path = pathlib.Path("/run/credproxy")    # 管家开的凭据代理套接字(只挂给本槽位)
    model_port: int = 0                                             # 0 = 按槽位号算

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SlotConfig":
        raw = env.get("SLOT_INDEX", "")
        if not raw.isdigit() or int(raw) > MAX_SLOT:
            raise podctl.ConfigError("SLOT_INDEX=%r 不合格:要 0–%d 的整数(由渲染出的清单注入)" % (raw, MAX_SLOT))
        slot = int(raw)
        agent = pathlib.Path(env.get("AGENT_HOME", "/opt/agent"))
        run = poolrun.Config.from_env(env)
        run.port, run.reports_port = slot_ports(slot)
        run.podctl_cmd = [sys.executable, "-E", "-s", str(agent / "podctl.py")]
        wipe = tuple(pathlib.Path(p) for p in (env.get("POOL_WIPE") or "/data:/tmp:/dev/shm").split(":") if p)
        return cls(slot=slot, run=run, agent=agent, wipe=wipe, data=pathlib.Path(env.get("POOL_DATA", "/data")))


def _write_shared(path: pathlib.Path, data: dict) -> None:
    """原子写一份组可读(0640)的 JSON。临时文件独占新建、不跟随链接。"""
    tmp = path.with_name(".%s.%s.part" % (path.name, secrets.token_hex(4)))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, SHARED_FILE_MODE)
    try:
        os.write(fd, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        os.fchmod(fd, SHARED_FILE_MODE)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class SlotSupervisor(poolrun.Supervisor):
    GDAA_MODE = None                    # 技能自己会 chmod 0700;规整权限时再加回组读
    SNAP_MODE = SHARED_FILE_MODE
    GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}   # 1 号进程跑 git 不读任何配置

    def __init__(self, scfg: SlotConfig, env: Optional[Mapping[str, str]] = None, log=print):
        super().__init__(scfg.run, env=env, log=log)
        self.scfg = scfg
        self.boot_id = secrets.token_hex(8)
        self.placeholders: Dict[int, socket.socket] = {}
        self._ph_warned: set = set()
        self.hello_done = ""
        self._next_busy = 0.0
        d = scfg.data
        for k in _SCRUB_ENV:
            self.base_env.pop(k, None)  # 凭证只来自本次 run.json;python 的注入口一律不继承
        self.model_port = scfg.model_port or slot_model_port(scfg.slot)
        self.relay = None
        self.base_env.update({"XDG_CONFIG_HOME": str(d / "config"), "XDG_CACHE_HOME": str(d / "cache"),
                              "HOME": str(d / "home"), "OPENCODE_CONFIG": str(d / "oc" / "opencode.json"),
                              "MODEL_PROXY_URL": "http://127.0.0.1:%d%s" % (self.model_port, credproxy.MODEL_PREFIX),
                              credproxy.GRMP_SOCKET_ENV: str(scfg.credproxy_dir / credproxy.SOCKET_NAME)})
        self.ack.update({"boot": self.boot_id, "slot": scfg.slot, "busy": False, "busy_at": 0.0, "hello": ""})

    # -- 启动:清盘 → 配置 → 顶层目录 → boot.json → 占端口 --------------------------------

    def _wipe(self) -> fdtree.Stats:
        st = fdtree.Stats()
        for root in (self.cfg.state,) + tuple(self.scfg.wipe):   # 状态盘先清:它可能是 /data 下另一块盘
            try:
                fd = fdtree.open_root(root)
            except FileNotFoundError:
                continue
            try:
                fdtree.wipe_contents(fd, stats=st)
            finally:
                os.close(fd)
        return st

    def _install_config(self) -> None:
        """镜像里的技能 / AGENTS.md / models.json / rg 拷到 /data 下(opencode 要往里写),再渲染模型配置。
        源是只读镜像、目标刚清空且容器里没有别的进程,按 cp -R 的语义拷(保留链接与可执行位)。"""
        d = self.scfg.data
        for sub in ("home", "oc"):
            (d / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
        for kind in ("config", "cache"):
            src = self.scfg.agent / kind / "opencode"
            dst = d / kind / "opencode"
            dst.mkdir(mode=0o700, parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
        text = json.dumps(podctl.render_opencode_config(self.base_env), ensure_ascii=False, indent=2) + "\n"
        podctl._write_private(d / "oc" / "opencode.json", text)

    def boot(self) -> dict:
        os.umask(0o007)
        wiped = self._wipe()
        state_empty = not self.cfg.state.exists() or not any(self.cfg.state.iterdir())
        self._install_config()
        self.cfg.state.mkdir(parents=True, exist_ok=True)
        for sub in TOP_DIRS:
            p = self.cfg.state / sub
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, SLOT_DIR_MODE)
        report = {"boot": self.boot_id, "slot": self.scfg.slot, "uid": os.getuid(), "started": time.time(),
                  "wiped": {"deleted": wiped.deleted, "skipped": wiped.skipped, "errors": wiped.errors[:5]},
                  "state_empty": state_empty}
        _write_shared(self.cfg.state / BOOT_FILE, report)
        self._write_ack()
        self._open_placeholders()
        self._open_relay()
        self.log("槽位 %d 启动:清盘删除 %d 项、错误 %d 项,boot %s,等待管家的指令"
                 % (self.scfg.slot, wiped.deleted, len(wiped.errors), self.boot_id))
        return report

    # -- 占端口 -----------------------------------------------------------------------

    def _open_placeholders(self) -> None:
        for port in (self.cfg.port, self.cfg.reports_port):
            if port in self.placeholders:
                continue
            s = socket.socket()
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # 只为越过 TIME_WAIT;不设 REUSEPORT
                s.bind(("0.0.0.0", port))
                s.listen(8)
                s.setblocking(False)
            except OSError as exc:
                s.close()
                if port not in self._ph_warned:     # 每轮都会再试,只在第一次失败时说
                    self._ph_warned.add(port)
                    self.log("占不住端口 %d(%s)—— 可能有别的进程在监听,管家会按 uid 核对;之后每轮再试" % (port, exc))
                continue
            if port in self._ph_warned:
                self._ph_warned.discard(port)
                self.log("端口 %d 已经占回来了" % port)
            self.placeholders[port] = s

    def _open_relay(self) -> None:
        """模型中继:从启动起就占着本槽位的模型端口(同 Pod 的别的槽位抢不走),转到管家开的模型套接字
        (只转模型;中间件的套接字不经任何 TCP 端口)。"""
        if self.relay is not None:
            return
        try:
            self.relay = credproxy.relay(self.model_port,
                                         str(self.scfg.credproxy_dir / credproxy.MODEL_SOCKET_NAME), log=self.log)
        except OSError as exc:
            if "relay" not in self._ph_warned:
                self._ph_warned.add("relay")
                self.log("模型中继占不住端口 %d(%s);之后每轮再试" % (self.model_port, exc))

    def _close_placeholders(self) -> None:
        for s in self.placeholders.values():
            s.close()
        self.placeholders.clear()

    def _serve_placeholders(self) -> None:
        """连上来的一律回 503 再正常关闭(不用 RST:kubectl port-forward 遇到 RST 会整条断掉)。
        先把对方已经发来的请求读掉 —— 带着没读的数据关闭,内核照样发 RST。这里留下的 TIME_WAIT 不碍事:
        opencode、报告服务与占位监听都设了 SO_REUSEADDR。"""
        for s in self.placeholders.values():
            while True:
                try:
                    conn, _addr = s.accept()
                except OSError:
                    break
                try:
                    conn.settimeout(0.05)
                    try:
                        conn.recv(65536)
                    except OSError:
                        pass
                    conn.sendall(_PLACEHOLDER_REPLY)
                    conn.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                finally:
                    conn.close()

    # -- 一轮 ---------------------------------------------------------------------------

    def _ctl_for_me(self, name: str) -> Optional[dict]:
        d = self._read_ctl(name)
        return d if d is not None and d.get("boot") == self.boot_id else None

    def tick(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        self._reap_orphans()
        if self.oc is None and not self.term:
            self._open_placeholders()       # 启动时被同 Pod 的别人抢了先:对方被腾挪走之后要能占回来
        self._open_relay()
        self._serve_placeholders()
        self._answer_hello()
        desired = self._ctl_for_me(poolrun.RUN_FILE)   # 不是给本次启动的指令(上一个人的残留)一律不理
        if self.term:
            self._stop_final(desired)
            return False
        if desired is not None and desired.get("state") == "exit":
            self._exit()
            return False
        if desired is not None and desired.get("since") != self.final_since:
            if desired.get("state") == "run":
                self._ensure_running(desired, now)
            elif desired.get("state") == "stop":
                self._stop_final(desired)
        req = self._ctl_for_me(poolrun.SNAP_FILE)
        seq = req.get("seq") if req else None
        if isinstance(seq, int) and seq > self.snap_done:
            changed, err = self.snapshot()
            self._normalize()
            self.snap_done = seq
            self._write_ack(snap_seq=seq, changed=changed, error=err or self.ack.get("error", ""))
        self._poll_busy(now)
        return True

    def _answer_hello(self) -> None:
        """管家每次重新认领本槽位时写一个随机 nonce,回执里带回它与 boot:证明这份回执是现在这个进程写的。"""
        hello = self._read_ctl(HELLO_FILE)
        nonce = hello.get("nonce") if hello else None
        if isinstance(nonce, str) and 0 < len(nonce) <= _NONCE_MAX and nonce != self.hello_done:
            self.hello_done = nonce
            self._write_ack(hello=nonce)

    def run_forever(self) -> int:
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        self.boot()
        while self.tick():
            time.sleep(self.cfg.poll_s)
        self._close_placeholders()
        self.log("槽位守护进程收尾完成,退出")
        return 0

    # -- 起停 -----------------------------------------------------------------------------

    def _env_for(self, desired: dict) -> Dict[str, str]:
        """本人的中间件令牌不在这里:它在管家的凭据代理里,技能脚本经套接字调用,本容器里拿不到。"""
        env = super()._env_for(desired)
        env["REPORTS_TOKEN"] = str(desired.get("reports_token") or "")
        return env

    def _prepare(self, env: Dict[str, str]) -> None:
        self._adopt()
        super()._prepare(env)

    def _adopt(self) -> None:
        """管家刚载入的文件属主是它:换成本槽位自己的(库文件除外),技能 chmod 自己的目录才不会 EPERM。
        每次新的 run 都走一遍;恢复保留槽位时没有别人的文件,只是一次遍历。"""
        st = fdtree.Stats()
        root = fdtree.open_root(self.cfg.state)
        try:
            for sub in poolrun.POOL_LOCALIZED:
                fd = fdtree.open_path(root, sub, create=False)
                if fd is None:
                    continue
                try:
                    fdtree.adopt(fd, stats=st, keep_file=poolrun._is_db)
                finally:
                    os.close(fd)
        finally:
            os.close(root)
        if st.files or st.errors:
            self.log("接管载入的文件:%d 项%s" % (st.files, ",失败 %d 项:%s" % (len(st.errors), st.errors[0])
                                                  if st.errors else ""))

    def _ensure_running(self, desired: dict, now: float) -> None:
        if self.oc is None and now >= self._next_start:
            rt = desired.get("reports_token")
            if not isinstance(rt, str) or len(rt) < 16:
                if self.ack.get("error") != "bad_run":
                    self.log("run.json 缺报告令牌(或太短),不启动")
                    self._write_ack(running=False, error="bad_run")
                return
            self._close_placeholders()  # 让给 opencode 与报告服务
        super()._ensure_running(desired, now)
        if self.oc is None:
            self._open_placeholders()   # 没起来或刚退出:端口不能空着

    def _halt(self) -> bool:
        """停 opencode 与报告服务、清掉其余进程、重新占住端口。返回是否中止了正在跑的对话。"""
        aborted = False
        if self.oc is not None and self.oc.poll() is None:
            aborted = self._abort_busy()
            self._terminate(self.oc)
        self.oc = None
        self._stop_reports()
        self._kill_others()
        self._open_placeholders()
        return aborted

    def _stop_final(self, desired: Optional[dict]) -> None:
        aborted = self._halt()
        changed, err = self.snapshot()
        self._normalize()
        since = (desired or {}).get("since")
        self.final_since = since
        self.log("已停止:opencode 已退出、后台进程已清,最终快照 %d 个库有变化%s"
                 % (len(changed), "(%s)" % err if err else ""))
        self._write_ack(since=since, running=False, final=True, aborted_running=aborted, changed=changed,
                        error=err, busy=False)

    def _exit(self) -> None:
        """腾挪:停掉一切后 1 号进程退出,kubelet 只重启本容器(同 Pod 其他槽位不受影响),新进程先清盘。"""
        self._halt()
        self._close_placeholders()
        self.log("收到腾挪指令:1 号进程退出,容器将被重启并清盘")

    def _kill_others(self) -> None:
        """1 号进程向容器里别的进程发 SIGKILL(kill(-1) 不含发起者自己)。不是 1 号进程时不做 ——
        在开发机上跑测试时 kill(-1) 会杀掉本用户的所有进程。"""
        if os.getpid() != 1:
            return
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                time.sleep(0.05)

    def _normalize(self) -> None:
        st = fdtree.Stats()
        try:
            fd = fdtree.open_root(self.cfg.state)
        except OSError as exc:
            self.log("规整权限失败:%s" % exc)
            return
        try:
            fdtree.grant_group(fd, stats=st)
        finally:
            os.close(fd)
        if st.errors:
            self.log("规整权限有 %d 处失败(只影响本人的回写):%s" % (len(st.errors), st.errors[0]))

    # -- 会话忙(网关据此判断「在用」:开着的标签页本身每 10 秒探活,不算) ---------------------

    def _poll_busy(self, now: float) -> None:
        if self.oc is None or self.oc.poll() is not None or now < self._next_busy:
            return
        self._next_busy = now + self.scfg.busy_every_s
        try:
            status = self._api("GET", "/session/status", timeout=1.0)
        except (OSError, ValueError):
            return
        busy = isinstance(status, dict) and any(
            isinstance(v, dict) and v.get("type") in ("busy", "retry") for v in status.values())
        if busy:
            self._write_ack(busy=True, busy_at=time.time())
        elif self.ack.get("busy"):
            self._write_ack(busy=False)

    # -- 回执 ---------------------------------------------------------------------------

    def _write_ack(self, **fields) -> None:
        self.ack.update(fields)
        self.ack["ts"] = time.time()
        self.ack["boot"] = self.boot_id
        _write_shared(self.cfg.state / poolrun.ACK_FILE, self.ack)


def main() -> int:
    try:
        return SlotSupervisor(SlotConfig.from_env(os.environ)).run_forever()
    except podctl.ConfigError as exc:
        print("槽位启动失败:%s" % exc, file=sys.stderr)
        return 2
