"""scripts/build-e2b-template.py:构建并注册 Fuyao(E2B)模板(2026-11-17)。

要点:模板名由版本号派生;三个基础镜像同一仓库同一版本;镜像名取自 OCI 目录本身(与 oci-serve 同一段解析);
E2B 接口按「建模板 → 开始构建 → 轮询状态」走,失败与超时都明确退出;团队 API Key 只进请求头,不出现在输出里。
"""
import argparse
import importlib.util
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_e2b_template", _ROOT / "scripts" / "build-e2b-template.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

KEY = "team-key-for-tests"


def _args(**kw):
    base = dict(tag="agent-v1.5.0-oc1.18.27", registry="ghcr.io/org/", reserve_mb=6144, arch="arm64",
                cpu=2, memory=4096)
    base.update(kw)
    return argparse.Namespace(**base)


def test_template_names_follow_the_release_version():
    assert bt.template_name("runtime", "agent-v1.5.0-oc1.18.27") == "gsdb-runtime-v1-5-0"
    assert bt.template_name("kb-import", "agent-v1.5.0-oc1.18.27") == "gsdb-import-v1-5-0"
    assert bt.template_name("runtime", "Dev_Build.7") == "gsdb-runtime-dev-build-7"


def test_the_build_uses_the_three_images_of_one_release_and_exports_oci():
    cmd = bt.build_command("runtime-e2b", "/w/runtime.tar", _args(), "runtime")
    text = " ".join(cmd)
    for img in ("gaussdb-agent-runtime", "gaussdb-agent-kb-import", "gaussdb-agent-kbstd"):
        assert "ghcr.io/org/%s:agent-v1.5.0-oc1.18.27" % img in text, img
    assert "--target runtime-e2b" in text and "RESERVE_MB=6144" in text and "linux/arm64" in text
    assert "type=oci,dest=/w/runtime.tar" in text and cmd[-1] == str(bt.ROOT)
    assert "Dockerfile.e2b" in text


def test_the_served_reference_comes_from_the_oci_layout_itself(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "a" * 64, "size": 1,
         "annotations": {"io.containerd.image.name": "docker.io/library/gaussdb-agent-runtime-e2b:v9",
                         "org.opencontainers.image.ref.name": "v9"}}]}))
    assert bt.served_ref(str(tmp_path), "arm64") == "gaussdb-agent-runtime-e2b:v9"
    (tmp_path / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": []}))
    with pytest.raises(SystemExit):
        bt.served_ref(str(tmp_path), "arm64")


def test_a_prebuilt_package_with_both_template_images_serves_each_by_name_and_tag(tmp_path):
    ann = lambda n, tg: {"io.containerd.image.name": "docker.io/library/%s:%s" % (n, tg),       # noqa: E731
                         "org.opencontainers.image.ref.name": tg}
    (tmp_path / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + c * 64, "size": 1,
         "annotations": ann(n, tg)} for c, n, tg in (("a", "gaussdb-agent-runtime-e2b", "v9"),
                                                     ("b", "gaussdb-agent-kb-import-e2b", "v9"),
                                                     ("c", "gaussdb-agent-runtime-e2b", "v8"))]}))
    assert bt.prebuilt_layout(str(tmp_path), "unused") == str(tmp_path)
    assert bt.served_ref(str(tmp_path), "arm64", "gaussdb-agent-runtime-e2b", "v9") == "gaussdb-agent-runtime-e2b:v9"
    assert bt.served_ref(str(tmp_path), "arm64", "gaussdb-agent-kb-import-e2b", "v9") == \
        "gaussdb-agent-kb-import-e2b:v9"
    with pytest.raises(SystemExit):
        bt.served_ref(str(tmp_path), "arm64", "gaussdb-agent-runtime-e2b", "v7")


