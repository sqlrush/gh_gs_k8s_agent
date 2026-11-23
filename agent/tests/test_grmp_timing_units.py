"""GRMP 调用耗时记录(v1.4.14,common/grmp/timing.py):给交互耗时报告拆出「GRMP 与数据库」的时间。

要守住的:成功、失败都记;只记时间与计数,参数值 / 结果 / 地址 / 令牌一样都不进日志;没有状态目录时不往别处落文件;
写日志出任何问题都不影响取数;日志有上限。
"""
import json
import pathlib
import sys
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import timing  # noqa: E402
from common.grmp.client import GrmpClient, GrmpError  # noqa: E402

BASE = "http://grmp-secret-host.internal:8080"
SECRET_PARAM = "orders_2026_secret"


def _client() -> GrmpClient:
    return GrmpClient(BASE, token="tok-very-secret", data_ip="10.0.0.5", timeout=1)


def _lines(home: pathlib.Path):
    path = home / "logs" / "grmp-timing.log"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _fake_post(responses):
    calls = []

    def post(self, path, payload):
        calls.append((path, payload))
        return responses[len(calls) - 1]
    return post, calls


LIST_OK = {"code": "0", "result": {"list": [{"cmd_name": "vacuum.dead_tuples", "id": "id-1", "param": []}],
                                   "hasNextPage": False}}
INVOKE_OK = {"status": "finished", "result": {"type": "array", "data": [{"a": "1"}, {"a": "2"}, {"a": "3"}]}}


def test_list_and_invoke_are_both_timed(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    post, _ = _fake_post([LIST_OK, INVOKE_OK])
    monkeypatch.setattr(GrmpClient, "_post", post)
    rows = _client().invoke("vacuum.dead_tuples", {"limit": 20, "table": SECRET_PARAM})
    assert len(rows) == 3
    recs = _lines(tmp_path)
    assert [r["kind"] for r in recs] == [timing.LIST, timing.INVOKE]
    inv = recs[1]
    assert inv["script"] == "vacuum.dead_tuples" and inv["ok"] is True and inv["rows"] == 3 and inv["bytes"] > 0
    assert inv["ms"] >= 0 and inv["ts"] > 1.7e9 and inv["pid"] > 0 and inv["status"] == "finished"
    assert recs[0]["rows"] == 1 and recs[0]["ok"] is True


def test_db_name_is_recorded_when_routed(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    post, _ = _fake_post([LIST_OK, INVOKE_OK])
    monkeypatch.setattr(GrmpClient, "_post", post)
    _client().invoke("vacuum.dead_tuples", {"dbName": "appdb", "limit": 5})
    assert _lines(tmp_path)[-1]["db"] == "appdb"


def test_nothing_sensitive_reaches_the_log(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    post, _ = _fake_post([LIST_OK, INVOKE_OK])
    monkeypatch.setattr(GrmpClient, "_post", post)
    _client().invoke("vacuum.dead_tuples", {"limit": 20, "table": SECRET_PARAM})
    text = (tmp_path / "logs" / "grmp-timing.log").read_text(encoding="utf-8")
    for leak in (SECRET_PARAM, "grmp-secret-host", "tok-very-secret", "10.0.0.5", '"a": "1"'):
        assert leak not in text, leak


def test_failures_are_timed_and_still_raised(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))

    def refused(*_a, **_k):
        raise ConnectionRefusedError(61, "Connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    with pytest.raises(GrmpError):
        _client().list_operations()
    rec = _lines(tmp_path)[-1]
    assert rec["kind"] == timing.LIST and rec["ok"] is False and rec["error"] == "GrmpError"


def test_unfinished_invoke_is_recorded_as_not_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    post, _ = _fake_post([LIST_OK, {"status": "failed", "msg": "boom"}])
    monkeypatch.setattr(GrmpClient, "_post", post)
    with pytest.raises(GrmpError):
        _client().invoke("vacuum.dead_tuples", {"limit": 1})
    rec = _lines(tmp_path)[-1]
    assert rec["kind"] == timing.INVOKE and rec["ok"] is False and rec["status"] == "failed"


def test_no_state_dir_means_no_file_anywhere(monkeypatch, tmp_path):
    """开发机 / 单测上默认状态目录不存在:不往别处落文件(与 grmp-errors.log 同一规则)。"""
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.delenv("GDAA_HOME", raising=False)
    monkeypatch.setattr(timing, "state_dir", lambda: tmp_path / "absent")
    assert timing.call(lambda: INVOKE_OK, timing.INVOKE, "x.y") is INVOKE_OK
    assert not (tmp_path / "absent").exists()


def test_log_write_failure_never_breaks_the_query(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "grmp-timing.log").mkdir()          # 日志路径被一个目录占了:写必然失败
    post, _ = _fake_post([LIST_OK, INVOKE_OK])
    monkeypatch.setattr(GrmpClient, "_post", post)
    assert len(_client().invoke("vacuum.dead_tuples", {"limit": 3})) == 3


def test_log_rolls_over_at_the_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    monkeypatch.setattr(timing, "_MAX", 200)
    for _ in range(10):
        timing.call(lambda: INVOKE_OK, timing.INVOKE, "x.y")
    logs = tmp_path / "logs"
    assert (logs / "grmp-timing.log.1").exists()
    assert (logs / "grmp-timing.log").stat().st_size <= 200 + 400
    assert sorted(p.name for p in logs.iterdir()) == ["grmp-timing.log", "grmp-timing.log.1"]


def test_caller_names_the_skill_script(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/root/.config/opencode/skills/gaussdb-vacuum/scripts/vacuum.py", "--session", "h"])
    assert timing._caller() == "gaussdb-vacuum/vacuum.py"
    monkeypatch.setattr(sys, "argv", ["/usr/bin/python3"])
    assert timing._caller() == "python3"
