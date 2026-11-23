"""沙箱里的启动器(docker/fuyao.py)与 entrypoint.sh 的 FUYAO 分支(2026-11-17)。

启动器对着真的状态服务跑(临时目录当 NAS 与沙箱本地盘);systemd 与 opencode 探活用替身。要点:
- 删掉撑磁盘的占位文件;沙箱令牌只进 root 可读的文件,给 agent 的环境里没有令牌、没有集群服务地址、没有任何密钥;
- 先拿独占再载入;拉知识库;按顺序起中继、同步、agent 三个服务;探活通过才返回;已在运行时不重复启动;
- 后台同步:续独占、回写、诊断环境拉知识库;独占丢了就停;
- 收尾:完整回写、清 dirty、--release 时放开独占;
- entrypoint.sh 在 FUYAO=1 时跳过独占、载入、回写、收尾(这些在 root 侧),只跑服务与 WAL 合并。
"""
import os
import pathlib
import re
import stat
import sys
import threading
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import fuyao  # noqa: E402
import remotesync as rs  # noqa: E402
import sbxtoken  # noqa: E402
import statesvc  # noqa: E402
import statesync  # noqa: E402

KEY = b"c" * 32


@pytest.fixture()
def box(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    (nas / "users" / "u1001" / "workspace").mkdir(parents=True)
    (nas / "users" / "u1001" / "workspace" / "a.txt").write_text("A")
    (nas / "kb" / "cases").mkdir(parents=True)
    (nas / "kb" / "cases" / "c1.md").write_text("案例")
    svc = statesvc.Service(str(nas), KEY, log=lambda m: None)
    srv = statesvc.ThreadingHTTPServer(("127.0.0.1", 0), statesvc.handler(svc))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data, run, var = tmp_path / "data", tmp_path / "run", tmp_path / "var"
    data.mkdir()
    for name, val in (("DATA", data), ("ME", data / "me"), ("KB", data / "kb"), ("RUN", run),
                      ("TOKEN_FILE", run / "sandbox-token"), ("ROOT_ENV", run / "root.env"),
                      ("AGENT_ENV", run / "agent.env"), ("VAR", var), ("KB_STATE", var / "kb-state.json"),
                      ("RESERVE", var / "disk-reserve")):
        monkeypatch.setattr(fuyao, name, str(val))
    var.mkdir()
    (var / "disk-reserve").write_bytes(b"\0" * 1024)
    units, state = [], {"up": False}

    def run_unit(name, argv, env_path=None, uid=None, gid=None, restart="on-failure", workdir="/data"):
        units.append((name, argv, env_path, uid))
        if name == fuyao.UNIT_AGENT:
            state["up"] = True
    monkeypatch.setattr(fuyao, "run_unit", run_unit)
    monkeypatch.setattr(fuyao, "active", lambda name: state["up"])
    monkeypatch.setattr(fuyao, "healthy", lambda pw, timeout=3.0: state["up"] and pw == "pw-1")
    env = {"GSDB_USER_ID": "u1001", "GSDB_KIND": "runtime", "GSDB_SANDBOX_ID": "sbx-1",
           "GSDB_SANDBOX_TOKEN": sbxtoken.issue(KEY, "sbx-1", "u1001", "runtime"),
           "GSDB_STATE_URL": "http://127.0.0.1:%d" % srv.server_address[1], "GSDB_CREDPROXY_URL": "http://10.0.0.9:8091",
           "OPENCODE_SERVER_PASSWORD": "pw-1", "GSDB_REPORTS_TOKEN": "rt-1",
           "GSDB_AGENT_UID": str(os.getuid()), "GSDB_AGENT_GID": str(os.getgid()),
           "MODEL_ID": "m1", "GRMP_API_HOST": "10.0.0.5", "MODEL_API_KEY": "不该出现", "GRMP_AUTH_TOKEN": "不该出现",
           "PATH": "/usr/bin"}
    yield env, nas, data, units, state
    srv.shutdown()
    srv.server_close()


def test_start_loads_state_and_kb_writes_secrets_root_only_and_starts_the_three_units(box):
    env, nas, data, units, _state = box
    assert fuyao.start(env) == 0
    assert not os.path.exists(fuyao.RESERVE), "占位文件删掉,空间留给用户状态"
    assert (data / "me" / "workspace" / "a.txt").read_text() == "A" and (data / "kb" / "cases" / "c1.md").exists()
    assert (nas / "users" / "u1001" / ".owner").exists() and (nas / "users" / "u1001" / statesync.MARK_DIRTY).exists()
    assert stat.S_IMODE(os.stat(fuyao.TOKEN_FILE).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(fuyao.ROOT_ENV).st_mode) == 0o600
    agent = fuyao.read_env_file(fuyao.AGENT_ENV)
    for k in ("GSDB_SANDBOX_TOKEN", "GSDB_STATE_URL", "GSDB_CREDPROXY_URL", "MODEL_API_KEY", "GRMP_AUTH_TOKEN", "PATH"):
        assert k not in agent, "agent 的环境里不该有 %s" % k
    assert agent["OPENCODE_SERVER_PASSWORD"] == "pw-1" and agent["REPORTS_TOKEN"] == "rt-1" and agent["FUYAO"] == "1"
    assert agent["GRMP_PROXY_SOCKET"] == "/run/credproxy/credproxy.sock" and agent["MODEL_ID"] == "m1"
    assert agent["NAS_ME"] == fuyao.ME and agent["TZ"] == "Asia/Shanghai", "镜像 ENV 在 systemd 服务里不生效,要补"
    assert [u[0] for u in units] == [fuyao.UNIT_RELAY, fuyao.UNIT_SYNC, fuyao.UNIT_AGENT]
    assert units[2][3] == int(env["GSDB_AGENT_UID"]) and units[0][3] is None, "中继、同步以 root,opencode 以 agent"
    assert fuyao.start(env) == 0 and len(units) == 3, "已在运行:不重复启动"


def test_the_import_environment_gets_a_writable_kb_and_its_clip_receiver(box):
    env, nas, data, units, _s = box
    env = dict(env, GSDB_KIND="kb-import", GSDB_SANDBOX_TOKEN=sbxtoken.issue(KEY, "sbx-1", "u1001", "kb-import"))
    (nas / "kb" / "docsets" / "G1").mkdir(parents=True)
    (nas / "kb" / "docsets" / "G1" / "a.md").write_text("官方")
    assert fuyao.start(env) == 0
    assert not (data / "kb" / "docsets").exists(), "官方文档集在模板里自带,不经状态服务拉(拉一遍近两分钟)"
    agent = fuyao.read_env_file(fuyao.AGENT_ENV)
    assert agent["KB_CLIP_RECEIVER"] == "1" and agent["GSDB_KB_INBOX"] == fuyao.KB + "/inbox/uploads"
    assert (nas / "admins" / "u1001" / ".owner").exists()


def test_missing_launch_settings_or_a_busy_lease_do_not_start(box, monkeypatch):
    env, nas, _d, units, _s = box
    assert fuyao.start(dict(env, GSDB_SANDBOX_TOKEN="")) == 2 and not units
    other = rs.Client(env["GSDB_STATE_URL"], sbxtoken.issue(KEY, "sbx-old", "u1001", "runtime"))
    rs.lease(other, "acquire")
    monkeypatch.setattr(fuyao, "LEASE_WAIT_S", 0)
    assert fuyao.start(env) == 3 and not units, "上一台沙箱还持有独占:不启动、不碰状态"


def test_a_vm_that_already_belongs_to_another_sandbox_refuses_to_start(box, capsys):
    """第二道防线(2026-11-18):网关万一把地址认错(恢复后平台回旧地址,旧地址已分给别人的沙箱),
    在别人的虚拟机里调了本人的启动器 —— 这台虚拟机里已有另一台沙箱的令牌,拒绝启动,不写入本人令牌和口令。"""
    env, nas, _d, units, state = box
    assert fuyao.start(env) == 0 and len(units) == 3                       # 这台虚拟机属于 sbx-1(u1001)
    before = pathlib.Path(fuyao.TOKEN_FILE).read_text()
    state["up"] = False                                                    # 让「已在运行」那条短路不生效
    intruder = dict(env, GSDB_USER_ID="u2002", GSDB_SANDBOX_ID="sbx-2", OPENCODE_SERVER_PASSWORD="pw-2",
                    GSDB_SANDBOX_TOKEN=sbxtoken.issue(KEY, "sbx-2", "u2002", "runtime"))
    assert fuyao.start(intruder) == 6 and len(units) == 3, "不重启服务"
    assert pathlib.Path(fuyao.TOKEN_FILE).read_text() == before, "不覆盖原令牌"
    assert "pw-2" not in pathlib.Path(fuyao.AGENT_ENV).read_text(), "不写入别人的口令"
    assert not (nas / "users" / "u2002").exists(), "不去拿别人的独占、不载入别人的状态"
    assert "sbx-1" in capsys.readouterr().out
    assert fuyao.start(dict(env)) == 0, "同一台沙箱重启动照常(启动器幂等)"


def test_the_launcher_reads_the_sandbox_of_a_token_without_the_key():
    assert sbxtoken.sandbox_of(sbxtoken.issue(KEY, "sbx-9", "u1001", "runtime")) == "sbx-9"
    for bad in ("", "st1.", "st1.!!!.x", "abc", None):
        assert sbxtoken.sandbox_of(bad) is None


def test_syncloop_writes_back_and_stop_finishes_and_releases(box, monkeypatch):
    env, nas, data, _units, _s = box
    assert fuyao.start(env) == 0
    (data / "me" / "workspace" / "b.txt").write_text("B")
    root_env = fuyao.read_env_file(fuyao.ROOT_ENV)
    assert fuyao.syncloop(root_env, once=True) == 0
    assert (nas / "users" / "u1001" / "workspace" / "b.txt").read_text() == "B"
    (data / "me" / "workspace" / "c.txt").write_text("C")
    monkeypatch.setattr(fuyao, "PODCTL", ["true"])
    assert fuyao.stop(release=True) == 0
    u = nas / "users" / "u1001"
    assert (u / "workspace" / "c.txt").read_text() == "C" and not (u / statesync.MARK_DIRTY).exists()
    assert not (u / ".owner").exists(), "删除之前放开独占"


def test_syncloop_stops_when_the_lease_is_gone(box):
    env, nas, _d, _u, _s = box
    assert fuyao.start(env) == 0
    (nas / "users" / "u1001" / ".owner").write_text('{"pod": "sbx/someone-else", "ts": %f}' % time.time())
    assert fuyao.syncloop(fuyao.read_env_file(fuyao.ROOT_ENV), once=True) == 0


def test_env_file_round_trips_quotes_and_drops_multiline_values(capsys):
    text = fuyao.env_file({"A": 'x"y\\z', "B": "line1\nline2", "BAD-KEY": "v", "C": "plain"})
    assert 'A="x\\"y\\\\z"' in text and "B=" not in text and "BAD-KEY" not in text
    assert "B、BAD-KEY" in capsys.readouterr().out, "写不进去的要记日志,不悄悄丢"
    p = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / ("envfile-%d" % os.getpid())
    p.write_text(text)
    try:
        assert fuyao.read_env_file(str(p)) == {"A": 'x"y\\z', "C": "plain"}
    finally:
        p.unlink()


# -- entrypoint.sh 的 FUYAO 分支 ----------------------------------------------------------------------

_EP = (_ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")


def test_entrypoint_in_fuyao_mode_leaves_lease_load_sync_and_finish_to_the_root_side():
    assert re.search(r'^if \[ "\$FUYAO" != "1" \]; then\n\s+\$PODCTL owner-acquire', _EP, re.M), "独占由状态服务管"
    assert '[ "$FUYAO" = "1" ] || $PODCTL state-load' in _EP, "载入由启动器做"
    assert '[ "$FUYAO" = "1" ] || $PODCTL state-sync' in _EP, "回写由 root 的 gsdb-statesync 做"
    m = re.search(r'^if \[ "\$FUYAO" = "1" \]; then\n(.*?)^fi$', _EP, re.M | re.S)
    assert m and 'exit "$RC"' in m.group(1) and m.start() < _EP.index("state-finish"), "收尾由 fuyao-stop 做"
    loop = _EP[_EP.index('( while sleep "$STATE_SYNC_SECONDS"'):]
    assert "db-checkpoint" in loop.split("done )")[0], "WAL 合并照做,以 agent 身份"


def test_instance_aliases_survive_the_env_file_with_the_same_meaning():
    import podctl
    raw = "db1=10.0.0.11/postgres   # 管理员\ndb1-mon=10.0.0.12/postgres\n\n"
    text = fuyao.env_file({"GRMP_INSTANCE_ALIASES": raw})
    p = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / ("envfile-al-%d" % os.getpid())
    p.write_text(text)
    try:
        got = fuyao.read_env_file(str(p))["GRMP_INSTANCE_ALIASES"]
    finally:
        p.unlink()
    assert "\n" not in got
    assert podctl.parse_instance_aliases(got) == podctl.parse_instance_aliases(raw), "合成一行,解析结果不变"


def test_syncloop_says_hello_on_the_ticks_between_lease_refreshes(box, monkeypatch):
    env, _nas, _data, _units, _s = box
    assert fuyao.start(env) == 0
    calls = []
    monkeypatch.setattr(fuyao.rs, "hello", lambda c: calls.append("hello"))
    monkeypatch.setattr(fuyao.rs, "lease", lambda c, a: calls.append("lease") or {})
    monkeypatch.setattr(fuyao, "LEASE_S", 10 ** 12)                  # 这一拍不到续独占的时候
    assert fuyao.syncloop(fuyao.read_env_file(fuyao.ROOT_ENV), once=True) == 0
    assert calls == ["hello"], "恢复后网关要尽快拿到新地址:每一拍都报到"
