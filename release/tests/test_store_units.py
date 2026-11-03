"""状态库:配置版本、密钥只留指纹 / 不回显、脱敏、发布与关卡跨重启。"""
from __future__ import annotations

import pathlib
import stat
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release.store import MASK, Store  # noqa: E402

PLAN = [{"id": "pkg", "subs": ["发现", "校验"]}, {"id": "kbimport", "skip": True}, {"id": "review"}]


@pytest.fixture()
def path(tmp_path):
    return tmp_path / "state" / "release.db"


def test_the_database_is_private_to_the_tool(path):
    Store(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_each_save_is_a_config_version(path):
    s = Store(path)
    v1 = s.save_config({"registry": "a"}, "首次")
    v2 = s.save_config({"registry": "b"}, "改仓库")
    assert s.config() == {"registry": "b"} and s.config_version(v1) == {"registry": "a"}
    assert [v["id"] for v in s.config_versions()] == [v2, v1]


def test_cluster_secrets_keep_only_a_fingerprint_and_tool_secrets_keep_the_value(path):
    s = Store(path)
    s.set_secret("model_api_key", "sk-cluster-only-123", keep=False)
    s.set_secret("registry_password", "reg-pass-456", keep=True)
    assert s.secret("model_api_key") == "" and s.secret("registry_password") == "reg-pass-456"
    marks = s.secret_marks()
    assert set(marks["model_api_key"]) == {"fingerprint", "set_at", "kept"}
    assert "sk-cluster-only-123" not in repr(marks)
    raw = path.read_bytes() + (path.parent / (path.name + "-wal")).read_bytes()
    assert b"sk-cluster-only-123" not in raw, "写进集群的密钥不在发版台落盘"


def test_every_known_secret_is_masked_in_logs_audit_and_errors(path):
    s = Store(path)
    s.set_secret("model_api_key", "sk-cluster-only-123", keep=False)
    s.remember_secret("trust-from-cluster-789")
    rid = s.create_run("upgrade", "agent-v1.4.3", "agent-v1.4.4", PLAN, None)
    s.log(rid, "pkg", "", "curl -H 'Authorization: sk-cluster-only-123' -H 'X-Agent-Trust: trust-from-cluster-789'")
    s.audit("更换密钥", "模型 API Key", "旧值 sk-cluster-only-123")
    s.update_step(rid, 0, error="401 with sk-cluster-only-123")
    text = repr(s.logs(rid)) + repr(s.audits()) + repr(s.run(rid))
    assert "sk-cluster-only-123" not in text and "trust-from-cluster-789" not in text and MASK in text


def test_runs_steps_logs_and_paging(path):
    s = Store(path)
    rid = s.create_run("upgrade", "agent-v1.4.3", "agent-v1.4.4", PLAN, 3)
    r = s.run(rid)
    assert [st["status"] for st in r["steps"]] == ["pending", "skipped", "pending"]
    assert r["steps"][0]["subs"] == [{"label": "发现", "st": "pending", "note": ""}, {"label": "校验", "st": "pending", "note": ""}]
    seqs = [s.log(rid, "pkg", "", "第 %d 行" % i) for i in range(5)]
    assert [l["text"] for l in s.logs(rid, after=seqs[2])] == ["第 3 行", "第 4 行"]
    assert s.logs(rid, step="review") == []


def test_a_restart_pauses_the_running_release_and_keeps_its_gate(path):
    s = Store(path)
    rid = s.create_run("upgrade", "agent-v1.4.3", "agent-v1.4.4", PLAN, None)
    s.update_run(rid, status="running")
    s.update_step(rid, 0, status="running")
    gid = s.add_gate(rid, "review", "review", {"objects": 3})
    s.close()
    s2 = Store(path)
    assert s2.mark_interrupted() == [rid]
    r = s2.run(rid)
    assert r["status"] == "paused" and r["steps"][0]["status"] == "interrupted"
    assert r["gate"]["id"] == gid and r["gate"]["payload"] == {"objects": 3}
    s2.resolve_gate(gid, "ok")
    assert s2.open_gate(rid) is None and s2.gate(gid)["choice"] == "ok"
    assert s2.active_run() == rid
