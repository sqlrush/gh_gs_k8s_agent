"""池化布局的状态搬运——固定池化的管家用(NAS 的 users/ ↔ 槽位的状态盘)。

管家挂着全部用户目录(`/pool/users`)与各槽位的状态盘(`/pool/slots/<i>/state`)。两块盘都按**不可信**处理:
  · 状态盘从 opencode 启动那一刻起由槽位写,而槽位里的模型能执行命令;
  · NAS 上的本人目录在按需模式下由用户的 Pod 直接读写,可能预先埋了链接。
所以这里对两边的一切读写都经 `fdtree`(逐级不跟随、打开后确认普通文件),**绑定之后不用 SQLite 打开
state 卷上的任何文件**:库的一致性快照由槽位生成在 `.pool/snap/`,这里只把快照当普通文件搬。

沿用现有模式(statesync)的规则:dirty 标记、非正常退出先给 NAS 留快照、没载入成功不传播删除、
库自检与从备份恢复、`.owner` 独占(30 秒刷新、90 秒过期)。

调用方(固定池化的管家,poolslot)负责打开并持有三个根描述符:`users/` 根、本人目录、槽位的状态盘根。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import fdtree
import podctl
import statesync

# 池化下本地化的子目录。比现有模式多两项:gdaa/sessions(登录句柄)与 reports(大盘的数据)——
# 槽位看不到 NAS,它们只能先载入本地。gdaa 下其余内容(config.yaml 每次绑定重新生成;
# credentials/ 与 key 容器版用不到)不载入、不回写,NAS 上原样保留(方案 §8)。
POOL_LOCALIZED: Tuple[str, ...] = ("xdg-data", "xdg-state", "workspace", "gdaa/sessions", "reports")
POOL_DIR = ".pool"                      # state 卷上两个容器之间的工作区,不回写 NAS
SNAP_DIR = POOL_DIR + "/snap"           # 槽位生成的库快照:<子目录>/<相对路径>
RESTORE_DIR = POOL_DIR + "/restore"     # 自检时从 NAS 备份拷过来待检的副本
MAIN_DB = "xdg-data/opencode/opencode.db"
BACKUP_DIR = "backup"
OWNER = podctl.OWNER_NAME
MARK_DIRTY = statesync.MARK_DIRTY
SNAPSHOT_PREFIX = statesync.SNAPSHOT_PREFIX
OWNER_STALE_S = 90
USER_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,52}[a-z0-9])?$")   # 与网关同一条规则
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# 版本令牌(固定池化,2026-10-23):每次取得独占标记时换一个新的随机值。保留在槽位里的副本记着当时的令牌,
# 回来时与 NAS 上的比:一样 = 这期间没人在别处用过,本地那份可以直接用;不一样 = 作废。
# 用随机值不用计数器:独占标记 90 秒过期被接管时,两边可能同时写出同一个「n+1」。
GEN = podctl.GEN_NAME


@dataclass(frozen=True)
class Modes:
    """目标一侧的权限。file=None 表示继承源权限(v1.2.0 的行为)。"""
    file: Optional[int]
    dir: int


LEGACY_MODES = Modes(file=None, dir=0o700)
NAS_MODES = Modes(file=0o600, dir=0o700)          # NAS 上只给管家(及按需模式的 Pod,同为 uid 1000)
SLOT_MODES = Modes(file=0o660, dir=0o2770)        # 槽位盘:管家 uid 1000 与槽位 uid 2000+i 同组 1000


def _skip_dir(sub: str) -> Callable[[str], bool]:
    """不搬的子目录:opencode 的锁目录(0700、临时的,管家进不去,搬了也没用)。"""
    def skip(rel: str) -> bool:
        return sub == "xdg-state" and (rel == "opencode/locks" or rel.startswith("opencode/locks/"))
    return skip
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


class PoolError(Exception):
    """带一个给平台分支用的 reason,和一句给人看的说明。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass
