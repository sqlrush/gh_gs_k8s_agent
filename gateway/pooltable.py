"""网关看到的整个池子(固定池化,2026-10-23):k8s 的 Pod 列表 + 各管家的 status,缓存几秒。

真相在管家;这里只是缓存,过期的后果由管家的 409 兜住(调用方刷新后重来)。k8s 那一半给的是管家给不了的:
Pod IP(网关直连)、各槽位容器的 containerID(腾挪核验的根)与最近一次启动时间(挑腾挪对象时避开重启退避)。
"""
from __future__ import annotations

import calendar
import concurrent.futures
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from . import poolalloc as pa
from .poolclient import StewardError

CORE = "/api/v1"
_SLOT_CONTAINER = re.compile(r"^slot-(\d+)$")


@dataclass(frozen=True)
class PodMeta:
    name: str
    ip: str
    containers: Dict[int, str] = field(default_factory=dict)       # 槽位号 → containerID
    started: Dict[int, float] = field(default_factory=dict)        # 槽位号 → 这个容器开始运行的时间
    ports: Dict[int, Tuple[int, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    at: float
    pods: Tuple[pa.PodView, ...]
    meta: Dict[str, PodMeta]


def _rfc3339(text: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def pod_meta(pod: dict) -> Optional[PodMeta]:
    """k8s Pod 对象 → PodMeta;正在删除、没有 IP、不在 Running 的返回 None(这一轮不分配到它)。"""
    md, st = pod.get("metadata") or {}, pod.get("status") or {}
    if md.get("deletionTimestamp") or st.get("phase") != "Running" or not st.get("podIP"):
        return None
    containers, started = {}, {}
    for cs in st.get("containerStatuses") or []:
        m = _SLOT_CONTAINER.match(cs.get("name") or "")
        if not m:
            continue
        i = int(m.group(1))
        containers[i] = cs.get("containerID") or ""
        started[i] = _rfc3339(((cs.get("state") or {}).get("running") or {}).get("startedAt") or "")
    return PodMeta(name=md.get("name") or "", ip=st["podIP"], containers=containers, started=started)


def slot_view(pod: str, s: dict, started: float) -> pa.SlotView:
    def num(k: str) -> float:
        try:
            return float(s.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0
    busy_at = num("busy_at")
    return pa.SlotView(pod=pod, index=int(s.get("slot", -1)), state=str(s.get("state") or pa.BOOTING),
                       user=s.get("user_id") or None,
                       last_seen=max(num("last_active"), busy_at, num("bound_at")),
                       retained_at=num("retained_at"), dirty=bool(s.get("dirty")), stale=bool(s.get("stale")),
                       listener=str(s.get("listener") or "unknown"), offender=bool(s.get("offender")),
                       restarted_at=started, reserved_for=s.get("reserved_for") or None,
                       reserved_until=num("reserved_until"), evict_pending=s.get("evicting_since") is not None)


class PoolTable:
    def __init__(self, kube, client, selector: str = "app=gaussdb-agent,role=pool", refresh_s: float = 5.0,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.kube, self.client, self.selector, self.refresh_s = kube, client, selector, refresh_s
        self.log, self.clock = log, clock
        self._snap: Optional[Snapshot] = None
        self._last: Optional[Snapshot] = None   # 作废了也留着:管家暂时连不上时沿用它看到的槽位
        self._gen = 0                           # 第几次真读;并发的刷新只读一次,但绝不返回比调用更早的视图
        self._lock = threading.Lock()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="pooltable")
        self._down_logged: Dict[str, bool] = {}

    def snapshot(self, max_age: Optional[float] = None) -> Snapshot:
        snap = self._snap
        age = self.refresh_s if max_age is None else max_age
        if snap is not None and self.clock() - snap.at < age:
            return snap
        return self.refresh()

    def invalidate(self) -> None:
        self._snap = None

    def refresh(self) -> Snapshot:
        ticket = self._gen
        with self._lock:                        # 同时来的刷新只做一次:等到锁时别人已经读过了就用它的
            if self._gen != ticket and self._snap is not None:
                return self._snap
            items = (self.kube.list(CORE, "pods", label_selector=self.selector).get("items") or [])
            metas = [m for m in (pod_meta(p) for p in items) if m is not None]
            futs = {m.name: self._pool.submit(self.client.status, m.name, m.ip) for m in metas}
            pods, meta = [], {}
            for m in sorted(metas, key=lambda x: x.name):
                try:
                    st = futs[m.name].result(timeout=self.client.timeout_s + 1)
                    self._down_logged.pop(m.name, None)
                except (StewardError, concurrent.futures.TimeoutError) as exc:
                    if not self._down_logged.get(m.name):
                        self._down_logged[m.name] = True
                        self.log("池化:%s 的管家暂时连不上,这一轮不往它分配(%s)" % (m.name, exc))
                    # 沿用上一次看到的槽位(标成不可分配):在用的人照常转发 —— 管家重启不该打断对话
                    last = self._last
                    prev = next((p for p in (last.pods if last else ()) if p.name == m.name), None)
                    pods.append(pa.PodView(m.name, prev.slots if prev else (), reachable=False))
                    meta[m.name] = last.meta.get(m.name, m) if last else m
                    continue
                raw = [s for s in st.get("slots") or [] if isinstance(s, dict) and isinstance(s.get("slot"), int)]
                slots = tuple(slot_view(m.name, s, m.started.get(s["slot"], 0.0)) for s in raw)
                ports = {s["slot"]: (int(s["ports"]["opencode"]), int(s["ports"]["reports"]))
                         for s in raw if isinstance(s.get("ports"), dict)}
                meta[m.name] = PodMeta(m.name, m.ip, m.containers, m.started, ports)
                pods.append(pa.PodView(m.name, slots, reachable=True))
            self._snap = self._last = Snapshot(self.clock(), tuple(pods), meta)
            self._gen += 1
            return self._snap

    def fresh_meta(self, pod: str) -> Optional[PodMeta]:
        """现读一次这个 Pod(腾挪核验要最新的 containerID,不能用缓存)。"""
        got = self.kube.get(CORE, "pods", pod)
        return pod_meta(got) if got else None
