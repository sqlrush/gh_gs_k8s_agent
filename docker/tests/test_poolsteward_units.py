"""固定池化的管家(poolsteward + poolslot,2026-10-23)。

真的槽位守护进程(slotrun,线程里 tick)+ 假 opencode(tests/fake_opencode.py)+ 管家,目录全在 tmp 里。
「容器重启」由测试台模拟:换一个新的 SlotSupervisor(新 boot)在同一组目录上启动,containerID 换一个。

同一个测试进程里跑几个槽位,和生产不一样的两处已关掉:kill(-1)(生产里每个槽位有自己的进程空间)
与收养孤儿(会把别的槽位的子进程收走)。
"""
import base64
import http.client
import http.server
import json
import os
import pathlib
import shutil
import socket
import socketserver
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import credproxy  # noqa: E402
import fdtree  # noqa: E402
import podctl  # noqa: E402
import poolauth  # noqa: E402
import poolrun  # noqa: E402
import poolslot  # noqa: E402
import poolsteward  # noqa: E402
import poolsync  # noqa: E402
import slotrun  # noqa: E402

GRMP_PREFIX = "/example/grmp/diagnostic/agent"      # 部署时配置的中间件路径前缀(GRMP_PATH_PREFIX);单测用样例

FAKE = pathlib.Path(__file__).with_name("fake_opencode.py")
KEY = b"c" * 32
POD = "pool-0"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _podctl_cmd() -> list:
    cmd = [sys.executable, str(_ROOT / "docker" / "podctl.py")]
    sp = os.environ.get("PYTHONPATH")
    return ["env", "PYTHONPATH=" + sp] + cmd if sp else cmd


