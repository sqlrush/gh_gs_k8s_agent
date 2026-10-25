"""固定池化下「这个请求转到哪」(2026-10-23):poolalloc.decide 的结论落成对管家的调用。

  转发   本人已在用:用缓存的口令;缓存没有(网关刚重启 / 另一个副本绑的)就再 bind 一次 —— 同一个人幂等取回凭证。
  恢复   回保留槽位(resume)。
  绑定   空闲槽位直接 bind;要先腾挪的,发 evict → 等 k8s 里那个容器换了 containerID → verify → bind。
         换了 Pod 的,绑好之后在后台腾挪旧的保留槽位。
  满员   挑一位闲置超过 10 分钟的在用者:先把他的槽位预留给本人(管家记着,所有网关副本都看得到),再让他转保留;
         接下来按「腾挪后绑定」走。等不及就回等待页(30 秒自动重试),预留保证下一次还是给他。

**非交互请求不分配**(opencode 网页每 10 秒的 /api/health、事件流):本人不在用时直接 NotActive ——
开着的标签页不能把转保留的槽位又拉起来(P0 摸底 S5)。

管家回 409(Pod 级 CAS、状态变了、被预留、版本令牌过期……)= 视图过期:刷新后重新 decide,直到等待上限。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from . import poolalloc as pa
from .poolclient import StewardError

# not_synced:管家用保留副本恢复 NAS 时写回失败(继续持有独占、后台重试,docker/poolslot.py _restore_nas),
# 稍后再来就行 —— 不是失败,不记日志;那份副本是唯一完整的,也不会被腾挪(写回不干净的保留永远不腾挪)
_RETRY_REASONS = {"pod_changed", "state", "reserved", "stale", "restarted", "user_mismatch", "owner_busy",
                  "not_restarted", "not_synced"}


class NotActive(Exception):
    """非交互请求(事件流、探活),而本人现在不在用:不为它分配。"""


class PoolFull(Exception):
    def __init__(self, retry_after: int):
        super().__init__("当前使用人数已满")
        self.retry_after = retry_after


class PoolWait(Exception):
    """在等(腾挪中的容器重启、别处还持有本人的独占……),超过了这一次请求的等待上限。"""

    def __init__(self, reason: str, retry_after: int):
        super().__init__(reason)
        self.reason, self.retry_after = reason, retry_after


@dataclass(frozen=True)
class Target:
    pod: str
    slot: int
    host: str
    port: int
    password: str
    reports_port: int
    reports_token: str
    since: float = 0.0


class PoolRouter:
    def __init__(self, table, client, policy: pa.Policy, grmp_lookup: Callable[[str], str],
                 wait_s: float = 60.0, retry_after: int = 30, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.table, self.client, self.policy, self.grmp_lookup = table, client, policy, grmp_lookup
        self.wait_s, self.retry_after, self.log, self.clock, self.sleep = wait_s, retry_after, log, clock, sleep
        self._creds: Dict[str, Target] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._activity: Dict[str, float] = {}

    # -- 活动(交互请求才记;清扫线程定期批量报给各管家) -------------------------------------------

    def note_activity(self, user: str) -> None:
        with self._lock:
            self._activity[user] = self.clock()

    def drain_activity(self) -> Dict[str, float]:
        with self._lock:
            out, self._activity = self._activity, {}
        return out

    def restore_activity(self, acts: Dict[str, float]) -> None:
        """报给管家失败的活动放回去,下一轮再报(只留更新的那个时间)。"""
        with self._lock:
            for user, ts in acts.items():
                if ts > self._activity.get(user, 0.0):
                    self._activity[user] = ts

    def forget(self, user: str) -> None:
        """缓存的凭证作废(转保留了 / 打到占位监听上了),下次重新 decide。"""
        with self._lock:
            self._creds.pop(user, None)
        self.table.invalidate()

    # -- 主入口 -------------------------------------------------------------------------------------

    def target(self, user: str, interactive: bool = True) -> Target:
        with self._lock:
            lock = self._locks.setdefault(user, threading.Lock())
        with lock:
            deadline = self.clock() + self.wait_s
            snap = self.table.snapshot()
            last = "starting"
            rechecked = False
            while True:
                d = pa.decide(snap.pods, user, self.clock(), self.policy)
                if d.kind == pa.FORWARD:
                    got = self._forward(snap, user, d.target)
                    if got is not None:
                        return got
                elif not interactive:
                    raise NotActive(user)
                elif d.kind == pa.FULL and d.early_retain is None:
                    if not rechecked:               # 判满员之前先现读一次:缓存的视图可能是几秒前的
                        rechecked = True
                        self.table.invalidate()
                        snap = self.table.refresh()
                        continue
                    raise self._full_or_wait(snap)
                else:
                    got, last = self._act(snap, user, d, deadline)
                    if got is not None:
                        return got
                if self.clock() >= deadline:
                    if d.kind == pa.FULL:
                        raise self._full_or_wait(snap)
                    raise PoolWait(last or d.reason or d.kind, min(self.retry_after, 10))
                self.sleep(0.5)
                self.table.invalidate()             # 视图已过期(409 / 刚做了动作):强制重读
                snap = self.table.refresh()

    def _full_or_wait(self, snap) -> Exception:
        """有 Pod 的管家暂时连不上时不说「满员」:那几个 Pod 里可能有空位,只是这会儿看不到(网关刚启动、管家在重启)。"""
        if any(not p.reachable for p in snap.pods):
            return PoolWait("steward_unreachable", min(self.retry_after, 10))
        return PoolFull(self.retry_after)

    def _act(self, snap, user: str, d: pa.Decision, deadline: float) -> Tuple[Optional[Target], str]:
        try:
            if d.kind == pa.WAIT:
                return None, d.reason
            if d.kind == pa.RESUME:
                return self._call(snap, user, d.target, "resume")
            if d.kind == pa.BIND:
                if d.evict_first is not None and not self._evict_and_verify(snap, d.evict_first, deadline):
                    return None, "evicting"
                got, why = self._call(snap, user, d.target, "bind")
                if got is not None and d.evict_after is not None:
                    self._evict_later(snap, d.evict_after)
                return got, why
            if d.kind == pa.FULL:
                return None, self._early_retain(snap, user, d.early_retain)
        except StewardError as exc:
            self.log("池化:为 %s %s 时管家不可达:%s" % (user, d.kind, exc))
            return None, "steward_unreachable"
        return None, d.kind

    # -- 各条路 -------------------------------------------------------------------------------------

    def _forward(self, snap, user: str, ref: pa.Ref) -> Optional[Target]:
        with self._lock:
            t = self._creds.get(user)
        if t is not None and (t.pod, t.slot) == ref:
            return t
        try:
            got, _why = self._call(snap, user, ref, "bind", need_token=False)   # 已在用:幂等取回凭证
        except StewardError as exc:
            self.log("池化:取回 %s 的凭证失败:%s" % (user, exc))
            return None
        return got

    def _call(self, snap, user: str, ref: pa.Ref, verb: str, need_token: bool = True) -> Tuple[Optional[Target], str]:
        pod, slot = ref
        meta = snap.meta.get(pod)
        view = next((p for p in snap.pods if p.name == pod), None)
        if meta is None or view is None:
            return None, "pod_gone"
        token = self.grmp_lookup(user) if need_token else ""
        fn = self.client.bind if verb == "bind" else self.client.resume
        # 租期记录用现读的 containerID:视图可能是腾挪之前的(容器刚重启过),拿旧 ID 去绑,
        # 管家记下的租期就是错的,下一次恢复会被当成「容器换过了」拒掉
        fresh = self.table.fresh_meta(pod) if need_token else None
        cid = (fresh or meta).containers.get(slot, "")
        code, body = fn(pod, meta.ip, slot, user, token, view.in_use, cid)
        if not (code == 200 and body.get("idempotent")):
            self.table.invalidate()                 # 状态变了,或者视图过期了
        if code == 200:
            t = Target(pod=pod, slot=slot, host=meta.ip, port=int(body["opencode"]["port"]),
                       password=body["opencode"]["password"], reports_port=int(body["reports"]["port"]),
                       reports_token=body["reports"]["token"], since=self.clock())
            with self._lock:
                self._creds[user] = t
            if not body.get("idempotent"):
                self.log("池化:%s → %s/槽位 %d(%s)" % (user, pod, slot, verb))
            return t, verb
        reason = str(body.get("reason") or code)
        if reason == "owner_busy" and "/" not in str(body.get("holder") or ""):
            # 独占在池子以外的地方(还没回收的按需模式 Pod):等它释放,不是池子的事
            raise PoolWait("held_elsewhere:%s" % body.get("holder"), self.retry_after)
        if reason not in _RETRY_REASONS:
            self.log("池化:%s 在 %s/槽位 %d 上 %s 失败(%d %s)" % (user, pod, slot, verb, code, reason))
        return None, reason

    def _evict_and_verify(self, snap, ref: pa.Ref, deadline: float) -> bool:
        """发 evict → 等 k8s 里这个容器换了 containerID → verify。到等待上限还没好就 False(下次接着等)。"""
        pod, slot = ref
        meta = snap.meta.get(pod)
        if meta is None:
            return False
        old = meta.containers.get(slot, "")
        code, body = self.client.evict(pod, meta.ip, slot, old)
        if code not in (202, 409):
            self.log("池化:腾挪 %s/槽位 %d 失败(%d %s)" % (pod, slot, code, body.get("reason")))
            return False
        while self.clock() < deadline:
            fresh = self.table.fresh_meta(pod)
            cid = fresh.containers.get(slot, "") if fresh else ""
            if cid and cid != old:
                code, body = self.client.verify(pod, fresh.ip, slot, cid)
                if code == 200:
                    return True
                if body.get("reason") == "state":            # 已经被别人核验过、甚至绑走了
                    return False
            self.sleep(1.0)
        return False

    def _evict_later(self, snap, ref: pa.Ref) -> None:
        pod, slot = ref
        meta = snap.meta.get(pod)
        if meta is None:
            return

        def run() -> None:
            try:
                code, body = self.client.evict(pod, meta.ip, slot, meta.containers.get(slot, ""))
                if code != 202:
                    self.log("池化:腾挪旧的保留槽位 %s/%d 未成(%d %s),交给清扫线程" % (pod, slot, code, body.get("reason")))
            except StewardError as exc:
                self.log("池化:腾挪旧的保留槽位 %s/%d 时管家不可达:%s" % (pod, slot, exc))
        threading.Thread(target=run, name="evict-after", daemon=True).start()

    def _early_retain(self, snap, user: str, ref: pa.Ref) -> str:
        pod, slot = ref
        meta = snap.meta.get(pod)
        view = next((s for p in snap.pods if p.name == pod for s in p.slots if s.index == slot), None)
        if meta is None or view is None or not view.user:
            return "full"
        code, body = self.client.reserve(pod, meta.ip, slot, user, self.policy.reserve_seconds)
        if code != 200:
            return "full"
        self.log("池化:满员,%s 闲置超过 %d 秒,提前转保留,腾出来的槽位预留给 %s"
                 % (view.user, self.policy.early_idle_seconds, user))
        code, body = self.client.retain(pod, meta.ip, slot, view.user)
        with self._lock:
            self._creds.pop(view.user, None)
        return "early_retain" if code == 200 else str(body.get("reason") or code)
