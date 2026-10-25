"""固定池化槽位的 1 号进程(slotrun,2026-10-23)。用假 opencode(tests/fake_opencode.py)驱动真进程。

槽位里跑的是会执行命令的模型,上一个人、同 Pod 的别人都可能在盘上 / 端口上动手脚。
这里核对的是每一道防线真的在:清盘先于一切、旧指令不认、端口不空着、凭证只认本次、回写前权限规整。
"""
import base64
import errno
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import podctl  # noqa: E402
import poolrun  # noqa: E402
import slotrun  # noqa: E402

FAKE = pathlib.Path(__file__).with_name("fake_opencode.py")
PW1, PW2 = "a" * 24, "b" * 24
RT = "r" * 32


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _podctl_cmd() -> list:
    """报告服务的命令。slotrun 不让 PYTHONPATH 传下去,而 scripts/linux-pytest.sh 的依赖(yaml)正是靠它进来的
    (镜像里装在系统 site-packages,不受影响):测试显式把自己的依赖路径交给这个子进程。"""
    cmd = [sys.executable, str(_ROOT / "docker" / "podctl.py")]
    sp = os.environ.get("PYTHONPATH")
    return ["env", "PYTHONPATH=" + sp] + cmd if sp else cmd


def _mode(p: pathlib.Path) -> int:
    return stat.S_IMODE(p.lstat().st_mode)


class Rig:
    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        self.ctl, self.kb, self.out = tmp / "ctl", tmp / "kb", tmp / "fake-out"
        self.data, self.tmpdir, self.shm = tmp / "data", tmp / "tmpfs", tmp / "shm"
        self.state = self.data / "state"
        self.agent = tmp / "agent"
        for p in (self.ctl, self.kb, self.state, self.tmpdir, self.shm):
            p.mkdir(parents=True)
        skills = self.agent / "config" / "opencode" / "skills" / "gaussdb-x"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("x", encoding="utf-8")
        (self.agent / "config" / "opencode" / "AGENTS.md").write_text("rules", encoding="utf-8")
        os.symlink("skills/gaussdb-x/SKILL.md", self.agent / "config" / "opencode" / "link.md")
        rg = self.agent / "cache" / "opencode" / "bin" / "rg"
        rg.parent.mkdir(parents=True)
        rg.write_text("#!/bin/sh\n", encoding="utf-8")
        rg.chmod(0o755)
        run = poolrun.Config(ctl_dir=self.ctl, state=self.state, kb_dir=self.kb,
                             opencode_cmd=[sys.executable, str(FAKE)],
                             podctl_cmd=_podctl_cmd(),
                             port=_free_port(), reports_port=_free_port(), poll_s=0.05,
                             stop_timeout_s=5, health_every_s=0.2, env_file=self.data / "env.sh")
        self.cp = pathlib.Path(tempfile.mkdtemp(dir="/tmp", prefix="sr"))   # macOS 套接字路径上限 104 字节
        self.scfg = slotrun.SlotConfig(slot=0, run=run, data=self.data, agent=self.agent,
                                       wipe=(self.data, self.tmpdir, self.shm), busy_every_s=0.1,
                                       credproxy_dir=self.cp, model_port=_free_port())
        self.env = dict(os.environ, FAKE_OUT=str(self.out), GRMP_API_HOST="10.0.0.1", GRMP_API_PORT="8080",
                        MODEL_BASE_URL="http://model", MODEL_API_KEY="k", MODEL_ID="m",
                        GRMP_AUTH_TOKEN="pod-level-token", PYTHONPATH="/evil", SANDBOX_CONTROL_KEY="ck",
                        GRMP_SM2_PRIVATE_KEY="ab" * 32)
        self.logs = []
        self.sup = None

    def start(self):
        self.sup = slotrun.SlotSupervisor(self.scfg, env=self.env, log=self.logs.append)
        return self.sup.boot()

    @property
    def cfg(self):
        return self.scfg.run

    def run(self, pw=PW1, since=111.0, state="run", boot=None, **extra):
        body = {"state": state, "since": since, "user_id": "u1234", "password": pw, "reports_token": RT,
                "boot": self.sup.boot_id if boot is None else boot}
        body.update(extra)
        (self.ctl / "run.json").write_text(json.dumps(body), encoding="utf-8")

    def snap(self, seq, boot=None):
        (self.ctl / "snap.json").write_text(json.dumps({"seq": seq, "boot": self.sup.boot_id if boot is None else boot}),
                                            encoding="utf-8")

    def ack(self) -> dict:
        return json.loads((self.state / ".pool" / "ack.json").read_text(encoding="utf-8"))

    def fake_env(self) -> dict:
        return json.loads((self.out / "env.json").read_text(encoding="utf-8"))

    def until(self, cond, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sup.tick()
            if cond():
                return True
            time.sleep(0.05)
        raise AssertionError("超时;日志:%s" % self.logs)

    def healthy(self, pw=PW1) -> bool:
        req = urllib.request.Request("http://127.0.0.1:%d/global/health" % self.cfg.port)
        req.add_header("Authorization", "Basic " + base64.b64encode(("opencode:" + pw).encode()).decode())
        try:
            urllib.request.urlopen(req, timeout=1).read()
            return True
        except OSError:
            return False

    def reports(self, token=None):
        req = urllib.request.Request("http://127.0.0.1:%d/whoami.json" % self.cfg.reports_port)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=1) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except OSError:
            return None, None

    def close(self):
        if self.sup is not None:
            self.sup.term = True
            self.sup.tick()
            self.sup._close_placeholders()
            if self.sup.relay is not None:
                self.sup.relay.close()