def test_a_prebuilt_package_for_another_arch_is_matched_with_its_arch_suffix(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "d" * 64, "size": 1,
         "annotations": {"io.containerd.image.name": "docker.io/library/gaussdb-agent-runtime-e2b:v9-amd64",
                         "org.opencontainers.image.ref.name": "v9-amd64"}}]}))
    a = _args(tag="v9", arch="amd64")
    assert bt.prebuilt_ref(str(tmp_path), a, "runtime") == "gaussdb-agent-runtime-e2b:v9-amd64"
    with pytest.raises(SystemExit):
        bt.prebuilt_ref(str(tmp_path), a, "kb-import")


class _FakeE2B:
    def __init__(self, statuses):
        self.calls, self.statuses = [], list(statuses)
        fake = self

        class H(BaseHTTPRequestHandler):
            def _reply(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.calls.append((method, self.path, body, self.headers.get("X-API-Key")))
                return body

            def do_POST(self):
                self._record("POST")
                if self.path == "/v3/templates":
                    return self._reply(202, {"templateID": "t1", "buildID": "b1"})
                return self._reply(202, {})

            def do_GET(self):
                self._record("GET")
                st = fake.statuses.pop(0) if len(fake.statuses) > 1 else fake.statuses[0]
                return self._reply(200, st)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def no_wait(monkeypatch):
    monkeypatch.setattr(bt.time, "sleep", lambda s: None)


def test_a_template_is_created_built_from_the_local_registry_and_waited_for(no_wait):
    fake = _FakeE2B([{"status": "building", "logs": ["a", "b"]}, {"status": "ready", "logs": []}])
    try:
        got = bt.E2B(fake.url, KEY).build("gsdb-runtime-v1-5-0", 2, 4096, "127.0.0.1:5050/img:v1")
    finally:
        fake.close()
    assert got["templateID"] == "t1" and got["buildID"] == "b1"
    (m1, p1, b1, k1), (m2, p2, b2, _k2) = fake.calls[:2]
    assert (m1, p1) == ("POST", "/v3/templates") and b1 == {"name": "gsdb-runtime-v1-5-0", "cpuCount": 2,
                                                              "memoryMB": 4096}
    assert (m2, p2, b2) == ("POST", "/v2/templates/t1/builds/b1", {"fromImage": "127.0.0.1:5050/img:v1"})
    assert all(c[3] == KEY for c in fake.calls), "团队 API Key 走请求头"
    assert "logsOffset=2" in fake.calls[3][1], "日志按偏移续读"


def test_a_failed_build_exits_with_the_platform_reason(no_wait):
    fake = _FakeE2B([{"status": "error", "reason": {"message": "pull failed"}}])
    try:
        with pytest.raises(SystemExit) as ei:
            bt.E2B(fake.url, KEY).build("gsdb-runtime-v1-5-0", 2, 4096, "127.0.0.1:5050/img:v1")
    finally:
        fake.close()
    assert "pull failed" in str(ei.value) and KEY not in str(ei.value)


def test_a_build_that_never_finishes_times_out(no_wait, monkeypatch):
    fake = _FakeE2B([{"status": "building"}])
    ticks = iter(range(0, 10000, 100))
    monkeypatch.setattr(bt.time, "time", lambda: float(next(ticks)))
    try:
        with pytest.raises(SystemExit) as ei:
            bt.E2B(fake.url, KEY).build("n", 2, 4096, "127.0.0.1:5050/img:v1", wait_s=300)
    finally:
        fake.close()
    assert "超时" in str(ei.value)


def test_main_reads_the_team_key_from_the_config_and_never_prints_it(tmp_path, monkeypatch, capsys):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"teamApiKey": KEY}))
    seen = []
    monkeypatch.setattr(bt, "build_oci", lambda target, out, a, kind: seen.append((target, kind)) or "img:v1")
    keys = []
    monkeypatch.setattr(bt, "serve_and_build",
                        lambda layout, ref, kind, a, api: keys.append(api.key) or {"name": bt.template_name(kind, a.tag)})
    assert bt.main(["--tag", "agent-v1.5.0-oc1.18.27", "--e2b-api", "http://127.0.0.1:1",
                    "--e2b-config", str(conf)]) == 0
    assert seen == [("runtime-e2b", "runtime"), ("kbimport-e2b", "kb-import")] and keys == [KEY, KEY]
    out = capsys.readouterr().out
    assert "gsdb-runtime-v1-5-0" in out and "gsdb-import-v1-5-0" in out and KEY not in out


