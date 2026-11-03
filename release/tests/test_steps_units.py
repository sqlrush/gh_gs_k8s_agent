"""各步骤里风险高的几处:解包挡恶意路径、变更预览遮敏感值、首次部署的密钥只留指纹、白名单关卡循环、按需模式跳过池子。"""
from __future__ import annotations

import io
import pathlib
import sys
import tarfile
import time

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import steps, whitelist  # noqa: E402
from release.engine import Engine, StepFailed  # noqa: E402
from release.kube import Result  # noqa: E402
from release.package import Package  # noqa: E402
from release.store import Store  # noqa: E402


class FakeKube:
    namespace = "gaussdb-agent"

    def __init__(self):
        self.secrets, self.calls = {}, []

    def apply_secret(self, name, data, labels=None):
        self.secrets[name] = dict(data)

    def get(self, kind, name, namespaced=True):
        return None

    def list(self, kind, selector="", namespaced=True):
        return []

    def patch(self, *a, **kw):
        self.calls.append(("patch",) + a)


def _tar(path, members):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


def test_extract_takes_only_k8s_and_refuses_escapes(tmp_path):
    t = tmp_path / "k.tar.gz"
    _tar(t, {"k8s/base/kustomization.yaml": b"resources: []\n", "docs/a.md": b"x", "k8s/../../evil": b"x",
             "/etc/passwd": b"x"})
    pkg = Package(tag="agent-v1.4.4-oc1.18.27", root=tmp_path, files={"k8s": t})
    root = steps._extract_k8s(pkg, tmp_path / "out")
    assert (root / "base" / "kustomization.yaml").is_file()
    assert not (tmp_path / "out" / "docs").exists() and not (tmp_path / "evil").exists()


def test_review_diff_masks_sensitive_configmap_values():
    diff = "-  GRMP_PATH_PREFIX: /old/secret/prefix\n+  GRMP_PATH_PREFIX: /new/secret/prefix\n+  MODEL_ID: m2\n"
    out = steps._mask_cm_values(diff)
    assert "/secret/" not in out and "MODEL_ID: m2" in out and out.count("已设置,不显示") == 2


def test_first_deploy_writes_secrets_then_keeps_only_fingerprints(tmp_path):
    store = Store(tmp_path / "r.db")
    store.set_secret("model_api_key", "sk-model-xyz", keep=True)
    store.set_secret("trust_secret", "trust-" + "a" * 20, keep=True)
    kube = FakeKube()
    svc = steps.Services(store=store, kube=kube, releases=tmp_path, state=tmp_path)
    logs = []

    class C:
        def log(self, m, lv=""):
            logs.append(m)
    steps.write_cluster_secrets(svc, C())
    assert kube.secrets["model-api"] == {"MODEL_API_KEY": "sk-model-xyz"}
    assert set(kube.secrets["gateway-auth"]) == {"TRUST_SECRET", "ADMIN_TOKEN"} and len(kube.secrets["gateway-auth"]["ADMIN_TOKEN"]) == 48
    assert len(kube.secrets["sandbox-control"]["KEY"]) == 48 and "grmp-sm2" not in kube.secrets
    assert store.secret("model_api_key") == "" and store.secret("admin_token") == "", "写进集群后发版台不留值"
    assert "sk-model-xyz" not in repr(logs) and store.secret_marks()["admin_token"]["fingerprint"]


def test_first_deploy_without_a_required_secret_stops(tmp_path):
    svc = steps.Services(store=Store(tmp_path / "r.db"), kube=FakeKube(), releases=tmp_path, state=tmp_path)
    with pytest.raises(StepFailed, match="model_api_key"):
        steps.write_cluster_secrets(svc, None)


def _engine(tmp_path, step_map, **svc_kw):
    store = Store(tmp_path / "e.db")
    svc = steps.Services(store=store, kube=FakeKube(), releases=tmp_path, state=tmp_path, **svc_kw)
    return store, Engine(store, step_map, services={"svc": svc}, sleep=lambda _s: time.sleep(0.01), poll_s=0.01)


def test_whitelist_gate_loops_until_the_team_has_registered_everything(tmp_path, monkeypatch):
    dml = (_ROOT / "docs" / "delivery" / "scripts.sql").read_text(encoding="utf-8")
    exp = whitelist.parse_dml(dml)
    rel = {"whitelist": {"scripts": whitelist.to_json(exp)}}
    monkeypatch.setattr(steps, "_pkg", lambda ctx: Package(tag="t", root=tmp_path, release=rel))
    rows = ["script_name,content_md5,parameter_config,is_valid"] + [
        '%s,%s,"%s",1' % (s.name, s.md5, str([{"key": k, "type": t} for k, t in s.params]).replace("'", '""'))
        for s in exp.values()]
    store, eng = _engine(tmp_path, {"whitelist": steps.step_whitelist})
    store.kv_set("whitelist.export", "\n".join(rows[:-2]) + "\n")              # 少登记了最后两条
    rid = eng.start("upgrade", "v3", "v4", [{"id": "whitelist", "subs": ["a", "b", "c", "d"]}], {"to_tag": "t"}, None)
    end = time.time() + 5
    while store.open_gate(rid) is None and time.time() < end:
        time.sleep(0.01)
    sh = store.kv_get("run:%d" % rid)["whitelist"]
    assert sh["counts"]["missing"] == 2 and len(sh["problems"]) == 2
    store.kv_set("whitelist.export", "\n".join(rows) + "\n")                    # 白名单团队补登记了
    eng.resolve(rid, "recheck")
    eng.wait()
    assert store.run(rid)["status"] == "done"
    assert store.kv_get("run:%d" % rid)["whitelist"]["counts"]["ok"] == len(exp)


