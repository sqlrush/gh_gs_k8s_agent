"""配置迁移与渲染:从目标版本包里的 k8s/base 生成要 apply 的清单。纯函数,集群现状由调用方读好传进来。

**不能直接 `kubectl apply -k k8s/base`**:它把三个 ConfigMap 重置为清单值(连 agent-roles 的知识库管理员名单一起清空)、
把网络策略的网段改回占位值。这里的规矩:
  - ConfigMap:集群现值保留;新版本新增的键取新清单默认值;资源配置里填了的覆盖;镜像标签与仓库前缀由发版台管。
    agent-roles 整份沿用集群的。集群里有、清单里没有的键(例如 runtime-doctor --proxy 加的 HTTPS_PROXY)保留。
  - 网络策略:集群里已有的策略,带 ipBlock 的外部规则(中间件、模型、apiserver、上游入口,以及客户加的其他出口)整条沿用,
    产品内部规则(按标签选 Pod 的)取新清单;资源配置里明确填了网段的,覆盖对应的那一类。
  - 命名空间、PVC 只在首次部署时建;Secret 不在这里(见 steps)。
  - 池子:升级时只改 StatefulSet 模板里的镜像(池子规模、资源限额都保留),首次部署才调 render-pool.py。
"""
from __future__ import annotations

import copy
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

IMAGE = re.compile(r"^(?:.*/)?(gaussdb-agent-[a-z-]+):[\w.-]+$")
PLACEHOLDER = "10.0.0.0/8"
MANAGED = {("gateway-config", "USER_IMAGE_TAG"), ("gateway-config", "USER_IMAGE_REGISTRY")}
SENSITIVE = {"GRMP_PATH_PREFIX"}            # 中间件路径前缀:界面与日志只显示「已设置」
KEEP_WHOLE = {"agent-roles"}
NAMESPACED_REFS = (("RoleBinding", "subjects"),)

# 资源配置字段 → (ConfigMap, 键)。只在资源配置里填了非空值时覆盖集群现值。
CONFIG_KEYS: Dict[str, Tuple[str, str]] = {
    "model.base_url": ("agent-config", "MODEL_BASE_URL"),
    "model.id": ("agent-config", "MODEL_ID"),
    "model.provider": ("agent-config", "MODEL_PROVIDER_NAME"),
    "model.modalities": ("agent-config", "MODEL_INPUT_MODALITIES"),
    "grmp.host": ("agent-config", "GRMP_API_HOST"),
    "grmp.port": ("agent-config", "GRMP_API_PORT"),
    "grmp.path_prefix": ("agent-config", "GRMP_PATH_PREFIX"),
    "grmp.appkey": ("agent-config", "GRMP_APPKEY"),
    "grmp.sign_mode": ("agent-config", "GRMP_SIGN_MODE"),
    "kb.clip_origins": ("agent-config", "KB_CLIP_ORIGINS"),
    "grmp.instance_aliases": ("agent-config", "GRMP_INSTANCE_ALIASES"),
    "sso.user_header": ("gateway-config", "USER_HEADER"),
    "sso.roles_header": ("gateway-config", "ROLES_HEADER"),
    "gateway.runtime_mode": ("gateway-config", "RUNTIME_MODE"),
    "gateway.pull_policy": ("gateway-config", "PULL_POLICY"),
}
# 网络策略里客户要填的四类外部规则:配置字段 → (策略名, 方向, 认法)
NET_FIELDS = {
    "net.grmp": ("runtime-policy", "egress", "grmp"),
    "net.model": ("runtime-policy", "egress", "model"),
    "net.model_kb": ("kb-import-policy", "egress", "model"),
    "net.apiserver": ("gateway-policy", "egress", "apiserver"),
    "net.upstream": ("gateway-policy", "ingress", "upstream"),
}


class RenderError(ValueError):
    pass


