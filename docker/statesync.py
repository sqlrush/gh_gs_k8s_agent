"""Pod 本地态 ⇄ NAS 同步(2026-09-21 策略变更)。

原策略:状态直接放在 NAS 上(`XDG_DATA_HOME=$NAS_ME/xdg-data`),每次写立刻落盘,
**零丢失窗口**,代价是 SQLite 跑在 NFS 上,写路径最慢的一环。

新策略:启动时 NAS → 本地,读写全在本地,定期回写,销毁前完整同步。
用户已确认接受「优雅关闭零丢失,非优雅关闭丢一个同步周期」。

**非优雅关闭包括**:OOMKilled(SIGKILL,没有 trap 的机会)、节点故障/断电、
`kubectl delete pod --force --grace-period=0`。这三种一个字节都同步不了,
是任何「本地读写 + 定期回写」方案的架构性代价,补不了 —— 只能让它可见(见 dirty 标记)。

四条硬规则:

① **SQLite 走 online backup API,绝不文件拷贝。** 开着 WAL 时主库/`-wal`/`-shm`
   三个文件不是任一瞬间都一致,拷出来的副本可能直接损坏,而且是静默的。
② **没 load 成功就不许传播删除。** 容器重建后本地是空的;这时跑一次「本地 → NAS」
   并传播删除,用户全部历史一次清空。所以删除只在 loaded 标记存在时才做。
③ **共享目录不本地化。** `/nas/kb` 是 runtime 读、kb-import 写的共享知识库,
   本地化意味着管理员刚导入的知识 runtime 看不到,**而且不报错**。
④ **加密凭据留在 NAS。** 本地化只是把密钥多复制一份到本地盘,没有性能收益。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

import fdtree

# 要本地化的子路径(相对 $NAS_ME)。**只加确实有性能收益的**,每加一项都放大丢失面。
LOCALIZED: Tuple[str, ...] = (
    "xdg-data",        # opencode.db 与 git 快照 —— 性能收益全在这
    "xdg-state",
    "workspace",       # git 仓 + 用户上传的文件
)
# **`gdaa` 整个留在 NAS**,两个原因:
#   · 它里面既有要本地化的 sessions/(登录句柄)又有绝不能本地化的 credentials/(加密凭据),
#     拆开就得靠符号链接 —— NFS + readOnlyRootFilesystem 上符号链接是 bug 的温床;
#   · 句柄是几百字节的小文件,本地化没有性能收益,不值得为它引入那份复杂度。
#   代价:非优雅退出时句柄还在而用那个句柄的对话丢了,用户重新 login 一次即可。

MARK_LOADED = ".state-loaded"
MARK_DIRTY = ".state-dirty"
SNAPSHOT_PREFIX = ".unclean-"
# M1(方案 §8.1):上次回写时各个库的签名,放在本地态根目录(不在 LOCALIZED 里,不会被搬)
SYNC_RECORD = ".state-sync.json"
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_SKIP_SUFFIXES = ("-wal", "-shm", ".db-journal")   # 由 backup API 负责,别单独拷


@dataclass(frozen=True)
class Layout:
    nas: pathlib.Path            # $NAS_ME
    local: pathlib.Path          # /data/state

    def pairs(self) -> List[Tuple[pathlib.Path, pathlib.Path]]:
        return [(self.nas / p, self.local / p) for p in LOCALIZED]


@dataclass
class Report:
    files_copied: int = 0
    dbs_copied: int = 0
    dbs_unchanged: int = 0      # M1:库自上次回写以来没变,跳过
    files_deleted: int = 0
    bytes_copied: int = 0
    skipped: int = 0            # 链接、FIFO 等非普通文件,不跟随也不复制
    refused_delete: bool = False
    errors: List[str] = field(default_factory=list)

    def line(self) -> str:
        s = "状态同步:文件 %d(%.1f MB)· 库 %d · 删除 %d" % (
            self.files_copied, self.bytes_copied / 1048576.0, self.dbs_copied, self.files_deleted)
        if self.dbs_unchanged:
            s += " · 库未变跳过 %d" % self.dbs_unchanged
        if self.skipped:
            s += " · 跳过非普通文件 %d(链接不跟随)" % self.skipped
        if self.refused_delete:
            s += " · **未传播删除**(本地没有 load 成功的标记)"
        if self.errors:
            s += " · 失败 %d 项:%s" % (len(self.errors), "; ".join(self.errors[:3]))
        return s


# --- 标记 ---------------------------------------------------------------------

def loaded(lay: Layout) -> bool:
    return (lay.local / MARK_LOADED).exists()


def is_dirty(lay: Layout) -> bool:
    return (lay.nas / MARK_DIRTY).exists()


def mark_dirty(lay: Layout) -> None:
    """开始用本地态了。走完收尾才清掉;下次启动还看得见就说明上次是非优雅退出。"""
    lay.nas.mkdir(parents=True, exist_ok=True)
    (lay.nas / MARK_DIRTY).write_text(str(int(time.time())), encoding="utf-8")


def clear_dirty(lay: Layout) -> None:
    try:
        (lay.nas / MARK_DIRTY).unlink()
    except FileNotFoundError:
        pass


# --- SQLite -------------------------------------------------------------------

def _is_db(p: pathlib.Path) -> bool:
    return p.suffix.lower() in _DB_SUFFIXES


def copy_db(src: pathlib.Path, dst: pathlib.Path) -> None:
    """一致性快照。**不要换成 shutil.copy** —— 见模块开头第 ① 条。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    try:
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
    except sqlite3.Error:
        tmp.unlink(missing_ok=True)     # 别留半截的 .part
        raise
    finally:
        s.close()
    os.replace(tmp, dst)          # 原子替换:同步中途被杀也不会留下半截的库


