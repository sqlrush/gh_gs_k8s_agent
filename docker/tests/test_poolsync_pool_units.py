"""poolsync 为固定池化加的能力(2026-10-23):两侧权限、版本令牌、看不见的子目录不传播删除、锁目录不搬。"""
import os
import pathlib
import stat
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import fdtree  # noqa: E402
import podctl  # noqa: E402
import poolsync as ps  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    users, state = tmp_path / "users", tmp_path / "state"
    users.mkdir()
    state.mkdir()
    ufd, sfd = fdtree.open_root(users), fdtree.open_root(state)
    me = ps.open_user(ufd, "u1234")
    yield users / "u1234", state, me, sfd
    for fd in (ufd, sfd, me):
        os.close(fd)


def _mode(p: pathlib.Path) -> int:
    return stat.S_IMODE(p.lstat().st_mode)


def test_load_into_a_slot_disk_uses_group_shared_modes(env):
    nas, state, me, sfd = env
    (nas / "workspace").mkdir(mode=0o700)
    (nas / "workspace" / "notes.md").write_text("x")
    os.chmod(nas / "workspace" / "notes.md", 0o600)
    ps.load(me, sfd, log=lambda m: None, modes=ps.SLOT_MODES)
    assert _mode(state / "workspace") == 0o2770 and _mode(state / "workspace" / "notes.md") == 0o660
    assert _mode(state / "xdg-data") == 0o2770, "新用户也按槽位权限建空目录"


def test_sync_back_to_nas_is_private_again(env):
    nas, state, me, sfd = env
    (state / "workspace").mkdir(mode=0o2770)
    (state / "workspace" / "a.md").write_text("x")
    os.chmod(state / "workspace" / "a.md", 0o660)
    ps.sync(me, sfd, {}, modes=ps.NAS_MODES)
    assert _mode(nas / "workspace" / "a.md") == 0o600 and _mode(nas / "workspace") == 0o700


@pytest.mark.skipif(os.geteuid() == 0, reason="root 无视权限位")
def test_an_unreadable_slot_directory_never_deletes_its_nas_copy(env):
    """槽位里 0700 的目录管家进不去 —— 看不见不等于没有,NAS 上那份不能被删除传播清掉。"""
    nas, state, me, sfd = env
    (nas / "workspace" / "secret-proj").mkdir(parents=True)
    (nas / "workspace" / "secret-proj" / "plan.md").write_text("keep me")
    (state / "workspace" / "secret-proj").mkdir(parents=True)
    (state / "workspace" / "secret-proj" / "plan.md").write_text("keep me")
    os.chmod(state / "workspace" / "secret-proj", 0o000)
    try:
        moved = ps.sync(me, sfd, {}, may_delete=True, modes=ps.NAS_MODES)
    finally:
        os.chmod(state / "workspace" / "secret-proj", 0o700)
    assert (nas / "workspace" / "secret-proj" / "plan.md").read_text() == "keep me"
    assert any("没有权限进入" in w for w in moved.warnings)


def test_opencode_lock_directories_are_neither_copied_nor_pruned(env):
    nas, state, me, sfd = env
    (nas / "xdg-state" / "opencode" / "locks" / "old.lock").mkdir(parents=True)
    (state / "xdg-state" / "opencode" / "locks" / "x.lock").mkdir(parents=True)
    (state / "xdg-state" / "opencode" / "locks" / "x.lock" / "pid").write_text("1")
    (state / "xdg-state" / "opencode" / "kv.json").write_text("{}")
    ps.sync(me, sfd, {}, modes=ps.NAS_MODES)
    assert (nas / "xdg-state" / "opencode" / "kv.json").exists()
    assert not (nas / "xdg-state" / "opencode" / "locks" / "x.lock").exists()


def test_gen_rotates_to_a_fresh_random_token(env):
    nas, _state, me, _sfd = env
    assert ps.gen_read(me) == ""
    a = ps.gen_rotate(me)
    b = ps.gen_rotate(me)
    assert len(a) == 32 and a != b and ps.gen_read(me) == b
    assert _mode(nas / ps.GEN) == 0o600


def test_a_planted_gen_link_is_not_followed(env):
    nas, _state, me, _sfd = env
    victim = nas.parent / "victim-gen"
    victim.write_text("other")
    os.symlink(victim, nas / ps.GEN)
    assert ps.gen_read(me) == ""
    ps.gen_rotate(me)
    assert victim.read_text() == "other" and not (nas / ps.GEN).is_symlink()


