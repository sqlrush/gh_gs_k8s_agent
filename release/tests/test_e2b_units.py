"""Fuyao 模式的两步(e2b.py):模板名与构建脚本同一规则、模板探测(探测脚本真跑,对着假 OpenSandbox)、沙箱换版。

假 OpenSandbox 用网关测试的那一个(gateway/tests/fake_fuyao.py,按平台实测行为写),这里只加两样:
不认识的模板建不出来、指定模板的沙箱删不掉。探测脚本经「假 kubectl exec」在本机子进程里跑,与网关容器里一样只读标准输入与环境变量。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway.tests.fake_fuyao import FakeOpenSandbox  # noqa: E402
from release import e2b, steps  # noqa: E402
from release.engine import Engine  # noqa: E402
from release.kube import Result  # noqa: E402
from release.store import Store  # noqa: E402

TAG = "agent-v1.5.0-oc1.18.27"
RT, IMP = "gsdb-runtime-v1-5-0", "gsdb-import-v1-5-0"
MARK = {e2b.PROBE_MARK[0]: e2b.PROBE_MARK[1]}
USER_BOX = {"gsdb-app": "gaussdb-agent", "gsdb-user": "u2001", "gsdb-kind": "runtime"}


def _build_script():
    spec = importlib.util.spec_from_file_location("build_e2b_template", str(_ROOT / "scripts" / "build-e2b-template.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- 模板名 ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tag", [TAG, "agent-v1.4.14-oc1.18.27", "agent-v2.0.0-oc1.20.1", "agent-v1.5.0-rc1",
                                 "Agent-V1.5.0_dev", "nightly.2026.11.17", "agent-v10.11.12-oc9"])
def test_template_names_follow_the_build_script(tag):
    """发版台镜像里没有 scripts/,规则另写了一份:两边对不上,网关就会去找一个没人建过的模板。"""
    script = _build_script()
    for kind in e2b.KINDS:
        assert e2b.template_name(kind, tag) == script.template_name(kind, tag), (kind, tag)
    assert set(e2b.KINDS) == set(script.KINDS), "两种模板的种类名要与构建脚本 --kind 的取值一致"


def test_template_kinds_are_the_gateways_kinds():
    from gateway.pods import KIND_KB_IMPORT, KIND_RUNTIME
    assert e2b.KINDS == (KIND_RUNTIME, KIND_KB_IMPORT), "/admin/fuyao 的 templates 按网关的种类名给"
    assert e2b.templates_for(TAG) == {"runtime": RT, "kb-import": IMP}


def test_the_build_hint_is_a_complete_command():
    one = e2b.build_hint(TAG, ["kb-import"], "amd64", "reg.example/agent")
    assert "python3 scripts/build-e2b-template.py --tag %s --arch amd64 --kind kb-import --registry reg.example/agent" % TAG in one
    assert "--from-oci <OCI 包>" in one and "模板构建服务所在主机" in one
    both = e2b.build_hint(TAG, list(e2b.KINDS), "arm64")
    assert "--kind" not in both and "--registry" not in both


# -- 替身 --------------------------------------------------------------------------------------------

class Platform(FakeOpenSandbox):
    """只认已注册的模板;undeletable 里的模板建出的沙箱删不掉(平台出错)。"""

    def __init__(self, templates=(RT, IMP), undeletable=(), capacity=None):
        super().__init__(capacity)
        self.templates, self.undeletable = set(templates), set(undeletable)

    def _create(self, body):
        uri = (body.get("image") or {}).get("uri")
        if uri not in self.templates:
            return 404, {"detail": {"message": "template %s not found" % uri}}
        return super()._create(body)

    def handle(self, method, path, query, body):
        parts = [p for p in path.split("/") if p]
        if method == "DELETE" and len(parts) == 3 and (self.boxes.get(parts[2]) or {}).get("template") in self.undeletable:
            return 500, {"detail": {"message": "delete failed"}}
        return super().handle(method, path, query, body)

    def probes(self):
        return [b for b in self.boxes.values() if b["metadata"].get(e2b.PROBE_MARK[0]) == e2b.PROBE_MARK[1]]


class GwKube:
    """一个就绪的网关副本;exec 进去:探测脚本在本机子进程里真跑(环境变量 = 网关容器的),/admin/fuyao 用 admin 回调答。"""
    namespace = "gaussdb-agent"

    def __init__(self, env=None, admin=None):
        self.env, self.admin, self.execs = dict(env or {}), admin, []

    def list(self, kind, selector="", namespaced=True):
        assert (kind, selector) == ("pods", "app=agent-gateway"), "只找网关,不看池子"
        return [{"metadata": {"name": "gw-0"}, "status": {"containerStatuses": [{"ready": True}]}}]

    def exec(self, pod, container, argv, stdin="", timeout=60):
        self.execs.append((argv[-1], json.loads(stdin)))
        if argv[-1] == e2b.ADMIN:
            return Result(0, json.dumps(self.admin(json.loads(stdin))) + "\n", "")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
        p = subprocess.run([sys.executable, "-c", argv[-1]], input=stdin, capture_output=True, text=True, timeout=60,
                           env=dict(env, **self.env))
        return Result(p.returncode, p.stdout, p.stderr)


def _gateway_config(api, **over):
    data = dict({"RUNTIME_MODE": "fuyao", "FUYAO_API": api, "FUYAO_RUNTIME_GROUP": "sg-gsdb-runtime",
                 "FUYAO_IMPORT_GROUP": "sg-gsdb-import", "FUYAO_CPU": "2", "FUYAO_MEMORY": "4Gi",
                 "FUYAO_RUNTIME_TEMPLATE": RT, "FUYAO_IMPORT_TEMPLATE": IMP}, **over)
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "gateway-config"}, "data": data}


def _run(tmp_path, kube, step, fn, gwcfg=None, resume_after=None):
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(yaml.safe_dump_all([gwcfg] if gwcfg else []), encoding="utf-8")
    store = Store(tmp_path / "e.db")
    svc = steps.Services(store=store, kube=kube, releases=tmp_path, state=tmp_path, sleep=lambda _s: None)
    eng = Engine(store, {"prep": lambda ctx: ctx.share(rendered=str(rendered)), step: fn}, services={"svc": svc},
                 sleep=lambda _s: None, poll_s=0.01)
    plan = [{"id": "prep", "subs": ["a"]}, {"id": step, "subs": ["a", "b", "c", "d"]}]
    rid = eng.start("upgrade", "agent-v1.4.14", "agent-v1.5.0", plan, {"to_tag": TAG, "arch": "arm64", "mode": "fuyao"}, None)
    eng.wait()
    if resume_after is not None and store.run(rid)["status"] == "failed":
        resume_after()
        eng.resume(rid)
        eng.wait()
    return store, rid


def _logs(store, rid):
    return " ".join(r["text"] for r in store.logs(rid, 0))


# -- 模板核对 ----------------------------------------------------------------------------------------

@pytest.fixture()
def platform():
    p = Platform()
    yield p
    p.close()


def test_both_templates_usable_and_every_probe_sandbox_is_gone(tmp_path, platform):
    platform.add(dict(MARK), template=RT)                     # 上次探测中断留下的
    user_box = platform.add(dict(USER_BOX), template="gsdb-runtime-v1-4-14")
    store, rid = _run(tmp_path, GwKube({"GATEWAY_FUYAO_API_KEY": "k-123"}), "template", e2b.step_template,
                      _gateway_config(platform.url))
    r = store.run(rid)
    assert r["status"] == "done", r["error"]
    assert list(platform.boxes) == [user_box], "探测沙箱(含上次留下的)全部删掉,用户的沙箱不动"
    creates = [(b, h) for m, p, b, h in platform.requests if m == "POST" and p == "/v1/sandboxes"]
    assert sorted(b["image"]["uri"] for b, _h in creates) == [IMP, RT]
    for body, headers in creates:
        assert "timeout" not in body, "不带 timeout:带了平台到点直接删且不能续期"
        assert body["metadata"] == MARK and body["entrypoint"] and body["resourceLimits"] == {"cpu": "2", "memory": "4Gi"}
        assert {k.lower(): v for k, v in headers.items()}.get("open-sandbox-api-key") == "k-123", "API Key 在网关容器的环境变量里读"
    assert {b["extensions"]["sandboxGroup"] for b, _h in creates} == {"sg-gsdb-runtime", "sg-gsdb-import"}
    subs = r["steps"][1]["subs"]
    assert [s["st"] for s in subs] == ["done"] * 4 and "已清理 1 台" in subs[3]["note"]
    assert store.kv_get("run:%d" % rid)["templates"]["swept"] == 1


def test_a_missing_template_fails_with_the_build_command_and_still_cleans_up(tmp_path):
    p = Platform(templates=(RT,))
    try:
        store, rid = _run(tmp_path, GwKube(), "template", e2b.step_template, _gateway_config(p.url))
        r = store.run(rid)
        assert r["status"] == "failed"
        assert IMP in r["error"] and "template %s not found" % IMP in _logs(store, rid)
        assert "python3 scripts/build-e2b-template.py --tag %s --arch arm64 --kind kb-import" % TAG in r["error"]
        assert "--from-oci" in r["error"] and "续跑" in r["error"]
        assert p.probes() == [] and p.boxes == {}, "失败了探测沙箱也要删干净"
        assert [s["st"] for s in r["steps"][1]["subs"]] == ["done", "done", "failed", "done"]
    finally:
        p.close()


def test_a_probe_sandbox_that_cannot_be_deleted_fails_the_step_and_a_rerun_sweeps_it(tmp_path):
    p = Platform(undeletable=(RT,))
    try:
        store, rid = _run(tmp_path, GwKube(), "template", e2b.step_template, _gateway_config(p.url),
                          resume_after=lambda: p.undeletable.clear())
        r = store.run(rid)
        assert r["status"] == "done", r["error"]
        assert "没删干净" in " ".join(x["text"] for x in store.logs(rid, 0) if x["level"] == "e")
        assert p.boxes == {}, "续跑这一步按探测标记把上次没删掉的清掉"
    finally:
        p.close()


def test_a_full_sandbox_group_is_not_mistaken_for_a_missing_template(tmp_path):
    p = Platform(capacity={"sg-gsdb-runtime": 0})
    try:
        store, rid = _run(tmp_path, GwKube(), "template", e2b.step_template, _gateway_config(p.url))
        r = store.run(rid)
        assert r["status"] == "failed" and "沙箱组没有空位" in r["error"] and "sg-gsdb-runtime" in r["error"]
        assert "build-e2b-template" not in r["error"] and p.boxes == {}
    finally:
        p.close()


def test_the_template_step_refuses_a_rendered_config_with_other_template_names(tmp_path, platform):
    store, rid = _run(tmp_path, GwKube(), "template", e2b.step_template,
                      _gateway_config(platform.url, FUYAO_IMPORT_TEMPLATE="gsdb-import-v1-4-14"))
    r = store.run(rid)
    assert r["status"] == "failed" and "FUYAO_IMPORT_TEMPLATE" in r["error"] and "配置迁移" in r["error"]
    assert platform.requests == [], "模板名不对就不去平台建沙箱"


def test_a_platform_that_is_down_is_not_mistaken_for_a_missing_template(tmp_path):
    store, rid = _run(tmp_path, GwKube(), "template", e2b.step_template, _gateway_config("http://127.0.0.1:9"))
    r = store.run(rid)
    assert r["status"] == "failed" and "连不上 OpenSandbox" in r["error"] and "http://127.0.0.1:9" in r["error"]
    assert "build-e2b-template" not in r["error"], "平台连不上不是模板没建,不让运维白跑一趟构建"
    assert r["steps"][1]["subs"][3]["st"] == "failed", "列举不了就核对不了删没删干净"


# -- 沙箱换版 ----------------------------------------------------------------------------------------

def _summary(templates, boxes):
    return {"code": 200, "body": {"mode": "fuyao", "templates": templates, "sandboxes": boxes,
                                  "outdated": sum(1 for b in boxes if b["outdated"]), "states": {"Running": len(boxes)}}}


OLD_BOXES = [{"sandbox": "sbx-1", "user_id": "u2001", "kind": "runtime", "state": "Running", "template": "gsdb-runtime-v1-4-14",
              "outdated": True},
             {"sandbox": "sbx-2", "user_id": "u9500", "kind": "runtime", "state": "Paused", "template": "gsdb-runtime-v1-4-14",
              "outdated": True},
             {"sandbox": "sbx-3", "user_id": "u9500", "kind": "kb-import", "state": "Running", "template": "gsdb-import-v1-4-14",
              "outdated": True},
             {"sandbox": "sbx-4", "user_id": "u3001", "kind": "runtime", "state": "Running", "template": RT, "outdated": False}]


def test_outdated_sandboxes_do_not_fail_the_swap_step(tmp_path):
    kube = GwKube(admin=lambda a: _summary({"runtime": RT, "kb-import": IMP}, OLD_BOXES))
    store, rid = _run(tmp_path, kube, "sandbox", e2b.step_sandbox)
    r = store.run(rid)
    assert r["status"] == "done", r["error"]
    assert r["steps"][1]["detail"] == "还有 3 台旧版沙箱,闲置后自动换新"
    assert store.kv_get("run:%d" % rid)["fuyao"]["outdated"] == 3
    assert all(a == {"action": "", "sandbox": ""} for _s, a in kube.execs), "换版只读进度,不删任何沙箱"


def test_the_swap_step_asks_again_while_an_old_gateway_replica_still_answers(tmp_path):
    seen = []

    def admin(_a):
        seen.append(1)
        return _summary({"runtime": RT if len(seen) > 2 else "gsdb-runtime-v1-4-14", "kb-import": IMP}, [])
    store, rid = _run(tmp_path, GwKube(admin=admin), "sandbox", e2b.step_sandbox)
    assert store.run(rid)["status"] == "done" and len(seen) == 3, "刚滚动完答话的可能是没退干净的旧副本:再问几次"
    assert store.run(rid)["steps"][1]["detail"] == "全部沙箱已是新模板(0 台)"


def test_a_gateway_that_never_picks_up_the_new_templates_fails_the_swap_step(tmp_path):
    kube = GwKube(admin=lambda a: _summary({"runtime": "gsdb-runtime-v1-4-14", "kb-import": IMP}, []))
    store, rid = _run(tmp_path, kube, "sandbox", e2b.step_sandbox)
    r = store.run(rid)
    assert r["status"] == "failed" and "还在用模板" in r["error"] and "FUYAO_RUNTIME_TEMPLATE" in r["error"]
    assert len(kube.execs) == e2b.SWAP_TRIES


def test_the_smoke_user_gets_a_new_template_sandbox_and_nobody_else_is_touched():
    deleted = []

    def admin(a):
        if a["action"]:
            deleted.append((a["action"], a["sandbox"], a.get("force")))
            return {"code": 200, "body": {"sandbox": a["sandbox"], "deleted": True, "synced": True}}
        return _summary({"runtime": RT, "kb-import": IMP}, OLD_BOXES)
    logs = []
    gone = e2b.refresh_smoke_user(GwKube(admin=admin), "u9500", lambda m, lv: logs.append(m), sleep=lambda _s: None)
    assert gone == ["sbx-2"] and deleted == [("delete", "sbx-2", True)], \
        "只动冒烟工号自己的旧版诊断沙箱(导入沙箱、别人的都不动);冒烟工号只有测试数据,回写没成也删(显式 force)"
    assert "u9500" in logs[0] and "sbx-2" in logs[0]


def test_admin_script_sends_force_only_when_asked(tmp_path):
    seen = []

    def admin(a):
        seen.append(a)
        return {"code": 409, "body": {"error": "收尾回写没成功,没有删除"}} if a["action"] else {"code": 200, "body": {}}
    adm = e2b.FuyaoAdmin(GwKube(admin=admin), sleep=lambda _s: None)
    with pytest.raises(e2b.pool.PoolError, match="没有删除"):
        adm.delete("sbx-9")
    adm.summary()
    assert seen[0] == {"action": "delete", "sandbox": "sbx-9"} and "force" not in seen[0], "默认不强删"
    assert seen[1] == {"action": "", "sandbox": ""}


@pytest.mark.parametrize("payload, posted", [
    ({"action": "delete", "sandbox": "sbx-1"}, {"sandbox": "sbx-1"}),
    ({"action": "delete", "sandbox": "sbx-1", "force": True}, {"sandbox": "sbx-1", "force": True}),
])
def test_the_admin_script_posts_force_to_the_gateway_only_when_asked(payload, posted):
    import http.server
    import threading
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = dict(os.environ, GATEWAY_LISTEN_PORT=str(srv.server_address[1]), GATEWAY_ADMIN_TOKEN="t")
        p = subprocess.run([sys.executable, "-c", e2b.ADMIN], input=json.dumps(payload), capture_output=True,
                           text=True, timeout=30, env=env)
    finally:
        srv.shutdown()
        srv.server_close()
    assert p.returncode == 0 and json.loads(p.stdout)["code"] == 200
    assert got == [("/admin/fuyao/delete", posted)]


def test_probe_and_admin_scripts_keep_secrets_inside_the_gateway_container(tmp_path, platform):
    assert "GATEWAY_FUYAO_API_KEY" in e2b.PROBE and "GATEWAY_ADMIN_TOKEN" in e2b.ADMIN
    kube = GwKube({"GATEWAY_FUYAO_API_KEY": "k-123"})
    _run(tmp_path, kube, "template", e2b.step_template, _gateway_config(platform.url))
    assert kube.execs and all("k-123" not in json.dumps(a) for _s, a in kube.execs), "密钥不经发版台、不进 exec 的参数"


def test_an_unreachable_gateway_is_a_clear_step_failure(tmp_path):
    class Down(GwKube):
        def exec(self, pod, container, argv, stdin="", timeout=60):
            return Result(1, "", "error: unable to upgrade connection")
    t0 = time.time()
    store, rid = _run(tmp_path, Down(), "sandbox", e2b.step_sandbox)
    r = store.run(rid)
    assert r["status"] == "failed" and "读网关的换版进度失败" in r["error"] and "unable to upgrade" in r["error"]
    assert time.time() - t0 < 10
