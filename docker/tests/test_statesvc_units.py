"""状态服务(docker/statesvc.py)与沙箱侧同步(docker/remotesync.py)(Fuyao 运行模式,2026-11-17)。

真起一个状态服务(临时目录当 NAS),沙箱侧客户端对着它跑。要点(沿用按需模式 statesync 的规则):
- 只认网关签发的沙箱令牌;工号与种类决定目录(诊断 users/、导入 admins/);
- 同一人同一时刻只有一台沙箱持有本人目录;过期可接管;丢了独占的沙箱回写被拒(LeaseLost);
- 上次非优雅退出时载入前先快照;载入打 dirty 标记、收尾清掉;
- 没改动不发请求;库是一致性快照、写入后清掉 NAS 上旧的 -wal;删除只在载入成功后传播;
- NAS 上预埋的链接不下发、沙箱里预埋的链接不上传;越界路径与不允许的目录整包拒绝;
- 知识库:诊断环境不拉官方文档集与收件目录、增量拉取;只有导入环境能发布;基线过期一个文件都不写;库被锁住时不发布。
"""
import io
import json
import os
import pathlib
import sqlite3
import sys
import tarfile
import threading
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import remotesync as rs  # noqa: E402
import sbxtoken  # noqa: E402
import statesvc  # noqa: E402
import statesync  # noqa: E402

KEY = b"c" * 32


