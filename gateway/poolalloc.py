"""固定池化的分配器(2026-10-23,设计稿 §5):纯函数,不碰网络、不碰 k8s,穷举测试。

输入是网关看到的整个池子(各 Pod 管家的 status + k8s 给的容器重启时间),输出是「这个人该去哪」(decide)
与「后台该做什么」(plan)。真相在各管家;网关的视图可能过期 —— 过期的后果由管家的 409 兜住
(Pod 级 CAS、状态核对),网关刷新后重新 decide。

**分配规则**(「在用」= 绑定中 / 在用 / 恢复中;保留着的人不算):
  1. 本人已在用 → 直接转发;正在绑定 / 恢复 / 转保留 → 等一下再看。
  2. 本人有保留槽位:他那个 Pod 里没有别人在用,或者集群里已经没有「无人在用」的 Pod → 回到自己的槽位;
     否则(他那个 Pod 有人在用、而别处还有空 Pod)→ 为了「16 人以内各自独占」分到空 Pod,旧的保留槽位腾挪。
     写回不干净的保留槽位(dirty)一律回原处:独占还在那里。
  3. 新人(或按上一条换 Pod):有无人在用的 Pod → 其中空闲槽位最多、保留人数最少的;没有 → 在用人数最少的。
     同分时按 (工号, Pod) 的 rendezvous 哈希:两个网关副本同时给两个新人分配时,大概率分到不同的 Pod。
     选中的 Pod 没有空闲槽位(全是保留)→ 现场腾挪其中最久没来的那位(优先挑上次重启已超过 10 分钟的,
     kubelet 的重启退避在 10 分钟后清零)。
  4. 一个能用的槽位都没有 → 满员。满员时挑一个闲置超过 10 分钟的在用者提前转保留,腾出的槽位预留给这个人
     (预留记在管家那里,所有网关副本都看得到,不会被第三个人抢走);等待页每 30 秒自动重试。

**后台**(plan):闲置超过 IDLE_SECONDS 的在用者转保留;过期(stale)、本人已在别处在用、保留超过 RETAIN_DAYS
的保留槽位腾挪;每个 Pod 至少留 FREE_SLOTS_PER_POD 个空闲槽位;清理中的槽位交给网关核验;
冒充别人端口的槽位先转保留再腾挪。写回不干净的保留槽位永远不腾挪。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

FREE, BINDING, ACTIVE, RETAINING, RETAINED = "free", "binding", "active", "retaining", "retained"
RESUMING, EVICTING, DISABLED, BOOTING = "resuming", "evicting", "disabled", "booting"
IN_USE = (BINDING, ACTIVE, RESUMING)
TRANSIENT = (BINDING, RESUMING, RETAINING)

Ref = Tuple[str, int]                    # (Pod 名, 槽位号)


@dataclass(frozen=True)
class SlotView:
    pod: str
    index: int
    state: str
    user: Optional[str] = None
    last_seen: float = 0.0               # 最后一次交互 / 会话忙 / 绑定的时间
    retained_at: float = 0.0
    dirty: bool = False
    stale: bool = False
    listener: str = "ok"
    offender: bool = False
    restarted_at: float = 0.0            # 这个容器最近一次(重新)启动的时间;0 = 不知道
    reserved_for: Optional[str] = None
    reserved_until: float = 0.0
    evict_pending: bool = False          # 已发腾挪、等核验(停用的槽位只有带这一条才需要核验)

    @property
    def ref(self) -> Ref:
        return self.pod, self.index

    def reserved_for_other(self, user: str, now: float) -> bool:
        return bool(self.reserved_for) and self.reserved_for != user and self.reserved_until > now

    @property
    def healthy(self) -> bool:
        return not self.listener.startswith("foreign:") and not self.offender


@dataclass(frozen=True)
class PodView:
    name: str
    slots: Tuple[SlotView, ...]
    reachable: bool = True

    @property
    def in_use(self) -> int:
        return sum(1 for s in self.slots if s.state in IN_USE)

    @property
    def free(self) -> int:
        return sum(1 for s in self.slots if s.state == FREE and s.healthy)

    @property
    def retained(self) -> int:
        return sum(1 for s in self.slots if s.state == RETAINED)


@dataclass(frozen=True)
class Policy:
    idle_seconds: float = 1800.0
    early_idle_seconds: float = 600.0
    retain_days: float = 7.0
    free_slots_per_pod: int = 1
    restart_cooldown_s: float = 600.0
    reserve_seconds: float = 120.0


FORWARD, WAIT, RESUME, BIND, FULL = "forward", "wait", "resume", "bind", "full"


@dataclass(frozen=True)
class Decision:
    kind: str
    target: Optional[Ref] = None
    evict_first: Optional[Ref] = None    # BIND:先腾挪目标 Pod 里的这个保留槽位,再绑到它上面
    evict_after: Optional[Ref] = None    # BIND:绑好之后腾挪本人旧的保留槽位(换了 Pod)
    early_retain: Optional[Ref] = None   # FULL:提前转保留这个在用者,腾出的槽位预留给本人
    reason: str = ""


RETAIN, EVICT, VERIFY = "retain", "evict", "verify"


@dataclass(frozen=True)
class Action:
    kind: str
    ref: Ref
    user: Optional[str] = None
    reason: str = ""


# --- 工具 ---------------------------------------------------------------------------------------

def rendezvous(user: str, pod: str) -> int:
    """同分时的次序:对每个工号,各 Pod 的排名是确定的、又互不相同(最高随机权重哈希)。"""
    return int.from_bytes(hashlib.sha256(("%s\0%s" % (user, pod)).encode("utf-8")).digest()[:8], "big")


def _slots(pods: Iterable[PodView]) -> List[SlotView]:
    return [s for p in pods if p.reachable for s in p.slots]


def _mine(pods: Sequence[PodView], user: str) -> List[SlotView]:
    return [s for s in _slots(pods) if s.user == user]


def _evictable(s: SlotView, user: str, now: float) -> bool:
    """这个保留槽位可以为别人腾出来吗:不 dirty、不是被预留给别人的、监听者没问题。"""
    return (s.state == RETAINED and not s.dirty and s.healthy and not s.reserved_for_other(user, now))


def _pick_victim(cands: Sequence[SlotView], now: float, policy: Policy) -> SlotView:
    """最久没来的那位;上次重启已过冷却期的优先(否则要多等 kubelet 的退避)。"""
    return min(cands, key=lambda s: (now - s.restarted_at < policy.restart_cooldown_s if s.restarted_at else False,
                                     s.last_seen, s.index))


def _reserved_for(s: SlotView, user: str, now: float) -> bool:
    return s.reserved_for == user and s.reserved_until > now


def _usable_in(pod: PodView, user: str, now: float) -> Tuple[List[SlotView], List[SlotView]]:
    """(可直接绑的空闲槽位, 可腾挪的保留槽位)。预留给本人的空闲槽位排在最前。"""
    free = [s for s in pod.slots if s.state == FREE and s.healthy and not s.reserved_for_other(user, now)]
    free.sort(key=lambda s: (s.reserved_for != user, s.index))
    return free, [s for s in pod.slots if _evictable(s, user, now)]


def _place(pods: Sequence[PodView], user: str, now: float, policy: Policy,
           avoid: Optional[str] = None) -> Optional[Decision]:
    """给新人(或换 Pod 的人)选一个槽位。avoid:不选这个 Pod(他刚因为有人在用而要离开它)。"""
    cands = []
    for p in pods:
        if not p.reachable or p.name == avoid:
            continue
        free, evictable = _usable_in(p, user, now)
        if free or evictable:
            reserved_mine = any(_reserved_for(s, user, now) for s in free + evictable)
            cands.append((p, free, evictable, reserved_mine))
    if not cands:
        return None
    mine = [c for c in cands if c[3]]
    if mine:                                # 满员时为他腾出来的那个,别处有空位也先用它(预留不白占)
        cands = mine
    empty = [c for c in cands if c[0].in_use == 0]
    if empty:
        key = lambda c: (-c[0].free, c[0].retained, -rendezvous(user, c[0].name))   # noqa: E731
        p, free, evictable, _ = min(empty, key=key)
    else:
        key = lambda c: (c[0].in_use, -c[0].free, c[0].retained, -rendezvous(user, c[0].name))   # noqa: E731
        p, free, evictable, _ = min(cands, key=key)
    if free:
        return Decision(BIND, target=free[0].ref, reason="free")
    reserved = [s for s in evictable if _reserved_for(s, user, now)]
    victim = reserved[0] if reserved else _pick_victim(evictable, now, policy)
    return Decision(BIND, target=victim.ref, evict_first=victim.ref, reason="evict_retained")


def _early_retain_candidate(pods: Sequence[PodView], now: float, policy: Policy) -> Optional[SlotView]:
    idle = [s for s in _slots(pods) if s.state == ACTIVE and now - s.last_seen > policy.early_idle_seconds
            and s.healthy and not s.reserved_for]
    return min(idle, key=lambda s: (s.last_seen, s.pod, s.index)) if idle else None


# --- 分配 -----------------------------------------------------------------------------------------

def decide(pods: Sequence[PodView], user: str, now: float, policy: Policy = Policy()) -> Decision:
    # 在用 / 正在变的看全部 Pod:管家暂时连不上的 Pod 里,槽位照样在跑(网关直连槽位,不经管家)
    anywhere = [s for p in pods for s in p.slots if s.user == user]
    active = [s for s in anywhere if s.state == ACTIVE]
    if active:
        return Decision(FORWARD, target=active[0].ref)
    moving = [s for s in anywhere if s.state in TRANSIENT]
    if moving:
        return Decision(WAIT, target=moving[0].ref, reason=moving[0].state)
    mine = _mine(pods, user)
    kept = sorted((s for s in mine if s.state == RETAINED and not s.stale),
                  key=lambda s: (not s.dirty, -s.retained_at))
    if kept:
        r = kept[0]
        home = next(p for p in pods if p.name == r.pod)
        if r.dirty:
            return Decision(RESUME, target=r.ref, reason="dirty")       # 独占还在那里,只能回去
        if r.healthy:
            if home.in_use == 0:
                return Decision(RESUME, target=r.ref, reason="home_quiet")
            has_empty = any(p.reachable and p.in_use == 0 and any(_usable_in(p, user, now)) for p in pods)
            if not has_empty:
                return Decision(RESUME, target=r.ref, reason="no_empty_pod")
        moved = _place(pods, user, now, policy, avoid=r.pod)
        if moved is not None:
            return Decision(BIND, target=moved.target, evict_first=moved.evict_first, evict_after=r.ref,
                            reason="moved:" + moved.reason)
        if r.healthy:
            return Decision(RESUME, target=r.ref, reason="no_other_place")
    placed = _place(pods, user, now, policy)
    if placed is not None:
        return placed
    pending = [s for s in _slots(pods) if _reserved_for(s, user, now) and s.state in (ACTIVE, RETAINING)]
    if pending:                             # 已经为他提前转保留了一位、还没转完:等,不再挑第二位
        return Decision(WAIT, target=pending[0].ref, reason="reserved")
    victim = _early_retain_candidate(pods, now, policy)
    return Decision(FULL, early_retain=victim.ref if victim else None, reason="full")


# --- 后台 -----------------------------------------------------------------------------------------

def plan(pods: Sequence[PodView], now: float, policy: Policy = Policy()) -> List[Action]:
    out: List[Action] = []
    taken = set()
    active_users = {s.user for s in _slots(pods) if s.state in IN_USE and s.user}

    def add(kind: str, s: SlotView, reason: str) -> None:
        if s.ref not in taken:
            taken.add(s.ref)
            out.append(Action(kind, s.ref, s.user, reason))

    for s in _slots(pods):
        if s.state == EVICTING or (s.state == DISABLED and s.evict_pending):
            add(VERIFY, s, "verify")
        elif s.offender and s.state == ACTIVE:
            add(RETAIN, s, "offender")
        elif s.offender and s.state in (RETAINED, FREE) and not s.dirty:
            add(EVICT, s, "offender")
        elif s.state == ACTIVE and now - s.last_seen > policy.idle_seconds:
            add(RETAIN, s, "idle")
        elif s.state == RETAINED and not s.dirty:
            if s.stale:
                add(EVICT, s, "stale")
            elif s.user in active_users:
                add(EVICT, s, "active_elsewhere")
            elif s.retained_at and now - s.retained_at > policy.retain_days * 86400:
                add(EVICT, s, "retain_days")
    for p in pods:
        if not p.reachable:
            continue
        free = p.free + sum(1 for s in p.slots if s.ref in taken and s.state == RETAINED)
        if free >= policy.free_slots_per_pod:
            continue
        cands = [s for s in p.slots if s.ref not in taken and s.state == RETAINED and not s.dirty and s.healthy
                 and not (s.reserved_for and s.reserved_until > now)]
        for _ in range(policy.free_slots_per_pod - free):
            if not cands:
                break
            v = _pick_victim(cands, now, policy)
            cands.remove(v)
            add(EVICT, v, "free_slots")
    return out
