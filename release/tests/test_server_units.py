"""接口:配置校验、遮住的敏感值保留、密钥不进记录、跨站与非 JSON 写拒绝、静态页不越界、没扫描不能开跑。"""
from __future__ import annotations

import http.client
import json
import pathlib
import sys
import threading

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import app as appmod  # noqa: E402
from release import server  # noqa: E402
from release.store import Store  # noqa: E402


class FakeKube:
    namespace = "gaussdb-agent"

    def __init__(self):
        self.secrets = {}

    def get(self, kind, name, namespaced=True):
        return None

    def list(self, kind, selector="", namespaced=True):
        return []

    def apply_secret(self, name, data, labels=None):
        self.secrets[name] = data


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    kube = FakeKube()
    monkeypatch.setattr(appmod.App, "kube", lambda self: kube)
    (tmp_path / "rel").mkdir()
    a = appmod.App(Store(tmp_path / "s" / "release.db"), tmp_path / "rel", tmp_path / "s")
    s = server.serve(a, "127.0.0.1", 0, ready=lambda _m: None)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield a, s.server_address[1], kube
    s.shutdown()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = dict({"Content-Type": "application/json"} if body is not None else {}, **(headers or {}))
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data.decode("utf-8", "replace")


def test_config_is_validated_and_the_masked_prefix_survives_a_round_trip(srv):
    a, port, _k = srv
    assert call(port, "PUT", "/api/config", {"config": {"nope": 1}})[0] == 400
    assert call(port, "PUT", "/api/config", {"config": {"registry.mode": "ftp"}})[0] == 400
    assert call(port, "PUT", "/api/config", {"config": {"grmp.path_prefix": "/real/p", "pool.pods": "3"}})[0] == 200
    code, view = call(port, "GET", "/api/config")
    assert view["config"]["grmp.path_prefix"] == appmod.MASK and view["config"]["pool.pods"] == 3
    call(port, "PUT", "/api/config", {"config": view["config"]})
    assert a.store.config()["grmp.path_prefix"] == "/real/p", "界面把遮住的值原样交回来时不能覆盖成占位符"
    assert "/real/p" not in json.dumps(a.store.audits(), ensure_ascii=False)


def test_secrets_are_write_only(srv):
    a, port, kube = srv
    code, out = call(port, "POST", "/api/secrets/model_api_key", {"value": "sk-abcdef-123"})
    assert code == 200 and out["written_to"] == "" and len(out["fingerprint"]) == 12
    view = json.dumps(call(port, "GET", "/api/config")[1]) + json.dumps(call(port, "GET", "/api/audit")[1])
    assert "sk-abcdef-123" not in view
    assert call(port, "POST", "/api/secrets/root_password", {"value": "xxxxxxx"})[0] == 400
    call(port, "POST", "/api/secrets/whitelist_password", {"value": "wl-pass-789"})
    notes = {x["target"]: x["detail"] for x in a.store.audits()}
    assert notes["model_api_key"].startswith("首次部署时写入集群") and notes["whitelist_password"].startswith("发版台自己用"), \
        "白名单只读账号的口令不进集群,操作记录不能说「写入集群」"


def test_cross_site_and_non_json_writes_are_refused(srv):
    _a, port, _k = srv
    assert call(port, "PUT", "/api/config", {"config": {}}, {"Origin": "http://evil.example"})[0] == 403
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/api/scan", body="tag=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert c.getresponse().status == 415


def test_static_files_cannot_escape_the_web_dir(srv):
    _a, port, _k = srv
    assert call(port, "GET", "/web/../app.py")[0] == 404
    assert call(port, "GET", "/web/%2e%2e/store.py")[0] == 404


def test_no_release_without_a_fresh_passing_scan(srv):
    _a, port, _k = srv
    code, out = call(port, "POST", "/api/runs", {"tag": "agent-v1.4.4-oc1.18.27"})
    assert code == 409 and "资源扫描" in out["error"]


def test_whitelist_export_is_checked_before_it_is_kept(srv):
    a, port, _k = srv
    assert call(port, "POST", "/api/whitelist/export", {"csv": "name,sum\n"})[0] == 400
    ok = "script_name,content_md5,parameter_config,is_valid\na.b,0123,[],1\n"
    assert call(port, "POST", "/api/whitelist/export", {"csv": ok}) == (200, {"rows": 1})
    assert a.store.kv_get("whitelist.export") == ok


def test_unknown_api_and_bad_gate_choice(srv):
    _a, port, _k = srv
    assert call(port, "GET", "/api/nope")[0] == 404
    assert call(port, "POST", "/api/runs/1/gate", {"choice": "drop"})[0] == 400
