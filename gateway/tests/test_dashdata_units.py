"""资源大盘的数据汇总(gateway/dashdata.py)与运维上报(gateway/opsreport.py),2026-11-18。

要点:
- 每一块各自降级:K8s 读失败(403 → 「需授权只读」、没装 CRD / metrics-server → 如实写明)只影响那一块;
- 平台的沙箱按 ID 与网关摘要关联、按承载 Pod 归组;别人的沙箱不显示;诊断与导入可以共用一个组;某个组不存在只标那一组;
- 非 Fuyao 模式:组件照常,Fuyao 那几块标「当前不是 Fuyao 模式」;token 用量如实标不可用;
- 整体缓存 TTL 内只建一次;
- 上报:请求计数报失败并回去下一轮再报;签名能被状态服务那一侧的校验认出;清扫器每轮报运行 / 暂停台数。
"""
import dataclasses
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import config as gwconfig  # noqa: E402
from gateway import dashdata, opsreport, sbxtoken  # noqa: E402
from gateway.kube import KubeError  # noqa: E402

KEY = b"k" * 32
SBX = "/apis/sandbox.flux.io/v1alpha1/namespaces/flux-system"


class FakeApi:
    """path → 返回值;值是异常就抛。没登记的路径按 404。"""

    def __init__(self, routes):
        self.routes, self.seen = dict(routes), []

    def request(self, method, path, body=None, content_type=""):
        self.seen.append(path)
        got = self.routes.get(path)
        if got is None:
            raise KubeError("not found", 404)
        if isinstance(got, Exception):
            raise got
        return got


class Mgr:
    def __init__(self, kube):
        self.kube = kube


class Summary:
    def summary(self):
        return {"mode": "fuyao", "states": {"Running": 1, "Paused": 1},
                "sandboxes": [{"sandbox": "a1", "user_id": "u1001", "kind": "runtime", "state": "Running"},
                              {"sandbox": "b2", "user_id": "u2001", "kind": "kb-import", "state": "Paused"}]}