@pytest.fixture()
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _bind_fails(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError as exc:
        return exc.errno == errno.EADDRINUSE
    finally:
        s.close()


# -- 启动清盘 -------------------------------------------------------------------------------

def test_boot_wipes_what_the_previous_user_left_before_installing_config(rig):
    planted = [rig.data / "home" / ".gitconfig", rig.data / "config" / "opencode" / "plugin" / "evil.js",
               rig.state / "xdg-data" / "opencode" / "opencode.db", rig.state / "gdaa" / "sitecustomize.py",
               rig.state / "workspace" / "AGENTS.md", rig.tmpdir / "x.pth", rig.shm / "sem.evil"]
    for p in planted:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("evil", encoding="utf-8")
    locked = rig.state / "reports" / "locked"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x", encoding="utf-8")
    locked.chmod(0o500)
    sealed = rig.data / "sealed"
    sealed.mkdir()
    (sealed / "f").write_text("x", encoding="utf-8")
    sealed.chmod(0o000)
    os.symlink("/etc", rig.data / "home-link")
    report = rig.start()
    for p in planted + [locked, sealed, rig.data / "home-link"]:
        assert not os.path.lexists(p), "上一个人的 %s 还在" % p
    assert report["state_empty"] and not report["wiped"]["errors"] and report["wiped"]["deleted"] >= len(planted)
    cfg = rig.data / "config" / "opencode"
    assert (cfg / "AGENTS.md").read_text(encoding="utf-8") == "rules", "清盘在拷配置之前:拷进来的不能被清掉"
    assert os.readlink(cfg / "link.md") == "skills/gaussdb-x/SKILL.md", "按 cp -R 的语义:链接原样保留"
    assert os.access(rig.data / "cache" / "opencode" / "bin" / "rg", os.X_OK)
    oc = json.loads((rig.data / "oc" / "opencode.json").read_text(encoding="utf-8"))
    assert oc["model"].endswith("/m") and _mode(rig.data / "oc" / "opencode.json") == 0o600


def test_boot_creates_its_own_group_shared_top_dirs_and_a_boot_record(rig):
    report = rig.start()
    for sub in slotrun.TOP_DIRS:
        p = rig.state / sub
        assert p.is_dir() and p.stat().st_uid == os.getuid() and _mode(p) & 0o777 == 0o770, sub
    rec = rig.state / slotrun.BOOT_FILE
    assert json.loads(rec.read_text(encoding="utf-8"))["boot"] == rig.sup.boot_id == report["boot"]
    assert _mode(rec) == 0o640 and _mode(rig.state / poolrun.ACK_FILE) == 0o640
    assert rig.ack()["boot"] == rig.sup.boot_id and not rig.ack()["running"]


def test_each_boot_gets_a_fresh_id(rig):
    rig.start()
    first = rig.sup.boot_id
    rig.close()
    rig.start()
    assert rig.sup.boot_id != first and len(first) == 16


# -- 指令只认本次启动 -------------------------------------------------------------------------

def test_commands_addressed_to_an_earlier_boot_are_ignored(rig):
    rig.start()
    rig.run(boot="0123456789abcdef")
    rig.snap(5, boot="0123456789abcdef")
    for _ in range(5):
        assert rig.sup.tick()
    assert rig.sup.oc is None and rig.ack()["snap_seq"] == 0, "上一个人的 run.json / snap.json 不能起作用"
    rig.run()
    rig.until(rig.healthy)


def test_exit_command_ends_pid_1(rig):
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    rig.run(state="exit", since=222.0)
    assert rig.sup.tick() is False
    assert rig.sup.oc is None and not rig.healthy() and not rig.sup.placeholders


def test_hello_is_answered_with_the_nonce_and_boot(rig):
    rig.start()
    (rig.ctl / slotrun.HELLO_FILE).write_text(json.dumps({"nonce": "n-1"}), encoding="utf-8")
    rig.sup.tick()
    a = rig.ack()
    assert a["hello"] == "n-1" and a["boot"] == rig.sup.boot_id
    (rig.ctl / slotrun.HELLO_FILE).write_text(json.dumps({"nonce": "x" * 500}), encoding="utf-8")
    rig.sup.tick()
    assert rig.ack()["hello"] == "n-1", "超长 nonce 不回"


# -- 凭证只认本次 run.json ----------------------------------------------------------------------

def test_run_gets_only_the_per_user_credentials(rig):
    """2026-10-23 凭据代理:中间件令牌、SM2 私钥、模型 Key 都不进槽位(哪怕指令里夹带了令牌),
    opencode 只拿到本槽位的模型中继地址与管家的套接字路径。"""
    rig.start()
    rig.run(grmp_token="grmp-of-u1234")
    rig.until(rig.healthy)
    env = rig.fake_env()
    assert env["GRMP_AUTH_TOKEN"] is None and env["MODEL_API_KEY"] is None and env["GRMP_SM2_PRIVATE_KEY"] is None
    assert env["MODEL_PROXY_URL"] == "http://127.0.0.1:%d/model" % rig.scfg.model_port
    assert env["GRMP_PROXY_SOCKET"] == str(rig.cp / "credproxy.sock") and env["REPORTS_TOKEN"] == RT
    assert env["OPENCODE_SERVER_PASSWORD"] == PW1 and env["SANDBOX_CONTROL_KEY"] is None
    assert env["PYTHONPATH"] is None, "python 的注入口不继承"
    assert env["HOME"] == str(rig.data / "home") and env["OPENCODE_CONFIG"] == str(rig.data / "oc" / "opencode.json")


def test_the_model_port_is_held_from_boot_and_relayed_to_the_stewards_proxy(rig):
    """opencode 经 127.0.0.1:<模型端口> 访问模型:1 号进程从启动起就占着这个端口(同 Pod 的别人抢不走),
    原样转到管家开给本槽位的模型套接字 —— Key 在那边加。"""
    import http.server
    import socketserver

    class _U(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def address_string(self):
            return "unix"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"path": self.path, "auth": self.headers.get("Authorization")}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = _U(str(rig.cp / "model.sock"), H)             # 管家开的模型套接字(只转模型;中间件的那个不经 TCP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rig.start()
        assert _bind_fails(rig.scfg.model_port), "启动就占住模型端口"
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", rig.scfg.model_port, timeout=5)
        c.request("POST", "/model/chat/completions", body=b"{}", headers={"Authorization": "Bearer credproxy"})
        assert json.loads(c.getresponse().read()) == {"path": "/model/chat/completions", "auth": "Bearer credproxy"}
        c.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_without_a_per_user_grmp_token_the_pod_level_one_is_not_used(rig):
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    assert rig.fake_env()["GRMP_AUTH_TOKEN"] is None


def test_run_without_a_reports_token_is_refused(rig):
    rig.start()
    rig.run(reports_token="short")
    rig.sup.tick()
    assert rig.sup.oc is None and rig.ack()["error"] == "bad_run"
    assert _bind_fails(rig.cfg.port), "拒绝启动时端口仍要占着"


def test_reports_port_needs_the_reports_token(rig):
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    rig.until(lambda: rig.reports(RT)[0] == 200)
    assert rig.reports(RT) == (200, {"user_id": "u1234"})
    assert rig.reports()[0] == 401 and rig.reports(PW1)[0] == 401, "同 Pod 的别人连得上 localhost,没令牌一律 401"


def test_run_stop_run_rotates_the_password(rig):
    rig.start()
    rig.run(pw=PW1, since=1.0)
    rig.until(rig.healthy)
    rig.run(pw=PW1, since=1.0, state="stop")
    rig.until(lambda: rig.ack()["final"])
    assert not rig.healthy() and rig.ack()["since"] == 1.0
    rig.run(pw=PW2, since=2.0)
    rig.until(lambda: rig.healthy(PW2))
    assert not rig.healthy(PW1), "恢复后旧口令必须失效"


# -- 端口不空着 -----------------------------------------------------------------------------

def test_placeholders_hold_both_ports_whenever_opencode_is_not_running(rig):
    rig.start()
    assert _bind_fails(rig.cfg.port) and _bind_fails(rig.cfg.reports_port)
    c = socket.create_connection(("127.0.0.1", rig.cfg.port), timeout=2)
    c.sendall(b"GET /global/health HTTP/1.1\r\nHost: x\r\n\r\n")
    c.settimeout(0.1)
    reply, deadline = b"", time.monotonic() + 5
    while not reply.endswith(b"}") and time.monotonic() < deadline:
        rig.sup.tick()                  # connect() 返回时服务端那头不一定已经进了 accept 队列
        try:
            reply += c.recv(4096)
        except socket.timeout:
            pass
    c.close()
    assert reply.startswith(b"HTTP/1.1 503 ") and reply.endswith(b'{"reason": "slot_not_running"}'), \
        "占位监听回 503 并正常关闭(RST 会让 kubectl port-forward 整条断掉)"
    head, _sep, payload = reply.partition(b"\r\n\r\n")
    assert b"Content-Length: %d" % len(payload) in head
    rig.run()
    rig.until(rig.healthy)
    assert not rig.sup.placeholders
    rig.run(state="stop")
    rig.until(lambda: rig.ack()["final"])
    assert _bind_fails(rig.cfg.port) and _bind_fails(rig.cfg.reports_port), "保留态端口要重新占住"


def test_a_port_taken_at_boot_is_taken_back_once_the_squatter_is_gone(rig):
    """槽位重启的空档里同 Pod 的别人先占了端口:1 号进程每轮再试,对方被腾挪走之后占回来。"""
    squat = socket.socket()
    squat.bind(("0.0.0.0", rig.cfg.port))
    squat.listen(1)
    rig.start()
    assert rig.cfg.port not in rig.sup.placeholders and rig.cfg.reports_port in rig.sup.placeholders
    rig.sup.tick()
    assert sum("占不住端口" in m for m in rig.logs) == 1, "每轮都再试,但只在第一次失败时说"
    squat.close()
    rig.sup.tick()
    assert rig.cfg.port in rig.sup.placeholders and any("占回来" in m for m in rig.logs)


def test_a_crashed_opencode_gets_the_placeholder_back_until_restart(rig):
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    rig.sup.oc.kill()
    rig.sup.oc.wait()
    rig.sup.tick()
    assert rig.sup.oc is None and rig.cfg.port in rig.sup.placeholders
    rig.until(rig.healthy)


# -- 快照与权限 -----------------------------------------------------------------------------

def test_snapshot_and_files_are_made_group_readable_before_the_ack(rig):
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    sess = rig.state / "gdaa" / "sessions" / "s1"
    sess.mkdir(parents=True)
    (sess / "meta.json").write_text("{}", encoding="utf-8")
    (sess / "meta.json").chmod(0o600)
    sess.chmod(0o700)                   # 技能每次运行都这么做
    rig.snap(1)
    rig.until(lambda: rig.ack()["snap_seq"] == 1)
    snap = rig.state / poolrun.SNAP_DIR / "xdg-data" / "opencode" / "opencode.db"
    assert snap.is_file() and _mode(snap) == 0o640
    assert _mode(sess) & 0o050 == 0o050 and _mode(sess / "meta.json") & 0o040 == 0o040
    assert "xdg-data/opencode/opencode.db" in rig.ack()["changed"]


def test_final_stop_normalizes_too(rig):
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    (rig.state / "reports" / "r.html").write_text("x", encoding="utf-8")
    (rig.state / "reports" / "r.html").chmod(0o600)
    rig.run(state="stop")
    rig.until(lambda: rig.ack()["final"])
    assert _mode(rig.state / "reports" / "r.html") & 0o040


def test_sigterm_stops_and_writes_the_final_ack(rig):
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    rig.sup.term = True
    assert rig.sup.tick() is False
    a = rig.ack()
    assert a["final"] and not a["running"] and (rig.out / "terminated").exists()


# -- 会话忙 ---------------------------------------------------------------------------------

def test_busy_sessions_are_reported_in_the_ack(rig):
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    (rig.out / "busy").write_text("1", encoding="utf-8")
    rig.until(lambda: rig.ack()["busy"])
    assert rig.ack()["busy_at"] > 0
    (rig.out / "busy").unlink()
    rig.until(lambda: not rig.ack()["busy"])


# -- 配置 -----------------------------------------------------------------------------------

def test_config_from_env():
    c = slotrun.SlotConfig.from_env({"SLOT_INDEX": "3", "POOL_WIPE": "/data:/tmp"})
    assert (c.run.port, c.run.reports_port) == (4130, 4131) and c.slot == 3
    assert c.run.podctl_cmd[1:3] == ["-E", "-s"] and c.run.podctl_cmd[-1] == "/opt/agent/podctl.py"
    assert c.wipe == (pathlib.Path("/data"), pathlib.Path("/tmp"))
    for bad in ("", "-1", "64", "1x"):
        with pytest.raises(podctl.ConfigError):
            slotrun.SlotConfig.from_env({"SLOT_INDEX": bad})


def test_slot_ports_never_collide_with_each_other_or_the_ondemand_ports():
    ports = [p for i in range(slotrun.MAX_SLOT + 1) for p in slotrun.slot_ports(i)]
    assert len(set(ports)) == len(ports) and not set(ports) & {4096, 4097, 4098}


def test_ondemand_pool_keeps_its_private_modes():
    assert poolrun.Supervisor.GDAA_MODE == 0o700 and poolrun.Supervisor.SNAP_MODE == 0o600
    assert not poolrun.Supervisor.GIT_ENV


def test_kill_others_only_as_pid_1(rig, monkeypatch):
    """kill(-1) 只在 1 号进程里做:开发机上跑测试时它会杀掉本用户的所有进程。
    (Linux 测试容器里 pytest 自己就是 1 号进程,那里其它用例真的走了 kill(-1)。)"""
    calls = []
    rig.start()
    monkeypatch.setattr(slotrun.os, "kill", lambda *a: calls.append(a))
    monkeypatch.setattr(slotrun.os, "getpid", lambda: 4242)
    rig.sup._kill_others()
    assert not calls
    monkeypatch.setattr(slotrun.os, "getpid", lambda: 1)
    monkeypatch.setattr(slotrun.os, "waitpid", lambda *a: (_ for _ in ()).throw(ChildProcessError()))
    rig.sup._kill_others()
    assert calls == [(-1, slotrun.signal.SIGKILL)]


def test_stop_kills_background_processes_left_by_the_model(rig):
    if os.getpid() != 1:
        pytest.skip("只有 1 号进程才 kill(-1);scripts/linux-pytest.sh 的容器里 pytest 是 1 号进程")
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    bg = subprocess.Popen(["sleep", "300"], start_new_session=True)     # 模型 nohup / setsid 起的后台命令
    rig.run(state="stop")
    rig.until(lambda: rig.ack()["final"])
    with pytest.raises(ProcessLookupError):
        os.kill(bg.pid, 0)


def test_a_new_run_adopts_what_the_steward_loaded(rig, monkeypatch):
    """管家载入的文件属主是 uid 1000;非 root 造不出别人的文件,这里只核对每次新的 run 都在起 opencode 之前接管。
    (接管本身的语义见 test_fdtree_pool_units 的 adopt 用例。)"""
    seen = []
    real = slotrun.fdtree.adopt

    def spy(fd, **kw):
        seen.append(((rig.out / "env.json").exists(), kw.get("keep_file")))
        return real(fd, **kw)

    monkeypatch.setattr(slotrun.fdtree, "adopt", spy)
    rig.start()
    rig.run()
    rig.until(rig.healthy)
    assert len(seen) >= len(poolrun.POOL_LOCALIZED)
    started_before, keep = seen[0]
    assert not started_before, "要在起 opencode 之前接管"
    assert keep("opencode.db") and not keep("a.json"), "库文件不复制"
