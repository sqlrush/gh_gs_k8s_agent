"""GRMP 客户端的网络层失败:任何连不上中间件的情况都要变成带地址的 GrmpError,不能是 Traceback。

现场(09-14)中间件换了地址后,老会话打旧地址;排查时最需要的信息就是「实际打的是哪个地址」,
而 v12.9 的报错只写 path,连接被对端关掉(http.client.RemoteDisconnected)时干脆没接住。
"""
import http.client
import pathlib
import socket
import sys
import urllib.error
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp.client import GrmpClient, GrmpError  # noqa: E402

BASE = "http://grmp-new.internal:8080"


def _client() -> GrmpClient:
    return GrmpClient(BASE, token="t", data_ip="10.0.0.5", timeout=1)


@pytest.mark.parametrize("exc", [
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    ConnectionRefusedError(61, "Connection refused"),
    socket.timeout("timed out"),
    urllib.error.URLError("[Errno 8] nodename nor servname provided"),
])
def test_network_failures_hide_the_address_but_log_it(monkeypatch, tmp_path, exc):
    """给用户看的报错不带中间件地址(红线第 5 条,11-12 行方要求界面不出报错原文);地址进本机诊断日志,
    09-14 现场「中间件换地址后老会话打旧地址」照样能按详情编号查到。"""
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(GrmpError) as info:
        _client().list_operations()
    msg = str(info.value)
    assert "grmp-new.internal" not in msg and "调用中间件失败" in msg and "详情编号 E-" in msg
    log = (tmp_path / "logs" / "grmp-errors.log").read_text(encoding="utf-8")
    assert "grmp-new.internal:8080" in log and msg.split("详情编号 ")[1][:8] in log


def test_http_error_carries_status_not_address(monkeypatch):
    def boom(*_a, **_k):
        raise urllib.error.HTTPError(BASE + "/x", 503, "Service Unavailable", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(GrmpError) as info:
        _client().list_operations()
    msg = str(info.value)
    assert "HTTP 503" in msg and "grmp-new.internal" not in msg


def _refused(*_a, **_k):
    raise ConnectionRefusedError(61, "Connection refused")


def test_the_field_layout_without_gsdb_home_still_logs_the_raw_error(monkeypatch, tmp_path):
    """2026-11-12 回归:行方技能包不设 GSDB_HOME,状态就在技能目录的 common/ 里(默认状态目录);
    原先此时不写日志,报错既没有详情编号、原文也没处可查。"""
    from common import config
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.delenv("GDAA_HOME", raising=False)
    monkeypatch.setattr(config, "_DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", _refused)
    with pytest.raises(GrmpError) as info:
        _client().list_operations()
    msg = str(info.value)
    assert "详情编号 E-" in msg and str(tmp_path / "logs" / "grmp-errors.log") in msg
    assert msg.split("详情编号 ")[1][:8] in (tmp_path / "logs" / "grmp-errors.log").read_text(encoding="utf-8")


def test_a_dev_machine_without_any_state_dir_writes_nothing(monkeypatch, tmp_path):
    from common import config
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.delenv("GDAA_HOME", raising=False)
    monkeypatch.setattr(config, "_DEFAULT_STATE_DIR", tmp_path / "absent")
    monkeypatch.setattr(urllib.request, "urlopen", _refused)
    with pytest.raises(GrmpError) as info:
        _client().list_operations()
    assert "详情编号" not in str(info.value) and not (tmp_path / "absent").exists()
