"""池化主容器守护进程(方案 §4、§6)。用假 opencode(tests/fake_opencode.py)驱动真进程。

守护进程是管家与 opencode 之间唯一的接口:管家只写 ctl、只读回执。
所以这里核对的都是「回执说的和实际发生的一致」——管家据此决定什么时候可以收尾。
"""
import json
import os
import pathlib
import socket
import sqlite3
import stat
import sys
import time
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import poolrun  # noqa: E402
import poolsync  # noqa: E402

FAKE = pathlib.Path(__file__).with_name("fake_opencode.py")
PW = "p" * 24


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _rows(path: pathlib.Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("select count(*) from t").fetchone()[0]
    finally:
        con.close()


class Rig:
    def __init__(self, tmp: pathlib.Path):
        self.ctl = tmp / "ctl"
        self.state = tmp / "state"
        self.kb = tmp / "kb"
        self.out = tmp / "fake-out"
        for p in (self.ctl, self.state, self.kb):
            p.mkdir()
        self.cfg = poolrun.Config(ctl_dir=self.ctl, state=self.state, kb_dir=self.kb,
                                  opencode_cmd=[sys.executable, str(FAKE)],
                                  podctl_cmd=[sys.executable, str(_ROOT / "docker" / "podctl.py")],
                                  port=_free_port(), reports_port=_free_port(), poll_s=0.05,
                                  stop_timeout_s=5, health_every_s=0.2, env_file=tmp / "env.sh")
        env = dict(os.environ, FAKE_OUT=str(self.out), GRMP_API_HOST="10.0.0.1", GRMP_API_PORT="8080")
        self.logs = []
        self.sup = poolrun.Supervisor(self.cfg, env=env, log=self.logs.append)

    def run(self, user="u1234", since=111.0, state="run"):
        (self.ctl / "run.json").write_text(json.dumps({"state": state, "since": since, "user_id": user,
                                                       "password": PW}), encoding="utf-8")

    def snap(self, seq):
        (self.ctl / "snap.json").write_text(json.dumps({"seq": seq}), encoding="utf-8")

    def ack(self) -> dict:
        return json.loads((self.state / ".pool" / "ack.json").read_text(encoding="utf-8"))

    def until(self, cond, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sup.tick()
            if cond():
                return True
            time.sleep(0.05)
        raise AssertionError("超时;日志:%s" % self.logs)

    def healthy(self) -> bool:
        import base64
        req = urllib.request.Request("http://127.0.0.1:%d/global/health" % self.cfg.port)
        req.add_header("Authorization", "Basic " + base64.b64encode(("opencode:" + PW).encode()).decode())
        try:
            urllib.request.urlopen(req, timeout=1).read()
            return True
        except OSError:
            return False

    def close(self):
        self.sup.term = True
        self.sup.tick()


@pytest.fixture()
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_idle_does_not_start_anything(rig):
    assert rig.sup.tick()
    assert rig.sup.oc is None and not (rig.state / ".pool" / "ack.json").exists()


def test_run_starts_opencode_with_the_bound_identity(rig):
    rig.run()
    rig.until(rig.healthy)
    env = json.loads((rig.out / "env.json").read_text(encoding="utf-8"))
    assert env["GSDB_USER_ID"] == "u1234" and env["OPENCODE_SERVER_PASSWORD"] == PW
    assert env["XDG_DATA_HOME"] == str(rig.state / "xdg-data") and env["GSDB_HOME"] == str(rig.state / "gdaa")
    assert env["GSDB_REPORTS_DIR"] == str(rig.state / "reports") and env["GSDB_KB_DIR"] == str(rig.kb)
    assert pathlib.Path(env["cwd"]).resolve() == (rig.state / "workspace").resolve()
    assert env["SANDBOX_CONTROL_KEY"] is None, "控制密钥只在管家"
    cfg = rig.state / "gdaa" / "config.yaml"
    assert "connection_mode: api" in cfg.read_text(encoding="utf-8")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600 and stat.S_IMODE((rig.state / "gdaa").stat().st_mode) == 0o700
    a = rig.ack()
    assert a["running"] and not a["final"] and a["since"] == 111.0
    assert "export GSDB_HOME=" in (rig.cfg.env_file).read_text(encoding="utf-8")
    assert PW not in rig.cfg.env_file.read_text(encoding="utf-8"), "env.sh 只放路径,不放口令"


def test_reports_port_serves_the_bound_user(rig):
    rig.run()
    rig.until(rig.healthy)

    def whoami():
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/whoami.json" % rig.cfg.reports_port, timeout=1) as r:
                return json.loads(r.read())
        except OSError:
            return None
    rig.until(lambda: whoami() is not None)
    assert whoami() == {"user_id": "u1234"}


def test_bad_run_file_is_refused(rig):
    rig.run(user="../u9999")
    rig.sup.tick()
    assert rig.sup.oc is None and rig.ack()["error"] == "bad_run"


def test_snapshot_request_produces_a_consistent_copy_and_m1_skips_unchanged(rig):
    rig.run()
    rig.until(rig.healthy)
    db = rig.state / "xdg-data" / "opencode" / "opencode.db"
    rig.until(db.exists)
    rig.snap(1)
    rig.until(lambda: rig.ack()["snap_seq"] == 1)
    snap = rig.state / ".pool" / "snap" / "xdg-data" / "opencode" / "opencode.db"
    assert rig.ack()["changed"] == ["xdg-data/opencode/opencode.db"] and _rows(snap) == 1
    assert stat.S_IMODE(snap.stat().st_mode) == 0o600, "快照会原样成为 NAS 上的库与退出备份"
    rig.snap(2)
    rig.until(lambda: rig.ack()["snap_seq"] == 2)
    assert rig.ack()["changed"] == [], "库没变,M1 不该再生成快照"
    con = sqlite3.connect(db)                       # opencode 又写了一轮(WAL 里)
    con.execute("insert into t values (5)")
    con.commit()
    con.close()
    rig.snap(3)
    rig.until(lambda: rig.ack()["snap_seq"] == 3)
    assert rig.ack()["changed"] == ["xdg-data/opencode/opencode.db"] and _rows(snap) == 2


def test_stop_aborts_running_turns_and_the_final_snapshot_has_the_last_words(rig):
    rig.run()
    rig.until(rig.healthy)
    (rig.out / "busy").write_text("1", encoding="utf-8")
    rig.run(state="stop")
    rig.until(lambda: rig.ack()["final"])
    a = rig.ack()
    assert a["aborted_running"] and (rig.out / "aborted").exists()
    assert (rig.out / "terminated").exists() and not a["running"] and a["since"] == 111.0
    snap = rig.state / ".pool" / "snap" / "xdg-data" / "opencode" / "opencode.db"
    assert _rows(snap) == 2, "停之前才落库的那一行必须在最终快照里"
    assert not rig.healthy()


def test_stop_is_done_once_per_run(rig):
    rig.run()
    rig.until(rig.healthy)
    rig.run(state="stop")
    rig.until(lambda: rig.ack()["final"])
    ts = rig.ack()["ts"]
    for _ in range(3):
        rig.sup.tick()
    assert rig.ack()["ts"] == ts and rig.sup.oc is None


def test_a_crashed_opencode_is_restarted_with_state_intact(rig):
    rig.run()
    rig.until(rig.healthy)
    first = rig.sup.oc.pid
    rig.sup.oc.kill()
    rig.until(lambda: rig.sup.oc is not None and rig.sup.oc.pid != first and rig.healthy(), timeout=15)
    assert any("意外退出" in m for m in rig.logs)
    assert _rows(rig.state / "xdg-data" / "opencode" / "opencode.db") >= 2


def test_sigterm_stops_and_writes_the_final_ack(rig):
    """直接删 Pod:各容器同时收到 SIGTERM。管家靠这份 final 回执知道可以收尾(停止握手)。"""
    rig.run()
    rig.until(rig.healthy)
    rig.sup.term = True
    assert rig.sup.tick() is False
    a = rig.ack()
    assert a["final"] and a["since"] == 111.0
    assert _rows(rig.state / ".pool" / "snap" / "xdg-data" / "opencode" / "opencode.db") == 2


def test_localized_set_matches_poolsync():
    assert poolrun.POOL_LOCALIZED == poolsync.POOL_LOCALIZED
    assert poolrun.SNAP_DIR == poolsync.SNAP_DIR


def test_config_from_env_defaults():
    c = poolrun.Config.from_env({})
    assert c.ctl_dir == pathlib.Path("/run/sandbox") and c.state == pathlib.Path("/data/state")
    assert c.opencode_cmd == ["opencode"] and c.port == 4096
