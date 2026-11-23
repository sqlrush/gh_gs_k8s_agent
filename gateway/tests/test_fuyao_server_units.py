"""网关 HTTP 层在 Fuyao 模式下(2026-11-17):导入环境也交给路由器、/admin/fuyao 运维接口。

复用固定池化 HTTP 层测试的替身(假 opencode、请求工具);路由器换成带 kind 参数、`handles_kb_import` 的替身。
"""
import dataclasses
import json
import pathlib
import sys
import threading

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import config as gwconfig  # noqa: E402
from gateway import pods, poolrouter, server  # noqa: E402
from gateway.tests.test_pods_units import FakeKube  # noqa: E402
from gateway.tests.test_server_pool_units import ADMIN, SECRET, _Opencode, _req, _serve  # noqa: E402


class SandboxRouter:
    handles_kb_import = True

    def __init__(self, oc_port, rep_port):
        self.calls, self.noted = [], []
        self.t = poolrouter.Target("sbx-1", 0, "127.0.0.1", oc_port, "pw-1", rep_port, "rt-1")

    def target(self, user, interactive=True, kind=pods.KIND_RUNTIME):
        self.calls.append((user, interactive, kind))
        return self.t

    def note_activity(self, user, kind=pods.KIND_RUNTIME):
        self.noted.append((user, kind))

    def forget(self, user, kind=None):
        pass


class Admin:
    def __init__(self):
        self.acts = []

    def summary(self):
        return {"mode": "fuyao", "sandboxes": [{"sandbox": "sbx-1", "user_id": "u1234"}]}

    def act(self, verb, sid, force=False):
        self.acts.append((verb, sid, force))
        return 200, {"sandbox": sid, verb: True}


@pytest.fixture()
def gw():
    oc, rep = _serve(_Opencode), _serve(_Opencode)
    cfg = dataclasses.replace(gwconfig.load({"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t",
                                             "GATEWAY_TRUST_SECRET": SECRET, "GATEWAY_ADMIN_TOKEN": ADMIN}), port=0)
    kube = FakeKube()
    kube.objects[("configmaps", "agent-roles")] = {"data": {"kb-admins": "u1234\n"}}
    mgr = pods.Manager(kube, namespace="ns", image_tag="t", pull_policy="IfNotPresent",
                       templates=_ROOT / "k8s" / "templates", runtime_on_demand=False)
    router = SandboxRouter(oc.server_address[1], rep.server_address[1])
    app = server.App(cfg, mgr, log=lambda m: None, pool=router)
    app.fuyao_admin = Admin()
    g = server.serve(app)
    threading.Thread(target=g.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % g.socket.getsockname()[1]
    yield base, router, app, kube
    for s in (g, oc, rep):
        s.shutdown()
        s.server_close()


def test_the_import_environment_goes_to_the_router_not_to_an_on_demand_pod(gw):
    base, router, _app, kube = gw
    code, _h, body = _req(base, "/session", headers={"Cookie": "%s=%s" % (server.TARGET_COOKIE, pods.KIND_KB_IMPORT)})
    assert code == 200 and json.loads(body)["auth"].startswith("Basic ")
    assert router.calls == [("u1234", True, pods.KIND_KB_IMPORT)]
    assert router.noted == [("u1234", pods.KIND_KB_IMPORT)]
    assert not [k for k in kube.objects if k[0] == "deployments"], "Fuyao 模式不建导入 Pod"


def test_the_diagnostic_environment_keeps_the_pool_call_shape(gw):
    base, router, *_ = gw
    code, *_ = _req(base, "/session")
    assert code == 200 and router.calls == [("u1234", True, pods.KIND_RUNTIME)]


def test_admin_fuyao_summary_and_actions(gw):
    base, _router, app, _kube = gw
    code, _h, body = _req(base, "/admin/fuyao", headers={"X-Admin-Token": ADMIN})
    assert code == 200 and json.loads(body)["mode"] == "fuyao"
    code, _h, body = _req(base, "/admin/fuyao/pause", headers={"X-Admin-Token": ADMIN}, method="POST")
    assert code == 400, "请求体没有 sandbox"
    import urllib.request
    r = urllib.request.Request(base + "/admin/fuyao/pause", data=json.dumps({"sandbox": "sbx-1"}).encode(),
                               headers={"X-Admin-Token": ADMIN, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=10) as resp:
        assert resp.status == 200 and json.loads(resp.read())["pause"] is True
    assert app.fuyao_admin.acts == [("pause", "sbx-1", False)], "没带 force 就是不强删"

    def post(path, doc):
        req = urllib.request.Request(base + path, data=json.dumps(doc).encode(), method="POST",
                                     headers={"X-Admin-Token": ADMIN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
    assert post("/admin/fuyao/delete", {"sandbox": "sbx-1", "force": True}) == 200
    assert app.fuyao_admin.acts[-1] == ("delete", "sbx-1", True)
    assert post("/admin/fuyao/delete", {"sandbox": "sbx-1", "force": "yes"}) == 400, "force 只认 true/false,不猜"
    assert len(app.fuyao_admin.acts) == 2
    code, *_ = _req(base, "/admin/pool", headers={"X-Admin-Token": ADMIN})
    assert code == 404, "不是固定池化"


def test_a_busy_platform_shows_the_full_waiting_page_not_an_error(gw):
    """平台满员时建沙箱回 5xx → 路由器抛 PoolWait(platform_busy):用户看到「人数已满」的等待页,不是「联系管理员」。"""
    base, router, _app, _kube = gw

    def busy(user, interactive=True, kind=pods.KIND_RUNTIME):
        raise poolrouter.PoolWait("platform_busy", 30)
    router.target = busy
    code, h, body = _req(base, "/session", headers={"Accept": "text/html"})
    assert code == 503 and h.get("Retry-After") == "30" and "人数已满" in body and "联系管理员" not in body
