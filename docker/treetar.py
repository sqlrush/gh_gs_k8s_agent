"""目录树 ⇄ tar 流(Fuyao 运行模式,2026-11-17):状态服务与沙箱两边共用。

打包:从根目录描述符出发逐级不跟随链接(fdtree.walk / _open_regular),只收与根同设备的普通文件;
      链接、FIFO、设备文件计数跳过。读的是已经打开的对象,中途被换成链接也换不走。
解包:流式读(不落临时文件),每个成员先校验再写 ——
  · 名字:相对路径、不许 `..` / `.` / 空段 / 绝对路径(fdtree._split 的规则),且顶层目录必须在允许范围里;
  · 类型:只接受普通文件(链接、硬链接、设备、FIFO 一律拒绝并计数);
  · 大小:单文件与总量各有上限,超了整次拒绝(不留半截);
  · 写入:目录逐级 open_path(不跟随),文件临时名 O_EXCL 创建再原子改名(目标是预埋的链接时替换掉链接本身);
  · `.gsdb-meta/` 下的成员不落盘,作为元数据(JSON)交给调用方。
"""
from __future__ import annotations

import io
import json
import os
import tarfile
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Dict, Iterable, List, Optional, Set

import fdtree

META_PREFIX = ".gsdb-meta/"
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


class TarRejected(ValueError):
    """包里有不该有的东西(越界路径、超限)。整次拒绝。"""


@dataclass
class Result:
    written: Set[str] = field(default_factory=set)
    meta: Dict[str, object] = field(default_factory=dict)
    skipped: int = 0
    bytes: int = 0
    errors: List[str] = field(default_factory=list)


def _info(name: str, size: int, mtime: float, mode: int = 0o600) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mtime, ti.mode = size, mtime, mode
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def add_meta(tar: tarfile.TarFile, name: str, obj: object) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    tar.addfile(_info(META_PREFIX + name, len(data), 0), io.BytesIO(data))


def add_file(tar: tarfile.TarFile, arcname: str, path: str) -> int:
    """把本地一个(调用方自己生成的)文件按 arcname 加进包。"""
    st = os.stat(path)
    with open(path, "rb") as f:
        tar.addfile(_info(arcname, st.st_size, st.st_mtime), f)
    return st.st_size


def pack(tar: tarfile.TarFile, root_fd: int, prefix: str = "", *,
         only: Optional[Set[str]] = None, skip: Optional[Callable[[str], bool]] = None,
         skip_dir: Optional[Callable[[str], bool]] = None) -> Dict[str, List[int]]:
    """把 root 下的普通文件加进包(名字 = prefix + 相对路径)。返回 {相对路径: [大小, 修改时间纳秒]}。

    only:只收这些相对路径(按清单补拉时用)。skip(相对路径)为真的不收。
    """
    dev = os.fstat(root_fd).st_dev
    out: Dict[str, List[int]] = {}
    for rel, dfd, name, lst in fdtree.walk(root_fd, skip_dir=skip_dir):
        if fdtree.is_temporary(name) or (only is not None and rel not in only) or (skip is not None and skip(rel)):
            continue
        if not lst.st_mode & 0o170000 == 0o100000:          # 不是普通文件:链接、FIFO……
            continue
        try:
            fd, st = fdtree._open_regular(dfd, name, dev)
        except (fdtree.UnsafePath, FileNotFoundError):
            continue
        with os.fdopen(fd, "rb") as f:
            tar.addfile(_info(prefix + rel, st.st_size, st.st_mtime), f)
        out[rel] = [st.st_size, st.st_mtime_ns]
    return out


def manifest(root_fd: int, *, skip: Optional[Callable[[str], bool]] = None,
             skip_dir: Optional[Callable[[str], bool]] = None) -> Dict[str, List[int]]:
    """{相对路径: [大小, 修改时间纳秒]},只列普通文件(lstat,不打开)。"""
    out: Dict[str, List[int]] = {}
    for rel, _dfd, name, lst in fdtree.walk(root_fd, skip_dir=skip_dir):
        if fdtree.is_temporary(name) or (skip is not None and skip(rel)):
            continue
        if lst.st_mode & 0o170000 == 0o100000:
            out[rel] = [lst.st_size, lst.st_mtime_ns]
    return out


def unpack(stream: BinaryIO, dest_fd: int, *, allowed_top: Callable[[str], bool],
           max_file: int, max_total: int, file_mode: int = 0o600, dir_mode: int = 0o700,
           drop_db_journals: bool = True) -> Result:
    """流式解包到 dest(目录描述符)。见模块说明。"""
    res = Result()
    total = 0
    try:
        tar = tarfile.open(fileobj=stream, mode="r|*")
    except tarfile.TarError as exc:
        raise TarRejected("不是有效的 tar 包:%s" % exc) from exc
    with tar:
        for m in tar:
            name = m.name
            while name.startswith("./"):
                name = name[2:]
            if name.startswith(META_PREFIX) and m.isreg():
                if m.size > 16 << 20:
                    raise TarRejected("元数据太大:%s" % name)
                f = tar.extractfile(m)
                try:
                    res.meta[name[len(META_PREFIX):]] = json.loads(f.read() if f else b"null")
                except ValueError as exc:
                    raise TarRejected("元数据不是 JSON:%s" % name) from exc
                continue
            if m.isdir():
                continue
            try:
                parts = fdtree._split(name)
            except ValueError:
                raise TarRejected("包里有越界路径:%r" % name) from None
            if not parts or not allowed_top(parts[0]):
                raise TarRejected("包里有不允许的位置:%r" % name)
            if not m.isreg():
                res.skipped += 1                          # 链接、硬链接、设备、FIFO
                continue
            if m.size > max_file:
                raise TarRejected("单个文件超过上限(%d 字节):%s" % (max_file, name))
            total += m.size
            if total > max_total:
                raise TarRejected("这一包超过总量上限(%d 字节)" % max_total)
            src = tar.extractfile(m)
            parent, base = "/".join(parts[:-1]), parts[-1]
            if fdtree.is_temporary(base):
                res.skipped += 1
                continue
            dfd = fdtree.open_path(dest_fd, parent, create=True, mode=dir_mode)
            try:
                def chunks(out: int, src=src) -> None:
                    while True:
                        buf = src.read(1 << 20)
                        if not buf:
                            break
                        os.write(out, buf)
                mt = int(m.mtime * 1e9)
                fdtree._write_atomic(dfd, base, chunks, file_mode, mtime_ns=(mt, mt))
                if drop_db_journals and base.lower().endswith(_DB_SUFFIXES):
                    # 库是整库一致性快照:旧的 -wal / -shm 留着会被 SQLite 套到新库上
                    for j in ("-wal", "-shm"):
                        try:
                            fdtree.unlink(dfd, base + j)
                        except OSError as exc:
                            res.errors.append("删 %s%s:%s" % (name, j, exc))
            finally:
                os.close(dfd)
            res.written.add(name)
            res.bytes += m.size
    return res


def keep_sets(paths: Iterable[str]) -> Dict[str, Set[str]]:
    """把「顶层目录/相对路径」的集合按顶层目录分组(删除传播按子目录做)。"""
    out: Dict[str, Set[str]] = {}
    for p in paths:
        top, _sep, rest = p.partition("/")
        if rest:
            out.setdefault(top, set()).add(rest)
    return out