class App:
    def __init__(self, kube, mode="fuyao", runtime_group="sg-run", import_group="sg-imp"):
        env = {"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "v1", "GATEWAY_TRUST_SECRET": "s" * 32}
        self.cfg = dataclasses.replace(gwconfig.load(env), runtime_mode=mode, fuyao_runtime_group=runtime_group,
                                       fuyao_import_group=import_group)
        self.mgr, self.fuyao_admin, self.logs = Mgr(kube), Summary() if mode == "fuyao" else None, []

    def log(self, m):
        self.logs.append(m)


def _dep(name, labels, cpu="100m", mem="128Mi", replicas=2, ready=2):
    return {"metadata": {"name": name}, "spec": {"replicas": replicas, "selector": {"matchLabels": labels},
            "template": {"spec": {"containers": [{"image": "img/%s:v1" % name, "resources": {
                "requests": {"cpu": cpu, "memory": mem}, "limits": {"cpu": "1", "memory": "512Mi"}}}]}}},
            "status": {"readyReplicas": ready}}


def _sg(name, per_pod, pods_now, pmin, pmax, cpu="2", mem="4Gi"):
    return {"spec": {"capacity": {"agentPodMin": pmin, "agentPodMax": pmax, "maxSandboxesPerPod": per_pod},
                     "sandboxResources": {"requests": {"cpu": cpu, "memory": mem}, "limits": {"cpu": cpu, "memory": mem}}},
            "status": {"phase": "Ready", "totalAgentPods": pods_now, "totalCapacity": pods_now * per_pod}}


def _sbx(sid, group, pod, phase="Running"):
    return {"metadata": {"name": sid}, "spec": {"sandboxGroup": group},
            "status": {"sandboxID": sid, "phase": phase, "assignedSandboxAgentPod": pod}}


def _routes(**over):
    r = {
        "/apis/apps/v1/namespaces/ns/deployments": {"items": [
            _dep("gateway", {"app": "agent-gateway"}), _dep("statesvc", {"app": "statesvc"}, cpu="500m", mem="512Mi", replicas=1, ready=1),
            {"metadata": {"name": "runtime-u1001"}, "spec": {}, "status": {}}]},
        "/api/v1/namespaces/ns/pods": {"items": [
            {"metadata": {"name": "gateway-x", "labels": {"app": "agent-gateway"}},
             "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 1}]}}]},
        "/apis/batch/v1/namespaces/ns/jobs": {"items": [{"metadata": {"name": "kbstd-install-v1"},
                                                         "status": {"conditions": [{"type": "Complete", "status": "True"}]}}]},
        SBX + "/sandboxgroups/sg-run": _sg("sg-run", 10, 3, 1, 6, cpu="500m", mem="2Gi"),
        SBX + "/sandboxgroups/sg-imp": _sg("sg-imp", 4, 1, 1, 2),
        SBX + "/subsandboxgroups": {"items": [
            {"spec": {"ownerSandboxGroup": "sg-run"}, "status": {"readyAgentPods": 3, "desiredAgentPods": 3, "allocated": 1}},
            {"spec": {"ownerSandboxGroup": "sg-imp"}, "status": {"readyAgentPods": 1, "desiredAgentPods": 1, "allocated": 1}},
            {"spec": {"ownerSandboxGroup": "someone-else"}, "status": {"readyAgentPods": 5}}]},
        SBX + "/sandboxes": {"items": [_sbx("a1", "sg-run", "sandbox-agent-pod-sg-run-0-002"), _sbx("b2", "sg-imp", "sandbox-agent-pod-sg-imp-0-000", "Paused"),
                                       _sbx("zz", "sg-run", "sandbox-agent-pod-sg-run-0-002")]},
    }
    r.update(over)
    return r


class Ops:
    def __init__(self, fail=False):
        self.fail = fail

    def stats(self):
        if self.fail:
            raise opsreport.OpsError("连不上状态服务")
        return {"date": "2026-11-18", "requests": {"total": 3}, "trend": [], "nas": None}


def test_a_full_fuyao_snapshot_joins_platform_sandboxes_with_the_gateway_summary():
    api = FakeApi(_routes())
    doc = dashdata.Collector(App(api), ops=Ops()).build()
    comps = doc["components"]
    assert [c["name"] for c in comps["items"]] == ["gateway", "statesvc"] and comps["user_deployments"] == 1
    gw = comps["items"][0]
    assert gw["per_replica"]["requests"] == {"cpu": 0.1, "mem": 128.0} and gw["ready"] == 2
    assert gw["pods"] == [{"name": "gateway-x", "phase": "Running", "ready": True, "restarts": 1}]
    assert doc["jobs"]["items"][0]["status"] == "Complete"
    assert doc["metrics"] == {"available": False, "reason": "未装 metrics-server(集群没有 metrics.k8s.io),实际 CPU / 内存不可用"}
    plat = doc["platform"]
    run = [g for g in plat["groups"] if g["name"] == "sg-run"][0]
    assert (run["agent_pods"], run["per_pod"], run["capacity"], run["capacity_max"], run["pod_min"]) == (3, 10, 30, 60, 1)
    assert run["ready_agent_pods"] == 3 and run["kinds"] == ["runtime"]
    assert run["pods"] == [{"name": "sandbox-agent-pod-sg-run-0-002", "sandboxes": ["a1"]}], "别人的沙箱 zz 不显示"
    assert run["per_sandbox"]["requests"] == {"cpu": 0.5, "mem": 2048.0}
    assert run["agent_reserve"]["requests"] == {"cpu": 5.0, "mem": 20480.0}
    assert plat["sandboxes"]["b2"] == {"phase": "Paused", "agent_pod": "sandbox-agent-pod-sg-imp-0-000", "group": "sg-imp"}
    assert doc["ops"]["requests"]["total"] == 3 and doc["ops"]["available"] is True
    assert doc["tokens"]["available"] is False and "不做" in doc["tokens"]["reason"]
    assert doc["fuyao"]["available"] is True and len(doc["fuyao"]["sandboxes"]) == 2
    json.dumps(doc)


def test_capacity_and_agent_pods_come_from_the_subgroups_not_the_group_totals():
    """鲲鹏现场(2026-11-18):sg-gaussdb-poc 调过 agentPodMax=3、agentPodMin=2 后,沙箱组状态是
    totalAgentPods=3 / totalCapacity=6(跟着最大值走),而真正调度用的子组是 desired=2、ready=2、capacity=4、allocated=0
    (三台都暂停)。大盘按子组的算,否则把容量说大了。"""
    poc = _sg("sg-poc", 2, 3, 2, 3)
    api = FakeApi(_routes(**{
        SBX + "/sandboxgroups/sg-run": poc, SBX + "/sandboxgroups/sg-imp": poc,
        SBX + "/subsandboxgroups": {"items": [{"spec": {"ownerSandboxGroup": "sg-run"},
                                               "status": {"readyAgentPods": 2, "desiredAgentPods": 2, "capacity": 4,
                                                          "allocated": 0, "idle": 4}}]}}))
    run = [g for g in dashdata.Collector(App(api), ops=Ops()).build()["platform"]["groups"] if g["name"] == "sg-run"][0]
    assert (run["agent_pods"], run["ready_agent_pods"], run["capacity"], run["allocated"]) == (2, 2, 4, 0)
    assert (run["pod_min"], run["pod_max"], run["capacity_max"]) == (2, 3, 6)


def test_without_platform_read_permission_only_the_platform_block_is_marked():
    api = FakeApi(_routes(**{SBX + "/subsandboxgroups": KubeError("forbidden", 403)}))
    app = App(api)
    doc = dashdata.Collector(app, ops=Ops()).build()
    assert doc["platform"]["available"] is False and doc["platform"]["reason"].startswith("需授权只读")
    assert doc["components"]["available"] is True and doc["fuyao"]["available"] is True and doc["ops"]["available"] is True
    assert any("sandbox.flux.io" in m for m in app.logs), "日志里写清哪一块、为什么"


def test_a_missing_crd_or_metrics_server_or_job_permission_is_stated_not_invented():
    api = FakeApi(_routes(**{SBX + "/subsandboxgroups": None, "/apis/batch/v1/namespaces/ns/jobs": KubeError("forbidden", 403),
                             "/apis/metrics.k8s.io/v1beta1/namespaces/ns/pods": KubeError("forbidden", 403)}))
    doc = dashdata.Collector(App(api), ops=Ops()).build()
    assert doc["platform"]["available"] is False and "不存在" in doc["platform"]["reason"]
    assert doc["jobs"]["reason"].startswith("需授权只读") and doc["metrics"]["reason"].startswith("需授权只读")


def test_metrics_are_used_when_the_cluster_has_them():
    api = FakeApi(_routes(**{"/apis/metrics.k8s.io/v1beta1/namespaces/ns/pods": {"items": [
        {"metadata": {"name": "gateway-x"}, "containers": [{"usage": {"cpu": "12500000n", "memory": "90Mi"}}]}]}}))
    doc = dashdata.Collector(App(api), ops=Ops()).build()
    assert doc["metrics"] == {"available": True, "pods": {"gateway-x": {"cpu": 0.0125, "mem": 90.0}}}


def test_one_group_shared_by_both_environments_and_a_missing_group():
    api = FakeApi(_routes())
    doc = dashdata.Collector(App(api, runtime_group="sg-run", import_group="sg-run"), ops=Ops()).build()
    groups = doc["platform"]["groups"]
    assert len(groups) == 1 and groups[0]["kinds"] == ["runtime", "kb-import"]
    doc = dashdata.Collector(App(api, import_group="sg-gone"), ops=Ops()).build()
    gone = [g for g in doc["platform"]["groups"] if g["name"] == "sg-gone"][0]
    assert gone["missing"] is True and "sg-gone" in gone["reason"]
    assert doc["platform"]["available"] is True, "缺一个组不影响另一个组"


def test_state_service_failure_marks_only_the_ops_block():
    doc = dashdata.Collector(App(FakeApi(_routes())), ops=Ops(fail=True)).build()
    assert doc["ops"]["available"] is False and doc["platform"]["available"] is True


def test_outside_fuyao_mode_components_still_show_and_fuyao_blocks_say_why():
    doc = dashdata.Collector(App(FakeApi(_routes()), mode="pool")).build()
    assert doc["components"]["available"] is True
    for k in ("fuyao", "platform", "ops"):
        assert doc[k] == {"available": False, "reason": "当前不是 Fuyao 模式(运行模式 pool)"}


def test_the_snapshot_is_cached_and_built_once_per_ttl():
    api = FakeApi(_routes())
    clock = {"t": 1000.0}
    c = dashdata.Collector(App(api), ops=Ops(), ttl_s=20, clock=lambda: clock["t"])
    c.get()
    n = len(api.seen)
    c.get()
    clock["t"] += 19
    c.get()
    assert len(api.seen) == n, "20 秒内不再打 K8s API"
    clock["t"] += 2
    c.get()
    assert len(api.seen) == 2 * n


def test_quantities_parse_like_kubernetes():
    assert dashdata.cpu_cores("2048m") == 2.048 and dashdata.cpu_cores("4") == 4.0 and dashdata.cpu_cores("x") is None
    assert dashdata.mem_mib("4Gi") == 4096 and dashdata.mem_mib("4294967296") == 4096 and dashdata.mem_mib("512Mi") == 512


# -- 上报 -------------------------------------------------------------------------------------------

class _OpsServer(BaseHTTPRequestHandler):
    got, fail = [], False

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        ok = sbxtoken.ops_verify(KEY, self.headers.get(sbxtoken.OPS_HEADER), self.command, self.path, body)
        type(self).got.append((self.path, ok, json.loads(body) if body else None))
        code = 503 if type(self).fail else 200 if ok else 401
        data = json.dumps({"ok": ok}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _handle

    def log_message(self, *a):
        pass


@pytest.fixture()
def ops_srv():
    _OpsServer.got, _OpsServer.fail = [], False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _OpsServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield opsreport.StateOps("http://127.0.0.1:%d" % srv.server_address[1], KEY)
    srv.shutdown()
    srv.server_close()


def test_reports_are_signed_so_the_state_service_accepts_them(ops_srv):
    ops_srv.report_requests({("u1001", "runtime"): 2})
    ops_srv.report_fleet(3, 1)
    ops_srv.stats()
    assert [(p, ok) for p, ok, _b in _OpsServer.got] == [("/v1/ops/requests", True), ("/v1/ops/fleet", True), ("/v1/ops/stats", True)]
    assert _OpsServer.got[0][2] == {"items": [{"u": "u1001", "k": "runtime", "n": 2}]}


def test_a_failed_report_is_kept_and_sent_with_the_next_round(ops_srv):
    counter, logs = opsreport.RequestCounter(), []
    rep = opsreport.Reporter(counter, ops_srv, log=logs.append)
    counter.hit("u1001", "runtime")
    _OpsServer.fail = True
    assert rep.flush() is False and rep.flush() is False
    assert len([m for m in logs if "报不出去" in m]) == 1, "同样的失败只记一次"
    counter.hit("u1001", "runtime")
    _OpsServer.fail = False
    assert rep.flush() is True
    assert _OpsServer.got[-1][2] == {"items": [{"u": "u1001", "k": "runtime", "n": 2}]}, "失败的那次并进下一轮"
    assert rep.flush() is True and len(_OpsServer.got) == 3, "没有新计数不发请求"


def test_the_sweeper_reports_running_and_paused_counts_each_round():
    from gateway import fuyaosweeper as fs
    from gateway.tests.test_fuyao_router_units import POLICY
    from gateway import fuyaorouter as fr
    from gateway.tests.fake_fuyao import FakeEnvd, FakeOpenSandbox

    fake = FakeOpenSandbox()
    try:
        from gateway import opensandbox as osb
        client = osb.Client(fake.url)
        router = fr.FuyaoRouter(client, FakeEnvd(), POLICY, KEY, password_for=lambda u: "pw",
                                launch_env=lambda u, k: {}, log=lambda m: None)
        fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "u1001", "gsdb-kind": "runtime", "gsdb-template": "tpl-run"})
        fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "u1002", "gsdb-kind": "runtime", "gsdb-template": "tpl-run",
                  "gsdb-paused": "9999999999", "gsdb-synced": "1"}, state="Paused")
        got = []
        sw = fs.FuyaoSweeper(router, busy_check=lambda h, p: True, log=lambda m: None, report=lambda r, p: got.append((r, p)))
        sw.sweep()
        assert got == [(1, 1)]
        sw.report = lambda r, p: (_ for _ in ()).throw(RuntimeError("状态服务挂了"))
        sw.sweep()                                  # 上报出错不影响清扫
    finally:
        fake.close()
