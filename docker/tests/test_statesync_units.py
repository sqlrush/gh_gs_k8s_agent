"""Pod 本地态与 NAS 的同步(2026-09-21 策略变更)。

原策略:状态直接放 NAS(XDG_DATA_HOME=$NAS_ME/xdg-data),每次写立刻落盘,零丢失窗口。
新策略:启动时 NAS → 本地,读写在本地,定期回写,销毁前完整同步。用户已确认接受
「非优雅关闭丢一个同步周期」。

**这块代码能把用户的历史会话弄丢,所以测试按最坏情况写:**

① SQLite **绝不能文件拷贝**。开着 WAL 时主库/-wal/-shm 三个文件不是任一瞬间都一致,
   拷出来的副本可能直接损坏,而且是静默的 —— 下次启动才发现。必须走 online backup API。
② **没 load 过就不许 sync。** 容器重建后本地是空的;如果这时候跑一次「本地 → NAS」
   的同步并传播删除,用户全部历史一次清空。必须有 load 成功的标记才允许传播删除。
③ **/nas/kb 绝不本地化。** 它是共享的:runtime 读、kb-import 写。本地化意味着管理员
   刚导入的知识 runtime 看不到,而且不报错。
④ **非优雅退出要留痕。** dirty 标记还在就说明上次没走完收尾,要在覆盖 NAS 之前
   留一份快照并大声记一行 —— 否则用户只会觉得「我明明问过那个问题」。
"""
import pathlib
import sqlite3
import sys
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import statesync as ss  # noqa: E402


