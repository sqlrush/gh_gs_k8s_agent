"""Fuyao 运行模式下发版台其余各处:识别模式与当前版本、步骤表、按 overlay 渲染并写模板名、配置迁移一步、扫描、冒烟的等待。"""
from __future__ import annotations

import http.server
import json
import pathlib
import sys
import threading
import time

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import e2b, plan as pl, render as rd, scan, steps  # noqa: E402
from release.engine import Engine  # noqa: E402
from release.kube import Result  # noqa: E402
from release.package import Package  # noqa: E402
from release.smoke import Smoke  # noqa: E402
from release.store import Store  # noqa: E402

K8S = _ROOT / "k8s"
OLD, TAG = "agent-v1.4.14-oc1.18.27", "agent-v1.5.0-oc1.18.27"
RT, IMP = "gsdb-runtime-v1-5-0", "gsdb-import-v1-5-0"
REG = "registry.example.internal/gaussdb-agent"
REQ = [{"map": "agent-config", "key": "GRMP_PATH_PREFIX"}]
EXISTING = ["Namespace/gaussdb-agent", "PersistentVolumeClaim/nas", "Deployment/gateway"]
HISTORY = {"agent-v1.5.0": {"version": "agent-v1.5.0", "mixed_with": [], "required": [], "steps": ["rebuild_kb_import"],
                            "whitelist": [], "notes": []},
           "agent-v1.4.14": {"version": "agent-v1.4.14", "mixed_with": [], "required": [], "steps": [], "whitelist": [],
                             "notes": []}}


def _deploy(image, replicas=1, ready=1):
    return {"spec": {"replicas": replicas, "template": {"spec": {"containers": [{"name": "c", "image": image}]}}},
            "status": {"readyReplicas": ready}}


class FyKube:
    """Fuyao 集群:gateway-config 是 fuyao 模式,有状态服务与凭据代理服务,**没有池子**(看池子 Pod 就算错)。"""
    namespace = "gaussdb-agent"

    def __init__(self, gw=OLD, statesvc=OLD, credproxy=OLD, cms=None, mode="fuyao"):
        self.mode = mode
        self.objects = {("deploy", "gateway"): _deploy("reg/gaussdb-agent-gateway:" + gw)}
        for name, tag in (("statesvc", statesvc), ("credproxy", credproxy)):
            if tag:
                self.objects[("deploy", name)] = _deploy("reg/gaussdb-agent-runtime:" + tag)
        if cms is None:
            cms = {"gateway-config": {"USER_IMAGE_TAG": gw, "RUNTIME_MODE": mode, "FUYAO_API": "http://opensandbox.example:80"}}
        for name, data in cms.items():
            self.objects[("configmap", name)] = {"data": data}
        self.objects[("namespace", "gaussdb-agent")] = {}
        self.objects[("pvc", "nas")] = {"status": {"phase": "Bound"}}

    def get(self, kind, name, namespaced=True):
        return self.objects.get((kind, name))

    def list(self, kind, selector="", namespaced=True):
        assert self.mode != "fuyao" or "role=pool" not in selector, "Fuyao 模式没有池子,不该去看池子 Pod"
        if kind == "nodes":
            return [{"metadata": {"labels": {"kubernetes.io/arch": "arm64"}}, "status": {"allocatable": {"memory": "16Gi"}}}]
        return []

    def can_i(self, verb, resource, namespaced=True):
        return True

    def version(self):
        return {"client": 31, "server": 31}

    def diff(self, manifest):
        return ""


def _pkg(tag):
    return Package(tag=tag, root=pathlib.Path("/r"), files={"arm64": pathlib.Path("a"), "k8s": pathlib.Path("k")},
                   release={"tag": tag, "min_upgrade_from": "agent-v1.4.0"})


# -- 计划 ------------------------------------------------------------------------------------------

