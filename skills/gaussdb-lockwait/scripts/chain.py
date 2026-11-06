"""阻塞链的上溯与环检测 —— 纯函数，不连库。

**为什么根要在这里算，而不是在 SQL 里用递归 CTE：** 死锁就是链上有环，
递归 CTE 撞上环会一路展开到把数据库跑爆，而死锁恰恰是这个 skill 最该报出来的
情形。放在 python 里，环能被检测出来并当场报「这是死锁」。

**为什么必须找到根：** 杀链条中间的节点不解堵。3 等 2、2 等 1 的时候杀掉 2，
3 会立刻改成等 1，现场没有任何变化，而操作的人以为自己处理过了。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

Edge = Tuple[int, int]      # (等待者 sessionid, 阻塞它的 sessionid)


def _blocker_of(edges: List[Edge]) -> Dict[int, int]:
    """等待者 → 直接阻塞它的会话。同一个等待者出现多条边时取第一条。"""
    out: Dict[int, int] = {}
    for waiter, blocker in edges:
        out.setdefault(waiter, blocker)
    return out


def roots(edges: List[Edge]) -> Dict[int, int]:
    """每个等待者 → 它的**根**阻塞者。

    走进环时停下并把当前节点当作根返回 —— 环里本来就没有根，
    但这个函数必须**返回**，不能挂住。环由 cycles() 单独报。
    """
    blocker = _blocker_of(edges)
    out: Dict[int, int] = {}
    for start in blocker:
        seen = {start}
        cur = start
        while cur in blocker:
            nxt = blocker[cur]
            if nxt in seen:          # 成环，停在这里
                cur = nxt
                break
            seen.add(nxt)
            cur = nxt
        out[start] = cur
    return out


def depth(edges: List[Edge], sessionid: int) -> int:
    """从该会话到根走了几层。不在链上的返回 0；环上最多走一圈。"""
    blocker = _blocker_of(edges)
    seen = {sessionid}
    n, cur = 0, sessionid
    while cur in blocker:
        nxt = blocker[cur]
        n += 1
        if nxt in seen:
            break
        seen.add(nxt)
        cur = nxt
    return n


def cycles(edges: List[Edge]) -> List[List[int]]:
    """检测环（死锁）。返回每个环上的会话列表，已去重。"""
    blocker = _blocker_of(edges)
    found: List[List[int]] = []
    seen_cycle = set()
    for start in blocker:
        order: List[int] = []
        pos: Dict[int, int] = {}
        cur = start
        while cur in blocker:
            if cur in pos:                       # 回到走过的节点 → 环
                ring = order[pos[cur]:]
                key = frozenset(ring)
                if key not in seen_cycle:
                    seen_cycle.add(key)
                    found.append(ring)
                break
            pos[cur] = len(order)
            order.append(cur)
            cur = blocker[cur]
    return found


def blocked_by_root(edges: List[Edge]) -> Dict[int, List[int]]:
    """根 → 它最终挡住的所有会话。报告按这个排序：挡得最多的排最前。"""
    temp: Dict[int, List[int]] = {}
    for waiter, root in roots(edges).items():
        if waiter == root:            # 环上的节点，不算被自己挡
            continue
        temp.setdefault(root, []).append(waiter)
    for k in temp:
        temp[k].sort()
    # 按被挡会话数从多到少排序（dicts 在 Python 3.7+ 保持插入顺序）
    out: Dict[int, List[int]] = {}
    for root in sorted(temp.keys(), key=lambda k: len(temp[k]), reverse=True):
        out[root] = temp[root]
    return out
