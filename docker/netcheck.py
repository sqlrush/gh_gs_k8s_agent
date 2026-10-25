"""谁在监听这个端口:读 /proc/net/tcp{,6},按 uid 认人(固定池化,2026-10-23)。

**为什么要查。** 同一个 Pod 里的槽位共用网络命名空间(一个 localhost)。某个槽位不在监听自己端口的空档
(保留态、空闲、重启中),同 Pod 别的槽位能抢先监听那个端口;管家探活连上的是冒充者,网关会把回来的人的
对话、口令、令牌转给别人(P0 摸底 S3 实测复现)。每个槽位的 uid 各不相同,/proc/net/tcp 第 8 列就是
监听者的 uid —— 管家据此判断端口上的是不是该槽位本人。

只读 /proc,标准库;在 Pod 里任何一个容器读到的都是同一个网络命名空间。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence

PROC_FILES = ("/proc/net/tcp", "/proc/net/tcp6")
_LISTEN = "0A"

OK, DOWN = "ok", "down"


@dataclass(frozen=True)
class Listener:
    addr: str           # 十六进制原样(只用于说明)
    port: int
    uid: int
    inode: int


def parse(text: str) -> List[Listener]:
    """解析 /proc/net/tcp 或 tcp6 的内容,只留 LISTEN 状态的行。格式不对的行跳过。"""
    out: List[Listener] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10 or parts[3] != _LISTEN:
            continue
        addr, _sep, port_hex = parts[1].rpartition(":")
        try:
            out.append(Listener(addr=addr, port=int(port_hex, 16), uid=int(parts[7]), inode=int(parts[9])))
        except ValueError:
            continue
    return out


def listeners(port: int, files: Sequence[str] = PROC_FILES) -> List[Listener]:
    found: List[Listener] = []
    for f in files:
        try:
            with open(f, encoding="ascii", errors="replace") as fh:
                found += [x for x in parse(fh.read()) if x.port == port]
        except FileNotFoundError:
            continue                    # 内核没开 IPv6
    return found


def verdict(port: int, uid: int, files: Sequence[str] = PROC_FILES) -> str:
    """ok(只有本人在听)/ down(没人听)/ foreign:<uid>(有别的 uid 在听,哪怕本人也在)。"""
    return judge(listeners(port, files), uid)


def judge(found: Iterable[Listener], uid: int) -> str:
    others = sorted({x.uid for x in found if x.uid != uid})
    if others:
        return "foreign:%d" % others[0]
    return OK if any(True for _ in found) else DOWN