def test_fuyao_mode_reads_the_cluster_services_instead_of_pool_pods():
    cur = pl.detect(FyKube())
    assert (cur.state, cur.mode, cur.version) == ("ok", "fuyao", "agent-v1.4.14")
    assert set(cur.tags) == {"deploy/gateway", "deploy/statesvc", "deploy/credproxy"}
    lag = pl.detect(FyKube(gw=TAG, statesvc=TAG, credproxy=OLD))
    assert lag.state == "partial" and lag.lagging() == ["deploy/credproxy"], "上次发布没走完:凭据代理服务还是旧版"
    assert pl.detect(FyKube(credproxy="")).state == "ok", "还没建的服务不算没走完(这次发布会建)"
    assert pl.runtime_mode(FyKube()) == "fuyao" and pl.runtime_mode(FyKube(cms={})) == ""


def test_fuyao_step_table_swaps_templates_instead_of_the_pool():
    p = pl.build(pl.detect(FyKube()), _pkg(TAG), HISTORY)
    assert p.kind == "upgrade" and p.mode == "fuyao" and p.problems == []
    ids = [s["id"] for s in p.steps]
    assert ids == ["pkg", "plan", "images", "config", "review", "whitelist", "template", "gateway", "sandbox", "kbstd",
                   "smoke", "done"], "没有池子替换、没有删除导入容器(版本要求重建导入容器也一样)"
    assert all(sid in steps.STEPS for sid in ids), "每一步都要有实现"
    s = {x["id"]: x for x in p.steps}
    assert "模板" in s["template"]["why"] and "闲置" in s["sandbox"]["why"] and "状态服务" in s["gateway"]["why"]
    assert p.as_dict()["mode"] == "fuyao", "模式进 run 的 meta,各步按它走"


def test_other_modes_keep_their_step_table():
    p = pl.build(pl.Current("ok", OLD, tags={"deploy/gateway": OLD}, mode="pool"), _pkg(TAG), HISTORY)
    ids = [s["id"] for s in p.steps]
    assert "pool" in ids and "kbimport" in ids and "template" not in ids and "sandbox" not in ids
    assert [r[0] for r in pl.step_table("")] == [r[0] for r in pl.STEPS]


def test_a_fuyao_rollback_still_checks_the_older_templates():
    cur = pl.detect(FyKube(gw=TAG, statesvc=TAG, credproxy=TAG))
    p = pl.build(cur, _pkg(OLD), HISTORY)
    s = {x["id"]: x for x in p.steps}
    assert p.kind == "rollback" and not s["template"]["skip"] and not s["sandbox"]["skip"] and s["whitelist"]["skip"]


def test_the_plan_step_stops_when_the_runtime_mode_changed_since_the_preview(tmp_path):
    store = Store(tmp_path / "e.db")
    svc = steps.Services(store=store, kube=FyKube(mode="pool"), releases=tmp_path, state=tmp_path)
    eng = Engine(store, {"plan": steps.step_plan}, services={"svc": svc}, sleep=lambda _s: None, poll_s=0.01)
    meta = {"kind": "upgrade", "from_v": "agent-v1.4.14", "to_v": "agent-v1.5.0", "to_tag": TAG, "pool_mode": "pod", "mode": "fuyao"}
    rid = eng.start("upgrade", "agent-v1.4.14", "agent-v1.5.0", [{"id": "plan", "subs": ["a", "b", "c"]}], meta, None)
    eng.wait()
    assert store.run(rid)["status"] == "failed" and "运行模式" in store.run(rid)["error"]


# -- 渲染 ------------------------------------------------------------------------------------------

def _fy_cluster():
    base = {d["metadata"]["name"]: dict(d.get("data") or {}) for d in rd.load_overlay(K8S, "fuyao") if d["kind"] == "ConfigMap"}
    agent = dict(base["agent-config"], GRMP_PATH_PREFIX="/real/prefix")
    gw = dict(base["gateway-config"], USER_IMAGE_TAG=OLD, FUYAO_RUNTIME_TEMPLATE="gsdb-runtime-v1-4-14",
              FUYAO_IMPORT_TEMPLATE="gsdb-import-v1-4-14", FUYAO_API="http://opensandbox.example:80", FUYAO_IDLE_SECONDS="900")
    nps = {}
    for f in (K8S / "local" / "networkpolicy-local.yaml", K8S / "fuyao" / "networkpolicy.yaml"):
        nps.update({d["metadata"]["name"]: d["spec"] for d in yaml.safe_load_all(f.read_text(encoding="utf-8")) if d})
    return {"agent-config": agent, "gateway-config": gw, "agent-roles": {"kb-admins": "u2001"}}, nps