def _mkdb(path: pathlib.Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table if not exists t (i integer)")
    con.executemany("insert into t values (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()


def _rowcount(path: pathlib.Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("select count(*) from t").fetchone()[0]
    finally:
        con.close()


@pytest.fixture()
def dirs(tmp_path):
    nas = tmp_path / "nas" / "me"
    local = tmp_path / "data" / "state"
    (nas / "xdg-data" / "opencode").mkdir(parents=True)
    (nas / "workspace").mkdir(parents=True)
    return ss.Layout(nas=nas, local=local)


# --- 启动加载 -----------------------------------------------------------------

def test_load_brings_nas_state_to_local(dirs):
    (dirs.nas / "workspace" / "a.sql").write_text("select 1", encoding="utf-8")
    _mkdb(dirs.nas / "xdg-data" / "opencode" / "opencode.db")
    ss.load(dirs)
    assert (dirs.local / "workspace" / "a.sql").read_text(encoding="utf-8") == "select 1"
    assert _rowcount(dirs.local / "xdg-data" / "opencode" / "opencode.db") == 3


def test_corrupt_nas_db_still_reaches_local_so_the_startup_check_can_restore_it(dirs):
    """NAS 上的库坏了,加载也必须把它带到本地 —— 否则自检看到「库不存在」就当首次启动。

    2026-09-23 v1.1.0 冒烟抓到:坏库的一致性读取失败,本地没有库,自检打印「首次启动,
    opencode 将新建」,用户全部历史**静默**变成空列表;下一次回写再用空库盖掉 NAS 上的坏库,
    连证据都没了。备份目录里明明有好的备份。

    修法:NAS → 本地方向,读不了的库原样复制,交给 podctl.db_check 去「留证、恢复、写明」。
    """
    import podctl
    oc = dirs.nas / "xdg-data" / "opencode"
    (oc / "opencode.db").write_bytes(b"garbage" * 2000)
    backup = dirs.nas / "backup"
    _mkdb(backup / "opencode.db.20260923T000000Z", rows=7)

    ss.load(dirs, log=lambda _m: None)
    local_db = dirs.local / "xdg-data" / "opencode" / "opencode.db"
    assert local_db.exists(), "坏库没带到本地,自检会误判成首次启动"
    assert podctl.db_check(local_db, backup) == "restored"
    assert _rowcount(local_db) == 7


def test_corrupt_local_db_is_never_pushed_raw_to_nas(dirs):
    """反方向不能原样复制:本地坏库推上 NAS 会盖掉 NAS 上那份好的。"""
    _mkdb(dirs.nas / "xdg-data" / "opencode" / "opencode.db", rows=5)
    ss.load(dirs, log=lambda _m: None)
    (dirs.local / "xdg-data" / "opencode" / "opencode.db").write_bytes(b"garbage" * 2000)
    rep = ss.sync(dirs)
    assert rep.errors, "本地坏库应当记成一条同步错误"
    assert _rowcount(dirs.nas / "xdg-data" / "opencode" / "opencode.db") == 5


def test_load_on_empty_nas_is_fine(dirs):
    """第一次登录:NAS 上什么都没有。"""
    ss.load(dirs)
    assert (dirs.local / "workspace").is_dir()


def test_load_writes_the_loaded_marker(dirs):
    ss.load(dirs)
    assert ss.loaded(dirs), "没有这个标记,sync 不许传播删除"


# --- 定期回写 -----------------------------------------------------------------

def test_sync_pushes_local_changes_to_nas(dirs):
    ss.load(dirs)
    (dirs.local / "workspace" / "new.sql").write_text("select 2", encoding="utf-8")
    ss.sync(dirs)
    assert (dirs.nas / "workspace" / "new.sql").read_text(encoding="utf-8") == "select 2"


def test_sync_uses_sqlite_backup_not_file_copy(dirs):
    """**核心**:db 正开着也要能同步出一份能打开的库。"""
    db = dirs.local / "xdg-data" / "opencode" / "opencode.db"
    ss.load(dirs)
    _mkdb(db, rows=5)
    con = sqlite3.connect(db)                      # 持着连接不放,模拟 opencode 正在跑
    con.execute("pragma journal_mode=WAL")
    con.execute("insert into t values (99)")
    con.commit()
    try:
        ss.sync(dirs)
    finally:
        con.close()
    out = dirs.nas / "xdg-data" / "opencode" / "opencode.db"
    assert _rowcount(out) == 6, "同步出来的库要包含已提交的那一行"


def test_m1_an_unchanged_db_is_not_copied_again(dirs):
    """M1(方案 §8.1):原来每 60 秒整库复制一遍,闲着的用户也一样;三年 500 MB 的库、16 人在线会超过千兆 NAS 的上限。"""
    ss.load(dirs)
    db = dirs.local / "xdg-data" / "opencode" / "opencode.db"
    _mkdb(db, rows=2)
    assert ss.sync(dirs).dbs_copied == 1
    rep = ss.sync(dirs)
    assert rep.dbs_copied == 0 and rep.dbs_unchanged == 1
    assert "库未变跳过 1" in rep.line()


def test_m1_a_same_size_write_within_the_same_second_is_still_copied(dirs):
    """**原来「库一律复制」防的就是这个(2026-09-21 踩点)**:拿源和**目标**比修改时间,目标的时间是「同步那一刻」,
    会漏掉同一秒内的写入;SQLite 原地改页时大小也不变。M1 跟**上次记下的源签名**比、而且用纳秒,所以照样抓得到。"""
    ss.load(dirs)
    db = dirs.local / "xdg-data" / "opencode" / "opencode.db"
    _mkdb(db, rows=2)
    ss.sync(dirs)
    size = db.stat().st_size
    con = sqlite3.connect(db)                      # 回滚日志模式:改页直接落主库文件,大小不变
    con.execute("update t set i = i + 100")
    con.commit()
    con.close()
    assert db.stat().st_size == size
    assert ss.sync(dirs).dbs_copied == 1
    con = sqlite3.connect(dirs.nas / "xdg-data" / "opencode" / "opencode.db")
    assert con.execute("select min(i) from t").fetchone()[0] == 100
    con.close()


def test_m1_never_skips_while_the_wal_still_holds_commits(dirs):
    """checkpoint 遇到读者返回 busy 时,提交还在 -wal 里、主库文件一字节没变——只看主库文件会漏。"""
    ss.load(dirs)
    db = dirs.local / "xdg-data" / "opencode" / "opencode.db"
    _mkdb(db, rows=2)
    ss.sync(dirs)
    con = sqlite3.connect(db)
    con.execute("pragma journal_mode=WAL")
    con.execute("insert into t values (7)")
    con.commit()
    try:
        assert pathlib.Path(str(db) + "-wal").stat().st_size > 0
        assert ss.sync(dirs).dbs_copied == 1
    finally:
        con.close()
    assert _rowcount(dirs.nas / "xdg-data" / "opencode" / "opencode.db") == 3


def test_m1_recopies_when_the_nas_copy_is_gone(dirs):
    ss.load(dirs)
    db = dirs.local / "xdg-data" / "opencode" / "opencode.db"
    _mkdb(db, rows=2)
    ss.sync(dirs)
    (dirs.nas / "xdg-data" / "opencode" / "opencode.db").unlink()
    assert ss.sync(dirs).dbs_copied == 1


def test_m1_record_is_not_synced_to_nas(dirs):
    ss.load(dirs)
    _mkdb(dirs.local / "xdg-data" / "opencode" / "opencode.db")
    ss.sync(dirs)
    assert (dirs.local / ss.SYNC_RECORD).exists()
    assert not list(dirs.nas.rglob(ss.SYNC_RECORD))


def test_a_symlink_in_the_workspace_is_not_followed_on_sync(dirs, tmp_path):
    """现有模式下搬运方是主容器自己,没有跨用户的后果;改走 fdtree 之后同样不跟随,只是更严。"""
    other = tmp_path / "other"
    other.mkdir()
    (other / "secret").write_text("not mine", encoding="utf-8")
    ss.load(dirs)
    (dirs.local / "workspace" / "x").symlink_to(other / "secret")
    rep = ss.sync(dirs)
    assert not (dirs.nas / "workspace" / "x").exists()
    assert rep.skipped == 1 and "链接不跟随" in rep.line()


def test_unchanged_files_are_not_recopied(dirs):
    ss.load(dirs)
    f = dirs.local / "workspace" / "a.sql"
    f.write_text("x", encoding="utf-8")
    ss.sync(dirs)
    target = dirs.nas / "workspace" / "a.sql"
    stamp = target.stat().st_mtime_ns
    time.sleep(0.01)
    report = ss.sync(dirs)
    assert target.stat().st_mtime_ns == stamp, "没变的文件不该重写"
    assert report.files_copied == 0


def test_local_deletion_propagates_after_a_successful_load(dirs):
    """用户删掉的会话不能在下次同步时复活。"""
    (dirs.nas / "workspace" / "gone.sql").write_text("x", encoding="utf-8")
    ss.load(dirs)
    (dirs.local / "workspace" / "gone.sql").unlink()
    ss.sync(dirs)
    assert not (dirs.nas / "workspace" / "gone.sql").exists()


def test_sync_without_load_never_deletes_on_nas(dirs):
    """**最危险的一条。** 容器重建后本地是空的,这时同步若传播删除,用户全部历史一次清空。"""
    (dirs.nas / "workspace" / "precious.sql").write_text("十年的积累", encoding="utf-8")
    _mkdb(dirs.nas / "xdg-data" / "opencode" / "opencode.db")
    assert not ss.loaded(dirs)
    report = ss.sync(dirs)
    assert (dirs.nas / "workspace" / "precious.sql").exists(), "没 load 过就敢删 NAS —— 绝对不行"
    assert report.refused_delete, "要明确报出「拒绝传播删除」,不能静默跳过"


# --- 非优雅退出的痕迹 ---------------------------------------------------------

def test_clean_shutdown_clears_the_dirty_mark(dirs):
    ss.load(dirs)
    ss.mark_dirty(dirs)
    ss.finish(dirs)
    assert not ss.is_dirty(dirs)


def test_unclean_shutdown_is_detected_on_next_start(dirs):
    ss.load(dirs)
    ss.mark_dirty(dirs)
    # 没有 finish —— 模拟 OOMKill
    assert ss.is_dirty(dirs)


def test_unclean_start_snapshots_nas_before_overwriting(dirs):
    """上次没走完收尾 → 覆盖 NAS 之前先留一份,并让这件事可见。"""
    (dirs.nas / "workspace" / "half.sql").write_text("半截", encoding="utf-8")
    ss.mark_dirty(dirs)
    logged = []
    ss.load(dirs, log=logged.append)
    snaps = list(dirs.nas.glob(ss.SNAPSHOT_PREFIX + "*"))
    assert snaps, "要留快照"
    assert (snaps[0] / "workspace" / "half.sql").read_text(encoding="utf-8") == "半截"
    assert any("非优雅" in m for m in logged), "要大声记一行"


def test_snapshot_goes_inside_nas_me_not_beside_it(dirs):
    """**踩点(2026-09-21 真跑时发现)。**

    原来快照建在 $NAS_ME 的**同级**(`/nas/me.unclean-N`),而容器里 `/nas` 本身是只读的
    —— 只有 /nas/me 与 /nas/kb 这两个 subPath 挂载点可写。结果快照永远建不出来,
    报 `[Errno 30] Read-only file system`,而「上次丢过数据」这件事就此不可见。
    """
    assert not ss.SNAPSHOT_PREFIX.startswith("/")
    ss.mark_dirty(dirs)
    ss.load(dirs, log=lambda _m: None)
    assert list(dirs.nas.glob(ss.SNAPSHOT_PREFIX + "*")), "快照要落在 $NAS_ME 里面"
    assert not list(dirs.nas.parent.glob("me" + ss.SNAPSHOT_PREFIX + "*")), "不能建在同级"


def test_snapshot_is_not_synced_back_or_pruned(dirs):
    """快照是给人事后查的,不能被下一轮同步当成用户数据搬来搬去或删掉。"""
    ss.mark_dirty(dirs)
    ss.load(dirs, log=lambda _m: None)
    snap = list(dirs.nas.glob(ss.SNAPSHOT_PREFIX + "*"))[0]
    (snap / "workspace").mkdir(parents=True, exist_ok=True)
    (snap / "workspace" / "keep.sql").write_text("x", encoding="utf-8")
    ss.sync(dirs)
    assert (snap / "workspace" / "keep.sql").exists(), "快照被同步逻辑删了"
    assert not (dirs.local / snap.name).exists(), "快照不该被拷到本地"


# --- 共享目录不许碰 -----------------------------------------------------------

def test_kb_is_never_in_the_localized_set():
    """kb 是共享的:runtime 读、kb-import 写。本地化 = 管理员刚导入的知识 runtime 看不到,
    而且不报错。"""
    for p in ss.LOCALIZED:
        assert not p.startswith("kb"), p
    assert "kb" not in ss.LOCALIZED


def test_gdaa_stays_entirely_on_nas():
    """`gdaa` 里既有该本地化的 sessions/ 又有绝不能本地化的 credentials/(加密凭据)。

    拆开要靠符号链接,而 NFS + readOnlyRootFilesystem 上符号链接是 bug 的温床;
    句柄是几百字节的小文件,本地化没有性能收益。所以整个留在 NAS。
    """
    for p in ss.LOCALIZED:
        assert not p.startswith("gdaa"), "%s:凭据不能落本地盘,句柄也没必要" % p


def test_localized_set_stays_small():
    """每加一项都放大非优雅退出的丢失面,所以这张表要有人守着。"""
    assert set(ss.LOCALIZED) == {"xdg-data", "xdg-state", "workspace"}