@pytest.fixture()
def svc(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    clock = {"t": time.time()}
    s = statesvc.Service(str(nas), KEY, log=lambda m: None, clock=lambda: clock["t"])
    srv = statesvc.ThreadingHTTPServer(("127.0.0.1", 0), statesvc.handler(s))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    yield base, nas, s, clock, tmp_path
    srv.shutdown()
    srv.server_close()


def client(base, sid="sbx-1", user="u1001", kind="runtime", key=KEY):
    return rs.Client(base, sbxtoken.issue(key, sid, user, kind))


def _db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("create table if not exists t(x)")
    c.executemany("insert into t values (?)", [(r,) for r in rows])
    c.commit()
    c.close()


def _rows(path):
    c = sqlite3.connect(path)
    try:
        return [r[0] for r in c.execute("select x from t order by rowid")]
    finally:
        c.close()


# -- 认人与独占 ---------------------------------------------------------------------------------------

def test_requests_without_a_valid_sandbox_token_are_refused(svc):
    base, *_ = svc
    for c in (rs.Client(base, ""), client(base, key=b"x" * 32), rs.Client(base, "st1.bogus.sig")):
        assert c.call_json("POST", "/v1/lease", {"action": "acquire"})[0] == 401


def test_one_sandbox_per_user_with_stale_takeover_and_lost_lease(svc):
    base, nas, s, clock, _ = svc
    a, b = client(base, "sbx-a"), client(base, "sbx-b")
    rs.lease(a, "acquire")
    owner = json.loads((nas / "users" / "u1001" / ".owner").read_text())
    assert owner["pod"] == "sbx/sbx-a", "与 podctl 同一格式,按需 / 池化模式也认得"
    with pytest.raises(rs.LeaseLost):
        rs.lease(b, "acquire")
    clock["t"] += statesvc.LEASE_STALE_S + 1
    assert rs.lease(b, "acquire")["took_over"] == "sbx/sbx-a"
    with pytest.raises(rs.LeaseLost):
        rs.lease(a, "refresh")
    assert client(base, "sbx-x", kind="kb-import").call_json("POST", "/v1/lease", {"action": "acquire"})[0] == 200, \
        "导入环境是另一个目录(admins/),互不影响"
    assert (nas / "admins" / "u1001" / ".owner").exists()


# -- 载入、回写、收尾 ---------------------------------------------------------------------------------

def _seed_nas(nas, user="u1001", dirty=False):
    u = nas / "users" / user
    _db(u / "xdg-data" / "opencode" / "opencode.db", ["hello"])
    (u / "workspace").mkdir(parents=True)
    (u / "workspace" / "a.txt").write_text("A")
    (u / "gdaa" / "sessions").mkdir(parents=True)
    (u / "gdaa" / "sessions" / "s1.yaml").write_text("db: og7")
    (u / "reports").mkdir()
    (u / "reports" / "r.json").write_text("{}")
    if dirty:
        (u / statesync.MARK_DIRTY).write_text("1")
    return u


def test_load_snapshots_after_an_unclean_exit_marks_dirty_and_never_follows_planted_links(svc):
    base, nas, *_ = svc
    u = _seed_nas(nas, dirty=True)
    other = nas / "users" / "u2002"
    other.mkdir(parents=True)
    (other / "secret.txt").write_text("别人的")
    os.symlink(other / "secret.txt", u / "workspace" / "evil")
    local = svc[4] / "local"
    c = client(base)
    rs.lease(c, "acquire")
    n = rs.load(c, str(local))
    assert n == 4 and (local / "workspace" / "a.txt").read_text() == "A"
    assert _rows(local / "xdg-data" / "opencode" / "opencode.db") == ["hello"]
    assert not (local / "workspace" / "evil").exists(), "NAS 上预埋的链接不下发"
    assert (local / statesync.MARK_LOADED).exists() and (u / statesync.MARK_DIRTY).exists()
    snaps = [p for p in u.iterdir() if p.name.startswith(statesync.SNAPSHOT_PREFIX)]
    assert len(snaps) == 1 and (snaps[0] / "workspace" / "a.txt").exists(), "上次非优雅退出:先快照"
    assert rs.sync(c, str(local)) == {"uploaded": 0, "skipped": True}, "刚载入:基线就是当前状态,不回传"


def test_sync_uploads_only_changes_snapshots_dbs_propagates_deletes_and_finish_clears_dirty(svc):
    base, nas, *_ = svc
    u = _seed_nas(nas)
    (u / "xdg-data" / "opencode" / "opencode.db-wal").write_bytes(b"stale wal")
    local = svc[4] / "local"
    c = client(base)
    rs.lease(c, "acquire")
    rs.load(c, str(local))
    (local / "workspace" / "a.txt").write_text("A2")
    (local / "workspace" / "b.txt").write_text("B")
    (local / "reports" / "r.json").unlink()
    _db(local / "xdg-data" / "opencode" / "opencode.db", ["world"])
    out = rs.sync(c, str(local))
    assert out["uploaded"] == 3 and out["deleted"] == 1
    assert (u / "workspace" / "a.txt").read_text() == "A2" and (u / "workspace" / "b.txt").read_text() == "B"
    assert not (u / "reports" / "r.json").exists(), "删除传播"
    assert _rows(u / "xdg-data" / "opencode" / "opencode.db") == ["hello", "world"]
    assert not (u / "xdg-data" / "opencode" / "opencode.db-wal").exists(), "旧 -wal 会被套到新库上,要清掉"
    assert rs.sync(c, str(local))["skipped"] is True
    os.symlink("/etc/hosts", local / "workspace" / "link")
    out = rs.sync(c, str(local))
    assert not (u / "workspace" / "link").exists(), "沙箱里预埋的链接不上传"
    rs.finish(c, str(local), release=True)
    assert not (u / statesync.MARK_DIRTY).exists() and not (u / ".owner").exists()


def test_without_a_successful_load_deletes_are_not_propagated(svc):
    base, nas, *_ = svc
    u = _seed_nas(nas)
    local = svc[4] / "fresh"
    (local / "workspace").mkdir(parents=True)
    (local / "workspace" / "only.txt").write_text("x")
    c = client(base)
    rs.lease(c, "acquire")
    rs.sync(c, str(local))
    assert (u / "workspace" / "a.txt").exists(), "本地是空的(没载入成功)时传播删除 = 清空全部历史"
    assert (u / "workspace" / "only.txt").exists()


def test_a_sandbox_that_lost_the_lease_cannot_overwrite(svc):
    base, nas, s, clock, tmp = svc
    _seed_nas(nas)
    a, b = client(base, "sbx-a"), client(base, "sbx-b")
    rs.lease(a, "acquire")
    rs.load(a, str(tmp / "la"))
    clock["t"] += statesvc.LEASE_STALE_S + 1
    rs.lease(b, "acquire")
    (tmp / "la" / "workspace" / "a.txt").write_text("旧副本")
    with pytest.raises(rs.LeaseLost):
        rs.sync(a, str(tmp / "la"))
    assert (nas / "users" / "u1001" / "workspace" / "a.txt").read_text() == "A"


@pytest.mark.parametrize("name", ["../escape.txt", "workspace/../../escape.txt", "/abs.txt", "etc/passwd"])
def test_archives_with_escaping_or_unexpected_paths_are_rejected_whole(svc, name):
    base, nas, *_ = svc
    _seed_nas(nas)
    c = client(base)
    rs.lease(c, "acquire")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ok = tarfile.TarInfo("workspace/fine.txt")
        ok.size = 2
        tar.addfile(ok, io.BytesIO(b"ok"))
        bad = tarfile.TarInfo(name)
        bad.size = 1
        tar.addfile(bad, io.BytesIO(b"x"))
    p = svc[4] / "bad.tar"
    p.write_bytes(buf.getvalue())
    code, out = c.upload("PUT", "/v1/state", str(p))
    assert code == 400
    assert not (nas / "escape.txt").exists() and not (svc[4] / "escape.txt").exists()


# -- 知识库 -------------------------------------------------------------------------------------------

def _seed_kb(nas):
    kb = nas / "kb"
    for rel, text in (("cases/c1.md", "案例一"), ("docsets/G1/a.md", "官方"), ("inbox/clips/x.md", "收件"),
                      ("kb.yaml", "store: {}")):
        (kb / rel).parent.mkdir(parents=True, exist_ok=True)
        (kb / rel).write_text(text)
    _db(kb / "index" / "store.sqlite", ["v"])
    return kb


def test_runtime_pulls_the_kb_incrementally_without_docsets_or_inbox(svc):
    base, nas, _s, _c, tmp = svc
    kb = _seed_kb(nas)
    c = client(base)
    local, state = tmp / "kb", str(tmp / "kb-state.json")
    out = rs.kb_pull(c, str(local), state)
    assert out["fetched"] == 3 and (local / "cases" / "c1.md").read_text() == "案例一"
    assert not (local / "docsets").exists() and not (local / "inbox").exists()
    assert oct((local / "cases" / "c1.md").stat().st_mode & 0o777) == "0o644", "诊断环境拿到的是只读副本"
    assert rs.kb_pull(c, str(local), state)["fetched"] == 0
    (kb / "cases" / "c2.md").write_text("案例二")
    (kb / "cases" / "c1.md").unlink()
    out = rs.kb_pull(c, str(local), state)
    assert out["fetched"] == 1 and out["removed"] == 1 and not (local / "cases" / "c1.md").exists()


def test_only_the_import_environment_publishes_and_a_stale_base_writes_nothing(svc):
    base, nas, _s, _c, tmp = svc
    kb = _seed_kb(nas)
    imp = client(base, "sbx-i", "u2001", "kb-import")
    local, state = tmp / "ikb", str(tmp / "ikb-state.json")
    rs.kb_pull(imp, str(local), state, writable_by=(os.getuid(), os.getgid()))
    assert not (local / "docsets").exists(), "文档集模板自带,导入环境也不拉"
    assert (local / "inbox" / "clips" / "x.md").exists()
    assert rs.kb_publish(imp, str(local), state) == {"skipped": "unchanged"}
    (local / "cases" / "c3.md").write_text("新案例")
    (local / "inbox" / "clips" / "x.md").unlink()
    (local / statesvc.KB_LOCK).write_text("busy")
    assert rs.kb_publish(imp, str(local), state) == {"skipped": "locked"}, "正在导入:不发布半截"
    (local / statesvc.KB_LOCK).unlink()
    out = rs.kb_publish(imp, str(local), state)
    assert out["version"] == 1 and (kb / "cases" / "c3.md").read_text() == "新案例"
    assert not (kb / "inbox" / "clips" / "x.md").exists() and (kb / "docsets" / "G1" / "a.md").exists(), \
        "官方文档集归 kbstd 管,发布不动它"
    assert rs.kb_publish(imp, str(local), state) == {"skipped": "unchanged"}
    # 另一位管理员先发布了:基线过期,一个文件都不写
    other = client(base, "sbx-j", "u2002", "kb-import")
    rs.kb_pull(other, str(tmp / "okb"), str(tmp / "okb.json"), writable_by=(os.getuid(), os.getgid()))
    (tmp / "okb" / "cases" / "c4.md").write_text("另一位")
    assert rs.kb_publish(other, str(tmp / "okb"), str(tmp / "okb.json"))["version"] == 2
    (local / "cases" / "c5.md").write_text("晚了一步")
    assert rs.kb_publish(imp, str(local), state) == {"stale": True, "server_version": 2}
    assert not (kb / "cases" / "c5.md").exists()
    runtime = client(base)
    p = tmp / "x.tar"
    with tarfile.open(p, "w") as t:
        pass
    assert runtime.upload("POST", "/v1/kb/publish", str(p), headers={rs.HDR_KB_BASE: "2"})[0] == 403


def test_docker_and_gateway_sandbox_token_modules_are_identical():
    assert (_ROOT / "docker" / "sbxtoken.py").read_bytes() == (_ROOT / "gateway" / "sbxtoken.py").read_bytes()


def test_the_state_service_and_sandbox_sync_modules_and_their_local_imports_are_in_the_image():
    """漏拷一个模块:构建成功、状态服务(或沙箱里的同步)一启动就 ImportError。"""
    import re
    copied = set()
    for m in re.finditer(r"^COPY\s+(.+?)\s+/opt/agent/\s*$", (_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8"),
                         re.M):
        copied |= {pathlib.Path(x).name for x in m.group(1).split()}
    local = {p.stem for p in (_ROOT / "docker").glob("*.py")}
    for mod in ("statesvc", "remotesync"):
        src = (_ROOT / "docker" / (mod + ".py")).read_text(encoding="utf-8")
        imported = set(re.findall(r"^import (\w+)", src, re.M)) | set(re.findall(r"^from (\w+) import", src, re.M))
        need = {mod} | (imported & local)
        assert {n + ".py" for n in need} <= copied, "Dockerfile 没拷:%s" % sorted({n + ".py" for n in need} - copied)


# -- 沙箱现在在哪(恢复后平台回的地址会过期) -----------------------------------------------------------

def _where(base, sid="sbx-1", user="u1001", kind="runtime"):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(base + "/v1/where",
                                 headers={"Authorization": "Bearer " + sbxtoken.issue(KEY, sid, user, kind)})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_the_service_remembers_where_each_sandbox_last_called_from(svc):
    base, _nas, _s, clock, _tmp = svc
    assert _where(base)[0] == 404, "还没见过这台沙箱"
    rs.lease(client(base), "acquire")
    code, got = _where(base)
    assert code == 200 and got["address"] == "127.0.0.1" and got["age"] == 0
    clock["t"] += 40
    assert _where(base)[1]["age"] == 40, "问地址本身不刷新时刻(网关用的也是这台沙箱的令牌)"
    assert _where(base, sid="sbx-2")[0] == 404, "别的沙箱的令牌问不到这台"
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(base + "/v1/where", timeout=5)
        raise AssertionError("不带令牌不该回 200")
    except urllib.error.HTTPError as e:
        assert e.code == 401


def test_hello_only_records_the_address_and_touches_no_nas(svc):
    base, nas, _s, _clock, _tmp = svc
    before = sorted(p.name for p in nas.rglob("*"))
    c = client(base, sid="sbx-h")
    rs.hello(c)
    assert _where(base, sid="sbx-h")[1]["address"] == "127.0.0.1"
    assert sorted(p.name for p in nas.rglob("*")) == before, "报到不写 NAS"
    bad = rs.Client(base, "st1.bad.token")
    try:
        rs.hello(bad)
        raise AssertionError("坏令牌不该报到成功")
    except rs.StateError:
        pass