def test_ondemand_mode_has_no_pool_to_replace(tmp_path):
    store, eng = _engine(tmp_path, {"pool": steps.step_pool})
    rid = eng.start("upgrade", "v3", "v4", [{"id": "pool", "subs": ["a", "b", "c"]}],
                    {"to_tag": "agent-v1.4.4-oc1.18.27", "kind": "upgrade", "pool_mode": "slot"}, None)
    eng.wait()
    r = store.run(rid)
    assert r["status"] == "done" and r["steps"][0]["detail"] == "按需模式,无需替换"


def test_the_review_diff_keeps_only_what_an_operator_needs():
    raw = ("diff -u -N /tmp/LIVE-1/apps.v1.Deployment.ns.gateway /tmp/MERGED-2/apps.v1.Deployment.ns.gateway\n"
           "--- /tmp/LIVE-1/x\t2026\n+++ /tmp/MERGED-2/x\t2026\n@@ -6,7 +6,7 @@\n"
           "     kubectl.kubernetes.io/last-applied-configuration: |\n"
           '       {"apiVersion":"apps/v1","kind":"Deployment"}\n'
           "-  generation: 3\n+  generation: 4\n   labels:\n-        image: gw:1.4.3\n+        image: gw:1.4.4\n")
    out = steps.clean_diff(raw)
    assert out.splitlines() == ["== apps.v1.Deployment.ns.gateway", "   labels:", "-        image: gw:1.4.3", "+        image: gw:1.4.4"]


def test_pushed_images_are_pinned_by_digest_preloaded_ones_are_not():
    class C:
        def shared(self):
            return {"digests": {"gaussdb-agent-runtime": "sha256:" + "d" * 64}}
    d = "sha256:" + "d" * 64
    assert steps.pinned_digests(C(), {"registry.mode": "push"}) == {"gaussdb-agent-runtime": d}
    assert steps.pinned_digests(C(), {}) == {"gaussdb-agent-runtime": d}, "默认就是推进内网仓库"
    assert steps.pinned_digests(C(), {"registry.mode": "preloaded"}) == {}, "直接载进节点的镜像没有仓库 digest,按 digest 找不到"
    cfg = {"registry.prefix": "reg.x/ns/"}
    assert steps.render_runtime_image(cfg, "agent-v1.4.4-oc1.18.27", d) == "reg.x/ns/gaussdb-agent-runtime:agent-v1.4.4-oc1.18.27@" + d
    assert steps.render_runtime_image({}, "agent-v1.4.4-oc1.18.27") == "gaussdb-agent-runtime:agent-v1.4.4-oc1.18.27"


def _job(name, image):
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "labels": {"app": "gaussdb-kbstd"}},
            "spec": {"template": {"spec": {"initContainers": [{"name": "fetch", "image": image}],
                                           "containers": [{"name": "install", "image": "kb-import:" + image.split(":", 1)[1]}]}}}}


class JobKube(FakeKube):
    def __init__(self, existing=None):
        super().__init__()
        self.jobs, self.calls = dict(existing or {}), []

    def get(self, kind, name, namespaced=True):
        return self.jobs.get(name) if kind == "job" else super().get(kind, name, namespaced)

    def delete(self, kind, name="", selector="", wait=True):
        self.calls.append(("delete", name)); self.jobs.pop(name, None)

    def apply(self, manifest):
        for d in yaml.safe_load_all(manifest):
            if d["metadata"]["name"] in self.jobs and steps.job_images(self.jobs[d["metadata"]["name"]]) != steps.job_images(d):
                raise RuntimeError("field is immutable")
            self.jobs[d["metadata"]["name"]] = d; self.calls.append(("apply", d["metadata"]["name"]))

    def run(self, args, **kw):
        return Result(0, "", "")

    def logs(self, target, container="", tail=200):
        return "[skipped] G25132C 已是这个版本"

    def list(self, kind, selector="", namespaced=True):
        return list(self.jobs.values()) if kind == "jobs" else super().list(kind, selector, namespaced)


@pytest.mark.parametrize("old_image, deleted", [("kbstd:v4@sha256:old", True), ("kbstd:v4@sha256:new", False)])
def test_kbstd_job_with_the_same_name_but_other_images_is_recreated(tmp_path, old_image, deleted):
    """Job 模板改不了:同一版本的 Job 已在(上次同版本、演练包、手工 apply)而镜像不同时,apply 会报 field is immutable ——
    2026-11-03 真 v1.4.4 演练撞上。删掉重跑;镜像相同就照旧 apply(安装内容相同会跳过)。"""
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(yaml.safe_dump(_job("kbstd-install-v1-4-4", "kbstd:v4@sha256:new")), encoding="utf-8")
    kube = JobKube({"kbstd-install-v1-4-4": _job("kbstd-install-v1-4-4", old_image)})
    store = Store(tmp_path / "e.db")
    svc = steps.Services(store=store, kube=kube, releases=tmp_path, state=tmp_path)
    eng = Engine(store, {"prep": lambda ctx: ctx.share(rendered=str(rendered)), "kbstd": steps.step_kbstd},
                 services={"svc": svc}, sleep=lambda _s: None, poll_s=0.01)
    rid = eng.start("upgrade", "v3", "v4", [{"id": "prep", "subs": ["a"]}, {"id": "kbstd", "subs": ["a", "b", "c"]}], {"to_tag": "t"}, None)
    eng.wait()
    assert store.run(rid)["status"] == "done", store.run(rid)["error"]
    assert (("delete", "kbstd-install-v1-4-4") in kube.calls) == deleted
    assert ("apply", "kbstd-install-v1-4-4") in kube.calls
