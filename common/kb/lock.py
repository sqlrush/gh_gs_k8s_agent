"""知识库写入互斥。

导入 Pod 可能不止一个,NAS 上的 graph/*.yaml、INDEX.md 是共享文件;两个 apply 交错写会互相覆盖。
用 O_EXCL 建 <kb>/.lock 做建议锁:能建成就是拿到;建不成读内容,过期(默认 10 分钟)视为持有者已死。
不用 fcntl:NFS 上的 fcntl 锁不可靠,而 O_EXCL 在 NFSv3 以上是原子的。
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import socket
import time
from typing import Iterator

LOCK_NAME = ".lock"
DEFAULT_TTL_S = 600


class KbLocked(ValueError):
    """另一个进程正在写知识库;调用方按退出码 2 报出,不重试、不强抢。"""


def _owner() -> str:
    return "%s:%d" % (socket.gethostname(), os.getpid())


def _read(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _try_create(path: pathlib.Path, payload: str) -> bool:
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return True


@contextlib.contextmanager
def hold(kb_dir: pathlib.Path, ttl_s: int = DEFAULT_TTL_S) -> Iterator[None]:
    """进入时取锁,退出(含异常)时释放。被占且未过期 → KbLocked。"""
    path = pathlib.Path(kb_dir) / LOCK_NAME
    payload = json.dumps({"owner": _owner(), "ts": time.time()})
    if not _try_create(path, payload):
        cur = _read(path)
        age = time.time() - float(cur.get("ts") or 0)
        if cur and age < ttl_s:
            raise KbLocked("知识库正被 %s 写入(%d 秒前开始),请稍后再试;确认对方已退出可删除 %s"
                           % (cur.get("owner") or "未知进程", int(age), path))
        try:                                   # 过期或看不懂的锁:持有者已死,接管
            path.unlink()
        except FileNotFoundError:
            pass
        if not _try_create(path, payload):      # 接管的一瞬间被别人抢先:那就是别人的
            cur = _read(path)
            raise KbLocked("知识库正被 %s 写入,请稍后再试" % (cur.get("owner") or "未知进程"))
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
