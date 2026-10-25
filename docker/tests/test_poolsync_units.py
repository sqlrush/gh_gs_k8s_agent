"""池化布局的状态搬运(管家用)。方案 §4、§8、§9。

管家挂着**全部**用户目录。这里的攻击用例都在问同一个问题:
「能不能诱使管家把 u9999 的东西读进 u1234 的目录,或者写坏 u9999 的东西?」
每条结尾都核对 u9999 的文件字节不变、u1234 那边没有它的内容。
"""
import json
import os
import pathlib
import sqlite3
import stat
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import fdtree  # noqa: E402
import poolsync as ps  # noqa: E402

SECRET = b"SQLite format 3\x00" + b"u9999 history" * 100


def _mkdb(path: pathlib.Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table if not exists t (i integer)")
    con.executemany("insert into t values (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()


def _rows(path: pathlib.Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("select count(*) from t").fetchone()[0]
    finally:
        con.close()


class Env:
    def __init__(self, tmp: pathlib.Path):
        self.users = tmp / "pool" / "users"
        self.state = tmp / "pool" / "state"
        self.users.mkdir(parents=True)
        self.state.mkdir(parents=True)
        self.victim = self.users / "u9999"
        (self.victim / "xdg-data" / "opencode").mkdir(parents=True)
        self.victim_db = self.victim / "xdg-data" / "opencode" / "opencode.db"
        self.victim_db.write_bytes(SECRET)
        self.users_fd = fdtree.open_root(self.users)
        self.state_fd = fdtree.open_root(self.state)
        self.me = self.users / "u1234"
        self._user_fd = None

    @property
    def user_fd(self) -> int:
        if self._user_fd is None:
            self._user_fd = ps.open_user(self.users_fd, "u1234")
        return self._user_fd

    def close(self):
        for fd in (self.users_fd, self.state_fd, self._user_fd):
            if fd is not None:
                os.close(fd)

    def assert_victim_untouched(self):
        assert self.victim_db.read_bytes() == SECRET, "u9999 的库被改了"
        assert sorted(p.name for p in self.victim.iterdir()) == ["xdg-data"], "u9999 目录里多了东西"
        for p in self.me.rglob("*"):
            if p.is_file() and not p.is_symlink():
                assert b"u9999 history" not in p.read_bytes(), "u9999 的内容出现在 %s" % p


@pytest.fixture()
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


# --- 本人目录与独占 ---------------------------------------------------------------

def test_open_user_creates_the_directory_private(env):
    fd = env.user_fd
    assert env.me.is_dir() and stat.S_IMODE(env.me.stat().st_mode) == 0o700
    assert os.fstat(fd).st_ino == env.me.stat().st_ino


@pytest.mark.parametrize("bad", ["../u9999", "U1234", "a/b", "", "-x", "x" * 60])
def test_open_user_rejects_bad_ids(env, bad):
    with pytest.raises(ValueError):
        ps.open_user(env.users_fd, bad)


def test_open_user_refuses_a_user_dir_that_is_a_link(env):
    env.me.symlink_to(env.victim)
    with pytest.raises(ps.PoolError) as ei:
        ps.open_user(env.users_fd, "u1234")
    assert ei.value.reason == "unsafe_user_dir"


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目录权限限制")
def test_unwritable_users_root_names_the_owner_and_the_fix(env):
    """kubelet 建出来的目录属主是 root(v1.1.6 待修第 1 项);部署手册要求先 chown 1000。"""
    env.users.chmod(0o555)
    try:
        with pytest.raises(ps.PoolError) as ei:
            ps.open_user(env.users_fd, "u1234")
    finally:
        env.users.chmod(0o755)
    assert ei.value.reason == "users_not_writable"
    assert "chown -R 1000:1000" in str(ei.value) and "uid=" in str(ei.value)


def test_owner_acquire_refresh_release(env):
    fd = env.user_fd
    assert ps.owner_acquire(fd, "sandbox-01", now=1000) == (True, "sandbox-01", 0)
    ok, holder, retry = ps.owner_acquire(fd, "sandbox-02", now=1010)
    assert (ok, holder) == (False, "sandbox-01") and retry == 80
    assert ps.owner_refresh(fd, "sandbox-01", now=1030)
    assert not ps.owner_refresh(fd, "sandbox-02", now=1030)
    ps.owner_release(fd, "sandbox-02")                       # 不是自己的不删
    assert (env.me / ".owner").exists()
    ps.owner_release(fd, "sandbox-01")
    assert not (env.me / ".owner").exists()


def test_stale_owner_is_taken_over(env):
    fd = env.user_fd
    ps.owner_acquire(fd, "sandbox-01", now=1000)
    assert ps.owner_acquire(fd, "sandbox-02", now=1090)[0]
    assert not ps.owner_refresh(fd, "sandbox-01", now=1091), "被接管之后,原持有者必须知道自己已经失去独占"


def test_a_planted_owner_link_is_replaced_not_written_through(env):
    (env.me).mkdir()
    (env.me / ".owner").symlink_to(env.victim_db)
    assert ps.owner_acquire(env.user_fd, "sandbox-01", now=1000)[0]
    assert not (env.me / ".owner").is_symlink()
    env.assert_victim_untouched()


def test_a_planted_dirty_link_is_replaced_not_written_through(env):
    env.me.mkdir()
    (env.me / ".state-dirty").symlink_to(env.victim_db)
    fd = env.user_fd
    assert ps.is_dirty(fd)
    ps.mark_dirty(fd, now=1000)
    ps.clear_dirty(fd)
    assert not (env.me / ".state-dirty").exists()
    env.assert_victim_untouched()


# --- 载入 -----------------------------------------------------------------------

def test_load_brings_everything_including_sessions_and_reports(env):
    _mkdb(env.me / "xdg-data" / "opencode" / "opencode.db", rows=4)
    (env.me / "workspace").mkdir()
    (env.me / "workspace" / "a.sql").write_text("select 1", encoding="utf-8")
    (env.me / "gdaa" / "sessions").mkdir(parents=True)
    (env.me / "gdaa" / "sessions" / "abc.yaml").write_text("h: 1", encoding="utf-8")
    (env.me / "gdaa" / "credentials").mkdir()
    (env.me / "gdaa" / "credentials" / "x.enc").write_bytes(b"secret")
    (env.me / "gdaa" / "key").write_bytes(b"k")
    (env.me / "gdaa" / "config.yaml").write_text("old", encoding="utf-8")
    (env.me / "reports" / "health").mkdir(parents=True)
    (env.me / "reports" / "health" / "r.json").write_text("{}", encoding="utf-8")
    moved, unclean = ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    assert not unclean and moved.dbs == 1 and moved.files == 4
    assert _rows(env.state / "xdg-data" / "opencode" / "opencode.db") == 4
    assert (env.state / "gdaa" / "sessions" / "abc.yaml").exists()
    assert (env.state / "reports" / "health" / "r.json").exists()
    assert not (env.state / "gdaa" / "credentials").exists(), "加密凭据不载入(方案 §8)"
    assert not (env.state / "gdaa" / "key").exists() and not (env.state / "gdaa" / "config.yaml").exists()
    for sub in ps.POOL_LOCALIZED:
        assert (env.state / sub).is_dir()


def test_load_for_a_new_user_creates_empty_dirs(env):
    moved, unclean = ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    assert moved.files == 0 and not unclean
    assert all((env.state / sub).is_dir() for sub in ps.POOL_LOCALIZED)


def test_load_does_not_follow_links_planted_in_the_nas_dir(env):
    """按需模式下主容器对本人 NAS 目录可写,能埋 `workspace/x → /pool/users/u9999/…`;
    这个字符串到了管家里会被解析成别人的目录。"""
    (env.me / "workspace").mkdir(parents=True)
    (env.me / "workspace" / "x").symlink_to(env.victim_db)
    (env.me / "xdg-data").symlink_to(env.victim / "xdg-data")          # 整个子目录换成链接
    moved, _ = ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    assert not (env.state / "workspace" / "x").exists()
    assert not list((env.state / "xdg-data").rglob("*"))
    assert moved.skipped == 1 and moved.errors
    env.assert_victim_untouched()


def test_unclean_last_time_snapshots_before_loading(env):
    (env.me / "workspace").mkdir(parents=True)
    (env.me / "workspace" / "half.sql").write_text("半截", encoding="utf-8")
    ps.mark_dirty(env.user_fd)
    logged = []
    _moved, unclean = ps.load(env.user_fd, env.state_fd, log=logged.append, now=1700000000)
    assert unclean and any("非优雅" in m for m in logged)
    assert (env.me / ".unclean-1700000000" / "workspace" / "half.sql").read_text(encoding="utf-8") == "半截"


def test_unclean_with_nothing_on_nas_says_so_instead_of_claiming_a_snapshot(env):
    """上次绑定后一次回写都没来得及就被强杀:NAS 上只有 .owner 与 .state-dirty。
    日志不能说「已快照到 .unclean-…」而那个目录并不存在(10-14 e2e)。"""
    ps.mark_dirty(env.user_fd)
    logged = []
    _moved, unclean = ps.load(env.user_fd, env.state_fd, log=logged.append, now=1700000000)
    assert unclean and not list(env.me.glob(".unclean-*"))
    assert any("没有任何可快照的内容" in m for m in logged) and not any("已先快照" in m for m in logged)


# --- 库自检 ---------------------------------------------------------------------

def test_selfcheck_restores_from_a_nas_backup(env):
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    db = env.state / "xdg-data" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"garbage" * 1000)
    _mkdb(env.me / "backup" / "opencode.db.20261013T000000Z", rows=7)
    assert ps.selfcheck(env.state, env.user_fd, env.state_fd, log=lambda _m: None) == "restored"
    assert _rows(db) == 7
    assert list((env.state / "xdg-data" / "opencode").glob("opencode.db.corrupt-*")), "坏库要留证"


def test_selfcheck_never_uses_a_backup_that_is_a_planted_link(env):
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    db = env.state / "xdg-data" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"garbage" * 1000)
    _mkdb(env.victim / "good.db", rows=9)                     # 别人一份「好的」库
    (env.me / "backup").mkdir()
    (env.me / "backup" / "opencode.db.20261014T000000Z").symlink_to(env.victim / "good.db")
    logged = []
    assert ps.selfcheck(env.state, env.user_fd, env.state_fd, log=logged.append) == "reset"
    assert any("不是普通文件" in m for m in logged)
    assert not db.exists() or _rows(db) != 9
    (env.victim / "good.db").unlink()
    env.assert_victim_untouched()


def test_selfcheck_ok_and_absent(env):
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    assert ps.selfcheck(env.state, env.user_fd, env.state_fd) == "absent"
    _mkdb(env.state / "xdg-data" / "opencode" / "opencode.db")
    assert ps.selfcheck(env.state, env.user_fd, env.state_fd) == "ok"


# --- 回写 -----------------------------------------------------------------------

def _snapshot(env, rows: int) -> pathlib.Path:
    """模拟主容器:把库的一致性快照写到 .pool/snap/(原子改名,换 inode)。"""
    snap = env.state / ".pool" / "snap" / "xdg-data" / "opencode" / "opencode.db"
    tmp = snap.with_name("tmp.db")
    if tmp.exists():
        tmp.unlink()
    _mkdb(tmp, rows=rows)
    os.replace(tmp, snap)
    return snap


def test_sync_pushes_files_and_the_snapshot_but_never_the_live_db(env):
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    (env.state / "workspace" / "new.sql").write_text("select 2", encoding="utf-8")
    _mkdb(env.state / "xdg-data" / "opencode" / "opencode.db", rows=100)   # 活着的库:不许碰
    _snapshot(env, rows=5)
    seen = {}
    moved = ps.sync(env.user_fd, env.state_fd, seen)
    assert (env.me / "workspace" / "new.sql").read_text(encoding="utf-8") == "select 2"
    assert _rows(env.me / "xdg-data" / "opencode" / "opencode.db") == 5, "NAS 上必须是快照,不是活库"
    assert moved.dbs == 1 and not (env.me / ".pool").exists()


def test_a_round_without_a_snapshot_never_deletes_the_nas_db(env):
    """**库只搬快照**——要是保留集合里只有快照,主容器还没生成快照(或没在时限内响应)的那一轮,
    删除传播会把 NAS 上这个人的 opencode.db 当成「已删」删掉。只有本地真没了,NAS 上的才删。"""
    _mkdb(env.me / "xdg-data" / "opencode" / "opencode.db", rows=8)
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    assert (env.state / "xdg-data" / "opencode" / "opencode.db").exists()
    assert not (env.state / ".pool").exists()                   # 主容器一份快照都还没给
    ps.sync(env.user_fd, env.state_fd, {}, may_delete=True)
    assert _rows(env.me / "xdg-data" / "opencode" / "opencode.db") == 8
    (env.state / "xdg-data" / "opencode" / "opencode.db").unlink()   # 本地真删了
    ps.sync(env.user_fd, env.state_fd, {}, may_delete=True)
    assert not (env.me / "xdg-data" / "opencode" / "opencode.db").exists()


def test_an_unchanged_snapshot_is_not_copied_again(env):
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    _snapshot(env, rows=5)
    seen = {}
    assert ps.sync(env.user_fd, env.state_fd, seen).dbs == 1
    assert ps.sync(env.user_fd, env.state_fd, seen).dbs == 0
    _snapshot(env, rows=6)
    assert ps.sync(env.user_fd, env.state_fd, seen).dbs == 1
    assert _rows(env.me / "xdg-data" / "opencode" / "opencode.db") == 6


def test_a_snapshot_replaced_by_a_link_to_someone_elses_db_is_refused(env):
    """主容器(模型)把快照换成指向 u9999 库的链接:管家按路径搬就会把别人的历史拷进本人目录。"""
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    snap = env.state / ".pool" / "snap" / "xdg-data" / "opencode"
    snap.mkdir(parents=True)
    (snap / "opencode.db").symlink_to(env.victim_db)
    moved = ps.sync(env.user_fd, env.state_fd, {})
    assert moved.skipped >= 1
    env.assert_victim_untouched()


def test_a_link_to_the_sidecars_own_environment_is_not_copied(env, tmp_path):
    """链接指向管家自己的 /proc/self/environ(里面有控制密钥)。macOS 没有 /proc,用一个「管家私有文件」代替。"""
    priv = tmp_path / "sidecar-private"
    priv.write_text("SANDBOX_CONTROL_KEY=topsecret", encoding="utf-8")
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    (env.state / "workspace" / "env").symlink_to(priv)
    if pathlib.Path("/proc/self/environ").exists():
        (env.state / "workspace" / "env2").symlink_to("/proc/self/environ")
    ps.sync(env.user_fd, env.state_fd, {})
    assert not (env.me / "workspace" / "env").exists() and not (env.me / "workspace" / "env2").exists()
    for p in env.me.rglob("*"):
        if p.is_file():
            assert b"topsecret" not in p.read_bytes()


def test_sync_does_not_follow_a_workspace_link_or_a_linked_directory(env):
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    (env.state / "workspace" / "x").symlink_to(env.victim_db)
    (env.state / "workspace" / "d").symlink_to(env.victim)
    (env.state / "reports").rmdir()
    (env.state / "reports").symlink_to(env.victim / "xdg-data")
    ps.sync(env.user_fd, env.state_fd, {})
    assert not (env.me / "workspace" / "x").exists() and not (env.me / "workspace" / "d").exists()
    env.assert_victim_untouched()


def test_sync_propagates_deletions_but_keeps_markers_and_untouched_gdaa(env):
    (env.me / "workspace").mkdir(parents=True)
    (env.me / "workspace" / "gone.sql").write_text("x", encoding="utf-8")
    (env.me / "gdaa" / "credentials").mkdir(parents=True)
    (env.me / "gdaa" / "credentials" / "x.enc").write_bytes(b"secret")
    (env.me / "gdaa" / "config.yaml").write_text("old", encoding="utf-8")
    ps.mark_dirty(env.user_fd)
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    (env.state / "workspace" / "gone.sql").unlink()
    (env.state / "gdaa" / "config.yaml").write_text("rendered", encoding="utf-8")
    ps.sync(env.user_fd, env.state_fd, {})
    assert not (env.me / "workspace" / "gone.sql").exists()
    assert (env.me / "gdaa" / "credentials" / "x.enc").read_bytes() == b"secret"
    assert (env.me / "gdaa" / "config.yaml").read_text(encoding="utf-8") == "old", "config.yaml 不回写"
    assert (env.me / ".state-dirty").exists()


def test_a_whole_missing_subdir_does_not_wipe_nas(env):
    """模型 `rm -rf workspace` 之后,本轮不对 workspace 传播删除——NAS 上那份是这个人全部的历史。"""
    (env.me / "workspace").mkdir(parents=True)
    (env.me / "workspace" / "precious.sql").write_text("十年", encoding="utf-8")
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    import shutil
    shutil.rmtree(env.state / "workspace")
    moved = ps.sync(env.user_fd, env.state_fd, {})
    assert (env.me / "workspace" / "precious.sql").exists()
    assert any("不对它传播删除" in w for w in moved.warnings) and not moved.errors


def test_sync_without_may_delete_never_deletes(env):
    (env.me / "workspace").mkdir(parents=True)
    (env.me / "workspace" / "keep.sql").write_text("k", encoding="utf-8")
    os.close(fdtree.open_path(env.state_fd, "workspace", create=True))
    ps.sync(env.user_fd, env.state_fd, {}, may_delete=False)
    assert (env.me / "workspace" / "keep.sql").exists()


# --- 退出备份 -------------------------------------------------------------------

def test_backup_copies_the_final_snapshot_and_keeps_three(env):
    ps.load(env.user_fd, env.state_fd, log=lambda _m: None)
    assert ps.backup(env.user_fd, env.state_fd, now=1) is None, "没有快照就没有备份"
    _snapshot(env, rows=4)
    names = [ps.backup(env.user_fd, env.state_fd, now=t) for t in (100, 200, 300, 400)]
    left = sorted(p.name for p in (env.me / "backup").iterdir())
    assert left == sorted(names[1:])
    assert _rows(env.me / "backup" / names[-1]) == 4


def test_moved_to_json_shape(env):
    m = ps.Moved(files=3, dbs=1, bytes=1048576 * 2, seconds=0.456)
    assert m.to_json() == {"files": 3, "dbs": 1, "mb": 2.0, "seconds": 0.46, "skipped": 0, "errors": 0,
                           "warnings": 0}
    json.dumps(m.to_json())
