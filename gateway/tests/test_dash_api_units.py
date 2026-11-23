"""大盘的数据接口(/dash/api/*,资源大盘,2026-11-18)与运维管理员角色。

要点:
- /dash/api/* 由网关自己答,不转前端、不建 / 不唤醒沙箱、不记活动;身份不可信照常拒;
- /dash/api/me 给工号、两种管理员身份、产品名;/dash/api/resources 只给运维管理员,别人 403;
- 运维管理员 = 角色头里带 OPS_ADMIN_ROLES 之一,或工号登记在 agent-roles 的 ops-admins(与 kb-admins 同一套读法);
- 请求计数只记交互请求(与记活动同一个口径),探活与大盘静态页不算;
- 产品名可配,默认「GaussDB 智能体」,不合法的值拒绝启动。
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
from gateway import opsreport, pods, server  # noqa: E402
from gateway.kube import KubeError  # noqa: E402
from gateway.tests.test_fuyao_server_units import Admin, SandboxRouter  # noqa: E402
from gateway.tests.test_pods_units import FakeKube  # noqa: E402
from gateway.tests.test_server_pool_units import ADMIN, SECRET, _Opencode, _req, _serve  # noqa: E402

BASE_ENV = {"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t", "GATEWAY_TRUST_SECRET": SECRET, "GATEWAY_ADMIN_TOKEN": ADMIN}


class Dash:
    def __init__(self):
        self.calls = 0

    def get(self):
        self.calls += 1
        return {"mode": "fuyao", "components": {"available": True, "items": []}}


@pytest.fixture()
def gw():
    oc, rep, fe = _serve(_Opencode), _serve(_Opencode), _serve(_Opencode)
    cfg = dataclasses.replace(gwconfig.load(dict(BASE_ENV, GATEWAY_PRODUCT_NAME="某某数据库智能体")), port=0)
    kube = FakeKube()
    kube.objects[("configmaps", "agent-roles")] = {"data": {"kb-admins": "u2001\n", "ops-admins": "u9001  # 运维\n"}}
    mgr = pods.Manager(kube, namespace="ns", image_tag="t", pull_policy="IfNotPresent",
                       templates=_ROOT / "k8s" / "templates", runtime_on_demand=False)
    router = SandboxRouter(oc.server_address[1], rep.server_address[1])
    app = server.App(cfg, mgr, log=lambda m: None, pool=router)
    app.fuyao_admin, app.dash, app.counter = Admin(), Dash(), opsreport.RequestCounter()
    g = server.serve(app)
    threading.Thread(target=g.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % g.socket.getsockname()[1], router, app, kube
    for s in (g, oc, rep, fe):
        s.shutdown()
        s.server_close()


def test_me_tells_who_i_am_my_roles_and_the_product_name(gw):
    base, router, _app, _kube = gw
    code, h, body = _req(base, "/dash/api/me", user="u9001")
    got = json.loads(body)
    assert code == 200 and h.get("Cache-Control") == "no-store"
    assert got == {"user_id": "u9001", "ops_admin": True, "kb_admin": False, "product_name": "某某数据库智能体", "mode": "ondemand",
                   "chat_home": server.LANDING}, "chat_home:本模式下对话页的落地路径(Fuyao 模式的工作目录不同)"
    got = json.loads(_req(base, "/dash/api/me", user="u2001")[2])
    assert got["kb_admin"] is True and got["ops_admin"] is False
    assert router.calls == [], "不建、不唤醒沙箱"


def test_resources_are_only_for_ops_admins(gw):
    base, _router, app, _kube = gw
    code, _h, body = _req(base, "/dash/api/resources", user="u1234")
    assert code == 403 and "运维管理员" in json.loads(body)["error"] and app.dash.calls == 0
    assert _req(base, "/dash/api/resources", user="u2001")[0] == 403, "知识库管理员不是运维管理员"
    code, _h, body = _req(base, "/dash/api/resources", user="u9001")
    assert code == 200 and json.loads(body)["mode"] == "fuyao"
    code, *_ = _req(base, "/dash/api/resources", user="u5555", headers={"X-Agent-Roles": "ops-admin"})
    assert code == 200, "角色头也算"


def test_dash_api_needs_a_trusted_identity_like_everything_else(gw):
    base, *_ = gw
    import urllib.error
    import urllib.request
    for path in ("/dash/api/me", "/dash/api/resources"):
        r = urllib.request.Request(base + path, headers={"X-Agent-User": "u9001", "X-Agent-Trust": "wrong" * 8})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(r, timeout=10)
        assert e.value.code == 403


def test_dash_api_is_answered_by_the_gateway_and_never_counted_or_routed(gw):
    base, router, app, _kube = gw
    for path in ("/dash/api/me", "/dash/api/resources", "/dash/api/nope"):
        _req(base, path, user="u9001")
    assert router.calls == [] and router.noted == [] and app.counter.drain() == {}
    assert _req(base, "/dash/api/nope", user="u9001")[0] == 404
    assert _req(base, "/dash/api/me", user="u9001", method="POST")[0] == 405
    app.dash = None
    assert _req(base, "/dash/api/resources", user="u9001")[0] == 503


def test_interactive_requests_are_counted_per_user_and_environment_probes_are_not(gw):
    base, router, app, _kube = gw
    _req(base, "/session")
    _req(base, "/session")
    _req(base, "/global/health")                        # 探活:不算
    _req(base, "/session", headers={"Cookie": "%s=%s" % (server.TARGET_COOKIE, pods.KIND_KB_IMPORT)}, user="u2001")
    assert app.counter.drain() == {("u1234", "runtime"): 2, ("u2001", "kb-import"): 1}


def test_ops_admins_come_from_the_same_roles_table_and_survive_a_read_failure():
    kube = FakeKube()
    kube.objects[("configmaps", "agent-roles")] = {"data": {"kb-admins": "u2001", "ops-admins": "U9001\n# u9002\nu9003 # 值班"}}
    mgr = pods.Manager(kube, namespace="ns", image_tag="t", pull_policy="IfNotPresent", templates=_ROOT / "k8s" / "templates",
                       log=lambda m: None)
    assert mgr.ops_admins(now=0) == {"u9001", "u9003"} and mgr.kb_admins(now=1) == {"u2001"}
    reads = []
    real_get = kube.get

    def boom(api, kind, name):
        reads.append(name)
        raise KubeError("k8s 一时不可达", 503)
    kube.get = boom
    assert mgr.ops_admins(now=5) == {"u9001", "u9003"} and reads == [], "30 秒内用缓存"
    assert mgr.ops_admins(now=100) == {"u9001", "u9003"} and reads == ["agent-roles"], "读失败沿用上一次"
    kube.get = real_get
    kube.objects[("configmaps", "agent-roles")] = {"data": {"kb-admins": "u2001"}}
    assert mgr.ops_admins(now=200) == frozenset(), "表里没有这个键 = 没有运维管理员"


def test_ops_admin_roles_and_product_name_are_configurable_and_validated():
    cfg = gwconfig.load(BASE_ENV)
    assert cfg.ops_admin_roles == ("ops-admin",) and cfg.product_name == "GaussDB 智能体"
    cfg = gwconfig.load(dict(BASE_ENV, GATEWAY_OPS_ADMIN_ROLES="SRE, dba-lead", GATEWAY_PRODUCT_NAME="  数据库运维助手 "))
    assert cfg.ops_admin_roles == ("sre", "dba-lead") and cfg.product_name == "数据库运维助手"
    for bad in ("<script>", "a" * 41, "x\ny"):
        with pytest.raises(gwconfig.ConfigError):
            gwconfig.load(dict(BASE_ENV, GATEWAY_PRODUCT_NAME=bad))
