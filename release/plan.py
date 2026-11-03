"""升级计划:识别集群当前版本,按「当前 → 目标」累计各版本的变化,生成这次要走的步骤。

当前版本看三处:gateway-config 的 USER_IMAGE_TAG、网关 Deployment 的镜像、池子每个容器的 spec 镜像。
三处一致 = 正常;不一致 = 上次发布没走完(只能接着发同一个目标,或回滚);网关不存在 = 首次部署。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from .package import Package, vkey

TAG = re.compile(r":(agent-v\d+\.\d+\.\d+-oc[\d.]+)(?:@sha256:[0-9a-f]{64})?$")    # 发版台按 digest 钉住的写法也认

STEPS = [  # id, 名字, 子步骤
    ("pkg", "读包与校验", ["发现发布包", "校验 sha256", "读取 release.yaml", "核对镜像 digest"]),
    ("plan", "生成升级计划", ["识别当前版本", "累计各版本的变化", "生成本次计划"]),
    ("images", "镜像入库", ["核对仓库", "上传缺的层", "推清单并回读 digest"]),
    ("config", "配置迁移", ["读取集群现状", "迁移配置", "检查必填项", "渲染清单", "生成差异"]),
    ("review", "变更预览 · 确认", ["生成差异", "等待运维确认", "已确认"]),
    ("whitelist", "白名单核对", ["生成应有清单", "读取已登记脚本", "逐条比对", "核对结果"]),
    ("gateway", "网关滚动更新", ["应用配置与清单", "滚动更新网关", "等待就绪"]),
    ("pool", "池子在线替换", ["更新池子模板", "逐个替换", "换管家"]),
    ("kbimport", "导入环境", ["删除旧的导入容器"]),
    ("kbstd", "知识库标准包", ["创建 Job", "等待完成", "核对结果"]),
    ("smoke", "冒烟验证", ["模拟登录", "真实对话", "诊断报告", "知识库检索"]),
    ("done", "完成", ["记录版本", "生成发布报告"]),
]


@dataclass(frozen=True)
class Current:
    state: str                      # none / ok / partial
    tag: str = ""                   # gateway-config 的 USER_IMAGE_TAG(上次发布的目标)
    registry: str = ""
    tags: Dict[str, str] = field(default_factory=dict)    # 位置 → 标签(例如 deploy/gateway、agent-pool-0/slot-1)

    @property
    def version(self) -> str:
        return self.tag.split("-oc")[0] if self.tag else ""

    def lagging(self) -> List[str]:
        return sorted(k for k, v in self.tags.items() if v != self.tag)


@dataclass
class Plan:
    kind: str                       # deploy / upgrade / rollback / resume
    from_v: str
    to_v: str
    to_tag: str
    pool_mode: str                  # slot(按槽位在线)/ pod(按 Pod 整体)/ fresh(首次部署)
    steps: List[dict] = field(default_factory=list)
    required: List[dict] = field(default_factory=list)
    special: List[str] = field(default_factory=list)
    whitelist_files: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _image_tag(image: str) -> str:
    m = TAG.search(image or "")
    return m.group(1) if m else ""


def _registry(image: str) -> str:
    name = (image or "").rsplit(":", 1)[0]
    return name.rsplit("/", 1)[0] if "/" in name else ""


def detect(kube) -> Current:
    gw = kube.get("deploy", "gateway")
    if gw is None:
        return Current(state="none")
    cm = kube.get("configmap", "gateway-config") or {}
    tag = str((cm.get("data") or {}).get("USER_IMAGE_TAG") or "")
    image = gw["spec"]["template"]["spec"]["containers"][0]["image"]
    tags = {"deploy/gateway": _image_tag(image)}
    for pod in kube.list("pods", "app=gaussdb-agent,role=pool"):
        for c in pod["spec"]["containers"]:
            tags["%s/%s" % (pod["metadata"]["name"], c["name"])] = _image_tag(c["image"])
    tag = tag or tags["deploy/gateway"]
    state = "ok" if all(v == tag for v in tags.values()) else "partial"
    return Current(state=state, tag=tag, registry=_registry(image), tags=tags)


def _span(history: Dict[str, dict], lo: str, hi: str) -> List[dict]:
    """lo < 版本 ≤ hi 的历史条目,旧的在前。"""
    return sorted((h for v, h in history.items() if vkey(lo) < vkey(v) <= vkey(hi)), key=lambda h: vkey(h["version"]))


def build(current: Current, target: Package, history: Dict[str, dict]) -> Plan:
    to_v, to_tag = target.version, target.tag
    if current.state == "none":
        kind, from_v = "deploy", ""
    elif current.state == "partial" and current.tag == to_tag:
        kind, from_v = "resume", min((v for v in current.tags.values() if v), key=vkey, default="").split("-oc")[0]
    elif vkey(to_v) < vkey(current.version):
        kind, from_v = "rollback", current.version
    else:
        kind, from_v = "upgrade", current.version
    p = Plan(kind=kind, from_v=from_v, to_v=to_v, to_tag=to_tag, pool_mode="fresh" if kind == "deploy" else "pod")
    if not target.ok:
        p.problems += ["目标包有问题:%s" % x for x in target.problems]
    if current.state == "partial" and kind != "resume" and kind != "rollback":
        p.problems.append("上次发布(目标 %s)没有走完:还没换的有 %s。只能接着发同一个版本,或回滚"
                          % (current.tag, "、".join(current.lagging()[:6])))
    if kind == "upgrade" and from_v == to_v:
        p.problems.append("集群已经是 %s" % to_v)
    if kind in ("upgrade", "deploy", "resume") and target.legacy:
        p.problems.append("%s 的包没有 release.yaml(v1.4.3 及更早的包),只能作为回滚目标" % to_v)

    if kind in ("upgrade", "resume"):
        minimum = (target.release or {}).get("min_upgrade_from", "")
        if minimum and from_v and vkey(from_v) < vkey(minimum):
            p.problems.append("从 %s 升级不在自动支持范围(最早 %s):请按《部署上线方案》§14 手工升级到 %s 再用发版台"
                              % (from_v, minimum, minimum))
        span = _span(history, from_v, to_v)
        if to_v not in history:
            p.problems.append("目标版本 %s 没有变化说明(release.yaml 的 history 里没有它)" % to_v)
        for h in span:
            p.required += [r for r in h.get("required") or [] if r not in p.required]
            p.special += [s for s in h.get("steps") or [] if s not in p.special]
            p.whitelist_files += h.get("whitelist") or []
            p.notes += ["%s:%s" % (h["version"], n) for n in h.get("notes") or []]
        top = history.get(to_v) or {}
        p.pool_mode = "slot" if from_v in (top.get("mixed_with") or []) else "pod"
    elif kind == "rollback":
        p.pool_mode = "slot" if to_v in ((history.get(from_v) or {}).get("mixed_with") or []) else "pod"
    elif kind == "deploy":
        for h in history.values():
            p.required += [r for r in h.get("required") or [] if r not in p.required]
    p.steps = _steps(p)
    return p


WHY = {
    "pkg": "每次都做", "plan": "每次都做", "images": "把目标版本的镜像推进内网仓库(已有的层跳过)",
    "config": "按「集群现值 + 新版本新增项 + 资源配置」迁移,渲染清单", "review": "改动集群前必须由运维确认",
    "whitelist": "白名单由白名单团队手工执行,这里按内容逐条核对", "gateway": "网关与用户镜像标签换到目标版本",
    "smoke": "每次都做", "done": "每次都做",
}


def _steps(p: Plan) -> List[dict]:
    out = []
    for sid, name, subs in STEPS:
        skip, why = False, WHY.get(sid, "")
        if sid == "gateway" and p.kind == "deploy":
            name, subs, why = "部署基础资源", ["应用配置与密钥", "网络策略与权限", "网关与前端", "等待就绪"], "首次部署"
        if sid == "pool":
            if p.kind == "deploy":
                name, subs, why = "部署池子", ["渲染池子清单", "应用", "等待槽位空闲"], "首次部署"
            elif p.pool_mode == "slot":
                why = "目标版本声明可与 %s 混跑:按槽位在线替换,不停服务" % (p.from_v if p.kind != "rollback" else p.to_v)
            else:
                name, subs = "池子按 Pod 替换", ["更新池子模板", "逐个 Pod:等不忙 → 转保留 → 重建", "核对"]
                why = "目标版本没有声明可与 %s 混跑:逐个 Pod 整体替换(其余 Pod 照常服务)" % p.from_v
        if sid == "kbimport":
            skip = "rebuild_kb_import" not in p.special or p.kind in ("deploy", "rollback")
            why = "版本要求重建导入容器(NAS 上的数据不动)" if not skip else "本次不需要"
        if sid == "whitelist" and p.kind == "rollback":
            skip, why = True, "白名单只增不删,回滚不用动"
        if sid == "kbstd":
            skip = p.kind == "rollback"
            why = "回滚不动知识库标准包" if skip else "每个版本都带安装 Job(内容相同会跳过)"
        out.append({"id": sid, "name": name, "subs": subs, "skip": skip, "why": why})
    return out