class Moved:
    files: int = 0
    dbs: int = 0
    bytes: int = 0
    skipped: int = 0
    deleted: int = 0
    seconds: float = 0.0
    errors: List[str] = field(default_factory=list)       # 真失败:数据可能没搬全(解绑据此回 500、保留 dirty)
    warnings: List[str] = field(default_factory=list)     # 有意不做的事,例如本轮不对某个子目录传播删除

    def add(self, st: fdtree.Stats) -> None:
        self.files += st.files
        self.bytes += st.bytes
        self.skipped += st.skipped
        self.deleted += st.deleted
        self.errors.extend(st.errors)

    def to_json(self) -> dict:
        return {"files": self.files, "dbs": self.dbs, "mb": round(self.bytes / 1048576.0, 1),
                "seconds": round(self.seconds, 2), "skipped": self.skipped, "errors": len(self.errors),
                "warnings": len(self.warnings)}


def is_db_name(name: str) -> bool:
    return name.lower().endswith(_DB_SUFFIXES)


def _is_sidecar(name: str) -> bool:
    return name.endswith(_SIDECAR_SUFFIXES)


def _name(rel: str) -> str:
    return rel.rpartition("/")[2]


def _utc_stamp(now: Optional[float] = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))


# --- 本人目录 -------------------------------------------------------------------

def open_user(users_fd: int, user_id: str) -> int:
    """打开(没有就建)`users/<工号>/`,确认可写。返回描述符,调用方关闭。

    建不出来 / 写不进去时说清属主:kubelet 按 subPath 建出来的目录属主是 root(v1.1.6 待修第 1 项),
    部署手册要求 NAS 上的 users/ 预先 chown 1000——没做的话这里就是第一个撞上的地方。
    """
    if not USER_ID_RE.match(user_id or ""):
        raise ValueError("工号格式不对:%r" % user_id)

    def _explain(fd: int, what: str) -> PoolError:
        st = os.fstat(fd)
        return PoolError("users_not_writable",
                         "%s 不可写:属主 uid=%d gid=%d、权限 %o,管家以 uid=%d 运行。"
                         "请按部署手册在 NFS 服务器上对 users/ 执行 chown -R 1000:1000"
                         % (what, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), os.getuid()))
    try:
        os.mkdir(user_id, 0o700, dir_fd=users_fd)
    except FileExistsError:
        pass
    except PermissionError:
        raise _explain(users_fd, "users/") from None
    try:
        fd = fdtree.open_dir(users_fd, user_id)
    except fdtree.UnsafePath:
        raise PoolError("unsafe_user_dir", "users/%s 不是普通目录(可能是符号链接),拒绝使用" % user_id) from None
    if fd is None:
        raise PoolError("user_dir_missing", "users/%s 建好之后又不见了" % user_id)
    probe = ".write-probe"
    try:
        fdtree.write_small(fd, probe, b"")
        fdtree.unlink(fd, probe)
    except PermissionError:
        err = _explain(fd, "users/%s" % user_id)
        os.close(fd)
        raise err from None
    return fd


# --- .owner 独占 ----------------------------------------------------------------

def _owner(user_fd: int) -> dict:
    try:
        raw = fdtree.read_small(user_fd, OWNER, limit=4096)
    except fdtree.UnsafePath:
        return {}               # 预先埋的链接 / 怪文件不是任何 Pod / 槽位写的标记:当成没有,写时替换掉它
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_owner(user_fd: int, pod: str, now: float) -> None:
    fdtree.write_small(user_fd, OWNER, json.dumps({"pod": pod, "ts": now}).encode("utf-8"))


def owner_acquire(user_fd: int, pod: str, now: Optional[float] = None,
                  stale_s: int = OWNER_STALE_S) -> Tuple[bool, str, int]:
    """拿独占标记。返回 (拿到没有, 当前持有者, 还要等几秒才过期)。**不等待**——bind 立即回 409(方案 §7.1)。

    pod 是持有者标识,按字符串**整串**比较:固定池化下传「pod/槽位」(如 pool-3/2),
    同一个 Pod 的两个槽位因此不会把对方当成自己。
    """
    now = time.time() if now is None else now
    cur = _owner(user_fd)
    holder = str(cur.get("pod") or "")
    try:
        age = now - float(cur.get("ts") or 0)
    except (TypeError, ValueError):
        age = stale_s
    if not cur or holder == pod or age >= stale_s:
        _write_owner(user_fd, pod, now)
        return True, pod, 0
    return False, holder, max(1, int(stale_s - age + 0.999))


