"""资源扫描:一项出错不影响其他项、各类失败的说法(假集群)。"""
from __future__ import annotations

import json

import pytest
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import scan  # noqa: E402
from release.kube import Result  # noqa: E402
from release.store import Store  # noqa: E402


class FakeKube:
    namespace = "gaussdb-agent"

    def __init__(self, probes=None, archs=("arm64",), objects=None, can=True, version=None):
        self.probes, self.archs, self.can = probes or {}, archs, can
        self.ver = version if version is not None else {"client": 31, "server": 31}
        self.objects = objects if objects is not None else {
            ("namespace", "gaussdb-agent"): {}, ("pvc", "nas"): {"status": {"phase": "Bound"}},
            ("secret", "gateway-auth"): {}, ("secret", "grmp-token-u9500"): {}}

    def get(self, kind, name, namespaced=True):
        return self.objects.get((kind, name))

    def list(self, kind, selector="", namespaced=True):
        if kind == "nodes":
            return [{"metadata": {"labels": {"kubernetes.io/arch": a}}, "status": {"allocatable": {"memory": "16Gi"}}}
                    for a in self.archs]
        if kind == "pods":
            return [{"metadata": {"name": "agent-pool-0"}, "status": {"containerStatuses": [{"name": "steward", "ready": True}]}}]
        return []

    def can_i(self, verb, resource, namespaced=True):
        return self.can

    def version(self):
        return self.ver

    def exec(self, pod, container, argv, stdin="", timeout=60):
        for key, out in self.probes.items():
            if key in stdin:
                if isinstance(out, Exception):
                    raise out
                return Result(0, json.dumps(out) + "\n", "")
        return Result(1, "", "no probe")


GOOD = {"MODEL_BASE_URL": {"code": 200, "ms": 50, "model": "m"}, "GRMP_API_HOST": {"ok": True, "ms": 3, "has_prefix": True},
        "release-probe": {"ok": True, "err": "", "uid": 1000, "free_gib": 100.0}}


def _scan(tmp_path, kube, cfg=None):
    st = Store(tmp_path / "r.db")
    st.save_config(dict({"smoke.user": "u9500", "registry.mode": "preloaded"}, **(cfg or {})))
    (tmp_path / "rel").mkdir(exist_ok=True)
    return {i.id: i for i in scan.Scanner(store=st, kube=kube, releases=tmp_path / "rel").run()}, st


def test_a_healthy_cluster(tmp_path):
    items, st = _scan(tmp_path, FakeKube(GOOD))
    assert {k: v.st for k, v in items.items() if k != "dir"} == {
        "registry": "warn", "k8s": "ok", "nas": "ok", "model": "ok", "grmp": "ok", "wl": "warn", "sso": "ok", "cap": "ok"}
    assert items["dir"].st == "bad" and "没有发现任何发布包" in items["dir"].msg
    assert st.kv_get("node_arch") == "arm64" and st.last_scan()["status"] == "bad"


def test_each_failure_names_its_cause(tmp_path):
    probes = dict(GOOD, MODEL_BASE_URL={"code": 401, "ms": 9, "model": "m"},
                  **{"release-probe": {"ok": False, "err": "Permission denied", "uid": 1000, "free_gib": 1}})
    items, _ = _scan(tmp_path, FakeKube(probes, archs=("arm64", "amd64")))
    assert "API Key" in items["model"].msg and items["model"].st == "bad"
    assert "属主改为 1000" in items["nas"].msg
    assert "架构混合" in items["k8s"].msg


def test_one_check_blowing_up_does_not_stop_the_others(tmp_path):
    probes = dict(GOOD, GRMP_API_HOST=RuntimeError("exec 超时"))
    items, _ = _scan(tmp_path, FakeKube(probes))
    assert items["grmp"].st == "bad" and items["grmp"].name == "中间件" and "exec 超时" in items["grmp"].msg
    assert items["model"].st == "ok" and items["sso"].st == "ok"


def test_missing_permissions_and_an_unprovisioned_smoke_user(tmp_path):
    objs = {("namespace", "gaussdb-agent"): {}, ("pvc", "nas"): {"status": {"phase": "Bound"}}, ("secret", "gateway-auth"): {}}
    items, _ = _scan(tmp_path, FakeKube(GOOD, objects=objs, can=False))
    assert items["k8s"].st == "bad" and "patch pods" in items["k8s"].msg
    assert "grmp-token-u9500" in items["sso"].msg


def test_before_the_first_deploy_cluster_side_checks_wait(tmp_path):
    kube = FakeKube({}, objects={})
    kube.list = lambda kind, selector="", namespaced=True: []
    items, _ = _scan(tmp_path, kube, {"smoke.user": "u9500"})
    assert items["k8s"].st == "warn" and items["nas"].st == "warn" and items["model"].st == "warn"
    assert items["sso"].st == "bad" and "信任凭据" in items["sso"].msg


def test_kubectl_far_from_the_cluster_version_is_a_warning_not_a_blocker(tmp_path):
    items, _ = _scan(tmp_path, FakeKube(GOOD, version={"client": 31, "server": 36}))
    assert items["k8s"].st == scan.WARN
    assert "kubectl 1.31 与集群 1.36 相差 5 个小版本" in items["k8s"].msg and "--kubectl" in items["k8s"].msg
    items, _ = _scan(tmp_path, FakeKube(GOOD, version={"client": 31, "server": 30}))
    assert items["k8s"].st == scan.OK, "相差 1 以内不提示"
    items, _ = _scan(tmp_path, FakeKube(GOOD, version={"client": 31}))
    assert items["k8s"].st == scan.OK, "读不到集群版本:不猜"


@pytest.mark.parametrize("code, st, says", [
    (503, "warn", "服务方现在忙"), (429, "warn", "稍后再扫"), (402, "bad", "余额不足"), (400, "bad", "模型 ID"),
    (404, "bad", "模型 ID 或地址"), (-1, "bad", "连不上"), (200, "ok", "对话通")])
def test_the_model_check_talks_to_the_model_and_says_why_it_failed(tmp_path, code, st, says):
    """只看 /models 会漏掉服务方过载(演练 #8 撞上)、模型 ID 写错、余额不足 —— 改成发一句最短的对话。"""
    probes = dict(GOOD, MODEL_BASE_URL={"code": code, "ms": 30, "model": "m", "err": "Service is too busy" if code == 503 else ""})
    items, _ = _scan(tmp_path, FakeKube(probes))
    assert items["model"].st == st and says in items["model"].msg
    if code == 503:
        assert "服务方原话:Service is too busy" in items["model"].msg


def test_the_model_probe_is_a_minimal_chat_with_the_agents_model():
    assert "/chat/completions" in scan.PROBE_MODEL and '"max_tokens": 8' in scan.PROBE_MODEL
    assert 'os.environ.get("MODEL_ID"' in scan.PROBE_MODEL and "/models\"" not in scan.PROBE_MODEL