def _doc(r, kind, name):
    return next(d for d in r.docs if d["kind"] == kind and d["metadata"]["name"] == name)


def test_fuyao_render_takes_the_overlay_and_writes_the_target_template_names():
    cms, nps = _fy_cluster()
    pins = {"gaussdb-agent-runtime": "sha256:" + "a" * 64}
    r = rd.render(K8S, {"registry.prefix": REG}, TAG, cms, nps, EXISTING, REQ, pins, mode="fuyao",
                  templates=e2b.templates_for(TAG))
    assert r.problems == [], r.problems
    gw = _doc(r, "ConfigMap", "gateway-config")["data"]
    assert (gw["FUYAO_RUNTIME_TEMPLATE"], gw["FUYAO_IMPORT_TEMPLATE"], gw["RUNTIME_MODE"]) == (RT, IMP, "fuyao")
    assert gw["FUYAO_IDLE_SECONDS"] == "900" and gw["FUYAO_API"] == "http://opensandbox.example:80", "集群现值保留"
    why = {(c.key, c.old, c.new, c.why) for c in r.changes}
    assert ("FUYAO_RUNTIME_TEMPLATE", "gsdb-runtime-v1-4-14", RT, "发版台管理") in why
    assert ("FUYAO_IMPORT_TEMPLATE", "gsdb-import-v1-4-14", IMP, "发版台管理") in why
    want = "%s/gaussdb-agent-runtime:%s@sha256:%s" % (REG, TAG, "a" * 64)
    for name in ("statesvc", "credproxy"):
        assert [c["image"] for c in rd._containers(_doc(r, "Deployment", name))] == [want], "集群服务跟着目标版本走"
    assert _doc(r, "NetworkPolicy", "gateway-to-sandboxes") and _doc(r, "Deployment", "gateway")
    assert not any(d["kind"] == "SandboxGroup" for d in r.docs), "沙箱组由平台管理员单独 apply"


def test_fuyao_render_in_another_namespace_moves_the_cluster_services_too():
    cms, nps = _fy_cluster()
    r = rd.render(K8S, {"registry.prefix": REG, "k8s.namespace": "agent-rel"}, TAG, cms, nps, EXISTING, REQ,
                  mode="fuyao", templates=e2b.templates_for(TAG))
    assert _doc(r, "Deployment", "statesvc")["metadata"]["namespace"] == "agent-rel"
    assert _doc(r, "RoleBinding", "credproxy")["subjects"][0]["namespace"] == "agent-rel"


def test_the_resource_config_cannot_switch_a_fuyao_cluster_to_another_mode():
    cms, nps = _fy_cluster()
    r = rd.render(K8S, {"registry.prefix": REG, "gateway.runtime_mode": "pool"}, TAG, cms, nps, EXISTING, REQ,
                  mode="fuyao", templates=e2b.templates_for(TAG))
    assert any("不切换运行模式" in p for p in r.problems)
    assert _doc(r, "ConfigMap", "gateway-config")["data"]["RUNTIME_MODE"] == "fuyao"


def test_a_version_without_the_fuyao_overlay_is_refused(tmp_path):
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    with pytest.raises(rd.RenderError, match="不支持 fuyao"):
        rd.render(tmp_path, {}, TAG, {}, {}, EXISTING, [], mode="fuyao", templates=e2b.templates_for(TAG))
    with pytest.raises(rd.RenderError, match="两种模板名"):
        rd.render(K8S, {}, TAG, {}, {}, EXISTING, [], mode="fuyao", templates={"runtime": RT})


def test_an_overlay_patch_other_than_configmap_data_is_refused(tmp_path):
    ov = tmp_path / "overlays" / "fuyao"
    ov.mkdir(parents=True)
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "kustomization.yaml").write_text("resources: [gw.yaml]\n", encoding="utf-8")
    (tmp_path / "base" / "gw.yaml").write_text("kind: Deployment\nmetadata: {name: gateway}\n", encoding="utf-8")
    (ov / "kustomization.yaml").write_text("resources: [../../base]\npatches: [{path: p.yaml}]\n", encoding="utf-8")
    (ov / "p.yaml").write_text("kind: Deployment\nmetadata: {name: gateway}\nspec: {replicas: 3}\n", encoding="utf-8")
    with pytest.raises(rd.RenderError, match="只认改 ConfigMap 的 data"):
        rd.load_overlay(tmp_path, "fuyao")