def owner_refresh(user_fd: int, pod: str, now: Optional[float] = None) -> bool:
    """还是自己持有才刷新;被别人接管了(本管家停了 90 秒以上)返回 False——调用方必须停止回写。"""
    if _owner(user_fd).get("pod") != pod:
        return False
    _write_owner(user_fd, pod, time.time() if now is None else now)
    return True


def owner_release(user_fd: int, pod: str) -> None:
    if _owner(user_fd).get("pod") == pod:
        fdtree.unlink(user_fd, OWNER)


# --- 版本令牌 ---------------------------------------------------------------------

def gen_read(user_fd: int) -> str:
    """NAS 上当前的版本令牌;没有 / 读不了 → 空串(当作「谁也对不上」)。"""
    try:
        raw = fdtree.read_small(user_fd, GEN, limit=256)
    except fdtree.UnsafePath:
        return ""
    return (raw or b"").decode("ascii", "replace").strip()


def gen_rotate(user_fd: int) -> str:
    """换一个新令牌并返回。**只在刚拿到独占标记之后调用**(本人在任何地方的旧副本因此作废)。"""
    new = secrets.token_hex(16)
    fdtree.write_small(user_fd, GEN, new.encode("ascii"), mode=0o600)
    return new


# 「确实不存在」:不是读不了、不是被换成链接,是没有了(连目录一起没了也算)。2026-10-24 测试环境:NAS 放在 Mac 的
# /tmp 下,macOS 每天 0 点的 tmp_cleaner 把三天没碰过的文件整批删掉,保留态的人回来时令牌(有时连主库)都没了。
# 固定池化的管家据此用保留副本恢复(poolslot.Slot._restore_nas),不当成「在别处用过」去腾挪 ——
# 腾挪会把唯一完整的那份清掉。读不准、路径上有链接 → False(交给原来的 stale 判断)。

def gen_missing(user_fd: int) -> bool:
    """NAS 上的版本令牌确实不存在。"""
    try:
        os.stat(GEN, dir_fd=user_fd, follow_symlinks=False)
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


def db_missing(user_fd: int) -> bool:
    """NAS 上的主库确实不存在。"""
    parent, _sep, base = MAIN_DB.rpartition("/")
    try:
        d = fdtree.open_path(user_fd, parent, create=False)
    except OSError:                     # 含 UnsafePath:路径上埋了链接,不是「不见了」
        return False
    if d is None:
        return True
    try:
        os.stat(base, dir_fd=d, follow_symlinks=False)
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False
    finally:
        os.close(d)


def db_fingerprint(user_fd: int) -> Optional[Tuple[int, int, int]]:
    """NAS 上主库的 (inode, 纳秒修改时间, 大小);没有 → None。版本令牌之外的第二道核对。"""
    parent, _sep, base = MAIN_DB.rpartition("/")
    try:
        d = fdtree.open_path(user_fd, parent, create=False)
    except fdtree.UnsafePath:
        return None
    if d is None:
        return None
    try:
        st = os.stat(base, dir_fd=d, follow_symlinks=False)
    except FileNotFoundError:
        return None
    finally:
        os.close(d)
    return st.st_ino, st.st_mtime_ns, st.st_size


# --- dirty 标记 -----------------------------------------------------------------

def is_dirty(user_fd: int) -> bool:
    try:
        os.stat(MARK_DIRTY, dir_fd=user_fd, follow_symlinks=False)
        return True             # 不管是不是普通文件:宁可多留一份快照
    except FileNotFoundError:
        return False


def mark_dirty(user_fd: int, now: Optional[float] = None) -> None:
    fdtree.write_small(user_fd, MARK_DIRTY, str(int(time.time() if now is None else now)).encode())


def clear_dirty(user_fd: int) -> None:
    fdtree.unlink(user_fd, MARK_DIRTY)


# --- 载入 -----------------------------------------------------------------------

