"""池子替换(算法与依据见 docs/specs/2026-11-03-release-pool-swap-experiment.md)。

按槽位在线替换,一个槽位:
  在用 → 等不忙(回执 busy=False 且距上次忙 ≥ quiet_s;超过 busy_timeout_s 交给运维:继续等 / 强制转保留)→ retain
  → evict(槽位进清理中,不再接受绑定与恢复)→ 紧接着 patch 这个容器的镜像(exit 与镜像变化合并成一次重启)
  → 等新容器运行(containerID 变了、spec 镜像是目标)→ verify → 空闲(新版本)
**不能直接 patch 空闲槽位**:安静重启那条路在 v1.4.3 的管家上会沿用旧容器 ID,下一个人转保留后恢复被拒(已修,但旧管家还在)。
管家最后换,且等本 Pod 所有槽位都不忙:换管家时它会把在用的槽位全部转保留(回答进行中会被中断)。

按 Pod 整体替换(目标版本不能与当前版本混跑):模板先换好 → 逐个 Pod 等不忙、把在用的转保留 → 删 Pod → 等重建、槽位全部空闲。

网关的 /admin/pool 经 kubectl exec 进网关容器调用:管理密钥只在网关容器的环境变量里,不出容器。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

ADMIN = r'''
import json, os, sys, urllib.request, urllib.error
a = json.loads(sys.stdin.read() or "{}")
url = "http://127.0.0.1:%s/admin/pool" % (os.environ.get("GATEWAY_LISTEN_PORT") or "8080")
hdr = {"X-Admin-Token": os.environ.get("GATEWAY_ADMIN_TOKEN", ""), "Content-Type": "application/json"}
if a.get("action"):
    req = urllib.request.Request(url + "/" + a["action"], data=json.dumps({"pod": a["pod"], "slot": a["slot"],
                                 "reason": "release"}).encode(), headers=hdr, method="POST")
else:
    req = urllib.request.Request(url, headers=hdr)
try:
    with urllib.request.urlopen(req, timeout=150) as r:
        print(json.dumps({"code": r.status, "body": json.loads(r.read() or b"{}")}))
except urllib.error.HTTPError as e:
    print(json.dumps({"code": e.code, "body": json.loads(e.read() or b"{}")}))
'''


class PoolError(Exception):
    pass


@dataclass
class GatewayAdmin:
    """经 kubectl exec 调网关的 /admin/pool。网关有两个副本,任选一个就绪的。"""
    kube: object
    sleep: Callable[[float], None] = time.sleep

    def _pods(self) -> List[str]:
        """就绪、且不在退出中的网关 Pod。刚滚动更新完时旧 Pod 还短暂显示就绪,exec 进去会被杀(退出码 137)。"""
        out = [p["metadata"]["name"] for p in self.kube.list("pods", "app=agent-gateway")
               if not p["metadata"].get("deletionTimestamp")
               and all(c.get("ready") for c in (p.get("status") or {}).get("containerStatuses") or [{}])]
        if not out:
            raise PoolError("没有就绪的网关 Pod,调不了 /admin/pool")
        return out

    def call(self, action: str = "", pod: str = "", slot: int = 0) -> Tuple[int, dict]:
        last = ""
        for attempt in range(3):
            pods = self._pods()
            res = self.kube.exec(pods[attempt % len(pods)], "", ["python3", "-c", ADMIN],
                                 stdin=json.dumps({"action": action, "pod": pod, "slot": slot}), timeout=180)
            if res.code == 0:
                doc = json.loads(res.out.strip().splitlines()[-1])
                return int(doc["code"]), doc.get("body") or {}
            last = (res.err.strip().splitlines() or ["退出码 %d" % res.code])[-1]
            self.sleep(2 * (attempt + 1))
        raise PoolError("调 /admin/pool 失败(试了 3 次):%s" % last)

    def slots(self) -> Dict[Tuple[str, int], dict]:
        code, body = self.call()
        if code != 200:
            raise PoolError("/admin/pool 返回 %d:%s" % (code, body.get("error", "")))
        return {(p["name"], s["slot"]): dict(s, reachable=p.get("reachable", True)) for p in body.get("pods") or []
                for s in p.get("slots") or []}


@dataclass
class Swapper:
    kube: object
    admin: GatewayAdmin
    image: str                                   # 目标运行时镜像(含仓库前缀与标签)
    log: Callable[[str, str], None] = lambda _lv, _m: None
    progress: Callable[[str, int, str, str], None] = lambda _p, _s, _st, _v: None     # (Pod, 槽位 / -1 = 管家, 状态, 版本)
    gate: Callable[[str, dict], str] = lambda _k, _p: "wait"                          # 交给运维:返回 wait / force
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    quiet_s: float = 10.0
    busy_timeout_s: float = 1800.0
    ready_timeout_s: float = 360.0               # kubelet 重启退避最长 5 分钟
    aborted: List[str] = field(default_factory=list)

    # -- 读 ---------------------------------------------------------------------------------------

    def container(self, pod: str, name: str) -> Tuple[str, str, bool]:
        """(spec 镜像, containerID, 是否在运行)"""
        doc = self.kube.get("pod", pod) or {}
        spec = next((c.get("image", "") for c in (doc.get("spec") or {}).get("containers") or [] if c["name"] == name), "")
        st = next((c for c in (doc.get("status") or {}).get("containerStatuses") or [] if c["name"] == name), {})
        return spec, st.get("containerID", ""), "running" in (st.get("state") or {})

    def slot(self, pod: str, i: int) -> dict:
        return self.admin.slots().get((pod, i)) or {}

    def _quiet(self, s: dict) -> bool:
        if "busy" not in s:
            # 网关还是 v1.4.3(摘要里没有 busy):退一步看 last_seen(会话忙时每几秒刷新一次),要求更久没动静
            return self.clock() - float(s.get("last_seen") or 0) >= max(self.quiet_s, 30.0)
        return not s.get("busy") and self.clock() - float(s.get("busy_at") or 0) >= self.quiet_s

    # -- 等 ---------------------------------------------------------------------------------------

    def _wait(self, cond: Callable[[], bool], timeout: float, what: str, step: float = 2.0) -> None:
        end = self.clock() + timeout
        while not cond():
            if self.clock() >= end:
                raise PoolError("%s:%d 秒内没有完成" % (what, timeout))
            self.sleep(step)

    def wait_not_busy(self, pod: str, slots: List[int]) -> str:
        """等这些槽位都不忙。超时交给运维;运维选强制就返回 force。"""
        start = self.clock()
        while True:
            cur = self.admin.slots()
            busy = [i for i in slots if (cur.get((pod, i)) or {}).get("state") == "active" and not self._quiet(cur[(pod, i)])]
            if not busy:
                return "quiet"
            if self.clock() - start >= self.busy_timeout_s:
                choice = self.gate("drain", {"pod": pod, "slots": busy, "waited_s": int(self.clock() - start)})
                if choice == "force":
                    self.log("w", "运维选择强制转保留 %s 槽位 %s(正在执行的请求会被中断)" % (pod, busy))
                    return "force"
                start = self.clock()
            self.sleep(2)

    # -- 一个槽位 ----------------------------------------------------------------------------------

    def swap_slot(self, pod: str, i: int) -> bool:
        name = "slot-%d" % i
        spec, cid, running = self.container(pod, name)
        s = self.slot(pod, i)
        if spec == self.image and running and s.get("state") in ("free", "active", "retained"):
            return False
        if s.get("state") == "active":
            self.progress(pod, i, "busy" if s.get("busy") else "active", "old")
            self.wait_not_busy(pod, [i])
            code, body = self.admin.call("retain", pod, i)
            if code != 200:
                raise PoolError("%s 槽位 %d 转保留失败(%d):%s" % (pod, i, code, body))
            if body.get("aborted_running"):
                self.aborted.append("%s/%d" % (pod, i))
                self.log("w", "%s 槽位 %d 转保留时中断了一个正在执行的请求" % (pod, i))
            self.progress(pod, i, "retain", "old")
            self.log("", "%s 槽位 %d 转保留:已写回 NAS(%s)" % (pod, i, json.dumps(body.get("synced") or {}, ensure_ascii=False)))
            s = self.slot(pod, i)
        if s.get("state") == "retained" and s.get("dirty"):
            self.log("w", "%s 槽位 %d 写回还没干净,等管家重试" % (pod, i))
            self._wait(lambda: not self.slot(pod, i).get("dirty"), self.ready_timeout_s, "%s 槽位 %d 写回" % (pod, i), 5)
        if self.slot(pod, i).get("state") != "evicting":
            code, body = self.admin.call("evict", pod, i)
            if code != 202:
                raise PoolError("%s 槽位 %d 腾挪失败(%d):%s" % (pod, i, code, body))
        self.kube.patch("pod", pod, {"spec": {"containers": [{"name": name, "image": self.image}]}})
        self.progress(pod, i, "restart", "new")
        self.log("", "腾挪 %s 槽位 %d,紧接着换镜像 → %s" % (pod, i, self.image))
        self._wait(lambda: self._new_running(pod, name, cid), self.ready_timeout_s, "%s/%s 新容器" % (pod, name), 1)
        self.progress(pod, i, "verify", "new")

        def verified() -> bool:
            st = self.slot(pod, i).get("state")
            if st == "free":
                return True
            if st in ("evicting", "disabled"):
                self.admin.call("verify", pod, i)
            return self.slot(pod, i).get("state") == "free"
        self._wait(verified, self.ready_timeout_s, "%s 槽位 %d 核验" % (pod, i), 2)
        self.progress(pod, i, "free", "new")
        self.log("g", "%s 槽位 %d → 空闲(新版本)" % (pod, i))
        return True

    def _new_running(self, pod: str, name: str, old_cid: str) -> bool:
        spec, cid, running = self.container(pod, name)
        return spec == self.image and running and bool(cid) and cid != old_cid

    # -- 管家 / 一个 Pod ---------------------------------------------------------------------------

    def swap_steward(self, pod: str, slots: List[int]) -> bool:
        spec, cid, running = self.container(pod, "steward")
        if spec == self.image and running:
            return False
        self.wait_not_busy(pod, slots)
        self.kube.patch("pod", pod, {"spec": {"containers": [{"name": "steward", "image": self.image}]}})
        self.progress(pod, -1, "restart", "new")
        self.log("", "%s 所有槽位都不忙,换管家(在用的人被转保留,下一句自动恢复)" % pod)
        self._wait(lambda: self._new_running(pod, "steward", cid), self.ready_timeout_s, "%s 管家新容器" % pod, 1)
        self._wait(lambda: any(k[0] == pod and v.get("reachable") for k, v in self.admin.slots().items()),
                   self.ready_timeout_s, "%s 管家重新可达" % pod, 2)
        self.progress(pod, -1, "ok", "new")
        return True

    def slot_count(self, pod: str) -> List[int]:
        doc = self.kube.get("pod", pod) or {}
        return sorted(int(c["name"].split("-")[1]) for c in (doc.get("spec") or {}).get("containers") or []
                      if c["name"].startswith("slot-"))

    def swap_pod_online(self, pod: str) -> None:
        slots = self.slot_count(pod)
        for i in slots:
            self.swap_slot(pod, i)
        self.swap_steward(pod, slots)

    def replace_pod(self, pod: str) -> None:
        """按 Pod 整体替换:模板应已换好(OnDelete)。在用的先等不忙再转保留,然后删 Pod 等重建。"""
        slots = self.slot_count(pod)
        choice = self.wait_not_busy(pod, slots)
        for i in slots:
            s = self.slot(pod, i)
            if s.get("state") == "active":
                code, body = self.admin.call("retain", pod, i)
                if code == 200 and body.get("aborted_running"):
                    self.aborted.append("%s/%d" % (pod, i))
                self.progress(pod, i, "retain", "old")
        if choice == "force":
            self.log("w", "%s 强制整体替换" % pod)
        old_uid = (self.kube.get("pod", pod) or {}).get("metadata", {}).get("uid")
        self.kube.delete("pod", pod, wait=True)
        self.log("", "删除 %s,等 StatefulSet 按新模板重建" % pod)

        def back() -> bool:
            doc = self.kube.get("pod", pod) or {}
            if doc.get("metadata", {}).get("uid") in (None, old_uid):
                return False
            cur = self.admin.slots()
            return all((cur.get((pod, i)) or {}).get("state") == "free" for i in slots)
        self._wait(back, self.ready_timeout_s, "%s 重建" % pod, 3)
        for i in slots:
            self.progress(pod, i, "free", "new")
        self.progress(pod, -1, "ok", "new")
