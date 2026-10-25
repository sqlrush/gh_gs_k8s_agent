#!/usr/bin/env python3
"""渲染固定池化的清单(2026-10-23):StatefulSet + headless Service + PDB + 两条 NetworkPolicy。

    python3 k8s/tools/render-pool.py --image <镜像> [--pool-size 16] [--slots-per-pod 8] > pool.yaml
    kubectl apply -f pool.yaml

为什么要渲染而不是 kustomize:每个 Pod 里的容器数 = 槽位数,每个槽位各有自己的 uid、端口、5 块盘与挂载,
kustomize 生成不了「按参数重复的容器」。只用标准库(平台的机器上不一定有 PyYAML),输出确定、可 diff。

**一个 Pod 的样子**:slot-0 … slot-N(uid 2000+i,同组 1000;只挂自己的盘与只读知识库,看不到 users/)
+ steward(uid 1000;挂 NAS users/ 与全部槽位的状态盘、指令目录;只有它有控制密钥)。

固定的几条(清单守卫逐条核对,见 gateway/tests/test_pool_manifest_units.py):
  · 不共享进程空间;所有容器只读根、禁止提权、去掉全部特权、seccomp RuntimeDefault;不挂 ServiceAccount 令牌;
  · updateStrategy OnDelete(改清单不会自动滚掉正在用的 Pod)、podManagementPolicy Parallel、PDB 最多 1 个不可用;
  · headless Service 带 publishNotReadyAddresses(某个槽位重启时整个 Pod 会 NotReady,P0 摸底 S2);网关直连 Pod IP;
  · 槽位里没有任何凭据(中间件令牌、SM2 私钥、模型 Key、控制密钥):凭据在管家的凭据代理里,每个槽位一个套接字。

--test-override KEY=VALUE 只给本地测试集群缩短时长,渲染结果会带标签 gaussdb-agent/test-overrides,
守卫测试保证正式清单(不带这个参数)里没有它。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Dict, List

PORT_BASE, PORT_STEP = 4100, 10           # 与 docker/slotrun.slot_ports 一致(有测试核对)
STEWARD_PORT = 4098
SLOT_UID_BASE = 2000                      # 镜像里有 slot0–slot15 的 passwd 条目
MAX_SLOTS = 16
APP = "gaussdb-agent"
ROLE = "pool"
NAME = "agent-pool"
TEST_LABEL = "gaussdb-agent/test-overrides"
_QTY = re.compile(r"^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti)?$")
_KEY = re.compile(r"^[A-Za-z0-9_./-]+$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
LOCKED = {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
          "runAsNonRoot": True}


def slot_ports(i: int):
    return PORT_BASE + PORT_STEP * i, PORT_BASE + PORT_STEP * i + 1


# --- 极简 YAML 输出(块风格;字符串一律 JSON 双引号,双引号字符串是合法 YAML) ---------------------------

def _scalar(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def _key(k: str) -> str:
    return k if _KEY.match(k) and k not in ("true", "false", "null", "yes", "no", "on", "off") else json.dumps(k)


def _emit(obj, indent: int = 0) -> List[str]:
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return [pad + "{}"]
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append("%s%s:" % (pad, _key(k)))
                out += _emit(v, indent + 1)
            else:
                out.append("%s%s: %s" % (pad, _key(k), "{}" if v == {} else "[]" if v == [] else _scalar(v)))
        return out
    if isinstance(obj, list):
        if not obj:
            return [pad + "[]"]
        out = []
        for item in obj:
            sub = _emit(item, indent + 1)
            out.append("%s- %s" % (pad, sub[0].lstrip()))
            out += sub[1:]
        return out
    return [pad + _scalar(obj)]


def to_yaml(docs: List[dict]) -> str:
    return "---\n".join("\n".join(_emit(d)) + "\n" for d in docs)


# --- 清单 ---------------------------------------------------------------------------------------------

def _secret_env(name: str, secret: str, key: str, optional: bool = False) -> dict:
    ref = {"name": secret, "key": key}
    if optional:
        ref["optional"] = True
    return {"name": name, "valueFrom": {"secretKeyRef": ref}}


def _slot(i: int, a) -> dict:
    oc, rep = slot_ports(i)
    return {
        "name": "slot-%d" % i,
        "image": a.image,
        "imagePullPolicy": a.image_pull_policy,
        "securityContext": dict(LOCKED, runAsUser=SLOT_UID_BASE + i, runAsGroup=1000),
        "ports": [{"containerPort": oc, "name": "oc-%d" % i}, {"containerPort": rep, "name": "rep-%d" % i}],
        # 槽位里**没有任何凭据**:中间件令牌、SM2 私钥、模型 Key 都在管家的凭据代理里(docker/credproxy.py)
        "env": [
            {"name": "POOL_MODE", "value": "slot"},      # entrypoint 第一条命令就 exec 槽位守护进程(先清盘)
            {"name": "SLOT_INDEX", "value": str(i)},
        ],
        "envFrom": [{"configMapRef": {"name": "agent-config"}}],
        "volumeMounts": [
            {"name": "nas", "mountPath": "/nas/kb", "subPath": "kb", "readOnly": True},   # 只有共享知识库
            {"name": "s%d-data" % i, "mountPath": "/data"},
            {"name": "s%d-state" % i, "mountPath": "/data/state"},
            {"name": "s%d-ctl" % i, "mountPath": "/run/sandbox", "readOnly": True},     # 指令目录:模型改不了
            {"name": "s%d-tmp" % i, "mountPath": "/tmp"},
            {"name": "s%d-shm" % i, "mountPath": "/dev/shm"},
            {"name": "s%d-sock" % i, "mountPath": "/run/credproxy", "readOnly": True},    # 管家的凭据代理套接字
        ],
        # 不配探针:opencode 只在在用时跑,存活由槽位守护进程查;管家监视回执与监听者
        "resources": {"requests": {"cpu": a.slot_cpu_request, "memory": a.slot_memory_request},
                      "limits": {"cpu": a.slot_cpu_limit, "memory": a.slot_memory_limit}},
    }


def _steward(a) -> dict:
    env = [
        {"name": "POOL_MODE", "value": "steward"},
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},   # 独占标记的持有者
        {"name": "SLOTS_PER_POD", "value": str(a.slots_per_pod)},
        _secret_env("SANDBOX_CONTROL_KEY", "sandbox-control", "KEY"),                     # 只在这里
        # 凭据代理:模型 Key 与 SM2 私钥只在管家;中间件令牌按人,绑定时由网关加密交来
        _secret_env("MODEL_API_KEY", "model-api", "MODEL_API_KEY"),
        _secret_env("GRMP_SM2_PRIVATE_KEY", "grmp-sm2", "GRMP_SM2_PRIVATE_KEY", optional=True),
    ]
    env += [{"name": k, "value": v} for k, v in sorted(a.overrides.items())]
    mounts = [{"name": "nas", "mountPath": "/pool/users", "subPath": a.users_subpath},
              {"name": "priv", "mountPath": "/pool/priv"},
              {"name": "steward-tmp", "mountPath": "/tmp"}]
    for i in range(a.slots_per_pod):
        mounts += [{"name": "s%d-state" % i, "mountPath": "/pool/slots/%d/state" % i},
                   {"name": "s%d-ctl" % i, "mountPath": "/pool/slots/%d/ctl" % i},
                   {"name": "s%d-sock" % i, "mountPath": "/pool/slots/%d/sock" % i}]
    probe = {"httpGet": {"path": "/healthz", "port": STEWARD_PORT}}
    return {
        "name": "steward",
        "image": a.image,
        "imagePullPolicy": a.image_pull_policy,
        "securityContext": dict(LOCKED, runAsUser=1000, runAsGroup=1000),
        "ports": [{"containerPort": STEWARD_PORT, "name": "control"}],
        "env": env,
        "envFrom": [{"configMapRef": {"name": "agent-config"}}],     # 模型地址、中间件地址与签名 / 工号旋钮
        "volumeMounts": mounts,
        "readinessProbe": dict(probe, periodSeconds=2),
        "livenessProbe": dict(probe, initialDelaySeconds=30, periodSeconds=20, failureThreshold=3),
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                      "limits": {"cpu": "1", "memory": a.steward_memory_limit}},
    }


def _volumes(a) -> List[dict]:
    vols = [{"name": "nas", "persistentVolumeClaim": {"claimName": a.nas_claim}},
            {"name": "priv", "emptyDir": {"sizeLimit": "1Mi"}},
            {"name": "steward-tmp", "emptyDir": {"sizeLimit": "64Mi"}}]
    for i in range(a.slots_per_pod):
        # 每块盘独立的 emptyDir(不用 subPath:subPath 的目录由 kubelet 以 root 建,且清盘时删不掉)
        vols += [{"name": "s%d-data" % i, "emptyDir": {"sizeLimit": "1Gi"}},
                 {"name": "s%d-state" % i, "emptyDir": {"sizeLimit": a.slot_state_size}},
                 {"name": "s%d-ctl" % i, "emptyDir": {"sizeLimit": "1Mi"}},
                 {"name": "s%d-tmp" % i, "emptyDir": {"sizeLimit": "1Gi"}},
                 {"name": "s%d-shm" % i, "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
                 {"name": "s%d-sock" % i, "emptyDir": {"sizeLimit": "1Mi"}}]
    return vols


def _labels(a) -> Dict[str, str]:
    out = {"app": APP, "role": ROLE}
    if a.overrides:
        out[TEST_LABEL] = "true"
    return out


def render(a) -> List[dict]:
    sel = {"app": APP, "role": ROLE}
    spec = {
        # 删 Pod 时管家让所有在用的槽位并行转保留(停 opencode、最终快照、完整写回);不要 --force --grace-period=0
        "terminationGracePeriodSeconds": a.grace_seconds,
        "enableServiceLinks": False,
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [_slot(i, a) for i in range(a.slots_per_pod)] + [_steward(a)],
        "volumes": _volumes(a),
    }
    if a.runtime_class:
        spec["runtimeClassName"] = a.runtime_class
    meta = {"name": NAME, "namespace": a.namespace, "labels": _labels(a)}
    sts = {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": meta,
           "spec": {"replicas": a.pool_size, "serviceName": NAME, "podManagementPolicy": "Parallel",
                    "updateStrategy": {"type": "OnDelete"}, "selector": {"matchLabels": sel},
                    "template": {"metadata": {"labels": _labels(a)}, "spec": spec}}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": dict(meta),
           "spec": {"clusterIP": "None", "publishNotReadyAddresses": True, "selector": sel,
                    "ports": [{"name": "control", "port": STEWARD_PORT, "targetPort": STEWARD_PORT}]}}
    pdb = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": dict(meta),
           "spec": {"maxUnavailable": 1, "selector": {"matchLabels": sel}}}
    return [svc, sts, pdb] + policies(a)


def _pool_ports(a) -> List[dict]:
    ports = [{"port": STEWARD_PORT, "protocol": "TCP"}]
    for i in range(a.slots_per_pod):
        ports += [{"port": p, "protocol": "TCP"} for p in slot_ports(i)]
    return ports


def policies(a) -> List[dict]:
    gw = [{"podSelector": {"matchLabels": {"app": "agent-gateway"}}},
          {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "agent-gateway"}}}]
    dns = {"to": [{"namespaceSelector": {}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
           "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]}
    pool = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "pool-policy", "namespace": a.namespace},
            "spec": {"podSelector": {"matchLabels": {"app": APP, "role": ROLE}}, "policyTypes": ["Ingress", "Egress"],
                     # 入站只放行网关;Pod 之间互相到不了。不放行 NFS(NAS 由节点上的 kubelet 挂,不走 Pod 网络)
                     "ingress": [{"from": gw, "ports": _pool_ports(a)}],
                     "egress": [dns,
                                {"to": [{"ipBlock": {"cidr": a.grmp_cidr}}],
                                 "ports": [{"port": a.grmp_port, "protocol": "TCP"}]},
                                {"to": [{"ipBlock": {"cidr": a.model_cidr}}],
                                 "ports": [{"port": p, "protocol": "TCP"} for p in a.model_ports]}]
                     + [{"to": [{"ipBlock": {"cidr": cidr}}], "ports": [{"port": p, "protocol": "TCP"} for p in ports]}
                        for cidr, ports in a.egress]}}
    to_pool = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
               "metadata": {"name": "gateway-to-pool", "namespace": a.namespace},
               "spec": {"podSelector": {"matchLabels": {"app": "agent-gateway"}}, "policyTypes": ["Egress"],
                        "egress": [{"to": [{"podSelector": {"matchLabels": {"app": APP, "role": ROLE}}}],
                                    "ports": _pool_ports(a)}]}}
    return [pool, to_pool]


# --- 参数 -----------------------------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="渲染固定池化清单(输出到标准输出)")
    p.add_argument("--image", required=True)
    p.add_argument("--image-pull-policy", default="IfNotPresent", choices=["IfNotPresent", "Always", "Never"])
    p.add_argument("--namespace", default="gaussdb-agent")
    p.add_argument("--pool-size", type=int, default=16, help="Pod 数,部署时全部启动,运行中固定")
    p.add_argument("--slots-per-pod", type=int, default=8, help="每个 Pod 的槽位数(最多 %d)" % MAX_SLOTS)
    p.add_argument("--users-subpath", default="users", help="NAS 上用户目录的上一层(与按需模式的 SUBPATH_ROOT 一致)")
    p.add_argument("--nas-claim", default="nas")
    p.add_argument("--slot-cpu-request", default="100m")
    p.add_argument("--slot-memory-request", default="768Mi", help="实测单人 0.7–0.9 GiB")
    p.add_argument("--slot-cpu-limit", default="2")
    p.add_argument("--slot-memory-limit", default="2Gi")
    p.add_argument("--slot-state-size", default="4Gi", help="每人在槽位里的文件上限(对话库、工作目录、报告)")
    p.add_argument("--steward-memory-limit", default="512Mi")
    p.add_argument("--grace-seconds", type=int, default=180)
    p.add_argument("--runtime-class", default="", help="集群有 gVisor / Kata 时填 runtimeClassName")
    p.add_argument("--grmp-cidr", default="10.0.0.0/8")
    p.add_argument("--grmp-port", type=int, default=8080)
    p.add_argument("--model-cidr", default="10.0.0.0/8")
    p.add_argument("--model-ports", default="443,80")
    p.add_argument("--egress", action="append", default=[], metavar="CIDR:PORT[,PORT]",
                   help="额外的出站目标(可重复),如 embedding 服务 10.1.2.3/32:11434")
    p.add_argument("--test-override", action="append", default=[], metavar="KEY=VALUE",
                   help="只给本地测试集群:管家的环境变量(缩短时长);渲染结果带测试标签")
    return p


def parse(argv: List[str]):
    p = _parser()
    a = p.parse_args(argv)
    if not 1 <= a.pool_size <= 64:
        p.error("--pool-size 要在 1–64 之间")
    if not 1 <= a.slots_per_pod <= MAX_SLOTS:
        p.error("--slots-per-pod 要在 1–%d 之间(镜像里只有 slot0–slot%d 的用户)" % (MAX_SLOTS, MAX_SLOTS - 1))
    if not a.image.strip() or any(c.isspace() for c in a.image):
        p.error("--image 不能为空或含空白")
    for name in ("slot_cpu_request", "slot_memory_request", "slot_cpu_limit", "slot_memory_limit",
                 "slot_state_size", "steward_memory_limit"):
        if not _QTY.match(getattr(a, name)):
            p.error("--%s=%r 不是合法的资源量" % (name.replace("_", "-"), getattr(a, name)))
    try:
        a.model_ports = [int(x) for x in a.model_ports.split(",") if x.strip()]
    except ValueError:
        p.error("--model-ports 要写成逗号分隔的端口号")
    raw_egress, a.egress = a.egress, []
    for item in raw_egress:
        cidr, sep, ports = item.rpartition(":")
        try:
            plist = [int(x) for x in ports.split(",") if x.strip()]
        except ValueError:
            plist = []
        if not sep or "/" not in cidr or not plist:
            p.error("--egress 要写成 网段:端口[,端口],如 10.1.2.3/32:11434:%r" % item)
        a.egress.append((cidr, plist))
    a.overrides = {}
    for item in a.test_override:
        k, sep, v = item.partition("=")
        if not sep or not _ENV_KEY.match(k):
            p.error("--test-override 要写成 KEY=VALUE:%r" % item)
        a.overrides[k] = v
    return a


def main(argv: List[str] = None) -> int:
    a = parse(sys.argv[1:] if argv is None else argv)
    sys.stdout.write(to_yaml(render(a)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
