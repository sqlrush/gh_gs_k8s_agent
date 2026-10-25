"""固定池化槽位守护进程(slotrun.SlotSupervisor)的基类:按指令起停 opencode、生成库快照、写回执。

它只听 ctl 目录(管家写,本容器只读挂载)里的两份指令,结果写回状态盘上的 `.pool/`:

| 读 | 内容 |
|---|---|
| ctl `run.json` | `{"state": "run"|"stop", "since", "user_id", "password"}`——以谁的身份跑 opencode,或停下 |
| ctl `snap.json` | `{"seq": n}`——管家定期回写前要一份新快照 |

| 写 | 内容 |
|---|---|
| `.pool/snap/<子目录>/<相对路径>` | 各个库的一致性快照(合并 WAL + 在线备份,原子改名) |
| `.pool/ack.json` | 已完成的快照序号、opencode 是否在跑、是否已最终停止、停止时是否中止了对话、最近的错误 |

**为什么快照由这里生成**:管家挂着全部用户目录,它不能用 SQLite 打开槽位盘上的任何文件(SQLite 按路径打开,
模型换一个链接就能让它读写别人的库)。这里是模型所在的一侧,按路径打开最多伤到本人的数据——与他本来就能删
自己的会话同级。

不在用时不起 opencode;收到 SIGTERM 时停 opencode、写最终快照与回执再退出——删 Pod 时各容器同时收到信号,
管家靠这份回执知道可以完整写回了。槽位那一层加的东西(启动清盘、按 boot 认指令、占位监听……)见 slotrun.py。
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shlex
import signal
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional

import podctl
import statesync

RUN_FILE = "run.json"
SNAP_FILE = "snap.json"
ACK_FILE = ".pool/ack.json"
SNAP_DIR = ".pool/snap"
POOL_LOCALIZED = ("xdg-data", "xdg-state", "workspace", "gdaa/sessions", "reports")   # 与 poolsync 一致(有测试核对)
_USER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,52}[a-z0-9])?$")
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass
class Config:
    ctl_dir: pathlib.Path = pathlib.Path("/run/sandbox")
    state: pathlib.Path = pathlib.Path("/data/state")
    kb_dir: pathlib.Path = pathlib.Path("/nas/kb")
    opencode_cmd: List[str] = field(default_factory=lambda: ["opencode"])
    podctl_cmd: List[str] = field(default_factory=lambda: ["python3", "/opt/agent/podctl.py"])
    port: int = 4096
    reports_port: int = 4097
    poll_s: float = 0.5
    stop_timeout_s: float = 15.0
    health_every_s: float = 20.0
    health_failures: int = 3            # 与按需模式 livenessProbe 的口径一致:20 秒一次、连败 3 次
    env_file: Optional[pathlib.Path] = pathlib.Path("/data/env.sh")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        return cls(
            ctl_dir=pathlib.Path(env.get("POOL_CTL_DIR", "/run/sandbox")),
            state=pathlib.Path(env.get("STATE_LOCAL", "/data/state")),
            kb_dir=pathlib.Path(env.get("NAS_KB", "/nas/kb")),
            opencode_cmd=shlex.split(env.get("OPENCODE_BIN", "opencode")),
            port=int(env.get("OPENCODE_PORT", "4096")),
            stop_timeout_s=float(env.get("POOL_STOP_TIMEOUT", "15")),
        )


def _is_db(name: str) -> bool:
    return name.lower().endswith(_DB_SUFFIXES)


class Supervisor:
    # 固定池化的槽位(slotrun)要让管家(另一个 uid、同组)读得到,覆盖成组可读;这里是按需池化的口径
    GDAA_MODE: Optional[int] = 0o700
    SNAP_MODE = 0o600
    GIT_ENV: Dict[str, str] = {}

    def __init__(self, cfg: Config, env: Optional[Mapping[str, str]] = None,
                 log: Callable[[str], None] = print):
        self.cfg = cfg
        self.base_env: Dict[str, str] = dict(os.environ if env is None else env)
        self.log = log
        self.oc: Optional[subprocess.Popen] = None
        self.reports: Optional[subprocess.Popen] = None
        self.current: Optional[dict] = None     # 正在执行的 run.json
        self.final_since: object = object()     # 已为哪一次 run 做过最终停止(初值不等于任何 since)
        self.snap_done = 0
        self.record: Dict[str, List[int]] = {}  # M1:各个库上次生成快照时的签名
        self.term = False
        self._next_start = 0.0
        self._backoff = 1.0
        self._next_health = 0.0
        self._health_fail = 0
        self.ack: Dict[str, object] = {"since": None, "running": False, "final": False, "snap_seq": 0,
                                       "changed": [], "aborted_running": False, "error": ""}

    # -- 指令与回执 ---------------------------------------------------------

    def _read_ctl(self, name: str) -> Optional[dict]:
        try:
            data = json.loads((self.cfg.ctl_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_ack(self, **fields) -> None:
        self.ack.update(fields)
        self.ack["ts"] = time.time()
        podctl._write_private(self.cfg.state / ACK_FILE, json.dumps(self.ack, ensure_ascii=False))

    # -- 一轮 -----------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> bool:
        """处理一轮指令。返回 False 表示收到 SIGTERM 且已收尾,该退出了。"""
        now = time.monotonic() if now is None else now
        self._reap_orphans()
        desired = self._read_ctl(RUN_FILE)
        if self.term:
            self._stop_final(desired)
            return False
        if desired is not None and desired.get("since") != self.final_since:
            if desired.get("state") == "run":
                self._ensure_running(desired, now)
            elif desired.get("state") == "stop":
                self._stop_final(desired)
        req = self._read_ctl(SNAP_FILE)
        seq = req.get("seq") if req else None
        if isinstance(seq, int) and seq > self.snap_done:
            changed, err = self.snapshot()
            self.snap_done = seq
            self._write_ack(snap_seq=seq, changed=changed, error=err or self.ack.get("error", ""))
        return True

    def run_forever(self) -> int:
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        self.log("守护进程就绪:等待管家的指令(%s)" % (self.cfg.ctl_dir / RUN_FILE))
        while self.tick():
            time.sleep(self.cfg.poll_s)
        self.log("守护进程收尾完成,退出")
        return 0

    def _on_term(self, *_a) -> None:
        self.term = True

    # -- 起 opencode ---------------------------------------------------------

    def _env_for(self, desired: dict) -> Dict[str, str]:
        st = self.cfg.state
        env = dict(self.base_env)
        env.update({
            "XDG_DATA_HOME": str(st / "xdg-data"),
            "XDG_STATE_HOME": str(st / "xdg-state"),
            "GSDB_HOME": str(st / "gdaa"),
            "GSDB_REPORTS_DIR": str(st / "reports"),
            "GSDB_KB_DIR": str(self.cfg.kb_dir),
            "WORKSPACE": str(st / "workspace"),
            "GSDB_USER_ID": desired["user_id"],
            "OPENCODE_SERVER_PASSWORD": desired["password"],
        })
        return env

    def _prepare(self, env: Dict[str, str]) -> None:
        st = self.cfg.state
        for sub in POOL_LOCALIZED + ("xdg-data/opencode", "xdg-state/opencode"):
            (st / sub).mkdir(parents=True, exist_ok=True)
        gdaa = st / "gdaa"
        if self.GDAA_MODE is not None:
            os.chmod(gdaa, self.GDAA_MODE)
        podctl._write_private(gdaa / "config.yaml", podctl.render_gdaa_config(env))   # 配置不是状态:每次重生成
        podctl.log_prune(st / "xdg-data" / "opencode" / "log", 7)
        ws = st / "workspace"
        if not (ws / ".git").exists():          # opencode 以 git 仓识别「项目」,与按需模式同一做法
            genv = dict(os.environ, **self.GIT_ENV) if self.GIT_ENV else None
            try:
                subprocess.run(["git", "-C", str(ws), "init", "-q"], check=False, env=genv)
                subprocess.run(["git", "-C", str(ws), "-c", "user.name=agent", "-c", "user.email=agent@pod",
                                "commit", "-q", "--allow-empty", "-m", "workspace init"], check=False, env=genv)
            except OSError as exc:              # 镜像里有 git;没有时会话照样能用,只是 Web 界面不按项目归类
                self.log("工作目录没能初始化成 git 仓(%s);会话可用,Web 界面的项目归类会缺失" % exc)
        if self.cfg.env_file is not None:       # exec 进容器排障时 `. /data/env.sh`;只放路径,不放口令
            keys = ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "GSDB_HOME",
                    "GSDB_KB_DIR", "GSDB_REPORTS_DIR", "OPENCODE_CONFIG", "HOME", "WORKSPACE")
            try:
                self.cfg.env_file.write_text("".join("export %s=%s\n" % (k, shlex.quote(env[k]))
                                                     for k in keys if env.get(k)), encoding="utf-8")
            except OSError:
                pass

    def _ensure_running(self, desired: dict, now: float) -> None:
        if self.oc is not None and self.oc.poll() is None:
            if self.reports is not None and self.reports.poll() is not None:
                self.log("报告只读端口退出了(rc=%s),重新拉起" % self.reports.returncode)
                self.reports = self._spawn_reports(self._env_for(self.current or desired))
            self._health(now)
            return
        if self.oc is not None:                 # 跑着跑着退出了:退避后重拉,状态都在 state 卷里
            rc = self.oc.returncode
            self.oc = None
            self._stop_reports()
            self._next_start = now + self._backoff
            self.log("opencode 意外退出(rc=%s),%.0f 秒后重新拉起" % (rc, self._backoff))
            self._backoff = min(self._backoff * 2, 30.0)
            self._write_ack(running=False, error="opencode 意外退出(rc=%s),将重新拉起" % rc)
            return
        if now < self._next_start:
            return
        user, pw = desired.get("user_id"), desired.get("password")
        if not isinstance(user, str) or not _USER_RE.match(user) or not isinstance(pw, str) or len(pw) < 16:
            if self.ack.get("error") != "bad_run":
                self.log("run.json 无效(工号或口令不合格),不启动")
                self._write_ack(running=False, error="bad_run")
            return
        env = self._env_for(desired)
        try:
            if self.current is None or self.current.get("since") != desired.get("since"):
                self._prepare(env)
            self.oc = subprocess.Popen(self.cfg.opencode_cmd + ["serve", "--hostname", "0.0.0.0",
                                                               "--port", str(self.cfg.port)],
                                       cwd=str(self.cfg.state / "workspace"), env=env)
            self.reports = self._spawn_reports(env)
        except (OSError, podctl.ConfigError) as exc:
            self.log("启动失败:%s" % exc)
            self._next_start = now + self._backoff
            self._write_ack(running=False, error="启动失败:%s" % exc)
            return
        self.current = desired
        self._health_fail = 0
        self._next_health = now + self.cfg.health_every_s
        self.log("已以工号 %s 启动 opencode(pid %d)" % (user, self.oc.pid))
        self._write_ack(since=desired.get("since"), running=True, final=False, error="")

    def _spawn_reports(self, env: Dict[str, str]) -> subprocess.Popen:
        return subprocess.Popen(self.cfg.podctl_cmd + ["serve-reports", "--dir", env["GSDB_REPORTS_DIR"],
                                                       "--port", str(self.cfg.reports_port),
                                                       "--kb-dir", env["GSDB_KB_DIR"]], env=env)

    # -- 存活检查(按需模式靠 livenessProbe;池化下 4096 空闲时不监听,探针放不进模板,由这里做) ---

    def _api(self, method: str, path: str, timeout: float = 3.0):
        pw = (self.current or {}).get("password", "")
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.cfg.port, path), method=method,
                                     data=b"{}" if method == "POST" else None)
        req.add_header("Authorization", "Basic " + base64.b64encode(("opencode:%s" % pw).encode()).decode())
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"null")

    def _health(self, now: float) -> None:
        if now < self._next_health:
            return
        self._next_health = now + self.cfg.health_every_s
        try:
            self._api("GET", "/global/health")
            self._health_fail = 0
            self._backoff = 1.0
        except (OSError, ValueError):
            self._health_fail += 1
            if self._health_fail >= self.cfg.health_failures:
                self.log("opencode 连续 %d 次健康检查失败,重启它" % self._health_fail)
                self._terminate(self.oc)        # 下一轮按「意外退出」重拉

    # -- 停 -------------------------------------------------------------------

    def _abort_busy(self) -> bool:
        """中止正在跑的对话(opencode 1.18.27:GET /session/status、POST /session/{id}/abort,10-14 实机核对)。"""
        try:
            status = self._api("GET", "/session/status")
        except (OSError, ValueError):
            return False
        aborted = False
        for sid, st in (status or {}).items() if isinstance(status, dict) else []:
            if isinstance(st, dict) and st.get("type") in ("busy", "retry") and re.match(r"^[A-Za-z0-9_-]+$", sid):
                try:
                    self._api("POST", "/session/%s/abort" % sid)
                    aborted = True
                except (OSError, ValueError):
                    pass
        return aborted

    def _terminate(self, proc: Optional[subprocess.Popen]) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=self.cfg.stop_timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def _stop_reports(self) -> None:
        self._terminate(self.reports)
        self.reports = None

    def _stop_final(self, desired: Optional[dict]) -> None:
        aborted = False
        if self.oc is not None and self.oc.poll() is None:
            aborted = self._abort_busy()
            self._terminate(self.oc)
        self.oc = None
        self._stop_reports()
        changed, err = self.snapshot()
        since = (desired or {}).get("since") if desired else None
        self.final_since = since
        self.log("已停止:opencode 已退出,最终快照 %d 个库有变化%s" % (len(changed), "(%s)" % err if err else ""))
        self._write_ack(since=since, running=False, final=True, aborted_running=aborted,
                        changed=changed, error=err)

    # -- 快照 -----------------------------------------------------------------

    def snapshot(self) -> "tuple[list, str]":
        """给 state 卷上每个库生成一致性快照;没变的跳过(M1)。返回 (有变化的库, 错误摘要)。"""
        changed: List[str] = []
        errors: List[str] = []
        for sub in POOL_LOCALIZED:
            root = self.cfg.state / sub
            if not root.is_dir() or root.is_symlink():
                continue
            for cur, _dirs, files in os.walk(root):     # 不跟随目录链接
                for name in files:
                    if not _is_db(name) or name.endswith(_SIDECAR_SUFFIXES):
                        continue
                    src = pathlib.Path(cur) / name
                    if src.is_symlink():
                        continue
                    rel = src.relative_to(self.cfg.state).as_posix()
                    dst = self.cfg.state / SNAP_DIR / rel
                    try:
                        self._checkpoint(src)
                        sig = statesync._db_signature(src)
                        if statesync._db_unchanged(sig, self.record.get(rel), dst):
                            continue
                        statesync.copy_db(src, dst)
                        os.chmod(dst, self.SNAP_MODE)   # 快照会原样成为 NAS 上的库与退出备份(管家写到 NAS 时另定 mode)
                        self.record[rel] = sig
                        changed.append(rel)
                    except (OSError, sqlite3.Error) as exc:
                        errors.append("%s:%s" % (rel, exc))
        return changed, "; ".join(errors[:3])

    @staticmethod
    def _checkpoint(db: pathlib.Path) -> None:
        """把 WAL 合回主库。遇到读者合不完时 -wal 仍非空,M1 不会跳过(statesync._db_unchanged)。"""
        try:
            con = sqlite3.connect(str(db), timeout=2)
            try:
                con.execute("pragma wal_checkpoint(TRUNCATE)")
            finally:
                con.close()
        except sqlite3.Error:
            pass

    # -- 1 号进程的本分 --------------------------------------------------------

    def _reap_orphans(self) -> None:
        """容器里的 1 号进程要收养并回收孤儿进程(模型 nohup 起的后台命令退出后会挂在这里成僵尸)。
        只回收不是自己 Popen 出来的子进程——那两个留给 Popen.poll,否则拿不到真实退出码。"""
        if os.getpid() != 1 or not hasattr(os, "waitid"):
            return
        mine = {p.pid for p in (self.oc, self.reports) if p is not None and p.returncode is None}
        while True:
            try:
                info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return
            if info is None or info.si_pid in mine:
                return
            try:
                os.waitpid(info.si_pid, 0)
            except ChildProcessError:
                pass