def _copy_sub(src_root: int, dst_root: int, sub: str, moved: Moved,
              skip: Optional[Callable[[str], bool]] = None,
              modes: Modes = LEGACY_MODES) -> Tuple[Optional[set], List[str]]:
    """复制一个子目录。返回 (见到的相对路径, 看不见的子目录);源不存在 → (None, []),源是链接等 → 记错并 (None, [])。"""
    try:
        src = fdtree.open_path(src_root, sub, create=False)
    except fdtree.UnsafePath as exc:
        moved.errors.append("%s:%s" % (sub, exc))
        return None, []
    if src is None:
        return None, []
    try:
        dst = fdtree.open_path(dst_root, sub, create=True, mode=modes.dir)
    except OSError as exc:
        os.close(src)
        moved.errors.append("%s:%s" % (sub, exc))
        return None, []
    try:
        st = fdtree.Stats()
        seen = fdtree.copy_tree(src, dst, stats=st, file_mode=modes.file, dir_mode=modes.dir,
                                skip=lambda rel: _is_sidecar(_name(rel)) or (skip is not None and skip(rel)),
                                skip_dir=_skip_dir(sub))
        moved.add(st)
        if st.blind:
            moved.warnings.append("%s:%d 个子目录没有权限进入,本轮不搬、也不对它们传播删除:%s"
                                  % (sub, len(st.blind), "、".join(st.blind[:3])))
        return seen, list(st.blind)
    finally:
        os.close(src)
        os.close(dst)


def snapshot_unclean(user_fd: int, now: Optional[float] = None) -> Optional[str]:
    """上次没走完收尾:覆盖之前先在本人目录里留一份(与现有模式同名同位置)。

    返回快照目录名;NAS 上还没有任何可快照的内容(上次绑定之后一次回写都没来得及)时返回 None——
    调用方据此如实说明,**不能说「已快照」而目录并不存在**(10-14 e2e 发现)。
    """
    name = SNAPSHOT_PREFIX + "%d" % int(time.time() if now is None else now)
    copied = 0
    for sub in POOL_LOCALIZED:
        try:
            src = fdtree.open_path(user_fd, sub, create=False)
        except fdtree.UnsafePath:
            continue
        if src is None:
            continue
        try:
            dst = fdtree.open_path(user_fd, name + "/" + sub, create=True)
            try:
                st = fdtree.Stats()
                fdtree.copy_tree(src, dst, stats=st, only_if_changed=False)
                copied += 1
            finally:
                os.close(dst)
        finally:
            os.close(src)
    return name if copied else None


def load(user_fd: int, state_fd: int, log: Callable[[str], None] = print,
         now: Optional[float] = None, modes: Modes = LEGACY_MODES) -> Tuple[Moved, bool]:
    """NAS → state 卷。库原样搬(NAS 上的库是静止的:独占在我们手里,写它的只有原子改名)。

    返回 (搬运统计, 上次是不是非正常退出)。modes 是 state 卷一侧的权限(固定池化传 SLOT_MODES)。
    """
    t0 = time.monotonic()
    unclean = is_dirty(user_fd)
    if unclean:
        snap = snapshot_unclean(user_fd, now)
        if snap:
            log("⚠ 上次是**非优雅**退出(dirty 标记还在):NAS 上那份已先快照到 %s/。"
                "最后一个同步周期内的对话可能已丢失。" % snap)
        else:
            log("⚠ 上次是**非优雅**退出(dirty 标记还在),但 NAS 上还没有任何可快照的内容:"
                "上次绑定之后一次回写都没来得及,那次的对话已丢失。")
    moved = Moved()
    for sub in POOL_LOCALIZED:
        seen, _blind = _copy_sub(user_fd, state_fd, sub, moved, modes=modes)
        if seen is None:                # NAS 上还没有(新用户)或不可用:本地照样建出空目录
            os.close(fdtree.open_path(state_fd, sub, create=True, mode=modes.dir))
            continue
        moved.dbs += sum(1 for rel in seen if is_db_name(_name(rel)))
    moved.seconds = time.monotonic() - t0
    return moved, unclean


# --- 库自检(只在 opencode 启动之前调用) -------------------------------------------

