"""资源大盘的数据汇总(/dash/api/resources,2026-11-18)。只给运维管理员,整体缓存 20 秒。

每一块各自取、各自降级:读失败的那一块标 {"available": false, "reason": ...},其余照常返回 ——
大盘宁可空一块并写明原因,也不编数,更不能因为一块读不到整页 500。

- components:本命名空间各 Deployment 的副本、就绪数、requests / limits、Pod(K8s API,网关的 Role 已有读权限);
- jobs:本命名空间的 Job(标准知识库安装);
- metrics:实际 CPU / 内存(metrics.k8s.io)。集群没装 metrics-server 时如实标不可用;
- fuyao:网关自己的沙箱摘要(FuyaoSweeper.summary(),与 /admin/fuyao 同一份);
- platform:平台的沙箱组、子组与沙箱(sandbox.flux.io,只读,ClusterRole 见 k8s/fuyao/platform-read.yaml),
  只取配置里的两个组,按沙箱 ID 与 fuyao 摘要关联,按承载 Pod 归组;
- ops:状态服务的统计(今天各工号请求次数、24 小时趋势、NAS 用量);
- tokens:这一版不做,如实标不可用。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import reaper
from .kube import KubeError

TTL_S = 20
PLATFORM_NS = "flux-system"                     # 平台的命名空间(sandboxgroups.yaml、FUYAO_API 都在这里)
SBX_API = "/apis/sandbox.flux.io/v1alpha1"
NEED_RBAC = "需授权只读"


def cpu_cores(q: Any) -> Optional[float]:
    """K8s 的 CPU 数量 → 核:'100m' → 0.1,'2' → 2.0,'2048m' → 2.048。认不出返回 None。"""
    s = str(q or "").strip()
    m = re.match(r"^([0-9.]+)(m|n|u)?$", s)
    if not m:
        return None
    v = float(m.group(1))
    return {"m": v / 1000, "u": v / 1e6, "n": v / 1e9}.get(m.group(2) or "", v)


_MEM = {"": 1 / 1048576, "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1048576,
        "k": 1000 / 1048576, "M": 1e6 / 1048576, "G": 1e9 / 1048576, "T": 1e12 / 1048576}


def mem_mib(q: Any) -> Optional[float]:
    """K8s 的内存数量 → MiB:'128Mi' → 128,'4Gi' → 4096,'4294967296' → 4096。"""
    s = str(q or "").strip()
    m = re.match(r"^([0-9.]+)(Ki|Mi|Gi|Ti|k|M|G|T)?$", s)
    if not m:
        return None
    return float(m.group(1)) * _MEM[m.group(2) or ""]


def _res(spec: dict) -> dict:
    """容器 resources 的 requests / limits → {cpu: 核, mem: MiB}(没声明的项为 None)。"""
    out = {}
    for k in ("requests", "limits"):
        d = (spec or {}).get(k) or {}
        out[k] = {"cpu": cpu_cores(d.get("cpu")) if "cpu" in d else None,
                  "mem": mem_mib(d.get("memory")) if "memory" in d else None}
    return out


def _sum(parts: List[dict]) -> dict:
    out = {}
    for k in ("requests", "limits"):
        vals = [p[k] for p in parts]
        out[k] = {f: (None if any(v[f] is None for v in vals) else round(sum(v[f] for v in vals), 4)) for f in ("cpu", "mem")}
    return out


def unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason}


def _why(exc: Exception, what: str) -> str:
    st = getattr(exc, "status", 0)
    if st == 403:
        return "%s:%s(网关的服务账号没有这项只读权限)" % (NEED_RBAC, what)
    if st == 404:
        return "%s 不存在" % what
    return "读 %s 失败(详见网关日志)" % what


class Collector:
    def __init__(self, app, ttl_s: int = TTL_S, clock: Callable[[], float] = time.time,
                 ops=None, platform_ns: str = PLATFORM_NS):
        self.app, self.ttl_s, self.clock, self.ops, self.platform_ns = app, ttl_s, clock, ops, platform_ns
        self._lock = threading.Lock()
        self._at, self._doc = float("-inf"), None

    @property
    def kube(self):
        return self.app.mgr.kube

    def get(self) -> dict:
        """缓存 ttl_s 秒;同一时刻只建一份(并发请求等这一份,不各自打一遍 K8s API)。"""
        with self._lock:
            now = self.clock()
            if self._doc is None or now - self._at >= self.ttl_s:
                self._doc, self._at = self.build(), now
            return self._doc

    def _safe(self, name: str, fn: Callable[[], dict]) -> dict:
        try:
            return fn()
        except Exception as exc:                    # noqa: BLE001 —— 一块出错不拖垮整页
            self.app.log("资源大盘:取 %s 失败:%s" % (name, exc))
            return unavailable(_why(exc, name))

    def build(self) -> dict:
        cfg = self.app.cfg
        fuyao_on = cfg.runtime_mode == "fuyao"
        not_fuyao = unavailable("当前不是 Fuyao 模式(运行模式 %s)" % cfg.runtime_mode)
        summary = self._safe("沙箱摘要", self._fuyao) if fuyao_on else not_fuyao
        return {
            "generated_at": self.clock(), "product_name": cfg.product_name, "mode": cfg.runtime_mode,
            "namespace": cfg.namespace, "version": {"user_image_tag": cfg.image_tag},
            "components": self._safe("本命名空间的 Deployment", self._components),
            "jobs": self._safe("本命名空间的 Job", self._jobs),
            "metrics": self._safe("metrics.k8s.io", self._metrics),
            "nodes": unavailable("节点容量要集群级读 nodes 的权限,这一版没有申请"),
            "fuyao": summary,
            "platform": self._safe("平台的沙箱组(sandbox.flux.io)", lambda: self._platform(summary)) if fuyao_on else not_fuyao,
            "ops": self._safe("状态服务的统计", self._ops) if fuyao_on else not_fuyao,
            "tokens": unavailable("token 用量这一版不做:交互耗时报告里有按次统计,还没有按工号汇总上报"),
        }

    # -- 本命名空间 ------------------------------------------------------------------------------------

    def _components(self) -> dict:
        ns = self.app.cfg.namespace
        deps = self.kube.request("GET", "/apis/apps/v1/namespaces/%s/deployments" % ns).get("items") or []
        pods = self.kube.request("GET", "/api/v1/namespaces/%s/pods" % ns).get("items") or []
        items, users = [], 0
        for d in deps:
            md, spec, st = d.get("metadata") or {}, d.get("spec") or {}, d.get("status") or {}
            name = md.get("name") or ""
            if reaper.user_of(name):
                users += 1                          # 按需模式下每人一个的用户 Deployment:只计数,不逐个列
                continue
            tpl = (spec.get("template") or {}).get("spec") or {}
            cs = tpl.get("containers") or []
            sel = ((spec.get("selector") or {}).get("matchLabels") or {})
            mine = [p for p in pods if sel and all(((p.get("metadata") or {}).get("labels") or {}).get(k) == v for k, v in sel.items())]
            items.append({
                "name": name, "replicas": int(spec.get("replicas") or 0), "ready": int(st.get("readyReplicas") or 0),
                "images": [c.get("image") for c in cs], "per_replica": _sum([_res(c.get("resources")) for c in cs]),
                "pods": [_pod(p) for p in mine],
            })
        items.sort(key=lambda x: x["name"])
        return {"available": True, "items": items, "user_deployments": users}

    def _jobs(self) -> dict:
        ns = self.app.cfg.namespace
        jobs = self.kube.request("GET", "/apis/batch/v1/namespaces/%s/jobs" % ns).get("items") or []
        out = []
        for j in jobs:
            st = j.get("status") or {}
            done = any(c.get("type") == "Complete" and c.get("status") == "True" for c in st.get("conditions") or [])
            failed = any(c.get("type") == "Failed" and c.get("status") == "True" for c in st.get("conditions") or [])
            out.append({"name": (j.get("metadata") or {}).get("name"), "status": "Complete" if done else "Failed" if failed else "Running",
                        "completed_at": st.get("completionTime")})
        return {"available": True, "items": out}

    def _metrics(self) -> dict:
        ns = self.app.cfg.namespace
        try:
            got = self.kube.request("GET", "/apis/metrics.k8s.io/v1beta1/namespaces/%s/pods" % ns)
        except KubeError as exc:
            if exc.status in (404, 503):
                return unavailable("未装 metrics-server(集群没有 metrics.k8s.io),实际 CPU / 内存不可用")
            raise
        pods = {}
        for p in got.get("items") or []:
            cs = p.get("containers") or []
            pods[(p.get("metadata") or {}).get("name")] = {
                "cpu": round(sum(cpu_cores((c.get("usage") or {}).get("cpu")) or 0 for c in cs), 4),
                "mem": round(sum(mem_mib((c.get("usage") or {}).get("memory")) or 0 for c in cs), 1)}
        return {"available": True, "pods": pods}

    # -- Fuyao ---------------------------------------------------------------------------------------

    def _fuyao(self) -> dict:
        adm = self.app.fuyao_admin
        if adm is None:
            return unavailable("网关的 Fuyao 清扫器没有启动")
        return dict(adm.summary(), available=True)

    def _ops(self) -> dict:
        if self.ops is None:
            return unavailable("没有配状态服务地址(FUYAO_STATE_URL)")
        return dict(self.ops.stats(), available=True)

    def _platform(self, summary: dict) -> dict:
        cfg = self.app.cfg
        want: Dict[str, List[str]] = {}
        for name, kind in ((cfg.fuyao_runtime_group, "runtime"), (cfg.fuyao_import_group, "kb-import")):
            want.setdefault(name, []).append(kind)          # 诊断与导入可以共用一个组(测试环境就是)
        base = "%s/namespaces/%s" % (SBX_API, self.platform_ns)
        subs = self.kube.request("GET", base + "/subsandboxgroups").get("items") or []
        sbxs = self.kube.request("GET", base + "/sandboxes").get("items") or []
        ours = {s.get("sandbox"): s for s in (summary.get("sandboxes") or [])} if summary.get("available") else {}
        groups = []
        for name, kinds in want.items():
            if not name:
                continue
            try:
                sg = self.kube.request("GET", "%s/sandboxgroups/%s" % (base, name))
            except KubeError as exc:
                if exc.status != 404:
                    raise
                groups.append({"name": name, "kinds": kinds, "missing": True,
                               "reason": "平台上没有这个沙箱组(%s/%s)" % (self.platform_ns, name)})
                continue
            spec, st = sg.get("spec") or {}, sg.get("status") or {}
            cap = spec.get("capacity") or {}
            mine = [x for x in subs if (x.get("spec") or {}).get("ownerSandboxGroup") == name]
            ready = sum(int((x.get("status") or {}).get("readyAgentPods") or 0) for x in mine)
            desired = sum(int((x.get("status") or {}).get("desiredAgentPods") or 0) for x in mine)
            per_pod = int(cap.get("maxSandboxesPerPod") or 0)
            # 以子组(真正调度的)为准:鲲鹏现场沙箱组状态的 totalAgentPods / totalCapacity 跟着 agentPodMax 走
            # (3 / 6),子组是 desired=2 / capacity=4(2026-11-18 实测)。没有子组时才退回沙箱组的数
            sub_cap = sum(int((x.get("status") or {}).get("capacity") or 0) for x in mine)
            agent_pods = desired or int(st.get("totalAgentPods") or 0)
            pods: Dict[str, List[str]] = {}
            unplaced: List[str] = []
            for s in sbxs:
                if (s.get("spec") or {}).get("sandboxGroup") != name:
                    continue
                sid = (s.get("status") or {}).get("sandboxID") or (s.get("metadata") or {}).get("name")
                if sid not in ours:
                    continue                        # 平台上这个组里别人的沙箱(不是本智能体建的)不显示
                pod = (s.get("status") or {}).get("assignedSandboxAgentPod") or ""
                (pods.setdefault(pod, []) if pod else unplaced).append(sid)
            res = _res(spec.get("sandboxResources") or {})
            groups.append({
                "name": name, "kinds": kinds, "phase": st.get("phase") or "",
                "pod_min": int(cap.get("agentPodMin") or 0), "pod_max": int(cap.get("agentPodMax") or 0), "per_pod": per_pod,
                "agent_pods": agent_pods, "ready_agent_pods": ready, "desired_agent_pods": desired,
                "capacity": sub_cap or agent_pods * per_pod or int(st.get("totalCapacity") or 0),
                "capacity_max": int(cap.get("agentPodMax") or 0) * per_pod,
                "allocated": sum(int((x.get("status") or {}).get("allocated") or 0) for x in mine),
                "per_sandbox": res, "agent_reserve": _sum([res] * per_pod) if per_pod else None,
                "pods": [{"name": p, "sandboxes": ids} for p, ids in sorted(pods.items())],
                "unplaced": unplaced,
            })
        phases = {}
        for s in sbxs:
            sid = (s.get("status") or {}).get("sandboxID") or (s.get("metadata") or {}).get("name")
            if sid in ours:
                phases[sid] = {"phase": (s.get("status") or {}).get("phase") or "",
                               "agent_pod": (s.get("status") or {}).get("assignedSandboxAgentPod") or "",
                               "group": (s.get("spec") or {}).get("sandboxGroup") or ""}
        return {"available": True, "namespace": self.platform_ns, "groups": groups, "sandboxes": phases,
                "note": "承载 Pod 由平台管理,逐个的就绪状态要读平台命名空间的 Pod(未申请),这里给的是子组上报的就绪数"}


def _pod(p: dict) -> dict:
    st = p.get("status") or {}
    cs = st.get("containerStatuses") or []
    return {"name": (p.get("metadata") or {}).get("name"), "phase": st.get("phase") or "",
            "ready": bool(cs) and all(c.get("ready") for c in cs), "restarts": sum(int(c.get("restartCount") or 0) for c in cs)}
