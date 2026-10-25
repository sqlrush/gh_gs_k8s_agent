"""用户 Pod 起不来时,读 Pod 状态与事件,归成一类原因(v1.1.6 待修第 2 项)。

**以前:** 用户等满 120 秒只拿到「您的环境正在启动,请稍后重试」,网关日志里没有原因 ——
2026-09-24 客户现场靠 `kubectl describe pod` 才看到是 NAS / 镜像 / 目录属主中的哪一个;
等下去也不会好的那几类(拉不到镜像、配置缺失、个人目录初始化失败)还让用户白等两分钟。

**两类读者,两份话:** category 给最终用户看,只说是哪一类、不带任何内部资源名(Secret 名、
镜像仓库地址都算);detail 给运维看(网关日志 / 控制 API),原因原文照录。

classify 是纯函数,单独可测;read 只负责从 k8s 取 Pod 与事件。需要的权限是本命名空间内
pods 的 get/list 与 events 的 list(k8s/base/gateway.yaml);没给时 read 抛 KubeError,
由调用方退回原来的「正在启动」说法。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .kube import KubeError

CORE = "/api/v1"
INIT_HOME = "prepare-home"          # k8s/templates 里建个人目录的初始化容器
MAX_EVENTS = 3
MAX_TEXT = 300

IMAGE = "拉不到镜像"
CONFIG = "容器配置缺失"
HOME = "个人目录初始化失败"
SCHEDULE = "集群资源不足,调度不上"
MOUNT = "存储(NAS)挂不上"
CRASH = "启动后反复退出"
NO_POD = "还没建出 Pod"
STARTING = "仍在启动"

# 等下去也不会好的等待原因:ErrImagePull 是第一次失败,可能只是网络抖动,不算;退避之后才算
_IMAGE_PERMANENT = {"ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}
_IMAGE_ANY = _IMAGE_PERMANENT | {"ErrImagePull"}
_CONFIG = {"CreateContainerConfigError", "CreateContainerError"}
_MOUNT_EVENTS = {"FailedMount", "FailedAttachVolume"}


@dataclass(frozen=True)
class Diagnosis:
    category: str       # 给用户看:一类原因
    detail: str         # 给运维看:原因原文
    permanent: bool     # 等下去也不会好 → 不等满超时,提前告诉用户


class NotReady(Exception):
    """等不到就绪。diagnosis 为 None = 读不到 Pod 状态(没给权限 / k8s 不可达),只知道超时。"""

    def __init__(self, diagnosis: Optional[Diagnosis], waited_s: int):
        self.diagnosis = diagnosis
        self.waited_s = waited_s
        super().__init__(self.detail)

    @property
    def category(self) -> str:
        return self.diagnosis.category if self.diagnosis else ""

    @property
    def permanent(self) -> bool:
        return bool(self.diagnosis and self.diagnosis.permanent)

    @property
    def detail(self) -> str:
        base = ("等了 %d 秒,判定等下去不会好" if self.permanent else "%d 秒内未就绪") % self.waited_s
        return "%s:%s" % (base, self.diagnosis.detail) if self.diagnosis else base


def _cut(text: Any) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + "…"


def _exit_text(term: Dict[str, Any]) -> str:
    parts = ["退出码 %s" % term.get("exitCode")]
    if term.get("reason"):
        parts.append(str(term["reason"]))
    if term.get("message"):
        parts.append(_cut(term["message"]))
    return "、".join(parts)


def _when(ev: Dict[str, Any]) -> str:
    return str(ev.get("lastTimestamp") or ev.get("eventTime") or ev.get("firstTimestamp") or "")


def _warnings(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ws = [e for e in events if e.get("type") == "Warning"]
    return sorted(ws, key=_when)[-MAX_EVENTS:]


def _with_events(head: str, events: List[Dict[str, Any]]) -> str:
    ws = _warnings(events)
    if not ws:
        return head
    return "%s;最近的告警事件:%s" % (head, " | ".join("%s %s" % (e.get("reason"), _cut(e.get("message"))) for e in ws))


def _init_failure(status: Dict[str, Any]) -> Optional[str]:
    for c in status.get("initContainerStatuses") or []:
        st = c.get("state") or {}
        term = st.get("terminated")
        if term and int(term.get("exitCode") or 0) != 0:
            return "初始化容器 %s 失败:%s" % (c.get("name"), _exit_text(term))
        if (st.get("waiting") or {}).get("reason") == "CrashLoopBackOff":
            last = (c.get("lastState") or {}).get("terminated") or {}
            return "初始化容器 %s 反复失败:%s" % (c.get("name"), _exit_text(last) if last else "没有退出信息")
    return None


def _waiting(status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """第一个处于等待态的容器(初始化容器在前)。"""
    for key in ("initContainerStatuses", "containerStatuses"):
        for c in status.get(key) or []:
            w = (c.get("state") or {}).get("waiting")
            if w and w.get("reason") not in (None, "", "PodInitializing", "ContainerCreating"):
                return {"container": c.get("name"), "restarts": c.get("restartCount", 0),
                        "last": (c.get("lastState") or {}).get("terminated") or {}, **w}
    return None


def classify(pod: Dict[str, Any], events: List[Dict[str, Any]]) -> Diagnosis:
    """Pod + 它的事件 → 一类原因。顺序即优先级:越确定、越「等下去不会好」的越先判。"""
    name = (pod.get("metadata") or {}).get("name", "?")
    status = pod.get("status") or {}
    phase = status.get("phase") or "未知"
    head = "Pod %s(%s)" % (name, phase)

    init = _init_failure(status)
    if init:
        return Diagnosis(HOME, _with_events("%s:%s" % (head, init), events), True)

    w = _waiting(status)
    if w and w.get("reason") in _IMAGE_ANY:
        text = "%s:容器 %s %s:%s" % (head, w["container"], w["reason"], _cut(w.get("message")))
        return Diagnosis(IMAGE, _with_events(text, events), w["reason"] in _IMAGE_PERMANENT)
    if w and w.get("reason") in _CONFIG:
        text = "%s:容器 %s %s:%s" % (head, w["container"], w["reason"], _cut(w.get("message")))
        return Diagnosis(CONFIG, _with_events(text, events), True)

    for c in status.get("conditions") or []:
        if c.get("type") == "PodScheduled" and c.get("status") == "False":
            text = "%s:调度不上(%s):%s" % (head, c.get("reason"), _cut(c.get("message")))
            return Diagnosis(SCHEDULE, _with_events(text, events), False)

    mount = [e for e in _warnings(events) if e.get("reason") in _MOUNT_EVENTS]
    if mount:
        return Diagnosis(MOUNT, _with_events("%s:存储卷挂不上" % head, events), False)

    if w and w.get("reason") == "CrashLoopBackOff":
        last = _exit_text(w["last"]) if w["last"] else "没有退出信息"
        text = "%s:容器 %s 已重启 %s 次,上一次 %s" % (head, w["container"], w["restarts"], last)
        return Diagnosis(CRASH, _with_events(text, events), False)

    return Diagnosis(STARTING, _with_events(head, events), False)


def read(kube, user_id: str, kind: str) -> Diagnosis:
    """取这个人这种形态的 Pod(跳过正在删的,取最新)与它的事件,交给 classify。

    读 Pod 失败(没权限 / k8s 不可达)→ 抛 KubeError,调用方退回只报超时。
    读事件失败不影响判断 —— Pod 状态本身已经够判大多数情况。
    """
    sel = "app=gaussdb-agent,role=%s,user=%s" % (kind, user_id)
    items = kube.list(CORE, "pods", label_selector=sel).get("items") or []
    live = [p for p in items if not (p.get("metadata") or {}).get("deletionTimestamp")]
    if not live:
        return Diagnosis(NO_POD, "%s-%s 还没有 Pod:Deployment 已建,但副本没建出来"
                                 "(配额 / 准入策略拒绝时会这样,看 kubectl describe rs -l user=%s)" % (kind, user_id, user_id),
                         False)
    pod = max(live, key=lambda p: str((p.get("metadata") or {}).get("creationTimestamp") or ""))
    try:
        events = kube.list(CORE, "events",
                           field_selector="involvedObject.name=%s" % pod["metadata"]["name"]).get("items") or []
    except (KubeError, OSError):
        events = []
    return classify(pod, events)