def _rows(path: pathlib.Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("select count(*) from t").fetchone()[0]
    finally:
        con.close()


def _wait(cond, timeout=15.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


class SlotRuntime:
    """一个槽位容器:SlotSupervisor 在线程里 tick;exit 之后按 kubelet 的样子重启(新 boot、新 containerID)。"""

    def __init__(self, pod: "Pod", i: int):
        self.pod, self.i = pod, i
        self.restarts = 0
        self.model_port = _free_port()      # 生产里是 4102+10i;同一测试进程里换一个空闲的
        self.auto_restart = True
        self.sup = None
        self.thread = None
        self.running = False
        self._start()

    @property
    def container_id(self) -> str:
        return "c-%d-%d" % (self.i, self.restarts)

    def _start(self):
        p = self.pod
        run = poolrun.Config(ctl_dir=p.slot_dir(self.i) / "ctl", state=p.slot_dir(self.i) / "state", kb_dir=p.kb,
                             opencode_cmd=[sys.executable, str(FAKE)], podctl_cmd=_podctl_cmd(),
                             port=p.ports[self.i][0], reports_port=p.ports[self.i][1], poll_s=0.05,
                             stop_timeout_s=5, health_every_s=1, env_file=p.slot_dir(self.i) / "data" / "env.sh")
        scfg = slotrun.SlotConfig(slot=self.i, run=run, data=p.slot_dir(self.i) / "data", agent=p.agent,
                                  wipe=(p.slot_dir(self.i) / "data",), busy_every_s=0.2,
                                  credproxy_dir=p.slot_dir(self.i) / "sock", model_port=self.model_port)
        env = dict(os.environ, FAKE_OUT=str(p.tmp / ("fake-%d" % self.i)), GRMP_API_HOST="10.0.0.1",
                   MODEL_BASE_URL="http://model", MODEL_API_KEY="k", MODEL_ID="m")
        self.sup = slotrun.SlotSupervisor(scfg, env=env, log=p.logs.append)
        self.sup.boot()
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()                           # 先起再挂上:收尾时 stop() 可能正好来 join(自动重启的竞态)
        self.thread = t

    def _loop(self):
        while self.running:
            if not self.sup.tick():
                self._release()
                self.running = False
                if self.auto_restart and not self.pod.closed:
                    self.restart()
                return
            time.sleep(0.05)

    def restart(self):
        """容器重启:旧进程没了(这里先让它收尾),新进程清盘、换 boot。"""
        if self.running:
            self.running = False
            self.thread.join(5)
            self.sup._terminate(self.sup.oc)
            self.sup._stop_reports()
            self._release()
        self.restarts += 1
        self._start()

    def _release(self):
        self.sup._close_placeholders()
        if self.sup.relay is not None:
            self.sup.relay.close()
            self.sup.relay = None

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join(5)
        if self.sup is not None:
            self.sup._terminate(self.sup.oc)
            self.sup._stop_reports()
            self._release()


class Pod:
    def __init__(self, tmp: pathlib.Path, n: int = 2, verdict=None, upstreams=None, **settings):
        self.tmp, self.n, self.closed = tmp, n, False
        self.upstreams = upstreams
        self.users, self.priv, self.kb, self.agent = tmp / "users", tmp / "priv", tmp / "kb", tmp / "agent"
        for p in (self.users, self.priv, self.kb, self.agent / "config" / "opencode"):
            p.mkdir(parents=True)
        (self.agent / "config" / "opencode" / "AGENTS.md").write_text("rules", encoding="utf-8")
        self.ports = {i: (_free_port(), _free_port()) for i in range(n)}
        for i in range(n):
            for sub in ("ctl", "state", "data", "sock"):
                (self.slot_dir(i) / sub).mkdir(parents=True)
        self.logs = []
        self.slots = [SlotRuntime(self, i) for i in range(n)]
        base = dict(sync_seconds=3600, ready_seconds=15, stop_wait_seconds=8, snap_wait_seconds=5, hello_seconds=5,
                    owner_refresh_seconds=3600, evict_timeout_seconds=3600)
        base.update(settings)
        self.settings = poolslot.Settings(pod=POD, **base)
        self.verdict = verdict
        self.st = None
        self.new_steward()

    def slot_dir(self, i: int) -> pathlib.Path:
        return self.tmp / "slots" / str(i)

    def new_steward(self, start: bool = True):
        if self.st is not None:
            self.st.stop.set()
        cfg = poolsteward.Config(key=KEY, pod=POD, slots=self.n, users_dir=self.users, slots_dir=self.tmp / "slots",
                                 priv_dir=self.priv, port=_free_port(), monitor_seconds=0.1, settings=self.settings,
                                 slot_ports=lambda i: self.ports[i], slot_uid=lambda i: os.getuid(),
                                 slot_model_port=lambda i: self.slots[i].model_port)
        self.st = poolsteward.Steward(cfg, log=self.logs.append, verdict=self.verdict, upstreams=self.upstreams)
        if start:
            self.st.start()
        return self.st

    def all_free(self):
        assert _wait(lambda: all(s.state == poolslot.FREE for s in self.st.slots)), self.logs[-20:]

    def bind(self, slot=0, user="u1234", grmp="", expect=None):
        body = {"slot": slot, "user_id": user, "expect_in_use": self.st.in_use() if expect is None else expect,
                "container_id": self.slots[slot].container_id}
        if grmp:
            body["grmp_token"] = poolauth.seal(KEY, grmp)
        return self.st.bind(body)

    def resume(self, slot=0, user="u1234", expect=None):
        return self.st.resume({"slot": slot, "user_id": user, "container_id": self.slots[slot].container_id,
                               "expect_in_use": self.st.in_use() if expect is None else expect})

    def user_fd(self, user="u1234") -> int:
        root = fdtree.open_root(self.users)
        try:
            return poolsync.open_user(root, user)
        finally:
            os.close(root)

    def owner(self, user="u1234") -> dict:
        try:
            return json.loads((self.users / user / poolsync.OWNER).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def close(self):
        self.closed = True
        if self.st is not None:
            self.st.stop.set()
            for srv in self.st._proxies:
                srv.shutdown()
                srv.server_close()
        for s in self.slots:
            s.stop()


def _healthy(port: int, pw: str) -> bool:
    req = urllib.request.Request("http://127.0.0.1:%d/global/health" % port)
    req.add_header("Authorization", "Basic " + base64.b64encode(("opencode:" + pw).encode()).decode())
    try:
        urllib.request.urlopen(req, timeout=1).read()
        return True
    except OSError:
        return False


@pytest.fixture()
def pod(tmp_path, monkeypatch):
    monkeypatch.setattr(slotrun.SlotSupervisor, "_kill_others", lambda self: None)
    monkeypatch.setattr(slotrun.SlotSupervisor, "_reap_orphans", lambda self: None)
    p = Pod(tmp_path)
    p.all_free()
    yield p
    p.close()


class _TCP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture()
def grmp_upstream():
    """假中间件:记下收到的请求头与报文。"""
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append(({k.lower(): v for k, v in self.headers.items()}, json.loads(body)))
            out = b'{"code": "0"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = _TCP(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], seen
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def proxied_pod(monkeypatch, grmp_upstream):
    """带凭据代理的 Pod。目录放 /tmp 下:macOS 套接字路径上限 104 字节。"""
    monkeypatch.setattr(slotrun.SlotSupervisor, "_kill_others", lambda self: None)
    monkeypatch.setattr(slotrun.SlotSupervisor, "_reap_orphans", lambda self: None)
    port, seen = grmp_upstream
    ups = credproxy.Upstreams.from_env({"GRMP_API_HOST": "127.0.0.1", "GRMP_API_PORT": str(port),
                                        "GRMP_PATH_PREFIX": GRMP_PREFIX,
                                        "GRMP_USER_ID_HEADER": "X-User-Id", "MODEL_BASE_URL": "http://127.0.0.1:9/v1",
                                        "MODEL_API_KEY": "mk", "AGENT_SKILLS_DIR": str(_ROOT / "agent")})
    p = Pod(pathlib.Path(tempfile.mkdtemp(dir="/tmp", prefix="ps")), upstreams=ups)
    p.all_free()
    yield p, seen
    p.close()


def _grmp_via(sock: pathlib.Path):
    c = http.client.HTTPConnection("x", timeout=10)
    c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.sock.connect(str(sock))
    try:
        c.request("POST", GRMP_PREFIX + "/common-operations", body=b'{"dataIp": "10.0.0.9"}',
                  headers={"Content-Type": "application/json", "X-User-Id": "u9999"})
        r = c.getresponse()
        return r.status, json.loads(r.read())
    finally:
        c.close()


# -- 新 Pod ----------------------------------------------------------------------------------

def test_fresh_pod_slots_handshake_into_free(pod):
    st = pod.st.status()
    assert st["free"] == 2 and st["in_use"] == 0 and st["pod"] == POD
    for s in pod.st.slots:
        assert s.rec["boot"] == pod.slots[s.i].sup.boot_id and s.rec["quiet"]


# -- 绑定 ------------------------------------------------------------------------------------

def test_bind_starts_the_users_opencode_with_per_user_credentials(pod):
    code, body = pod.bind(grmp="grmp-of-u1234")
    assert code == 200, body
    pw, rt = body["opencode"]["password"], body["reports"]["token"]
    assert _healthy(body["opencode"]["port"], pw)
    req = urllib.request.Request("http://127.0.0.1:%d/whoami.json" % body["reports"]["port"])
    req.add_header("Authorization", "Bearer " + rt)
    assert json.loads(urllib.request.urlopen(req, timeout=2).read()) == {"user_id": "u1234"}
    env = json.loads((pod.tmp / "fake-0" / "env.json").read_text(encoding="utf-8"))
    assert env["GRMP_AUTH_TOKEN"] is None and env["MODEL_API_KEY"] is None and env["GSDB_USER_ID"] == "u1234"
    assert pod.st.slots[0].credentials() == ("u1234", "grmp-of-u1234"), "令牌留在管家,给凭据代理用"
    assert "grmp-of-u1234" not in (pod.slot_dir(0) / "ctl" / "run.json").read_text(encoding="utf-8")
    assert pod.owner()["pod"] == "%s/0" % POD, "独占标记按「pod/槽位」整串记"
    fd = pod.user_fd()
    assert poolsync.gen_read(fd) == pod.st.slots[0].rec["gen"] and poolsync.is_dirty(fd)
    os.close(fd)
    text = json.dumps(pod.st.status())
    for secret in (pw, rt, "grmp-of-u1234"):
        assert secret not in text, "status 不能带秘密"


def test_bind_is_idempotent_for_the_same_user_and_cas_guards_the_pod(pod):
    code, first = pod.bind()
    assert code == 200
    code, again = pod.bind(expect=7)
    assert code == 200 and again["idempotent"] and again["opencode"]["password"] == first["opencode"]["password"]
    code, body = pod.bind(slot=1, user="u2000", expect=0)
    assert code == 409 and body["reason"] == "pod_changed" and body["in_use"] == 1, "网关以为 Pod 是空的,其实已有 1 人"
    code, _ = pod.bind(slot=1, user="u2000", expect=1)
    assert code == 200


def test_bind_refuses_a_user_held_elsewhere_without_touching_the_slot(pod):
    fd = pod.user_fd()
    assert poolsync.owner_acquire(fd, "pool-7/3")[0]
    os.close(fd)
    code, body = pod.bind()
    assert code == 409 and body["reason"] == "owner_busy" and body["holder"] == "pool-7/3"
    assert pod.st.slots[0].state == poolslot.FREE


def test_bind_refuses_a_plaintext_grmp_token(pod):
    code, body = pod.st.bind({"slot": 0, "user_id": "u1234", "expect_in_use": 0, "grmp_token": "plain"})
    assert code == 400 and body["reason"] == "bad_token"


# -- 转保留与恢复 ---------------------------------------------------------------------------------

def _retain(pod, slot=0, user="u1234"):
    return pod.st.retain({"slot": slot, "user_id": user})


def test_retain_writes_everything_back_and_releases_the_owner(pod):
    code, b = pod.bind()
    assert code == 200
    code, body = _retain(pod)
    assert code == 200 and body["state"] == poolslot.RETAINED and not body["dirty"], body
    nas_db = pod.users / "u1234" / "xdg-data" / "opencode" / "opencode.db"
    assert _rows(nas_db) == 2, "停之前最后一段对话也写回了"
    assert pod.owner() == {} and not poolsync.is_dirty(pod.user_fd())
    s = pod.st.slots[0]
    assert s.rec["quiet"] and s.rec["fingerprint"] and not _healthy(b["opencode"]["port"], b["opencode"]["password"])
    assert pod.st.in_use() == 0


def test_resume_reuses_the_slot_files_with_a_new_password_and_gen(pod):
    _c, b = pod.bind()
    gen1 = pod.st.slots[0].rec["gen"]
    _retain(pod)
    code, body = pod.resume()
    assert code == 200 and body["resumed"], body
    assert body["opencode"]["password"] != b["opencode"]["password"]
    assert _healthy(body["opencode"]["port"], body["opencode"]["password"])
    assert pod.st.slots[0].rec["gen"] != gen1 and pod.owner()["pod"] == "%s/0" % POD


def test_resume_after_the_user_worked_elsewhere_is_stale(pod):
    pod.bind()
    _retain(pod)
    fd = pod.user_fd()
    assert poolsync.owner_acquire(fd, "ondemand-pod")[0]
    poolsync.gen_rotate(fd)                 # 按需模式的 Pod 取独占时也换令牌
    poolsync.owner_release(fd, "ondemand-pod")
    os.close(fd)
    code, body = pod.resume()
    assert code == 409 and body["reason"] == "stale"
    s = pod.st.slots[0]
    assert s.rec["stale"] and pod.owner() == {}, "对不上就放掉独占,交给网关腾挪"
    assert pod.st.evict({"slot": 0, "container_id": pod.slots[0].container_id})[0] == 202


def test_resume_while_the_user_is_active_elsewhere_is_owner_busy(pod):
    pod.bind()
    _retain(pod)
    fd = pod.user_fd()
    poolsync.owner_acquire(fd, "pool-5/1")
    os.close(fd)
    code, body = pod.resume()
    assert code == 409 and body["reason"] == "owner_busy" and pod.st.slots[0].rec["stale"]


def test_a_dirty_retained_slot_keeps_the_owner_and_refuses_eviction(pod, monkeypatch):
    pod.bind()
    real = poolsync.sync
    monkeypatch.setattr(poolsync, "sync", lambda *a, **kw: poolsync.Moved(errors=["NAS 写满了"]))
    code, body = _retain(pod)
    assert code == 200 and body["dirty"]
    s = pod.st.slots[0]
    assert pod.owner()["pod"] == "%s/0" % POD, "写回不干净:继续持有独占,他到别处登录会被挡回这里"
    code, body = pod.st.evict({"slot": 0, "container_id": pod.slots[0].container_id})
    assert code == 409 and body["reason"] == "not_synced"
    monkeypatch.setattr(poolsync, "sync", real)
    s.next_sync = 0                         # 让后台的重试马上来一轮
    assert _wait(lambda: not s.rec["dirty"]), pod.logs[-10:]
    assert pod.owner() == {}
    assert pod.st.evict({"slot": 0, "container_id": pod.slots[0].container_id})[0] == 202


# -- NAS 上的数据不见了(2026-10-24) -------------------------------------------------------------------
# 测试环境的 NAS 曾放在 Mac 的 /tmp 下,macOS 每天 0 点的 tmp_cleaner 删掉三天没碰过的文件:保留超过三天的人,
# NAS 上的令牌、主库、备份全没了(闲置账号连整个目录都没了)。原来回来时判 stale → 网关腾挪,清掉槽位里
# 唯一完整的那份 → 从空 NAS 载入,空库启动。

def _wipe_nas(pod, user="u1234"):
    """tmp_cleaner 的样子:这个人在 NAS 上的东西整个没了,连目录。"""
    shutil.rmtree(pod.users / user)


def _nas_db(pod, user="u1234") -> pathlib.Path:
    return pod.users / user / "xdg-data" / "opencode" / "opencode.db"


def test_resume_after_the_nas_copy_vanished_restores_it_from_the_retained_slot(pod):
    pod.bind()
    _retain(pod)
    rows = _rows(_nas_db(pod))
    _wipe_nas(pod)
    code, body = pod.resume()
    assert code == 200 and body["resumed"], (body, pod.logs[-5:])
    s = pod.st.slots[0]
    assert not s.rec["stale"] and not s.rec["restore"] and s.state == poolslot.ACTIVE
    assert _rows(_nas_db(pod)) == rows, "库回到了 NAS(就是转保留时那一份)"
    assert list((pod.users / "u1234" / "backup").iterdir()), "退出备份也补回来了"
    fd = pod.user_fd()
    assert poolsync.gen_read(fd) == s.rec["gen"], "写回成功之后换上新令牌"
    os.close(fd)
    assert any("NAS 上的数据不见了" in m for m in pod.logs)
    assert _retain(pod)[0] == 200 and pod.resume()[0] == 200, "之后照常:令牌与指纹都对得上"


def test_a_failed_restore_keeps_the_owner_and_writes_no_gen_until_the_nas_copy_is_back(pod, monkeypatch):
    pod.bind()
    _retain(pod)
    rows = _rows(_nas_db(pod))
    _wipe_nas(pod)
    real = poolsync.sync
    monkeypatch.setattr(poolsync, "sync", lambda *a, **kw: poolsync.Moved(errors=["NAS 写满了"]))
    code, body = pod.resume()
    assert code == 503 and body["reason"] == "not_synced", body
    s = pod.st.slots[0]
    assert s.state == poolslot.RETAINED and s.rec["dirty"] and s.rec["restore"] and not s.rec["stale"]
    assert pod.owner()["pod"] == "%s/0" % POD, "按写回不干净的保留走:继续持有独占,他到别处登录会被挡回这里"
    assert not (pod.users / "u1234" / poolsync.GEN).exists(), "写回成功之前不补令牌"
    assert pod.st.evict({"slot": 0, "container_id": pod.slots[0].container_id})[1]["reason"] == "not_synced"
    monkeypatch.setattr(poolsync, "sync", real)
    s.next_sync = 0                         # 让后台的重试马上来一轮
    assert _wait(lambda: not s.rec["dirty"]), pod.logs[-10:]
    assert _rows(_nas_db(pod)) == rows and not s.rec["restore"] and pod.owner() == {}
    fd = pod.user_fd()
    assert poolsync.gen_read(fd) == s.rec["gen"], "写回补上之后才补令牌,就是下次回来要对的那个"
    os.close(fd)
    assert pod.resume()[0] == 200


def test_gen_gone_but_db_untouched_also_restores_from_the_retained_slot(pod):
    """令牌写在绑定 / 恢复时、库写在会话结束时:会话跨过午夜,tmp_cleaner 会先删令牌、晚一晚才删库,
    同一晚被删的还有别的老文件(这里用 workspace 里的一个文件代表)。库还是转保留时记下的那份 = 没人写过它:
    照样用保留副本恢复。以前判 stale → 腾挪清盘 → 从 NAS 重新载入,那个文件就丢了。"""
    pod.bind()
    (pod.slot_dir(0) / "state" / "workspace" / "notes.md").write_text("会话里写的", encoding="utf-8")
    _retain(pod)
    nas = pod.users / "u1234"
    s = pod.st.slots[0]
    rows = _rows(_nas_db(pod))
    (nas / poolsync.GEN).unlink()
    (nas / "workspace" / "notes.md").unlink()
    fd = pod.user_fd()
    assert poolsync.gen_missing(fd) and list(poolsync.db_fingerprint(fd)) == s.rec["fingerprint"]
    os.close(fd)
    code, body = pod.resume()
    assert code == 200 and body["resumed"], (body, pod.logs[-5:])
    assert not s.rec["stale"] and not s.rec["restore"]
    assert (nas / "workspace" / "notes.md").read_text(encoding="utf-8") == "会话里写的", "被删的老文件补回了 NAS"
    assert _rows(_nas_db(pod)) == rows
    fd = pod.user_fd()
    assert poolsync.gen_read(fd) == s.rec["gen"]
    os.close(fd)
    assert any("NAS 上的数据不见了" in m for m in pod.logs)


@pytest.mark.parametrize("left", ["gen", "db"])
def test_if_the_gen_or_the_db_changed_it_is_still_stale(pod, left):
    """令牌还在但变了、或库变了 → 照旧 stale。

    这也是安全论证:保留期间(干净的转保留已释放独占)谁取得过独占,都先写一个新令牌 —— 按需模式的 Pod 走
    podctl.owner_acquire,别的槽位走 poolsync.gen_rotate;之后他的库就算被清掉,令牌也还在。
    库被别人写过,指纹一定会变(原子改名换 inode;原地写也改纳秒修改时间、这里还改了大小)。"""
    pod.bind()
    _retain(pod)
    nas = pod.users / "u1234"
    assert pod.owner() == {}, "干净的转保留已释放独占"
    if left == "gen":
        assert podctl.owner_acquire(nas, "runtime-u1234-abc", wait_s=0)     # 按需模式的 Pod 用过
        podctl.owner_release(nas, "runtime-u1234-abc")
        _nas_db(pod).unlink()                                                 # 他那段的库后来被清掉了
    else:
        (nas / poolsync.GEN).unlink()
        db = _nas_db(pod)
        db.write_bytes(db.read_bytes() + b"\0")                               # 库还在,但不是记下的那一份
    fd = pod.user_fd()
    if left == "gen":
        assert not poolsync.gen_missing(fd)
    else:
        assert poolsync.gen_missing(fd) and list(poolsync.db_fingerprint(fd)) != pod.st.slots[0].rec["fingerprint"]
    os.close(fd)
    code, body = pod.resume()
    assert code == 409 and body["reason"] == "stale"
    assert pod.st.slots[0].rec["stale"] and not pod.st.slots[0].rec["restore"]


# -- 腾挪与核验 -----------------------------------------------------------------------------------

def test_evict_restarts_the_container_and_verify_needs_a_new_container_id(pod):
    pod.bind()
    _retain(pod)
    old_cid, old_boot = pod.slots[0].container_id, pod.st.slots[0].rec["boot"]
    assert pod.st.evict({"slot": 0, "container_id": old_cid})[0] == 202
    assert _wait(lambda: pod.slots[0].restarts == 1 and pod.slots[0].running)
    code, body = pod.st.verify({"slot": 0, "container_id": old_cid})
    assert code == 409 and body["reason"] == "not_restarted", "containerID 没变不认"
    code, body = pod.st.verify({"slot": 0, "container_id": pod.slots[0].container_id})
    assert code == 200 and body["state"] == poolslot.FREE, body
    s = pod.st.slots[0]
    assert s.rec["boot"] != old_boot and s.rec["user"] is None and s.disk_is_clean()
    assert not (pod.slot_dir(0) / "state" / "xdg-data" / "opencode" / "opencode.db").exists()


def test_a_reserved_slot_is_evicted_and_handed_to_the_waiting_user_only(pod):
    pod.bind(user="u1234")
    _retain(pod)
    assert pod.st.reserve({"slot": 0, "user_id": "u7777", "seconds": 120})[0] == 200
    assert pod.st.evict({"slot": 0, "container_id": pod.slots[0].container_id})[0] == 202
    assert _wait(lambda: pod.slots[0].restarts == 1 and pod.slots[0].running)
    assert _wait(lambda: pod.st.verify({"slot": 0, "container_id": pod.slots[0].container_id})[0] == 200)
    assert pod.st.status()["slots"][0]["reserved_for"] == "u7777", "核验后预留照旧"
    code, body = pod.bind(slot=0, user="u8888")
    assert (code, body["reason"]) == (409, "reserved")
    assert pod.bind(slot=0, user="u7777")[0] == 200
    assert pod.st.status()["slots"][0]["reserved_for"] is None, "等的人绑上了,预留清掉"


def test_two_gateways_verifying_the_same_slot_do_not_clobber_a_bind_in_between(bare, monkeypatch):
    """两个网关副本的清扫同时核验同一个槽位:后到的那个拿到锁时状态已经变了(先到的核验完、有人绑上了),必须 409。"""
    st, _v, _l = bare
    s = st.slots[0]
    s._set(state=poolslot.EVICTING, evict={"boot": "old", "container_id": "c-old", "at": time.time()})
    entered, release = threading.Event(), threading.Event()

    def slow_handshake(seconds=None):
        entered.set()
        release.wait(5)
        return None                         # 先到的这一次核验不过(回 no_hello),但它占着锁
    monkeypatch.setattr(s, "handshake", slow_handshake)
    out = {}
    t1 = threading.Thread(target=lambda: out.setdefault("first", s.verify("c-new")))
    t1.start()
    assert entered.wait(5)
    t2 = threading.Thread(target=lambda: out.setdefault("second", s.verify("c-new")))
    t2.start()
    time.sleep(0.2)                         # 第二个已经过了锁外的检查,在等锁
    s._set(state=poolslot.BINDING, user="u7777", evict=None)    # 这期间有人绑上了
    release.set()
    t1.join(5)
    t2.join(5)
    assert out["second"] == (409, {"reason": "state", "state": poolslot.BINDING})
    assert s.state == poolslot.BINDING and s.user == "u7777", "绑定中的记录不能被核验冲掉"


def test_bind_keeps_the_container_id_the_steward_verified(pod):
    """网关的视图可能是腾挪之前的:绑定时它带来的容器 ID 不能盖掉管家核验时记下的那个。"""
    s = pod.st.slots[0]
    s._set(container_id="c-verified")
    code, _b = pod.st.bind({"slot": 0, "user_id": "u1234", "expect_in_use": 0, "container_id": "c-stale-view"})
    assert code == 200 and s.rec["container_id"] == "c-verified"


def test_verify_before_the_restart_is_refused(pod):
    pod.slots[0].auto_restart = False
    assert pod.st.evict({"slot": 0, "container_id": "c-0-0"})[0] == 202
    code, body = pod.st.verify({"slot": 0, "container_id": "c-0-new"})
    assert code == 409 and body["reason"] in ("no_hello", "same_boot"), body


def test_an_eviction_that_never_verifies_disables_the_slot_and_can_still_recover(tmp_path, monkeypatch):
    monkeypatch.setattr(slotrun.SlotSupervisor, "_kill_others", lambda self: None)
    monkeypatch.setattr(slotrun.SlotSupervisor, "_reap_orphans", lambda self: None)
    p = Pod(tmp_path, n=1, evict_timeout_seconds=0.5)
    try:
        p.all_free()
        p.slots[0].auto_restart = False
        p.st.evict({"slot": 0, "container_id": "c-0-0"})
        assert _wait(lambda: p.st.slots[0].state == poolslot.DISABLED, 10)
        assert p.st.slots[0].rec["error"] == "evict_timeout"
        p.slots[0].restart()
        assert p.st.verify({"slot": 0, "container_id": p.slots[0].container_id})[0] == 200
    finally:
        p.close()


def test_evict_is_refused_for_slots_in_use(pod):
    pod.bind()
    code, body = pod.st.evict({"slot": 0, "container_id": "x"})
    assert code == 409 and body["reason"] == "state"


# -- 重启过的槽位 ----------------------------------------------------------------------------------

def test_a_restarted_active_slot_is_treated_as_lost_and_goes_to_eviction(pod):
    pod.bind()
    pod.slots[0].restart()                  # 1 号进程崩溃重启:盘被新进程清空
    s = pod.st.slots[0]
    assert _wait(lambda: s.state == poolslot.EVICTING), pod.logs[-10:]
    assert s.rec["error"] == "restarted" and pod.owner() == {}, "独占要放掉,他才能到别处重新载入"
    assert pod.st.verify({"slot": 0, "container_id": pod.slots[0].container_id})[0] == 200


def test_a_slot_restarted_under_an_active_user_never_wipes_their_nas_history(pod):
    """槽位被强杀重启:新的 1 号进程清了盘、建了空的顶层目录。紧接着的定期回写要是照常做(带删除传播),
    NAS 上这个人的历史就被删光了。必须先认出「换了 boot」,不回写、按丢失处理。"""
    pod.bind()
    s = pod.st.slots[0]
    (pod.slot_dir(0) / "state" / "workspace" / "notes.md").write_text("要留着", encoding="utf-8")
    s.sync_once()                                            # 先正常回写一次:NAS 上有库与 notes.md
    nas = pod.users / "u1234"
    assert (nas / "workspace" / "notes.md").exists() and (nas / "xdg-data" / "opencode" / "opencode.db").exists()
    pod.slots[0].restart()
    with s.op:                                               # 抢在监视线程之前,直接来一轮定期回写
        out = s.sync_once()
    assert out.get("restarted"), out
    assert (nas / "workspace" / "notes.md").read_text(encoding="utf-8") == "要留着"
    assert _rows(nas / "xdg-data" / "opencode" / "opencode.db") >= 1, "NAS 上的库不能被删"
    assert s.state == poolslot.EVICTING and pod.owner() == {}


def test_a_restarted_free_slot_is_accepted_after_checking_the_wipe(pod):
    old = pod.st.slots[0].rec["boot"]
    pod.slots[0].restart()
    s = pod.st.slots[0]
    assert _wait(lambda: s.rec["boot"] != old and s.state == poolslot.FREE)


# -- 管家自己重启 ---------------------------------------------------------------------------------

def test_steward_restart_keeps_an_active_slot_active(pod):
    _c, b = pod.bind()
    st2 = pod.new_steward()
    s = st2.slots[0]
    assert _wait(lambda: s.ufd is not None), "恢复时重新打开本人的 NAS 目录"
    assert s.state == poolslot.ACTIVE and _healthy(b["opencode"]["port"], b["opencode"]["password"])
    assert s.sync_once()["snapshot_confirmed"]
    code, again = pod.bind(expect=1)
    assert code == 200 and again["opencode"]["password"] == b["opencode"]["password"], "网关重建缓存时能取回凭证"


def test_steward_restart_mid_retain_finishes_the_retain(pod):
    pod.bind()
    pod.st.slots[0]._set(state=poolslot.RETAINING)
    st2 = pod.new_steward()
    assert _wait(lambda: st2.slots[0].state == poolslot.RETAINED)
    assert not st2.slots[0].rec["dirty"] and pod.owner() == {}


def test_steward_restart_mid_load_never_writes_back_and_evicts(pod):
    (pod.users / "u1234" / "workspace").mkdir(parents=True)
    (pod.users / "u1234" / "workspace" / "keep.md").write_text("on nas", encoding="utf-8")
    pod.st.stop.set()
    with pod.st.lock:
        assert pod.st.slots[0].begin_bind("u1234", "c-0-0") is None   # 取了独占、还没载入就「崩了」
    st2 = pod.new_steward()
    assert _wait(lambda: st2.slots[0].state == poolslot.EVICTING)
    assert (pod.users / "u1234" / "workspace" / "keep.md").exists(), "半份载入不能回写(删除传播)"
    assert pod.owner() == {}


# -- 停止信号 ----------------------------------------------------------------------------------------

def test_sigterm_retains_every_active_slot_in_parallel(pod):
    assert pod.bind(slot=0, user="u1111")[0] == 200
    assert pod.bind(slot=1, user="u2222")[0] == 200
    t0 = time.monotonic()
    pod.st.on_sigterm()
    took = time.monotonic() - t0
    for i, u in ((0, "u1111"), (1, "u2222")):
        assert pod.st.slots[i].state == poolslot.RETAINED and not pod.st.slots[i].rec["dirty"]
        assert _rows(pod.users / u / "xdg-data" / "opencode" / "opencode.db") == 2 and pod.owner(u) == {}
    assert took < 2 * pod.settings.stop_wait_seconds


# -- 监视 ------------------------------------------------------------------------------------------

def test_busy_sessions_and_touch_feed_the_status(pod):
    pod.bind()
    (pod.tmp / "fake-0" / "busy").write_text("1", encoding="utf-8")
    assert _wait(lambda: pod.st.status()["slots"][0]["busy"])
    now = time.time()
    assert pod.st.touch({"activity": [[0, "u1234", now], [1, "u9999", now], ["x", 1, 2]]}) == (200, {"touched": 2})
    assert pod.st.slots[0].rec["last_active"] == pytest.approx(now)
    assert pod.st.slots[1].rec["last_active"] is None, "不在用的槽位不记活动"


# -- 外来监听者(单元级:非 root 造不出别的 uid) ------------------------------------------------------

@pytest.fixture()
def bare(tmp_path):
    """不起槽位进程的管家:只测状态机与监视的判断。槽位 uid 取 5000+i。"""
    verdicts = {}
    users, priv = tmp_path / "users", tmp_path / "priv"
    users.mkdir()
    for i in range(3):
        for sub in ("ctl", "state"):
            (tmp_path / "slots" / str(i) / sub).mkdir(parents=True)
    cfg = poolsteward.Config(key=KEY, pod=POD, slots=3, users_dir=users, slots_dir=tmp_path / "slots", priv_dir=priv,
                             port=_free_port(), settings=poolslot.Settings(pod=POD, hello_seconds=0.2,
                                                                           ready_seconds=0.5, stop_wait_seconds=0.3),
                             slot_ports=lambda i: (6100 + 10 * i, 6101 + 10 * i), slot_uid=lambda i: 5000 + i,
                             slot_model_port=lambda i: 6102 + 10 * i)
    logs = []
    st = poolsteward.Steward(cfg, log=logs.append, verdict=lambda port, uid: verdicts.get(port, "ok"))
    for s in st.slots:
        s._set(state=poolslot.FREE, boot="b%d" % s.i, quiet=True)
        (tmp_path / "slots" / str(s.i) / "state" / ".pool").mkdir()          # 槽位活着(启动时写过回执)但不响应
        (tmp_path / "slots" / str(s.i) / "state" / ".pool" / "ack.json").write_text(
            json.dumps({"boot": "b%d" % s.i}), encoding="utf-8")
    yield st, verdicts, logs
    st.close()


def test_a_foreign_listener_is_flagged_and_traced_to_the_offending_slot(bare):
    st, verdicts, logs = bare
    verdicts[6100] = "foreign:5002"
    st.monitor_once()
    out = st.status()["slots"]
    assert out[0]["listener"] == "foreign:5002" and out[2]["offender"] and not out[1]["offender"]
    assert any("冒充" in m for m in logs)
    verdicts.clear()
    st.monitor_once()
    assert st.status()["slots"][0]["listener"] == "ok" and not st.status()["slots"][2]["offender"]


def test_the_model_relay_port_is_watched_like_the_others(bare):
    """槽位重启的空档里同 Pod 的别人先占了我的模型端口:opencode 会把对话发给它。和对话端口一样核对、定位、拒绝绑定。"""
    st, verdicts, _logs = bare
    verdicts[6102] = "foreign:5002"
    st.monitor_once()
    out = st.status()["slots"]
    assert out[0]["listener"] == "foreign:5002" and out[2]["offender"]
    code, body = st.bind({"slot": 0, "user_id": "u1234", "expect_in_use": 0, "container_id": "c"})
    assert code in (502, 504) and st.slots[0].state == poolslot.RETAINED, body


def test_bind_onto_a_squatted_port_fails_and_is_retained_not_served(bare):
    st, verdicts, _logs = bare
    verdicts[6100] = "foreign:5001"
    code, body = st.bind({"slot": 0, "user_id": "u1234", "expect_in_use": 0, "container_id": "c"})
    assert code in (502, 504) and st.slots[0].state == poolslot.RETAINED, body


STATE_MATRIX = [
    # (状态, 附加字段, 操作, 期望状态码, 期望原因)
    (poolslot.FREE, {}, "resume", 409, "state"),
    (poolslot.FREE, {}, "retain", 409, "state"),
    (poolslot.FREE, {}, "verify", 409, "state"),
    (poolslot.ACTIVE, {}, "bind_other", 409, "state"),
    (poolslot.ACTIVE, {}, "resume_other", 409, "state"),
    (poolslot.ACTIVE, {}, "retain_other", 409, "user_mismatch"),
    (poolslot.ACTIVE, {}, "evict", 409, "state"),
    (poolslot.ACTIVE, {}, "disable", 409, "state"),
    (poolslot.RETAINED, {}, "bind_other", 409, "state"),
    (poolslot.RETAINED, {}, "retain", 409, "state"),
    (poolslot.RETAINED, {}, "resume_other", 409, "user_mismatch"),
    (poolslot.RETAINED, {"stale": True}, "resume", 409, "stale"),
    (poolslot.RETAINED, {"container_id": "c-old"}, "resume", 409, "restarted"),
    (poolslot.RETAINED, {"dirty": True}, "evict", 409, "not_synced"),
    (poolslot.RETAINING, {}, "evict", 409, "state"),
    (poolslot.RETAINING, {}, "disable", 409, "state"),
    (poolslot.EVICTING, {"evict": {"boot": "b0", "container_id": "c", "at": 0}}, "bind", 409, "state"),
    (poolslot.EVICTING, {"evict": {"boot": "b0", "container_id": "c", "at": 0}}, "resume", 409, "state"),
    (poolslot.EVICTING, {"evict": {"boot": "b0", "container_id": "c", "at": 0}}, "evict", 409, "state"),
    (poolslot.EVICTING, {"evict": {"boot": "b0", "container_id": "c", "at": 0}}, "verify_same", 409, "not_restarted"),
    (poolslot.DISABLED, {}, "verify", 409, "state"),
    (poolslot.DISABLED, {}, "bind", 409, "state"),
    (poolslot.BOOTING, {}, "bind", 409, "state"),
    (poolslot.BOOTING, {}, "evict", 409, "state"),
]


@pytest.mark.parametrize("state,extra,op,code,reason", STATE_MATRIX)
def test_state_matrix(bare, state, extra, op, code, reason):
    st, _v, _l = bare
    st.slots[0]._set(**dict({"state": state, "user": "u1234", "container_id": "c"}, **extra))
    in_use = st.in_use()
    calls = {
        "bind": lambda: st.bind({"slot": 0, "user_id": "u1234", "expect_in_use": in_use}),
        "bind_other": lambda: st.bind({"slot": 0, "user_id": "u5555", "expect_in_use": in_use}),
        "resume": lambda: st.resume({"slot": 0, "user_id": "u1234", "expect_in_use": in_use, "container_id": "c"}),
        "resume_other": lambda: st.resume({"slot": 0, "user_id": "u5555", "expect_in_use": in_use}),
        "retain": lambda: st.retain({"slot": 0, "user_id": "u1234"}),
        "retain_other": lambda: st.retain({"slot": 0, "user_id": "u5555"}),
        "evict": lambda: st.evict({"slot": 0, "container_id": "c"}),
        "verify": lambda: st.verify({"slot": 0, "container_id": "c-new"}),
        "verify_same": lambda: st.verify({"slot": 0, "container_id": "c"}),
        "disable": lambda: st.disable({"slot": 0, "reason": "ops"}),
    }
    got_code, body = calls[op]()
    assert (got_code, body.get("reason")) == (code, reason), body
    assert st.slots[0].state == state, "被拒绝的操作不能改状态"


def test_a_reservation_keeps_third_parties_out_and_survives_the_eviction(bare):
    st, _v, _l = bare
    assert st.reserve({"slot": 0, "user_id": "u1111", "seconds": 60})[0] == 200
    assert st.reserve({"slot": 0, "user_id": "u2222", "seconds": 60})[1]["reason"] == "reserved"
    code, body = st.bind({"slot": 0, "user_id": "u2222", "expect_in_use": 0})
    assert (code, body["reason"]) == (409, "reserved"), "第三个人抢不走"
    out = st.status()["slots"][0]
    assert out["reserved_for"] == "u1111" and out["reserved_until"] > time.time()
    s = st.slots[0]
    s._set(state=poolslot.EVICTING, evict={"boot": "b0", "container_id": "c", "at": time.time()})
    s._set(**s._reservation())
    assert s._reservation()["reserved_for"] == "u1111"
    for bad in ({"slot": 0, "user_id": "u1", "seconds": 0}, {"slot": 0, "user_id": "u1", "seconds": 601},
                {"slot": 0, "seconds": 5}, {"slot": 9, "user_id": "u1", "seconds": 5}):
        assert st.reserve(bad)[0] == 400


# -- 控制接口 --------------------------------------------------------------------------------------

def _call(st, method, path, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    hdrs = headers if headers is not None else poolauth.sign_request(KEY, method, path, POD, data)
    c = http.client.HTTPConnection("127.0.0.1", st.cfg.port, timeout=10)
    c.request(method, path, body=data, headers=dict(hdrs, **{"Content-Type": "application/json"}))
    r = c.getresponse()
    out = r.read()
    sig = r.getheader(poolauth.HDR_RESP_SIG) or ""
    c.close()
    return r.status, (json.loads(out) if out else None), hdrs, out, sig


def test_control_api_is_signed_both_ways_and_refuses_replays(bare):
    st, _v, _l = bare
    srv = poolsteward.make_server(st, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _call(st, "GET", "/healthz", headers={})[:2] == (200, {"ok": True})
        assert _call(st, "GET", "/pool/status", headers={})[:2] == (401, {})
        code, body, hdrs, raw, sig = _call(st, "GET", "/pool/status")
        assert code == 200 and len(body["slots"]) == 3
        assert poolauth.check_response(KEY, POD, hdrs[poolauth.HDR_NONCE], code, raw, sig), "应答要带管家的签名"
        assert _call(st, "GET", "/pool/status", headers=hdrs)[0] == 401, "同一个签名不能重放"
        other = poolauth.sign_request(KEY, "GET", "/pool/status", "pool-9", b"")
        assert _call(st, "GET", "/pool/status", headers=other)[0] == 401, "签给别的 Pod 的不认"
        code, body, *_ = _call(st, "POST", "/pool/disable", {"slot": 1, "reason": "ops"})
        assert code == 200 and st.slots[1].state == poolslot.DISABLED
        assert _call(st, "POST", "/pool/bind", raw=b"x" * (poolsteward.MAX_BODY + 1), headers={})[0] == 413
        assert _call(st, "POST", "/pool/nope", {})[0] == 404
    finally:
        srv.shutdown()


def test_steward_port_squatted_by_a_slot_evicts_that_slot(bare, monkeypatch):
    st, _v, logs = bare
    calls = []

    def fake_make_server(_st):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(98, "Address already in use")
        return "srv"
    monkeypatch.setattr(poolsteward, "make_server", fake_make_server)
    monkeypatch.setattr(poolsteward.netcheck, "listeners",
                        lambda port, files: [poolsteward.netcheck.Listener("0", port, 5001, 1)])
    assert poolsteward.bind_server(st, attempts=3, wait_s=0) == "srv"
    assert st.slots[1].state == poolslot.EVICTING and st.slots[1].rec["error"] == "squatted_steward_port"
    run = json.loads((st.slots[1].ctl_dir / "run.json").read_text(encoding="utf-8"))
    assert run == {"state": "exit", "boot": "b1"}, "按可信的 boot 发 exit"


def test_each_slots_credproxy_forwards_only_for_its_own_user_while_in_use(proxied_pod):
    """2026-10-23:中间件令牌在管家这里。槽位 i 的套接字只按槽位 i 当前在用的人转发 —— 空闲 / 保留时一律拒绝,
    别的槽位的套接字也拿不到这个人的令牌;工号由代理按绑定写入,请求里自带的不算。"""
    pod, seen = proxied_pod
    sock = [pod.slot_dir(i) / "sock" / credproxy.SOCKET_NAME for i in range(2)]
    assert _grmp_via(sock[0])[0] == 403, "没人在用:不转发"
    code, body = pod.bind(grmp="grmp-of-u1234")
    assert code == 200, body
    assert _grmp_via(sock[0])[0] == 200
    h, payload = seen[-1]
    assert h["auth"] == "grmp-of-u1234" and h["x-user-id"] == "u1234" and payload == {"dataIp": "10.0.0.9"}
    assert _grmp_via(sock[1])[0] == 403, "同 Pod 的别的槽位拿不到这个人的令牌"
    model_sock = pod.slot_dir(0) / "sock" / credproxy.MODEL_SOCKET_NAME
    assert _grmp_via(model_sock)[0] == 404, "模型套接字(能被 localhost 上的中继连到)绝不转中间件"
    c = http.client.HTTPConnection("127.0.0.1", pod.slots[0].model_port, timeout=10)   # 别的槽位也连得上这个端口
    c.request("POST", GRMP_PREFIX + "/common-operations", body=b"{}")
    assert c.getresponse().status == 404, "经模型中继借不到这个人的中间件身份"
    c.close()
    assert "grmp-of-u1234" not in json.dumps(pod.st.status())
    code, _b = pod.st.retain({"slot": 0, "user_id": "u1234"})
    assert code == 200
    assert _grmp_via(sock[0])[0] == 403, "转保留之后不再转发"
    assert pod.st.slots[0].rec["grmp_token"] == "", "转保留时令牌一并清掉,回来恢复时网关再交一次"
    n = len(seen)
    assert pod.resume()[0] == 200
    assert _grmp_via(sock[0])[0] == 503 and len(seen) == n, "恢复时没交令牌:报清楚,不拿空令牌去调"


def test_ctl_files_are_group_readable_and_status_never_has_secrets(bare):
    st, _v, _l = bare
    s = st.slots[0]
    s._set(state=poolslot.ACTIVE, user="u1234", password="pw-secret-123456789", reports_token="rt-secret-12345678",
           since=1.0)
    s._set(grmp_token="grmp-secret")
    s._write_run("run")
    assert "grmp-secret" not in (s.ctl_dir / "run.json").read_text(encoding="utf-8")
    assert oct((s.ctl_dir / "run.json").stat().st_mode & 0o777) == oct(poolslot.CTL_MODE)
    text = json.dumps(st.status())
    assert "pw-secret" not in text and "rt-secret" not in text and "grmp-secret" not in text
    assert oct((st.cfg.priv_dir / "slot-0.json").stat().st_mode & 0o777) == oct(0o600), "口令只落在管家私有目录"


def test_config_from_env():
    c = poolsteward.Config.from_env({"SANDBOX_CONTROL_KEY": "k" * 32, "POD_NAME": "pool-3", "SLOTS_PER_POD": "4",
                                     "STATE_SYNC_SECONDS": "30"})
    assert c.pod == "pool-3" and c.slots == 4 and c.settings.sync_seconds == 30 and c.settings.pod == "pool-3"
    assert c.slot_uid(3) == 2003 and c.slot_ports(1) == (4110, 4111)
    for bad in ({}, {"SANDBOX_CONTROL_KEY": "short"}, {"SANDBOX_CONTROL_KEY": "k" * 32, "SLOTS_PER_POD": "0"},
                {"SANDBOX_CONTROL_KEY": "k" * 32, "SLOTS_PER_POD": "x"}):
        with pytest.raises(poolsteward.ConfigError):
            poolsteward.Config.from_env(bad)
