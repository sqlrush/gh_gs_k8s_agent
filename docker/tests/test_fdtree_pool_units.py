"""fdtree 为固定池化加的能力(2026-10-23):目标权限、看不见的子目录、清盘、给组加权限。

背景(P0 摸底 S1):槽位 uid 2000+i 与管家 uid 1000 同组,靠组权限互通 —— 复制到槽位盘的文件不能继承 NAS 上的
0600 / 0700;槽位里的 opencode 会建 0700 的锁目录,管家进不去,不能让整轮回写因此中断、更不能因此误删 NAS 上的东西。
"""
import os
import pathlib
import stat
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import fdtree  # noqa: E402

needs_nonroot = pytest.mark.skipif(os.geteuid() == 0, reason="root 无视权限位,这几条在 root 下没有意义")


def _mode(p: pathlib.Path) -> int:
    return stat.S_IMODE(p.lstat().st_mode)


def test_new_directories_get_exactly_the_requested_mode_regardless_of_umask(tmp_path):
    old = os.umask(0o077)
    try:
        root = fdtree.open_root(tmp_path)
        fd = fdtree.open_path(root, "a/b", create=True, mode=0o2770)
        os.close(fd)
        os.close(root)
    finally:
        os.umask(old)
    assert _mode(tmp_path / "a") == 0o2770 and _mode(tmp_path / "a" / "b") == 0o2770


def test_existing_directories_are_not_rechmodded(tmp_path):
    (tmp_path / "a").mkdir(mode=0o700)
    root = fdtree.open_root(tmp_path)
    os.close(fdtree.open_path(root, "a", create=True, mode=0o2770))
    os.close(root)
    assert _mode(tmp_path / "a") == 0o700


def test_copy_tree_applies_the_target_modes_instead_of_the_source_ones(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "x").mkdir(parents=True, mode=0o700)
    (src / "x" / "f").write_text("hi")
    os.chmod(src / "x" / "f", 0o600)
    dst.mkdir()
    s, d = fdtree.open_root(src), fdtree.open_root(dst)
    fdtree.copy_tree(s, d, stats=fdtree.Stats(), file_mode=0o660, dir_mode=0o2770)
    os.close(s)
    os.close(d)
    assert _mode(dst / "x") == 0o2770 and _mode(dst / "x" / "f") == 0o660


def test_special_bits_are_never_copied(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "f").write_text("hi")
    os.chmod(src / "f", 0o4755)
    s, d = fdtree.open_root(src), fdtree.open_root(dst)
    fdtree.copy_file(s, "f", d, "f")
    os.close(s)
    os.close(d)
    assert _mode(dst / "f") == 0o755


@needs_nonroot
def test_an_unreadable_subdirectory_is_reported_not_fatal(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "ok").mkdir(parents=True)
    (src / "ok" / "f").write_text("1")
    (src / "locks" / "x.lock").mkdir(parents=True)
    (src / "locks" / "x.lock" / "pid").write_text("2")
    os.chmod(src / "locks" / "x.lock", 0o000)
    dst.mkdir()
    s, d = fdtree.open_root(src), fdtree.open_root(dst)
    st = fdtree.Stats()
    try:
        seen = fdtree.copy_tree(s, d, stats=st)
    finally:
        os.chmod(src / "locks" / "x.lock", 0o700)
        os.close(s)
        os.close(d)
    assert seen == {"ok/f"} and st.blind == ["locks/x.lock/"]
    assert st.covers("locks/x.lock/pid") and not st.covers("ok/f")


def test_skip_dir_is_not_entered(tmp_path):
    src = tmp_path / "src"
    (src / "opencode" / "locks").mkdir(parents=True)
    (src / "opencode" / "locks" / "a").write_text("x")
    (src / "opencode" / "b").write_text("y")
    s = fdtree.open_root(src)
    got = [rel for rel, *_ in fdtree.walk(s, skip_dir=lambda rel: rel == "opencode/locks")]
    os.close(s)
    assert got == ["opencode/b"]


def test_wipe_removes_everything_without_following_links(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("not mine to delete")
    disk = tmp_path / "disk"
    (disk / "a" / "b").mkdir(parents=True)
    (disk / "a" / "b" / "f").write_text("x")
    (disk / ".pth").write_text("import evil")
    os.symlink(victim, disk / "to-victim")
    os.symlink(victim / "keep", disk / "a" / "file-link")
    os.mkfifo(disk / "fifo")
    os.chmod(disk / "a" / "b", 0o500)            # 上一个使用者把目录改成只读
    os.chmod(disk / "a", 0o000)
    root = fdtree.open_root(disk)
    st = fdtree.Stats()
    fdtree.wipe_contents(root, stats=st)
    assert fdtree.is_empty(root)
    os.close(root)
    assert not st.errors, st.errors
    assert (victim / "keep").read_text() == "not mine to delete"


def test_wipe_leaves_a_nested_mount_point_alone(tmp_path, monkeypatch):
    disk = tmp_path / "disk"
    (disk / "mnt").mkdir(parents=True)
    (disk / "mnt" / "data").write_text("other device")
    real_stat = os.stat

    def fake_stat(path, *a, **kw):            # 把 mnt 伪装成另一个设备上的目录
        st = real_stat(path, *a, **kw)
        if path == "mnt":
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1) + tuple(st)[3:])
        return st
    monkeypatch.setattr(fdtree.os, "stat", fake_stat)
    root = fdtree.open_root(disk)
    st = fdtree.Stats()
    fdtree.wipe_contents(root, stats=st)
    os.close(root)
    assert (disk / "mnt" / "data").exists() and st.skipped == 1


