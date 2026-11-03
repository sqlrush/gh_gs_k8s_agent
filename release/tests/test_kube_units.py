"""kubectl 封装:参数、错误、密钥只走标准输入、diff 退出码、port-forward。"""
from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release.kube import Kube, KubeError  # noqa: E402


class FakeRunner:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])

    def __call__(self, cmd, input="", capture_output=True, text=True, timeout=60):
        self.calls.append((cmd, input))
        code, out, err = self.replies.pop(0) if self.replies else (0, "", "")
        if code == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, code, out, err)


def _kube(replies=None, **kw):
    r = FakeRunner(replies)
    return Kube(namespace="gaussdb-agent", kubeconfig="/kube/config", context="prod", runner=r, **kw), r


def test_every_call_carries_kubeconfig_context_and_namespace():
    k, r = _kube([(0, json.dumps({"items": [{"metadata": {"name": "n1"}}]}), "")])
    assert k.list("nodes", namespaced=False)[0]["metadata"]["name"] == "n1"
    assert r.calls[0][0] == ["kubectl", "--kubeconfig", "/kube/config", "--context", "prod", "get", "nodes", "-o", "json"]
    k.run(["get", "pods"])
    assert r.calls[1][0][5:7] == ["-n", "gaussdb-agent"]


def test_get_returns_none_for_not_found_and_raises_otherwise():
    k, _r = _kube([(1, "", 'Error from server (NotFound): deployments.apps "gateway" not found'),
                   (1, "", "error: You must be logged in to the server (Unauthorized)")])
    assert k.get("deploy", "gateway") is None
    with pytest.raises(KubeError, match="Unauthorized"):
        k.get("deploy", "gateway")


def test_secret_values_go_through_stdin_only():
    k, r = _kube()
    k.apply_secret("model-api", {"MODEL_API_KEY": "sk-very-secret"})
    cmd, stdin = r.calls[0]
    assert "sk-very-secret" not in " ".join(cmd) and "sk-very-secret" in stdin
    assert "sk-very-secret" not in repr(k.commands), "「执行的命令」面板只显示参数"
    assert "stringData" in stdin and "namespace: gaussdb-agent" in stdin


def test_diff_exit_one_means_changes_not_failure():
    k, _r = _kube([(1, "-  image: a\n+  image: b\n", ""), (0, "", ""), (2, "", "error: unable to recognize")])
    assert "+  image: b" in k.diff("x")
    assert k.diff("x") == ""
    with pytest.raises(KubeError, match="变更预览失败"):
        k.diff("x")


def test_exec_and_timeouts():
    k, r = _kube([(0, "ok", ""), ("timeout", "", "")])
    assert k.exec("gateway-1", "", ["python3", "-"], stdin="print(1)").out == "ok"
    assert r.calls[0][0][-6:] == ["exec", "-i", "gateway-1", "--", "python3", "-"]
    with pytest.raises(KubeError, match="没有返回"):
        k.exec("p", "c", ["true"])


def test_port_forward_reads_the_local_port_and_stops_the_process():
    class P:
        def __init__(self, *a, **kw):
            self.stdout = io.StringIO("Forwarding from 127.0.0.1:43210 -> 8080\n")
            self.stderr = io.StringIO("")
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0
    made = []

    def popen(*a, **kw):
        made.append(P())
        return made[-1]
    k, _r = _kube()
    with k.port_forward("svc/gateway", 80, popen=popen) as port:
        assert port == 43210
    assert made[0].terminated


def test_a_port_forward_that_never_comes_up_is_an_error():
    class P:
        def __init__(self):
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("error: services \"gateway\" not found\n")

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1
    k, _r = _kube()
    with pytest.raises(KubeError, match="not found"):
        with k.port_forward("svc/gateway", 80, popen=lambda *a, **kw: P(), wait_s=1):
            pass


def test_version_reads_both_minors_and_tolerates_a_missing_server():
    both = {"clientVersion": {"major": "1", "minor": "31"}, "serverVersion": {"major": "1", "minor": "27+"}}
    k, r = _kube([(0, json.dumps(both), ""), (1, json.dumps({"clientVersion": {"minor": "31"}}), "connection refused"),
                  (1, "", "boom")])
    assert k.version() == {"client": 31, "server": 27}, "托管集群的 27+ 也认"
    assert r.calls[0][0][-3:] == ["version", "-o", "json"] and "-n" not in r.calls[0][0]
    assert k.version() == {"client": 31}, "连不上集群:只有 client,不报错"
    assert k.version() == {}