def test_the_config_step_writes_the_target_template_names(tmp_path, monkeypatch):
    cms, nps = _fy_cluster()
    kube = FyKube(cms=cms)
    kube.list = lambda kind, selector="", namespaced=True: [{"metadata": {"name": n}, "spec": s} for n, s in nps.items()] \
        if kind == "networkpolicy" else []
    monkeypatch.setattr(steps, "_pkg", lambda ctx: Package(tag=TAG, root=tmp_path, files={}))
    monkeypatch.setattr(steps, "_extract_k8s", lambda pkg, dest: K8S)
    store = Store(tmp_path / "e.db")
    store.save_config({"registry.prefix": REG})
    svc = steps.Services(store=store, kube=kube, releases=tmp_path, state=tmp_path)
    eng = Engine(store, {"config": steps.step_config}, services={"svc": svc}, sleep=lambda _s: None, poll_s=0.01)
    meta = {"kind": "upgrade", "to_v": "agent-v1.5.0", "to_tag": TAG, "mode": "fuyao", "required": REQ}
    rid = eng.start("upgrade", "agent-v1.4.14", "agent-v1.5.0", [{"id": "config", "subs": list("abcde")}], meta, None)
    eng.wait()
    assert store.run(rid)["status"] == "done", store.run(rid)["error"]
    rendered = store.kv_get("run:%d" % rid)["rendered"]
    gw = e2b.rendered_gateway_config(rendered)
    assert (gw["FUYAO_RUNTIME_TEMPLATE"], gw["FUYAO_IMPORT_TEMPLATE"]) == (RT, IMP)
    assert "Deployment/statesvc" in store.kv_get("run:%d" % rid)["objects"]
    assert any("k8s/overlays/fuyao" in x["text"] for x in store.logs(rid, 0))


# -- 扫描 ------------------------------------------------------------------------------------------

def _scan(tmp_path, kube):
    st = Store(tmp_path / "r.db")
    st.save_config({"smoke.user": "u9500", "registry.mode": "preloaded"})
    kube.objects.setdefault(("secret", "gateway-auth"), {"data": {"TRUST_SECRET": "dA==", "ADMIN_TOKEN": "YQ=="}})
    kube.objects.update({("secret", "grmp-token-u9500"): {}})
    (tmp_path / "rel").mkdir(exist_ok=True)
    return {i.id: i for i in scan.Scanner(store=st, kube=kube, releases=tmp_path / "rel").run()}


def test_the_fuyao_scan_checks_the_cluster_services_and_not_the_pool(tmp_path):
    items = _scan(tmp_path, FyKube())
    assert items["fuyao"].st == "ok" and "状态服务、凭据代理服务就绪" in items["fuyao"].msg
    assert items["fuyao"].addr == "http://opensandbox.example:80"
    assert items["model"].st == "warn" and "冒烟" in items["model"].msg and "池子" not in items["model"].msg
    assert items["grmp"].st == "warn" and items["nas"].st == "ok" and "状态服务" in items["nas"].msg


def test_the_fuyao_scan_names_what_is_missing(tmp_path):
    kube = FyKube(cms={"gateway-config": {"RUNTIME_MODE": "fuyao", "FUYAO_API": ""}})
    kube.objects[("deploy", "credproxy")] = _deploy("x", replicas=2, ready=1)
    del kube.objects[("deploy", "statesvc")]
    it = _scan(tmp_path, kube)["fuyao"]
    assert it.st == "bad" and "FUYAO_API 没填" in it.msg and "没有 Deployment statesvc" in it.msg and "credproxy 就绪 1/2" in it.msg


@pytest.mark.parametrize("data", [{"TRUST_SECRET": "dA=="}, {"TRUST_SECRET": "dA==", "ADMIN_TOKEN": ""}])
def test_the_fuyao_scan_flags_a_missing_admin_token_before_the_gateway_refuses_to_start(tmp_path, data):
    """1.5.0 起 Fuyao 模式的网关缺 ADMIN_TOKEN 不启动:扫描先报出来,别等到「网关滚动」一步超时才发现。"""
    kube = FyKube()
    kube.objects[("secret", "gateway-auth")] = {"data": data}
    it = _scan(tmp_path, kube)["fuyao"]
    assert it.st == "bad" and "ADMIN_TOKEN" in it.msg and "gateway-auth" in it.msg