def selfcheck(state_dir: pathlib.Path, user_fd: int, state_fd: int,
              log: Callable[[str], None] = print, modes: Modes = LEGACY_MODES) -> str:
    """返回 ok / absent / restored / reset。modes 是 state 卷一侧的权限(固定池化传 SLOT_MODES:
    从备份恢复出来的库要让槽位读写得到;NAS 上的备份是 0600)。

    **只在 opencode 启动之前调用**:那时槽位还没执行过任何用户命令,state 卷可信,可以用 SQLite 按路径打开。
    NAS 上的备份不可信(可能是预先埋的链接),先按描述符拷进 state 卷再查,查过的才用。
    """
    db = state_dir / MAIN_DB
    if not db.exists():
        return "absent"
    if podctl._integrity_ok(db):
        return "ok"
    corrupt = db.with_name("opencode.db.corrupt-%s" % _utc_stamp())
    for suffix in ("", "-wal", "-shm"):
        src = db.with_name(db.name + suffix)
        if src.exists():
            src.rename(corrupt.with_name(corrupt.name + suffix))
    log("数据库自检: opencode.db 损坏,已改名为 %s 留证" % corrupt.name)
    try:
        bfd = fdtree.open_path(user_fd, BACKUP_DIR, create=False)
    except fdtree.UnsafePath:
        bfd = None
    if bfd is not None:
        try:
            dev = os.fstat(bfd).st_dev
            tdir = fdtree.open_path(state_fd, RESTORE_DIR, create=True, mode=modes.dir)
            try:
                for name in reversed([n for n in fdtree.listdir(bfd) if podctl._BACKUP_RE.match(n)]):
                    try:
                        fdtree.copy_file(bfd, name, tdir, name, dev=dev, only_if_changed=False, mode=modes.file)
                    except fdtree.UnsafePath:
                        log("数据库自检: 备份 %s 不是普通文件(可能是链接),跳过" % name)
                        continue
                    cand = state_dir / RESTORE_DIR / name
                    if podctl._integrity_ok(cand):
                        os.replace(cand, db)
                        log("数据库自检: 已从备份 %s 恢复;该备份之后的对话丢失" % name)
                        return "restored"
                    log("数据库自检: 备份 %s 也损坏,跳过" % name)
                    fdtree.unlink(tdir, name)
            finally:
                os.close(tdir)
        finally:
            os.close(bfd)
    log("数据库自检: 没有可用备份,以空库启动;历史对话丢失,请从 NAS 快照找回")
    return "reset"


# --- 回写 -----------------------------------------------------------------------

Ident = Tuple[int, int, int]


def sync(user_fd: int, state_fd: int, snap_seen: Dict[str, Ident], may_delete: bool = True,
         modes: Modes = LEGACY_MODES, still_valid: Optional[Callable[[], bool]] = None) -> Moved:
    """state 卷 → NAS。普通文件按 fdtree 增量搬;库**只搬槽位生成的快照**(.pool/snap/),
    state 卷上活着的库文件一概不碰。snap_seen 记各个快照上次搬过时的身份(inode、纳秒修改时间、大小),
    没变就不搬(M1 在管家这一侧的样子)。

    某个子目录在 state 卷上整个不见了(或被换成链接)时**不对它传播删除**:那更像是误删,
    NAS 上那份是这个人全部的历史。

    **state 卷上还在的库,不管这一轮有没有快照,都算「还在」**:库不经普通复制,只搬快照;
    要是保留集合里只有快照,槽位还没生成快照(或 20 秒内没响应)的那一轮,删除传播就会把
    NAS 上这个人的 opencode.db 当成「已删」删掉。只有本地真没了这个库,NAS 上的才删。

    still_valid:每次传播删除之前问一次「来源还是那块盘吗」。固定池化的槽位重启时新的 1 号进程会清盘、
    重建空的顶层目录 —— 目录都在、里面是空的,按上面的规则删除传播会把 NAS 上这个人的历史全部删掉。
    返回假就不再传播删除(这一轮复制过去的照常留着)。
    """
    t0 = time.monotonic()
    moved = Moved()
    for sub in POOL_LOCALIZED:
        live_dbs: set = set()

        def skip_db(rel: str, _live=live_dbs) -> bool:
            if is_db_name(_name(rel)):
                _live.add(rel)
                return True
            return False

        seen, blind = _copy_sub(state_fd, user_fd, sub, moved, skip=skip_db, modes=modes)
        if seen is None:
            moved.warnings.append("%s:state 卷上没有这个目录(或不是普通目录),本轮不对它传播删除" % sub)
            continue
        seen |= live_dbs
        _sync_snapshots(user_fd, state_fd, sub, snap_seen, seen, moved, modes)
        if may_delete and still_valid is not None and not still_valid():
            moved.errors.append("来源的盘已不是原来那块(槽位重启过),不传播删除")
            may_delete = False
        if may_delete:
            try:
                dst = fdtree.open_path(user_fd, sub, create=True, mode=modes.dir)
            except OSError as exc:
                moved.errors.append("%s:%s" % (sub, exc))
                continue
            skip_sub = _skip_dir(sub)

            def protect(rel: str, _blind=blind, _skip=skip_sub) -> bool:
                return (_is_sidecar(_name(rel)) or fdtree.is_temporary(_name(rel))
                        or any(rel.startswith(b) for b in _blind) or _skip(rel.rpartition("/")[0]))
            try:
                st = fdtree.Stats()
                fdtree.prune(dst, seen, stats=st, protect=protect)
                moved.add(st)
            finally:
                os.close(dst)
    moved.seconds = time.monotonic() - t0
    return moved