@dataclass
class Change:
    obj: str
    key: str
    old: Optional[str]
    new: Optional[str]
    why: str

    def show(self) -> dict:
        mask = self.key in SENSITIVE
        return {"obj": self.obj, "key": self.key, "why": self.why,
                "old": ("已设置" if self.old else "未设置") if mask else self.old,
                "new": ("已设置" if self.new else "未设置") if mask else self.new}


@dataclass
class Rendered:
    docs: List[dict] = field(default_factory=list)
    changes: List[Change] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def yaml(self) -> str:
        return yaml.safe_dump_all(self.docs, allow_unicode=True, sort_keys=False)


def ref(doc: dict) -> str:
    return "%s/%s" % (doc.get("kind"), (doc.get("metadata") or {}).get("name"))


# -- 读清单 --------------------------------------------------------------------------------------

def load_base(k8s_root: pathlib.Path) -> List[dict]:
    """k8s/base:kustomization 只有 resources(没有补丁、生成器)—— 出现别的写法说明清单的组织变了,直接报错让人看。"""
    base = k8s_root / "base"
    kz = yaml.safe_load((base / "kustomization.yaml").read_text(encoding="utf-8")) or {}
    extra = set(kz) - {"apiVersion", "kind", "resources"}
    if extra:
        raise RenderError("k8s/base/kustomization.yaml 用到了发版台不认识的写法:%s" % sorted(extra))
    docs: List[dict] = []
    for res in kz.get("resources") or []:
        for d in yaml.safe_load_all((base / res).read_text(encoding="utf-8")):
            if d:
                docs.append(d)
    return docs


# -- 各项变换 --------------------------------------------------------------------------------------

def set_namespace(docs: List[dict], ns: str) -> None:
    for d in docs:
        md = d.setdefault("metadata", {})
        if d.get("kind") == "Namespace":
            md["name"] = ns
            continue
        md["namespace"] = ns
        if d.get("kind") == "RoleBinding":
            for s in d.get("subjects") or []:
                if s.get("kind") == "ServiceAccount":
                    s["namespace"] = ns
        for c in _containers(d):
            for e in c.get("env") or []:
                if e.get("name") == "GATEWAY_NAMESPACE" and "value" in e:
                    e["value"] = ns


def _containers(doc: dict) -> List[dict]:
    spec = doc.get("spec") or {}
    tpl = (spec.get("jobTemplate") or {}).get("spec", spec)
    pod = ((tpl.get("template") or {}).get("spec")) or {}
    return list(pod.get("initContainers") or []) + list(pod.get("containers") or [])


def image_ref(registry: str, repo: str, tag: str, digest: str = "") -> str:
    """<仓库前缀>/<镜像>:<标签>[@<digest>]。带 digest 时节点按 digest 拉:同一标签换过内容(重新打包、演练包),
    节点上缓存的旧镜像不会被 IfNotPresent 当成「已有」接着用。"""
    base = "%s/%s:%s" % (registry, repo, tag) if registry else "%s:%s" % (repo, tag)
    return base + ("@" + digest if digest else "")


def retarget(docs: List[dict], registry: str, tag: str, digests: Optional[Dict[str, str]] = None) -> List[str]:
    """所有 gaussdb-agent-* 镜像:加内网仓库前缀、换成目标标签(给了 digests 就按 digest 钉住)。返回改过的「对象/容器」。"""
    touched = []
    for d in docs:
        for c in _containers(d):
            m = IMAGE.match(str(c.get("image") or ""))
            if m:
                c["image"] = image_ref(registry, m.group(1), tag, (digests or {}).get(m.group(1), ""))
                touched.append("%s/%s" % (ref(d), c.get("name")))
    return touched