# -- --template-name:同一版本改虚拟机规格时另起一个名字(2026-11-18) --------------------------------------------

def test_the_default_name_still_follows_the_version_and_an_explicit_name_overrides_it():
    assert bt.chosen_name("runtime", _args(template_name="")) == "gsdb-runtime-v1-5-0", "不给就照旧"
    assert bt.chosen_name("kb-import", _args()) == "gsdb-import-v1-5-0", "老调用方没有这个属性也照旧"
    assert bt.chosen_name("runtime", _args(template_name="gsdb-runtime-v1-5-0-c4m8")) == "gsdb-runtime-v1-5-0-c4m8"


@pytest.mark.parametrize("bad", ["Gsdb-Runtime", "gsdb_runtime", "-gsdb", "gsdb runtime", "a" * 64, "gsdb/x"])
def test_an_explicit_name_must_be_a_plain_template_name(bad, tmp_path):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"teamApiKey": KEY}))
    with pytest.raises(SystemExit) as ei:
        bt.main(["--tag", "agent-v1.5.0-oc1.18.27", "--kind", "runtime", "--template-name=" + bad,
                 "--e2b-api", "http://127.0.0.1:1", "--e2b-config", str(conf)])
    assert "--template-name" in str(ei.value)


def test_an_explicit_name_needs_a_single_kind(tmp_path, monkeypatch):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"teamApiKey": KEY}))
    called = []
    monkeypatch.setattr(bt, "serve_and_build", lambda *a: called.append(a))
    with pytest.raises(SystemExit) as ei:
        bt.main(["--tag", "agent-v1.5.0-oc1.18.27", "--template-name", "gsdb-runtime-x",
                 "--e2b-api", "http://127.0.0.1:1", "--e2b-config", str(conf)])
    assert "--kind" in str(ei.value) and not called, "一个名字给不了两种模板;在调平台之前就拒绝"


def test_main_registers_the_explicit_name_with_the_given_size(tmp_path, monkeypatch, capsys):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"teamApiKey": KEY}))
    monkeypatch.setattr(bt, "build_oci", lambda target, out, a, kind: "img:v1")
    built = []

    class Api:
        def __init__(self, base, key):
            pass

        def build(self, name, cpu, memory, image, wait_s=1800):
            built.append((name, cpu, memory))
            return {"name": name, "templateID": "t", "buildID": "b", "seconds": 1}
    monkeypatch.setattr(bt, "E2B", Api)
    monkeypatch.setattr(bt.subprocess, "Popen", lambda *a, **k: type("P", (), {"terminate": lambda s: None,
                                                                              "wait": lambda s, t=None: 0})())
    monkeypatch.setattr(bt.urllib.request, "urlopen", lambda *a, **k: type("R", (), {
        "__enter__": lambda s: s, "__exit__": lambda s, *e: False})())
    assert bt.main(["--tag", "agent-v1.5.0-oc1.18.27", "--kind", "runtime", "--cpu", "4", "--memory", "8192",
                    "--template-name", "gsdb-runtime-v1-5-0-c4m8", "--e2b-api", "http://127.0.0.1:1",
                    "--e2b-config", str(conf)]) == 0
    assert built == [("gsdb-runtime-v1-5-0-c4m8", 4, 8192)]
    assert "gsdb-runtime-v1-5-0-c4m8" in capsys.readouterr().out
