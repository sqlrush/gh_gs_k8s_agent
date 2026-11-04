"""配置迁移与渲染:用仓库里真实的 k8s/base;集群现状仿测试环境的本地覆盖(端口、额外出口都不一样)。"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import render as rd  # noqa: E402

K8S = _ROOT / "k8s"
TAG = "agent-v1.4.4-oc1.18.27"
REG = "registry.example.internal/gaussdb-agent"
FULL_CFG = {"registry.prefix": REG, "grmp.path_prefix": "/p/q", "net.grmp": {"cidrs": ["10.20.0.5/32"], "ports": [8080]},
            "net.model": {"cidrs": ["10.30.0.0/24"], "ports": [443]}, "net.model_kb": {"cidrs": ["10.30.0.0/24"], "ports": [443]}}
REQ = [{"map": "agent-config", "key": "GRMP_PATH_PREFIX"}]


def _doc(r, kind, name):
    return next(d for d in r.docs if d["kind"] == kind and d["metadata"]["name"] == name)


def _local_netpols():
    docs = list(yaml.safe_load_all((K8S / "local" / "networkpolicy-local.yaml").read_text(encoding="utf-8")))
    return {d["metadata"]["name"]: d["spec"] for d in docs if d}


EXISTING = ["Namespace/gaussdb-agent", "PersistentVolumeClaim/nas", "Deployment/gateway"]


# -- 首次部署 ----------------------------------------------------------------------------------------

def test_fresh_deploy_points_every_image_at_the_registry_and_target_tag():
    r = rd.render(K8S, FULL_CFG, TAG, {}, {}, [], REQ)
    images = [c["image"] for d in r.docs for c in rd._containers(d)]
    assert images and all(i.startswith(REG + "/gaussdb-agent-") and i.endswith(":" + TAG) for i in images), images
    gw = _doc(r, "ConfigMap", "gateway-config")["data"]
    assert gw["USER_IMAGE_TAG"] == TAG and gw["USER_IMAGE_REGISTRY"] == REG
    assert r.problems == [], r.problems
    assert any(d["kind"] == "Namespace" for d in r.docs) and any(d["kind"] == "PersistentVolumeClaim" for d in r.docs)


def test_fresh_deploy_stops_on_placeholders_and_missing_required_config():
    r = rd.render(K8S, {"registry.prefix": REG}, TAG, {}, {}, [], REQ)
    text = " ".join(r.problems)
    assert "GRMP_PATH_PREFIX" in text and "中间件网段还是占位值" in text and "模型服务网段还是占位值" in text


def test_another_namespace_is_carried_into_rbac_and_the_gateway_env():
    r = rd.render(K8S, dict(FULL_CFG, **{"k8s.namespace": "agent-rel"}), TAG, {}, {}, [], REQ)
    assert all(d["metadata"].get("namespace") == "agent-rel" for d in r.docs if d["kind"] != "Namespace")
    assert _doc(r, "Namespace", "agent-rel")
    rb = _doc(r, "RoleBinding", "gateway")
    assert rb["subjects"][0]["namespace"] == "agent-rel"
    env = {e["name"]: e.get("value") for c in rd._containers(_doc(r, "Deployment", "gateway")) for e in c.get("env") or []}
    assert env["GATEWAY_NAMESPACE"] == "agent-rel"


# -- 升级 ------------------------------------------------------------------------------------------

def _cluster_cms():
    base = {d["metadata"]["name"]: dict(d.get("data") or {}) for d in rd.load_base(K8S) if d["kind"] == "ConfigMap"}
    agent = dict(base["agent-config"], GRMP_PATH_PREFIX="/real/prefix", MODEL_ID="deepseek-flash", HTTPS_PROXY="http://p:3128")
    agent.pop("KB_CLIP_MAX_MB", None)            # 假装这是旧版本:新版本才有这个键
    gw = dict(base["gateway-config"], USER_IMAGE_TAG="agent-v1.4.3-oc1.18.27", POOL_IDLE_SECONDS="3600")
    return {"agent-config": agent, "gateway-config": gw, "agent-roles": {"kb-admins": "u2001,u9500"}}


def test_upgrade_keeps_cluster_values_adds_new_keys_and_moves_the_tag():
    r = rd.render(K8S, {"registry.prefix": REG}, TAG, _cluster_cms(), _local_netpols(), EXISTING, REQ)
    agent = _doc(r, "ConfigMap", "agent-config")["data"]
    gw = _doc(r, "ConfigMap", "gateway-config")["data"]
    assert agent["GRMP_PATH_PREFIX"] == "/real/prefix" and agent["MODEL_ID"] == "deepseek-flash"
    assert agent["HTTPS_PROXY"] == "http://p:3128", "集群里有、清单里没有的键(runtime-doctor 加的代理)要留着"
    assert "KB_CLIP_MAX_MB" in agent and gw["POOL_IDLE_SECONDS"] == "3600" and gw["USER_IMAGE_TAG"] == TAG
    assert _doc(r, "ConfigMap", "agent-roles")["data"] == {"kb-admins": "u2001,u9500"}, "知识库管理员名单不能被清空"
    why = {(c.key, c.why) for c in r.changes}
    assert ("KB_CLIP_MAX_MB", "新版本新增(默认值)") in why and ("USER_IMAGE_TAG", "发版台管理") in why
    assert not any(c.key == "GRMP_PATH_PREFIX" for c in r.changes)
    assert r.problems == [] and not any(d["kind"] in ("Namespace", "PersistentVolumeClaim") for d in r.docs)


def test_resource_config_overrides_and_sensitive_values_are_not_shown():
    r = rd.render(K8S, {"registry.prefix": REG, "grmp.path_prefix": "/new/prefix", "model.id": "m2"}, TAG,
                  _cluster_cms(), _local_netpols(), EXISTING, REQ)
    shown = {c.key: c.show() for c in r.changes}
    assert shown["MODEL_ID"]["new"] == "m2" and shown["MODEL_ID"]["old"] == "deepseek-flash"
    assert shown["GRMP_PATH_PREFIX"]["new"] == "已设置" and "/new/prefix" not in repr(shown)


def test_upgrade_keeps_the_clusters_external_rules_and_takes_internal_ones_from_the_new_manifest():
    r = rd.render(K8S, {"registry.prefix": REG}, TAG, _cluster_cms(), _local_netpols(), EXISTING, REQ)
    rt = _doc(r, "NetworkPolicy", "runtime-policy")["spec"]["egress"]
    ports = sorted(tuple(sorted(p["port"] for p in rule.get("ports") or [])) for rule in rt)
    assert (8781,) in ports and (11434,) in ports and (443,) in ports, "测试环境的中间件 8781、Ollama 11434 都要留着"
    assert (53, 53) in ports, "DNS 这条内部规则取自新清单"
    gw = _doc(r, "NetworkPolicy", "gateway-policy")["spec"]
    assert any("192.168.194.0/24" in str(rule) for rule in gw["egress"]), "apiserver 段沿用集群的"
    assert any(4095 in [p["port"] for p in rule.get("ports") or []] for rule in gw["egress"])


def test_a_configured_model_range_replaces_only_the_model_rule():
    cfg = {"registry.prefix": REG, "net.model": {"cidrs": ["10.30.0.0/24"], "ports": [443]}}
    r = rd.render(K8S, cfg, TAG, _cluster_cms(), _local_netpols(), EXISTING, REQ)
    rt = _doc(r, "NetworkPolicy", "runtime-policy")["spec"]["egress"]
    assert "0.0.0.0/0" not in str(rt) and "10.30.0.0/24" in str(rt)
    assert "11434" in str(rt) and "8781" in str(rt), "认不出的行方规则(Ollama)不受影响"


# -- 其他 ------------------------------------------------------------------------------------------

def test_an_unknown_kustomization_feature_is_refused(tmp_path):
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "kustomization.yaml").write_text("resources: []\npatches: [{path: x.yaml}]\n", encoding="utf-8")
    with pytest.raises(rd.RenderError, match="不认识的写法"):
        rd.load_base(tmp_path)


def test_render_pool_for_a_fresh_deploy_and_the_image_patch_for_upgrades():
    img = "%s/gaussdb-agent-runtime:%s" % (REG, TAG)
    docs = rd.render_pool(K8S, dict(FULL_CFG, **{"pool.pods": 3, "pool.slots": 3}), img, "gaussdb-agent")
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    names = [c["name"] for c in sts["spec"]["template"]["spec"]["containers"]]
    assert names == ["slot-0", "slot-1", "slot-2", "steward"] and sts["spec"]["replicas"] == 3
    assert all(c["image"] == img for c in sts["spec"]["template"]["spec"]["containers"])
    patch = rd.pool_image_patch(sts, "x:new")
    assert [c["name"] for c in patch["spec"]["template"]["spec"]["containers"]] == names


def test_images_are_pinned_by_digest_when_given_so_a_stale_cached_tag_is_not_reused():
    """同一标签换过内容(重新打包、演练包)时,节点 IfNotPresent 会接着用缓存的旧镜像 —— 钉 digest 就按 digest 拉。"""
    pins = {"gaussdb-agent-gateway": "sha256:" + "a" * 64, "gaussdb-agent-kbstd": "sha256:" + "b" * 64}    # 两个仓库都有的镜像
    r = rd.render(K8S, FULL_CFG, TAG, {}, {}, [], REQ, pins)
    by_repo = {c["image"].split("/")[-1].split(":")[0]: c["image"] for d in r.docs for c in rd._containers(d)}
    assert by_repo["gaussdb-agent-gateway"] == "%s/gaussdb-agent-gateway:%s@sha256:%s" % (REG, TAG, "a" * 64)
    assert by_repo["gaussdb-agent-kbstd"].endswith(":%s@sha256:%s" % (TAG, "b" * 64))
    assert by_repo["gaussdb-agent-kb-import"].endswith(":" + TAG), "没给 digest 的照旧只写标签"
    assert _doc(r, "ConfigMap", "gateway-config")["data"]["USER_IMAGE_TAG"] == TAG, "网关建用户容器只认标签,不带 digest"