def migrate_configmaps(docs: List[dict], cluster: Dict[str, Dict[str, str]], cfg: Dict[str, Any],
                       registry: str, tag: str) -> List[Change]:
    """cluster: ConfigMap 名 → 集群里现在的 data(不存在的不出现)。就地改 docs 里的 ConfigMap,返回逐键变化。"""
    changes: List[Change] = []
    overrides: Dict[Tuple[str, str], str] = {v: str(cfg[k]) for k, v in CONFIG_KEYS.items()
                                             if cfg.get(k) not in (None, "")}
    overrides[("gateway-config", "USER_IMAGE_TAG")] = tag
    overrides[("gateway-config", "USER_IMAGE_REGISTRY")] = registry
    for d in docs:
        if d.get("kind") != "ConfigMap":
            continue
        name = d["metadata"]["name"]
        manifest = {k: str(v) for k, v in (d.get("data") or {}).items()}
        have = cluster.get(name)
        if name in KEEP_WHOLE and have is not None:
            d["data"] = dict(have)
            continue
        out = dict(have) if have is not None else {}
        for k, v in manifest.items():
            if have is None or k not in have:
                out[k] = v
                if have is not None:
                    changes.append(Change("ConfigMap/" + name, k, None, v, "新版本新增(默认值)"))
        for (cm, k), v in overrides.items():
            if cm != name or out.get(k) == v:
                continue
            why = "发版台管理" if (cm, k) in MANAGED else "资源配置"
            changes.append(Change("ConfigMap/" + name, k, out.get(k), v, why))
            out[k] = v
        d["data"] = out
    return changes


def required_missing(docs: List[dict], required: Sequence[dict]) -> List[str]:
    data = {d["metadata"]["name"]: d.get("data") or {} for d in docs if d.get("kind") == "ConfigMap"}
    return ["%s 的 %s" % (r["map"], r["key"]) for r in required
            if not str((data.get(r["map"]) or {}).get(r["key"]) or "").strip()]


def _peers(rule: dict, direction: str) -> List[dict]:
    return list(rule.get("to" if direction == "egress" else "from") or [])


def _external(rule: dict, direction: str) -> bool:
    return any("ipBlock" in p for p in _peers(rule, direction))


def classify(rule: dict, direction: str, policy: str, grmp_port: str) -> str:
    """外部规则是哪一类:upstream(网关入站)/ apiserver(网关出站)/ model(443、80)/ grmp(中间件端口)。
    认不出的(客户自己加的出口,例如测试环境访问宿主 Ollama 的 11434)返回空 —— 原样保留,资源配置也不覆盖它。"""
    if not _external(rule, direction):
        return ""
    if direction == "ingress":
        return "upstream"
    if policy == "gateway-policy":
        return "apiserver"
    ports = {str(p.get("port")) for p in rule.get("ports") or []}
    if ports & {"443", "80"}:
        return "model"
    if grmp_port and grmp_port in ports:
        return "grmp"
    return ""


def _net_rule(spec: Dict[str, Any], direction: str) -> dict:
    peers = [{"ipBlock": {"cidr": c}} for c in spec.get("cidrs") or []]
    rule = {"to" if direction == "egress" else "from": peers}
    if spec.get("ports"):
        rule["ports"] = [{"port": int(p), "protocol": "TCP"} for p in spec["ports"]]
    return rule


def merge_netpols(docs: List[dict], cluster: Dict[str, dict], cfg: Dict[str, Any], fresh: bool) -> List[str]:
    """cluster: 策略名 → 集群里现在的 spec。就地改 docs,返回还停在占位网段的问题。"""
    problems = []
    agent = next((d.get("data") or {} for d in docs if d.get("kind") == "ConfigMap"
                  and d["metadata"]["name"] == "agent-config"), {})
    grmp_port = str(agent.get("GRMP_API_PORT") or "8080")
    for d in docs:
        if d.get("kind") != "NetworkPolicy":
            continue
        name, spec = d["metadata"]["name"], d.setdefault("spec", {})
        have = cluster.get(name)
        for direction in ("ingress", "egress"):
            if direction not in spec:
                continue
            new_rules = [r for r in spec.get(direction) or [] if not _external(r, direction)]
            ext = ([r for r in (have or {}).get(direction) or [] if _external(r, direction)] if have is not None
                   else [r for r in spec.get(direction) or [] if _external(r, direction)])
            for fld, (pol, dirn, kind) in NET_FIELDS.items():
                val = cfg.get(fld)
                if pol == name and dirn == direction and isinstance(val, dict) and val.get("cidrs"):
                    ext = [r for r in ext if classify(r, direction, name, grmp_port) != kind] + [_net_rule(val, direction)]
            spec[direction] = new_rules + ext
            if fresh or have is None:
                for r in ext:
                    kind = classify(r, direction, name, grmp_port)
                    if kind in ("grmp", "model") and any(p.get("ipBlock", {}).get("cidr") == PLACEHOLDER
                                                         for p in _peers(r, direction)):
                        problems.append("网络策略 %s 的%s网段还是占位值 %s:在资源配置里填实际地址段"
                                        % (name, {"grmp": "中间件", "model": "模型服务"}[kind], PLACEHOLDER))
    return problems


