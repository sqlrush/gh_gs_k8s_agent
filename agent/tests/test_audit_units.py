"""执行人:容器里每个 Pod 一个人,GSDB_USER_ID 由平台注入;findings 信封与 health 报告都要能落到人。

单机沙箱没有这个变量:输出必须保持原样(信封里 user_id 是空串,报告里不多任何一行)。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from common import audit  # noqa: E402
from common.finding import Finding, Severity, findings_to_json  # noqa: E402
import report  # noqa: E402
from model import HealthEvidence  # noqa: E402


def test_actor_id_reads_env(monkeypatch):
    monkeypatch.setenv("GSDB_USER_ID", " u12345 ")
    assert audit.actor_id() == "u12345"


def test_actor_id_empty_when_unset(monkeypatch):
    monkeypatch.delenv("GSDB_USER_ID", raising=False)
    assert audit.actor_id() == ""


def _finding() -> Finding:
    return Finding(dimension="lock", code="LOCK_WAIT", severity=Severity.WARN, metric="waiters",
                   value="3", threshold="1", evidence="x")


def test_findings_json_carries_user_id(monkeypatch):
    monkeypatch.setenv("GSDB_USER_ID", "u12345")
    payload = json.loads(findings_to_json([_finding()], "lockwait"))
    assert payload["user_id"] == "u12345" and payload["skill"] == "lockwait"
    assert payload["findings"][0]["skill"] == "lockwait"


def test_findings_json_user_id_is_empty_string_when_unset(monkeypatch):
    monkeypatch.delenv("GSDB_USER_ID", raising=False)
    assert json.loads(findings_to_json([], "lockwait"))["user_id"] == ""


def test_health_report_names_the_actor_under_the_title(monkeypatch):
    monkeypatch.setenv("GSDB_USER_ID", "u12345")
    out = report.render_health(HealthEvidence(conn="og", target="10.0.0.5"))
    lines = out.splitlines()
    assert lines[0].startswith("# Health Evidence — og (10.0.0.5)")
    assert "执行人：u12345" in lines[1:3]


def test_health_report_has_no_actor_line_when_unset(monkeypatch):
    monkeypatch.delenv("GSDB_USER_ID", raising=False)
    out = report.render_health(HealthEvidence(conn="og"))
    assert "执行人" not in out
    assert out.startswith("# Health Evidence — og\n\n总体状态：")