def _sync_snapshots(user_fd: int, state_fd: int, sub: str, snap_seen: Dict[str, Ident],
                    seen: set, moved: Moved, modes: Modes = LEGACY_MODES) -> None:
    try:
        snap = fdtree.open_path(state_fd, SNAP_DIR + "/" + sub, create=False)
    except fdtree.UnsafePath as exc:
        moved.errors.append("%s 快照目录:%s" % (sub, exc))
        return
    if snap is None:
        return
    try:
        dev = os.fstat(state_fd).st_dev
        for rel, dfd, name, lst in fdtree.walk(snap):
            if not is_db_name(name):
                continue
            key = sub + "/" + rel
            ident = (lst.st_ino, lst.st_mtime_ns, lst.st_size)
            seen.add(rel)               # 快照在 = 库还在:不能被删除传播当成「已删」
            if snap_seen.get(key) == ident:
                continue
            parent, _sep, base = rel.rpartition("/")
            try:
                dst = fdtree.open_path(user_fd, sub + ("/" + parent if parent else ""), create=True, mode=modes.dir)
            except OSError as exc:
                moved.errors.append("%s:%s" % (key, exc))
                continue
            try:
                n = fdtree.copy_file(dfd, name, dst, base, dev=dev, only_if_changed=False, mode=modes.file)
                moved.dbs += 1
                moved.bytes += n
                snap_seen[key] = ident
            except fdtree.UnsafePath:
                moved.skipped += 1
            except OSError as exc:
                moved.errors.append("%s:%s" % (key, exc))
            finally:
                os.close(dst)
    finally:
        os.close(snap)


def backup(user_fd: int, state_fd: int, keep: int = 3, now: Optional[float] = None,
           modes: Modes = LEGACY_MODES) -> Optional[str]:
    """退出备份:把槽位的最终快照拷成 NAS 上的 backup/opencode.db.<UTC 时间戳>,保留最近 keep 份。没有快照 → None。"""
    parent, _sep, base = MAIN_DB.rpartition("/")
    try:
        sdir = fdtree.open_path(state_fd, SNAP_DIR + "/" + parent, create=False)
    except fdtree.UnsafePath:
        return None
    if sdir is None:
        return None
    try:
        bdir = fdtree.open_path(user_fd, BACKUP_DIR, create=True, mode=modes.dir)
        try:
            target = "opencode.db.%s" % _utc_stamp(now)
            try:
                fdtree.copy_file(sdir, base, bdir, target, dev=os.fstat(state_fd).st_dev, only_if_changed=False,
                                 mode=modes.file)
            except FileNotFoundError:
                return None
            olds = [n for n in fdtree.listdir(bdir) if podctl._BACKUP_RE.match(n)]
            for old in (olds[:-keep] if keep > 0 else []):
                fdtree.unlink(bdir, old)
            return target
        finally:
            os.close(bdir)
    finally:
        os.close(sdir)