# --- 复制 ---------------------------------------------------------------------

def _db_signature(src: pathlib.Path) -> List[int]:
    """M1 的判据:主库文件的纳秒修改时间与大小,外加 `-wal` 的大小。"""
    st = src.stat()
    try:
        wal = os.stat(str(src) + "-wal").st_size
    except FileNotFoundError:
        wal = 0
    return [st.st_mtime_ns, st.st_size, wal]


def _db_unchanged(sig: List[int], recorded: Optional[List[int]], dst: pathlib.Path) -> bool:
    """**M1:库自上次回写以来没变就不回写**(方案 §8.1)。

    原来库一律复制,理由是拿源与**目标**比修改时间会漏掉同一秒内的写入(目标的时间是「同步那一刻」)。
    这里不跟目标比,跟**上次回写前记下的源签名**比,而且用纳秒——那条顾虑不成立了。
    `-wal` 非空时一律不跳过:checkpoint 遇到读者会返回 busy,WAL 里的提交还没进主库文件,
    只看主库文件会漏。目标不在(有人删了)也不跳过。
    """
    return sig[2] == 0 and recorded is not None and list(recorded) == sig and dst.exists()


def _copy_tree(src_root: pathlib.Path, dst_root: pathlib.Path, rep: Report,
               raw_if_unreadable_db: bool = False,
               record: Optional[Dict[str, List[int]]] = None, key_prefix: str = "") -> Set[str]:
    """复制 src_root → dst_root,返回相对路径集合(给删除传播用)。

    普通文件经 fdtree(逐级不跟随链接,链接与 FIFO 计入 skipped;2026-10-14 池化评审后两种模式共用);
    库仍走 online backup API——现有模式下搬运方是主容器自己,只看得到本人目录。

    raw_if_unreadable_db:库连一致性读取都做不到(已损坏)时原样复制。**只给 NAS → 本地用。**
    加载时不带过来,本地就没有库,podctl.db_check 看到的是「不存在 = 首次启动」,
    于是以空库启动、历史静默清空,下一次回写再拿空库盖掉 NAS 上的坏库 —— 连证据都没了。
    原样带过来,db_check 才能走它本来的「留证、从备份恢复、写明」。
    反方向绝不能这样做:本地坏库原样推上去会盖掉 NAS 上那份好的。

    record:给了就启用 M1(只在本地 → NAS 方向),键为 key_prefix + 相对路径。
    """
    dst_root.mkdir(parents=True, exist_ok=True)
    if not src_root.exists():
        return set()
    dbs: List[str] = []

    def skip(rel: str) -> bool:
        name = rel.rpartition("/")[2]
        if name.endswith(_SKIP_SUFFIXES):
            return True
        if name.lower().endswith(_DB_SUFFIXES):
            dbs.append(rel)
            return True
        return False

    sfd, dfd = fdtree.open_root(src_root), fdtree.open_root(dst_root)
    try:
        st = fdtree.Stats()
        seen = fdtree.copy_tree(sfd, dfd, stats=st, skip=skip)
    finally:
        os.close(sfd)
        os.close(dfd)
    rep.files_copied += st.files
    rep.bytes_copied += st.bytes
    rep.skipped += st.skipped
    rep.errors.extend(st.errors)
    for rel in dbs:
        src, dst = src_root / rel, dst_root / rel
        if src.is_symlink() or not src.is_file():
            rep.skipped += 1
            continue
        seen.add(rel)
        try:
            sig = _db_signature(src)            # 复制**之前**取:复制途中的写入会让下一轮的签名对不上
            if record is not None and _db_unchanged(sig, record.get(key_prefix + rel), dst):
                rep.dbs_unchanged += 1
                continue
            try:
                copy_db(src, dst)
            except sqlite3.DatabaseError as exc:
                if not raw_if_unreadable_db:
                    raise
                shutil.copy2(src, dst)
                rep.errors.append("%s:库无法读取(%s),已原样带到本地交给启动自检" % (rel, exc))
                continue
            rep.dbs_copied += 1
            if record is not None:
                record[key_prefix + rel] = sig
        except (OSError, sqlite3.Error) as exc:
            # 一个文件坏了不能让整次同步停摆 —— 记下来,其余照抄
            rep.errors.append("%s:%s" % (rel, exc))
    return seen