# -- 池子 ------------------------------------------------------------------------------------------

def render_pool(k8s_root: pathlib.Path, cfg: Dict[str, Any], image: str, namespace: str,
                python: str = sys.executable) -> List[dict]:
    """首次部署:调包里带的 render-pool.py。参数取自资源配置(池子规模、出口网段)。"""
    args = [python, str(k8s_root / "tools" / "render-pool.py"), "--image", image, "--namespace", namespace,
            "--pool-size", str(cfg.get("pool.pods") or 2), "--slots-per-pod", str(cfg.get("pool.slots") or 8)]
    grmp, model = cfg.get("net.grmp") or {}, cfg.get("net.model") or {}
    if grmp.get("cidrs"):
        args += ["--grmp-cidr", grmp["cidrs"][0], "--grmp-port", str((grmp.get("ports") or [8080])[0])]
    if model.get("cidrs"):
        args += ["--model-cidr", model["cidrs"][0], "--model-ports", ",".join(str(p) for p in model.get("ports") or [443])]
    if cfg.get("gateway.pull_policy"):
        args += ["--image-pull-policy", str(cfg["gateway.pull_policy"])]
    p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RenderError("render-pool.py 失败:%s" % (p.stderr.strip().splitlines() or ["?"])[-1])
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def pool_image_patch(sts: dict, image: str) -> Dict[str, Any]:
    """升级时只改 StatefulSet 模板里每个容器的镜像(strategic merge,按容器名)。"""
    names = [c["name"] for c in sts["spec"]["template"]["spec"]["containers"]]
    return {"spec": {"template": {"spec": {"containers": [{"name": n, "image": image} for n in names]}}}}


# -- 合在一起 ----------------------------------------------------------------------------------------

def render(k8s_root: pathlib.Path, cfg: Dict[str, Any], tag: str, cluster_cms: Dict[str, Dict[str, str]],
           cluster_netpols: Dict[str, dict], existing: Sequence[str], required: Sequence[dict],
           digests: Optional[Dict[str, str]] = None) -> Rendered:
    """existing:集群里已有的对象(「Kind/name」),用来跳过命名空间与 PVC。digests:镜像 → 平台清单 digest(钉住用)。"""
    ns = cfg.get("k8s.namespace") or "gaussdb-agent"
    registry = str(cfg.get("registry.prefix") or "").rstrip("/")
    fresh = "Deployment/gateway" not in existing
    out = Rendered(docs=copy.deepcopy(load_base(k8s_root)))
    keep = []
    for d in out.docs:
        r = ref(d)
        if d.get("kind") in ("Namespace", "PersistentVolumeClaim") and (r in existing or d.get("kind") == "Namespace"
                                                                        and f"Namespace/{ns}" in existing):
            out.skipped.append("%s(已存在,不动)" % r)
            continue
        keep.append(d)
    out.docs = keep
    set_namespace(out.docs, ns)
    retarget(out.docs, registry, tag, digests)
    out.changes = migrate_configmaps(out.docs, cluster_cms, cfg, registry, tag)
    out.problems += ["必填配置没填:%s" % m for m in required_missing(out.docs, required)]
    out.problems += merge_netpols(out.docs, cluster_netpols, cfg, fresh)
    return out