def test_other_modes_have_no_fuyao_item(tmp_path):
    assert "fuyao" not in _scan(tmp_path, FyKube(mode="ondemand"))


# -- 冒烟 ------------------------------------------------------------------------------------------

TRUST = "t" * 24


class SlowGateway:
    """建沙箱时网关把请求挂住:前 hang 个带信任头的请求睡 hold 秒才回(客户端早已超时)。"""

    def __init__(self, hang=1, hold=1.0):
        self.hang, self.hold, self.seen = hang, hold, 0
        gw = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                if self.headers.get("X-Agent-Trust") != TRUST:
                    code, body = 403, b"{}"
                else:
                    gw.seen += 1
                    if gw.seen <= gw.hang:
                        time.sleep(gw.hold)
                    code, body = 200, b"[]"
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_a_request_held_while_the_sandbox_is_built_is_retried_not_fatal():
    g = SlowGateway(hang=1, hold=1.0)
    try:
        s = Smoke(host="127.0.0.1", port=g.srv.server_address[1], user="u9500", trust=TRUST, sleep=lambda _s: None,
                  request_timeout_s=0.3, ready_timeout_s=30)
        assert s.login().ok and g.seen == 2
    finally:
        g.close()


def test_a_gateway_that_never_answers_fails_the_login_with_a_reason():
    g = SlowGateway(hang=99, hold=1.0)
    try:
        s = Smoke(host="127.0.0.1", port=g.srv.server_address[1], user="u9500", trust=TRUST, sleep=lambda _s: None,
                  request_timeout_s=0.2, ready_timeout_s=0.5)
        c = s.login()
        assert not c.ok and "没有准备好" in c.detail
    finally:
        g.close()


class _Ctx:
    def __init__(self):
        self.logs = []

    def log(self, m, lv=""):
        self.logs.append((lv, m))


def test_fuyao_smoke_refreshes_the_smoke_user_and_waits_as_long_as_the_gateway_holds(tmp_path):
    calls = []

    class K(FyKube):
        def list(self, kind, selector="", namespaced=True):
            return [{"metadata": {"name": "gw-0"}, "status": {"containerStatuses": [{"ready": True}]}}]

        def exec(self, pod, container, argv, stdin="", timeout=60):
            a = json.loads(stdin)
            calls.append(a)
            if a["action"]:
                return Result(0, json.dumps({"code": 200, "body": {"deleted": True, "synced": True}}) + "\n", "")
            box = {"sandbox": "sbx-9", "user_id": "u9500", "kind": "runtime", "template": "gsdb-runtime-v1-4-14", "outdated": True}
            return Result(0, json.dumps({"code": 200, "body": {"sandboxes": [box], "templates": {}}}) + "\n", "")
    svc = steps.Services(store=Store(tmp_path / "e.db"), kube=K(), releases=tmp_path, state=tmp_path, sleep=lambda _s: None)
    ctx = _Ctx()
    waits = steps.fuyao_smoke_waits(ctx, svc, "u9500", {"FUYAO_WAIT_SECONDS": "120"})
    assert waits == {"ready_timeout_s": 360.0, "request_timeout_s": 150.0}
    assert {"action": "delete", "sandbox": "sbx-9", "force": True} in calls and "sbx-9" in ctx.logs[0][1], \
        "冒烟工号只有测试数据:回写没成也要删(显式 force),否则冒烟跑在旧模板上"

    class Down(K):
        def exec(self, pod, container, argv, stdin="", timeout=60):
            return Result(1, "", "boom")
    svc = steps.Services(store=svc.store, kube=Down(), releases=tmp_path, state=tmp_path, sleep=lambda _s: None)
    ctx = _Ctx()
    assert steps.fuyao_smoke_waits(ctx, svc, "u9500", {})["request_timeout_s"] == 120.0
    assert ctx.logs[0][0] == "w" and "旧模板" in ctx.logs[0][1], "清不掉只提醒,冒烟照跑"
