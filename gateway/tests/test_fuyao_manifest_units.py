"""Fuyao 运行模式的集群清单(k8s/fuyao、k8s/overlays/fuyao)守卫(2026-11-17)。

与 base 同一套规矩:不注入 Service 发现变量、只读根文件系统、emptyDir 有上限;凭据代理对 Secret 只能按名取、不能列举;
集群服务与 base 同一个镜像版本(发版时一起改);沙箱组建在平台命名空间、不进 overlay。
"""
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FY = _ROOT / "k8s" / "fuyao"


def _docs(path):
    return [d for d in path.read_text(encoding="utf-8").split("\n---\n") if d.strip()]


def test_cluster_services_follow_the_base_hardening_rules():
    for name in ("statesvc.yaml", "credproxy.yaml"):
        text = (_FY / name).read_text(encoding="utf-8")
        assert "enableServiceLinks: false" in text, name
        assert "readOnlyRootFilesystem: true" in text and 'drop: ["ALL"]' in text, name
        assert "runAsNonRoot: true" in text, name
        for body in re.findall(r"emptyDir: \{([^}]*)\}", text):
            assert "sizeLimit" in body, name


def test_credproxy_can_get_but_never_list_secrets():
    role = [d for d in _docs(_FY / "credproxy.yaml") if "kind: Role\n" in d][0]
    assert re.search(r'resources: \["secrets"\]\s*\n\s*verbs: \["get"\]', role), "只能按名取 grmp-token-<工号>"
    assert '"list"' not in role and '"watch"' not in role


def test_cluster_services_use_the_same_image_version_as_the_base():
    base = (_ROOT / "k8s" / "base" / "gateway.yaml").read_text(encoding="utf-8")
    tag = re.search(r"image: gaussdb-agent-gateway:(\S+)", base).group(1)
    for name in ("statesvc.yaml", "credproxy.yaml"):
        assert "gaussdb-agent-runtime:%s" % tag in (_FY / name).read_text(encoding="utf-8"), \
            "%s 的镜像版本要与 base 一致(发版脚本一起改)" % name


def test_sandbox_groups_live_in_the_platform_namespace_and_stay_out_of_the_overlay():
    groups = (_FY / "sandboxgroups.yaml").read_text(encoding="utf-8")
    assert groups.count("kind: SandboxGroup") == 2 and groups.count("namespace: flux-system") == 2
    kz = (_FY / "kustomization.yaml").read_text(encoding="utf-8")
    assert "sandboxgroups" not in kz.split("resources:")[1], "沙箱组由平台管理员单独 apply"
    ov = (_ROOT / "k8s" / "overlays" / "fuyao" / "configmap-gateway-fuyao.yaml").read_text(encoding="utf-8")
    assert 'RUNTIME_MODE: "fuyao"' in ov
    for g in re.findall(r"name: (sg-gsdb-\w+)", groups):
        assert g in ov, "overlay 里的沙箱组名要与 sandboxgroups.yaml 一致:%s" % g


# -- 资源大盘(2026-11-18) ----------------------------------------------------------------------------

def _yaml(path):
    import yaml
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def test_platform_read_is_read_only_on_the_three_sandbox_resources_and_bound_to_the_gateway():
    docs = {d["kind"]: d for d in _yaml(_FY / "platform-read.yaml")}
    assert set(docs) == {"ClusterRole", "ClusterRoleBinding"}
    rules = docs["ClusterRole"]["rules"]
    assert rules == [{"apiGroups": ["sandbox.flux.io"], "resources": ["sandboxgroups", "subsandboxgroups", "sandboxes"],
                      "verbs": ["get", "list"]}], "只读、只这三种资源;不给平台命名空间的 Pod / Secret"
    b = docs["ClusterRoleBinding"]
    assert b["roleRef"]["name"] == docs["ClusterRole"]["metadata"]["name"]
    assert b["subjects"] == [{"kind": "ServiceAccount", "name": "gateway", "namespace": "gaussdb-agent"}]
    assert "platform-read.yaml" in (_FY / "kustomization.yaml").read_text(encoding="utf-8")


def test_release_render_moves_the_binding_subject_with_the_namespace_and_keeps_cluster_objects_unnamespaced():
    from release import render
    docs = render.load_overlay(_ROOT / "k8s", "fuyao")
    render.set_namespace(docs, "zz-other")
    crb = [d for d in docs if d["kind"] == "ClusterRoleBinding"][0]
    assert crb["subjects"][0]["namespace"] == "zz-other"
    for d in docs:
        if d["kind"] in ("ClusterRole", "ClusterRoleBinding"):
            assert "namespace" not in d["metadata"], "集群级对象不写命名空间(与 kubectl kustomize 一致)"


def test_kustomize_moves_the_binding_subject_with_the_overlay_namespace(tmp_path):
    import shutil
    import subprocess
    kubectl = shutil.which("kubectl")
    if not kubectl:
        pytest.skip("没有 kubectl")
    shutil.copytree(_ROOT / "k8s", tmp_path / "k8s")
    zz = tmp_path / "k8s" / "overlays" / "zz"
    zz.mkdir()
    (zz / "kustomization.yaml").write_text("apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
                                          "namespace: zz-other\nresources:\n  - ../fuyao\n", encoding="utf-8")
    out = subprocess.run([kubectl, "kustomize", str(zz)], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        pytest.skip("kubectl kustomize 不可用:%s" % out.stderr[:200])
    import yaml
    crb = [d for d in yaml.safe_load_all(out.stdout) if d and d["kind"] == "ClusterRoleBinding"][0]
    assert crb["subjects"][0]["namespace"] == "zz-other" and "namespace" not in crb["metadata"]


def test_the_gateway_can_reach_the_state_service_through_the_network_policies():
    docs = {d["metadata"]["name"]: d for d in _yaml(_FY / "networkpolicy.yaml")}
    out = docs["gateway-to-sandboxes"]["spec"]["egress"]
    assert any(r["to"] == [{"podSelector": {"matchLabels": {"app": "gaussdb-statesvc"}}}] and
               r["ports"] == [{"port": 8090, "protocol": "TCP"}] for r in out)
    inn = docs["statesvc-policy"]["spec"]["ingress"]
    assert any(r["from"] == [{"podSelector": {"matchLabels": {"app": "agent-gateway"}}}] for r in inn)


def test_ops_admins_ship_empty_and_product_name_defaults_to_the_generic_name():
    roles = _yaml(_ROOT / "k8s" / "base" / "configmap-agent-roles.yaml")[0]["data"]
    assert roles["ops-admins"] == "" and roles["kb-admins"] == "", "交付时不能带任何样例工号"
    gw = _yaml(_ROOT / "k8s" / "base" / "configmap-gateway.yaml")[0]["data"]
    assert gw["PRODUCT_NAME"] == "GaussDB 智能体" and gw["OPS_ADMIN_ROLES"] == "ops-admin"


def test_the_two_service_urls_are_left_empty_so_the_gateway_derives_them_from_its_namespace():
    """base 不再写死 gaussdb-agent:换命名空间部署时留空,网关按 GATEWAY_NAMESPACE 推导(gateway/config.py)。"""
    cm = (_ROOT / "k8s" / "base" / "configmap-gateway.yaml").read_text(encoding="utf-8")
    for key in ("FUYAO_STATE_URL", "FUYAO_CREDPROXY_URL"):
        m = re.search(r'^  %s:\s*"([^"]*)"' % key, cm, re.M)
        assert m and m.group(1) == "", "%s 要留空(默认按命名空间推导)" % key
