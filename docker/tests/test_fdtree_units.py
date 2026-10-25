"""按文件描述符的目录树操作(池化方案 §9,2026-10-14 评审)。

固定池化的管家挂着**全部**用户目录,却要搬运一块「模型能执行命令的槽位」可写的盘。
按路径复制时,目录里的一个符号链接就能让它把别人的文件拷进来;
「先 lstat、是链接就跳过」也不够——检查与打开之间可以被换掉。
所以这里的每一条测试都是一种「诱使搬运方读到或写到根目录之外」的手法。
"""
import errno
import os
import pathlib
import stat
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import fdtree  # noqa: E402


@pytest.fixture()
def roots(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    other = tmp_path / "other"          # 「别人的目录」:任何内容都不许出现在 dst 里
    for p in (src, dst, other):
        p.mkdir()
    (other / "secret.db").write_bytes(b"u9999 history")
    s, d = fdtree.open_root(src), fdtree.open_root(dst)
    yield src, dst, other, s, d
    os.close(s)
    os.close(d)


def _files(root: pathlib.Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


# --- 正常复制 -----------------------------------------------------------------

def test_copy_tree_copies_nested_regular_files_and_keeps_mtime(roots):
    src, dst, _o, s, d = roots
    (src / "a" / "b").mkdir(parents=True)
    (src / "a" / "b" / "x.sql").write_text("select 1", encoding="utf-8")
    (src / "top.txt").write_text("t", encoding="utf-8")
    os.utime(src / "top.txt", ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    stats = fdtree.Stats()
    seen = fdtree.copy_tree(s, d, stats=stats)
    assert seen == {"a/b/x.sql", "top.txt"}
    assert _files(dst) == {"a/b/x.sql": b"select 1", "top.txt": b"t"}
    assert (dst / "top.txt").stat().st_mtime_ns == 1_700_000_000_123_456_789
    assert stats.files == 2 and stats.skipped == 0 and not stats.errors


def test_unchanged_files_are_not_rewritten(roots):
    src, dst, _o, s, d = roots
    (src / "f").write_text("v1", encoding="utf-8")
    fdtree.copy_tree(s, d, stats=fdtree.Stats())
    ino = (dst / "f").stat().st_ino
    stats = fdtree.Stats()
    fdtree.copy_tree(s, d, stats=stats)
    assert stats.files == 0 and (dst / "f").stat().st_ino == ino


def test_a_same_size_edit_within_the_same_second_is_still_copied(roots):
    """旧代码按整秒比修改时间,同一秒内改了且大小不变就漏掉;保留纳秒之后不会。"""
    src, dst, _o, s, d = roots
    (src / "f").write_text("v1", encoding="utf-8")
    os.utime(src / "f", ns=(1_700_000_000_100_000_000,) * 2)
    fdtree.copy_tree(s, d, stats=fdtree.Stats())
    (src / "f").write_text("v2", encoding="utf-8")
    os.utime(src / "f", ns=(1_700_000_000_900_000_000,) * 2)
    fdtree.copy_tree(s, d, stats=fdtree.Stats())
    assert (dst / "f").read_text(encoding="utf-8") == "v2"


def test_a_destination_with_whole_second_timestamps_does_not_recopy_every_cycle(roots):
    """NAS 只存到秒时,纳秒逐位比较会让每个周期都重拷;按目标的精度比。"""
    src, dst, _o, s, d = roots
    (src / "f").write_text("v1", encoding="utf-8")
    os.utime(src / "f", ns=(1_700_000_000_123_456_789,) * 2)
    fdtree.copy_tree(s, d, stats=fdtree.Stats())
    os.utime(dst / "f", ns=(1_700_000_000_000_000_000,) * 2)     # 模拟秒级精度的文件系统
    stats = fdtree.Stats()
    fdtree.copy_tree(s, d, stats=stats)
    assert stats.files == 0


def test_files_created_after_the_root_was_opened_are_listed_on_the_first_walk(tmp_path):
    """overlayfs 上对旧描述符第一次 scandir 返回空(2026-10-14 在 python:3.12-slim 的 /tmp 实测)。
    root 描述符在 bind 时就打开、文件是之后写进来的——这正是池化的常态。"""
    fd = fdtree.open_root(tmp_path)
    try:
        (tmp_path / "late").write_text("x", encoding="utf-8")
        assert [rel for rel, *_ in fdtree.walk(fd)] == ["late"]
    finally:
        os.close(fd)


def test_skip_predicate_excludes_files_from_copy_and_from_seen(roots):
    src, dst, _o, s, d = roots
    (src / "opencode.db").write_bytes(b"db")
    (src / "keep.txt").write_text("k", encoding="utf-8")
    seen = fdtree.copy_tree(s, d, stats=fdtree.Stats(), skip=lambda rel: rel.endswith(".db"))
    assert seen == {"keep.txt"} and not (dst / "opencode.db").exists()


# --- 读的一侧:源目录不可信 ---------------------------------------------------

def test_a_symlink_to_someone_elses_file_is_not_followed(roots):
    src, dst, other, s, d = roots
    (src / "x").symlink_to(other / "secret.db")
    stats = fdtree.Stats()
    seen = fdtree.copy_tree(s, d, stats=stats)
    assert seen == set() and _files(dst) == {} and stats.skipped == 1


def test_a_symlinked_directory_is_not_descended(roots):
    src, dst, other, s, d = roots
    (src / "ws").mkdir()
    (src / "ws" / "d").symlink_to(other)
    fdtree.copy_tree(s, d, stats=fdtree.Stats())
    assert _files(dst) == {}


def test_a_fifo_is_skipped_and_does_not_hang(roots):
    src, dst, _o, s, d = roots
    os.mkfifo(src / "pipe")
    stats = fdtree.Stats()
    assert fdtree.copy_tree(s, d, stats=stats) == set()
    assert stats.skipped == 1


def test_a_file_swapped_for_a_link_after_the_check_is_refused_at_open(roots):
    """「检查之后、打开之前」被换成链接:walk 看到的是普通文件,打开时已是链接。
    打开本身带 O_NOFOLLOW,所以换上去的链接在打开那一刻被拒,而不是靠前面那次检查。"""
    src, dst, other, s, d = roots
    (src / "f").write_bytes(b"mine")
    listed = os.stat("f", dir_fd=s, follow_symlinks=False)
    assert stat.S_ISREG(listed.st_mode)                     # 检查时还是普通文件
    (src / "f").unlink()
    (src / "f").symlink_to(other / "secret.db")             # 抢先换掉
    with pytest.raises(fdtree.UnsafePath):
        fdtree.copy_file(s, "f", d, "f")
    assert _files(dst) == {}


def test_a_file_on_another_device_is_refused(roots):
    src, _dst, _o, s, d = roots
    (src / "f").write_bytes(b"x")
    with pytest.raises(fdtree.UnsafePath):
        fdtree.copy_file(s, "f", d, "f", dev=os.fstat(s).st_dev + 1)


# --- 写的一侧:目标目录也可能被预先埋了链接 -------------------------------------

def test_an_intermediate_destination_directory_that_is_a_link_is_refused(roots):
    src, dst, other, s, d = roots
    (src / "sub").mkdir()
    (src / "sub" / "f").write_bytes(b"mine")
    (dst / "sub").symlink_to(other)                         # 目标里预先埋了链接
    stats = fdtree.Stats()
    fdtree.copy_tree(s, d, stats=stats)
    assert sorted(p.name for p in other.iterdir()) == ["secret.db"]     # 没往别人目录里写
    assert stats.errors and "sub" in stats.errors[0]


def test_a_destination_file_that_is_a_link_is_replaced_not_written_through(roots):
    src, dst, other, s, d = roots
    (src / "f").write_bytes(b"mine")
    (dst / "f").symlink_to(other / "secret.db")
    fdtree.copy_tree(s, d, stats=fdtree.Stats())
    assert (other / "secret.db").read_bytes() == b"u9999 history"
    assert not (dst / "f").is_symlink() and (dst / "f").read_bytes() == b"mine"


def test_open_path_rejects_dotdot_and_absolute_parts(roots):
    _src, _dst, _o, s, _d = roots
    for bad in ("../x", "a/../../x", "/etc", "a//b", "."):
        with pytest.raises(ValueError):
            fdtree.open_path(s, bad, create=True)


def test_open_path_creates_missing_directories_and_refuses_links(roots):
    src, _dst, other, s, _d = roots
    fd = fdtree.open_path(s, "a/b/c", create=True)
    os.close(fd)
    assert (src / "a" / "b" / "c").is_dir()
    (src / "l").symlink_to(other)
    with pytest.raises(fdtree.UnsafePath):
        fdtree.open_path(s, "l/x", create=True)
    assert fdtree.open_path(s, "missing/x", create=False) is None


# --- 删除传播 -----------------------------------------------------------------

def test_prune_removes_unkept_files_and_links_but_never_follows_them(roots):
    _src, dst, other, _s, d = roots
    (dst / "keep").write_bytes(b"k")
    (dst / "gone").write_bytes(b"g")
    (dst / "planted").symlink_to(other / "secret.db")
    (dst / "dir").mkdir()
    (dst / "dir" / "gone2").write_bytes(b"g")
    (dst / "ldir").symlink_to(other)
    (dst / ".state-dirty").write_text("1", encoding="utf-8")
    stats = fdtree.Stats()
    fdtree.prune(d, {"keep"}, stats=stats, protect=lambda rel: rel == ".state-dirty")
    assert sorted(p.name for p in dst.iterdir()) == [".state-dirty", "dir", "keep"]
    assert list((dst / "dir").iterdir()) == []
    assert (other / "secret.db").read_bytes() == b"u9999 history"
    assert stats.deleted == 4


# --- 小文件 -------------------------------------------------------------------

def test_read_small_refuses_links_and_oversized_files(roots):
    src, _dst, other, s, _d = roots
    (src / "ok.json").write_text("{}", encoding="utf-8")
    (src / "l.json").symlink_to(other / "secret.db")
    (src / "big").write_bytes(b"x" * 100)
    assert fdtree.read_small(s, "ok.json") == b"{}"
    assert fdtree.read_small(s, "missing") is None
    with pytest.raises(fdtree.UnsafePath):
        fdtree.read_small(s, "l.json")
    with pytest.raises(fdtree.UnsafePath):
        fdtree.read_small(s, "big", limit=10)


def test_write_small_replaces_a_planted_link_instead_of_writing_through_it(roots):
    src, _dst, other, s, _d = roots
    (src / ".state-dirty").symlink_to(other / "secret.db")
    fdtree.write_small(s, ".state-dirty", b"123")
    assert (other / "secret.db").read_bytes() == b"u9999 history"
    assert (src / ".state-dirty").read_bytes() == b"123" and not (src / ".state-dirty").is_symlink()
    assert stat.S_IMODE((src / ".state-dirty").stat().st_mode) == 0o600


def test_temporary_files_end_with_part_so_both_sync_directions_skip_them(roots):
    """写到一半被杀留下的临时文件,下一轮不能被当成用户文件搬来搬去。"""
    name = fdtree._tmp_name("opencode.db")
    assert name.startswith(".") and name.endswith(".part")
    assert fdtree.is_temporary(name)