def test_owner_holder_is_compared_as_a_whole(env):
    """固定池化下持有者是「pod/槽位」:同一个 Pod 的另一个槽位不能当成自己。"""
    _nas, _state, me, _sfd = env
    assert ps.owner_acquire(me, "pool-3/1", now=1000)[0]
    ok, holder, _wait = ps.owner_acquire(me, "pool-3/2", now=1001)
    assert not ok and holder == "pool-3/1"


def test_db_fingerprint(env):
    nas, _state, me, _sfd = env
    assert ps.db_fingerprint(me) is None
    (nas / "xdg-data" / "opencode").mkdir(parents=True)
    (nas / "xdg-data" / "opencode" / "opencode.db").write_bytes(b"x" * 10)
    ino, _mt, size = ps.db_fingerprint(me)
    assert size == 10 and ino == (nas / "xdg-data" / "opencode" / "opencode.db").stat().st_ino


def test_gen_and_db_missing_only_when_they_are_really_gone(env):
    """「NAS 上的数据不见了」只认确实不存在:还在、读不准、路径上埋了链接,都不算(交给原来的 stale 判断)。"""
    nas, _state, me, _sfd = env
    assert ps.gen_missing(me) and ps.db_missing(me), "新目录:两样都没有(连目录都没有也算)"
    ps.gen_rotate(me)
    assert not ps.gen_missing(me), "令牌还在"
    (nas / ps.GEN).unlink()
    assert ps.gen_missing(me)
    db = nas / "xdg-data" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"x")
    assert not ps.db_missing(me), "库还在"
    db.unlink()
    assert ps.db_missing(me), "目录在、库不在"
    (nas / "xdg-data" / "opencode").rmdir()
    (nas / "xdg-data" / "opencode").symlink_to(nas)
    assert not ps.db_missing(me), "路径上是链接:不是「不见了」"
    (nas / ps.GEN).symlink_to(nas / "nowhere")
    assert not ps.gen_missing(me), "令牌是个(悬空的)链接:也不是「不见了」"


def test_on_demand_owner_acquire_rotates_gen_too(tmp_path):
    """按需模式下用过之后,池化里这个人的旧保留副本必须作废。"""
    assert podctl.owner_acquire(tmp_path, "runtime-u1234-abc", wait_s=0)
    first = (tmp_path / podctl.GEN_NAME).read_text()
    assert podctl.owner_acquire(tmp_path, "runtime-u1234-abc", wait_s=0)
    assert (tmp_path / podctl.GEN_NAME).read_text() != first


def test_sync_never_propagates_deletes_once_the_source_is_no_longer_valid(env):
    """槽位在复制期间重启(盘被清空):删除传播之前再问一次,来源失效就不删 NAS 上的任何东西。"""
    nas, state, me, sfd = env
    (nas / "workspace").mkdir()
    (nas / "workspace" / "history.md").write_text("all of it")
    (state / "workspace").mkdir()                            # 新的 1 号进程建的空目录
    moved = ps.sync(me, sfd, {}, modes=ps.NAS_MODES, still_valid=lambda: False)
    assert (nas / "workspace" / "history.md").exists() and any("不传播删除" in e for e in moved.errors)
    ps.sync(me, sfd, {}, modes=ps.NAS_MODES, still_valid=lambda: True)
    assert not (nas / "workspace" / "history.md").exists(), "来源有效时照常传播删除"


def test_selfcheck_restore_into_a_slot_disk_is_group_shared(env):
    """从 NAS 备份(0600)恢复出来的库要让槽位(另一个 uid、同组)读写得到。"""
    import sqlite3
    nas, state, me, sfd = env
    ps.load(me, sfd, log=lambda m: None, modes=ps.SLOT_MODES)
    db = state / "xdg-data" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"garbage" * 1000)
    (nas / "backup").mkdir()
    con = sqlite3.connect(nas / "backup" / "opencode.db.20261013T000000Z")
    con.execute("create table t (i integer)")
    con.commit()
    con.close()
    os.chmod(nas / "backup" / "opencode.db.20261013T000000Z", 0o600)
    assert ps.selfcheck(state, me, sfd, log=lambda _m: None, modes=ps.SLOT_MODES) == "restored"
    assert _mode(db) == 0o660 and _mode(state / ps.RESTORE_DIR) == 0o2770