def test_grant_group_adds_read_to_own_files_and_dirs_only(tmp_path):
    disk = tmp_path / "disk"
    (disk / "gdaa" / "sessions").mkdir(parents=True)
    f = disk / "gdaa" / "sessions" / "s.json"
    f.write_text("{}")
    os.chmod(f, 0o600)
    os.chmod(disk / "gdaa" / "sessions", 0o700)
    os.chmod(disk / "gdaa", 0o700)
    target = tmp_path / "elsewhere"             # 盘外的文件:不能经链接被改
    target.write_text("x")
    os.chmod(target, 0o600)
    os.symlink(target, disk / "gdaa" / "link")
    root = fdtree.open_root(disk)
    st = fdtree.Stats()
    fdtree.grant_group(root, stats=st)
    os.close(root)
    assert _mode(disk / "gdaa") & 0o050 == 0o050
    assert _mode(disk / "gdaa" / "sessions") & 0o050 == 0o050
    assert _mode(f) & 0o040 == 0o040
    assert _mode(target) == 0o600, "链接指向的文件不能被改"


# -- 接管属主(槽位在起 opencode 之前,把管家载入的文件换成自己的) ---------------------------

def _ino(p: pathlib.Path) -> int:
    return p.lstat().st_ino


def _adopt_tree(tmp_path):
    root = tmp_path / "t"
    (root / "d" / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("A", encoding="utf-8")
    (root / "opencode.db").write_text("DB", encoding="utf-8")
    (root / "d" / "f.txt").write_text("F", encoding="utf-8")
    (root / "d" / "sub" / "g").write_text("G", encoding="utf-8")
    (root / "d").chmod(0o2770)
    (root / "a.txt").chmod(0o660)
    os.utime(root / "a.txt", ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    os.symlink("/etc/passwd", root / "link")
    foreign = {_ino(root / n) for n in ("a.txt", "opencode.db", "d", "d/f.txt", "d/sub/g")}
    return root, foreign


def test_adopt_replaces_foreign_files_and_dirs_with_own_copies(tmp_path):
    root, foreign = _adopt_tree(tmp_path)
    before = {n: _ino(root / n) for n in ("a.txt", "opencode.db", "d", "d/sub", "d/f.txt", "d/sub/g")}
    st = fdtree.Stats()
    fd = fdtree.open_root(root)
    fdtree.adopt(fd, stats=st, foreign=lambda s: s.st_ino in foreign, keep_file=lambda n: n.endswith(".db"))
    os.close(fd)
    assert not st.errors
    for n in ("a.txt", "d", "d/f.txt", "d/sub/g"):
        assert _ino(root / n) != before[n], "%s 应换成新的 inode(属于槽位自己)" % n
    assert _ino(root / "opencode.db") == before["opencode.db"], "库文件不复制"
    assert _ino(root / "d" / "sub") == before["d/sub"], "自己的目录原样移过去"
    assert (root / "a.txt").read_text(encoding="utf-8") == "A" and (root / "d" / "sub" / "g").read_text(encoding="utf-8") == "G"
    assert _mode(root / "a.txt") == 0o660 and (root / "a.txt").stat().st_mtime_ns == 1_600_000_000_000_000_000
    assert _mode(root / "d") == 0o2770
    assert os.readlink(root / "link") == "/etc/passwd"
    assert not [n for n in os.listdir(root) if n.startswith(fdtree.ADOPT_PREFIX)]


def test_adopt_is_a_no_op_when_everything_is_already_own(tmp_path):
    root, _foreign = _adopt_tree(tmp_path)
    st = fdtree.Stats()
    fd = fdtree.open_root(root)
    fdtree.adopt(fd, stats=st)
    os.close(fd)
    assert st.files == 0 and not st.errors


def test_adopt_puts_children_back_when_a_dir_cannot_be_replaced(tmp_path, monkeypatch):
    root, foreign = _adopt_tree(tmp_path)
    real_rmdir = os.rmdir

    def rmdir(name, *a, **kw):
        if name == "d":
            raise PermissionError(13, "denied")
        return real_rmdir(name, *a, **kw)

    monkeypatch.setattr(fdtree.os, "rmdir", rmdir)
    st = fdtree.Stats()
    fd = fdtree.open_root(root)
    fdtree.adopt(fd, stats=st, foreign=lambda s: s.st_ino in foreign)
    os.close(fd)
    assert any("接管目录 d" in e for e in st.errors)
    assert sorted(os.listdir(root / "d")) == ["f.txt", "sub"], "内容不能散在两处"
    assert not [n for n in os.listdir(root) if n.startswith(fdtree.ADOPT_PREFIX)]