def _protected(rel: str) -> bool:
    name = rel.rpartition("/")[2]
    return name in (MARK_LOADED, MARK_DIRTY) or name.endswith(_SKIP_SUFFIXES)


def _prune(dst_root: pathlib.Path, keep: Set[str], rep: Report) -> None:
    if not dst_root.exists():
        return
    dfd = fdtree.open_root(dst_root)
    try:
        st = fdtree.Stats()
        fdtree.prune(dfd, keep, stats=st, protect=_protected)
    finally:
        os.close(dfd)
    rep.files_deleted += st.deleted
    rep.errors.extend(st.errors)


def _load_record(lay: Layout) -> Dict[str, List[int]]:
    try:
        data = json.loads((lay.local / SYNC_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}                               # 没有或坏了:这一轮全部照拷,不会漏
    return data if isinstance(data, dict) else {}


def _save_record(lay: Layout, record: Dict[str, List[int]]) -> None:
    fd = fdtree.open_root(lay.local)
    try:
        fdtree.write_small(fd, SYNC_RECORD, json.dumps(record).encode("utf-8"))
    finally:
        os.close(fd)


# --- 对外三个动作 -------------------------------------------------------------

def load(lay: Layout, log: Callable[[str], None] = print) -> Report:
    """启动:NAS → 本地。上次是非优雅退出时,先给 NAS 留一份快照。"""
    rep = Report()
    if is_dirty(lay):
        # 快照必须放在 **$NAS_ME 里面**。`/nas` 本身是只读的(只有 /nas/me 与 /nas/kb
        # 这两个 subPath 挂载点可写),放同级目录永远建不出来 —— 2026-09-21 真跑时
        # 报的是 `[Errno 30] Read-only file system: '/nas/me.unclean-…'`。
        # 只快照 LOCALIZED 那几个子目录:既避免把快照目录自己也拷进去,又够用。
        snap = lay.nas / (SNAPSHOT_PREFIX + "%d" % int(time.time()))
        try:
            for sub in LOCALIZED:
                src = lay.nas / sub
                if src.exists():
                    shutil.copytree(src, snap / sub, dirs_exist_ok=True)
            log("⚠ 上次是**非优雅**退出(dirty 标记还在):NAS 上那份已先快照到 %s/。"
                "这次启动会用它继续,但上次最后一个同步周期内的对话可能已丢失。"
                % snap.relative_to(lay.nas.parent))
        except OSError as exc:
            log("⚠ 上次是**非优雅**退出,但快照失败(%s);继续启动 —— "
                "上次最后一个同步周期内的对话可能已丢失。" % exc)
    lay.local.mkdir(parents=True, exist_ok=True)
    for nas_dir, local_dir in lay.pairs():
        _copy_tree(nas_dir, local_dir, rep, raw_if_unreadable_db=True)
    (lay.local / MARK_LOADED).write_text(str(int(time.time())), encoding="utf-8")
    log("状态已加载到本地:%s" % rep.line())
    return rep


def sync(lay: Layout, log: Callable[[str], None] = lambda _m: None) -> Report:
    """定期 / 收尾:本地 → NAS。没有 loaded 标记时**不传播删除**(见第 ② 条)。"""
    rep = Report()
    may_delete = loaded(lay)
    rep.refused_delete = not may_delete
    record = _load_record(lay)
    for sub, (nas_dir, local_dir) in zip(LOCALIZED, lay.pairs()):
        seen = _copy_tree(local_dir, nas_dir, rep, record=record, key_prefix=sub + "/")
        if may_delete:
            _prune(nas_dir, seen, rep)
    if lay.local.is_dir():
        try:
            _save_record(lay, record)
        except OSError as exc:              # 记不下来只是下一轮多拷一次,不影响正确性
            rep.errors.append("%s:%s" % (SYNC_RECORD, exc))
    log(rep.line())
    return rep


def finish(lay: Layout, log: Callable[[str], None] = print) -> Report:
    """销毁前:完整同步一次,然后清掉 dirty 标记 —— 顺序不能反。"""
    rep = sync(lay, log=lambda _m: None)
    clear_dirty(lay)
    log("收尾同步完成并已清 dirty 标记。%s" % rep.line())
    return rep
