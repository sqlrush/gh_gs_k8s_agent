"""固定池化的后台清扫(2026-10-23):每一轮先把交互活动报给各管家,再按 poolalloc.plan 转保留 / 腾挪 / 核验。

两个网关副本各跑一份:动作对管家是幂等的(第二个到的拿 409),不需要选主。活动先报再判闲置 ——
不然本副本刚转发过的人,会因为管家那里还没记上而被转保留。转保留最慢(停 opencode、完整写回),放到线程池里做,
同一个槽位正在做的不重复下发。
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Callable, Dict, List, Optional, Set

from . import poolalloc as pa
from .poolclient import StewardError


class PoolSweeper:
    def __init__(self, table, client, router, policy: pa.Policy, sweep_s: float = 30.0,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.table, self.client, self.router, self.policy = table, client, router, policy
        self.sweep_s, self.log, self.clock = sweep_s, log, clock
        self._busy: Set[pa.Ref] = set()
        self._lock = threading.Lock()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="poolsweep")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 活动 ------------------------------------------------------------------------------------

    def flush_activity(self, snap) -> int:
        acts = self.router.drain_activity()
        if not acts:
            return 0
        by_pod: Dict[str, List[list]] = {}
        mapped = set()
        for p in snap.pods:
            for s in p.slots:
                if s.state == pa.ACTIVE and s.user in acts:
                    by_pod.setdefault(p.name, []).append([s.index, s.user, acts[s.user]])
                    mapped.add(s.user)
        # 这一轮对不上号的(他那个 Pod 的管家暂时连不上、视图还没刷到他)放回去;太旧的就不要了
        keep = {u: ts for u, ts in acts.items() if u not in mapped and self.clock() - ts < self.policy.idle_seconds}
        if keep:
            self.router.restore_activity(keep)
        n = 0
        for pod, items in by_pod.items():
            meta = snap.meta.get(pod)
            if meta is None:
                continue
            try:
                code, body = self.client.touch(pod, meta.ip, items)
                n += int(body.get("touched") or 0) if code == 200 else 0
            except StewardError as exc:
                self.log("池化:向 %s 报活动失败(下一轮再报):%s" % (pod, exc))
                self.router.restore_activity({user: ts for _slot, user, ts in items})   # 放回去,别丢
        return n

    # -- 一轮 ------------------------------------------------------------------------------------

    def sweep_once(self, wait: bool = False) -> List[pa.Action]:
        try:
            self.flush_activity(self.table.snapshot())
            snap = self.table.refresh()
        except Exception as exc:                        # noqa: BLE001  一轮失败不能让清扫停掉
            self.log("池化:清扫读取池子失败(下一轮再试):%s" % exc)
            return []
        actions = pa.plan(snap.pods, self.clock(), self.policy)
        futs = []
        for a in actions:
            with self._lock:
                if a.ref in self._busy:
                    continue
                self._busy.add(a.ref)
            futs.append(self._pool.submit(self._run, snap, a))
        if wait:
            concurrent.futures.wait(futs, timeout=300)
        return actions

    def _run(self, snap, a: pa.Action) -> None:
        pod, slot = a.ref
        meta = snap.meta.get(pod)
        try:
            if meta is None:
                return
            if a.kind == pa.RETAIN:
                code, body = self.client.retain(pod, meta.ip, slot, a.user)
                self.router.forget(a.user)
                if code == 200:
                    self.log("池化:%s 转保留(%s,%s/槽位 %d%s)" % (a.user, a.reason, pod, slot,
                                                              ",写回不干净" if body.get("dirty") else ""))
            elif a.kind == pa.EVICT:
                code, body = self.client.evict(pod, meta.ip, slot, meta.containers.get(slot, ""))
                if code == 202:
                    self.log("池化:腾挪 %s/槽位 %d(%s,原来是 %s)" % (pod, slot, a.reason, a.user))
            elif a.kind == pa.VERIFY:
                fresh = self.table.fresh_meta(pod)
                cid = fresh.containers.get(slot, "") if fresh else ""
                if cid:
                    code, body = self.client.verify(pod, fresh.ip, slot, cid)
                    if code == 200:
                        self.log("池化:%s/槽位 %d 腾挪核验通过,已空闲" % (pod, slot))
        except StewardError as exc:
            self.log("池化:%s %s/槽位 %d 时管家不可达:%s" % (a.kind, pod, slot, exc))
        except Exception as exc:                        # noqa: BLE001
            self.log("池化:%s %s/槽位 %d 出错:%s" % (a.kind, pod, slot, exc))
        finally:
            with self._lock:
                self._busy.discard(a.ref)
            self.table.invalidate()

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.sweep_s):
                self.sweep_once()
        self._thread = threading.Thread(target=loop, name="poolsweeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class PoolAdmin:
    """/admin/pool:整池摘要(不含秘密)与几个运维动作。腾挪 / 核验用现读的 containerID。"""

    ACTIONS = ("retain", "evict", "verify", "disable")

    def __init__(self, table, client, router):
        self.table, self.client, self.router = table, client, router

    def summary(self) -> dict:
        snap = self.table.refresh()
        return {"pods": [{"name": p.name, "reachable": p.reachable, "in_use": p.in_use, "free": p.free,
                          "retained": p.retained,
                          "slots": [{"slot": s.index, "state": s.state, "user_id": s.user, "last_seen": s.last_seen,
                                     "dirty": s.dirty, "stale": s.stale, "listener": s.listener,
                                     "offender": s.offender, "reserved_for": s.reserved_for} for s in p.slots]}
                         for p in snap.pods]}

    def act(self, action: str, pod: str, slot: int, reason: str = "admin") -> "tuple[int, dict]":
        if action not in self.ACTIONS:
            return 404, {"error": "没有这个动作:%s" % action}
        meta = self.table.fresh_meta(pod)
        snap = self.table.snapshot()
        view = next((s for p in snap.pods if p.name == pod for s in p.slots if s.index == slot), None)
        if meta is None or view is None:
            return 404, {"error": "没有这个 Pod 或槽位:%s/%s" % (pod, slot)}
        cid = meta.containers.get(slot, "")
        try:
            if action == "retain":
                code, body = self.client.retain(pod, meta.ip, slot, view.user or "")
                if view.user:
                    self.router.forget(view.user)
            elif action == "evict":
                code, body = self.client.evict(pod, meta.ip, slot, cid)
            elif action == "verify":
                code, body = self.client.verify(pod, meta.ip, slot, cid)
            else:
                code, body = self.client.disable(pod, meta.ip, slot, reason)
        except StewardError as exc:
            return 502, {"error": str(exc)}
        finally:
            self.table.invalidate()
        return code, body
