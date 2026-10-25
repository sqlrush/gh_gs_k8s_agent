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
def test_network_failures_become_grmp_error_naming_the_address(monkeypatch, exc):
    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(GrmpError) as info:
        _client().list_operations()
    msg = str(info.value)
    assert "grmp-new.internal:8080" in msg and "失败" in msg


def test_http_error_still_carries_status_and_address(monkeypatch):
    def boom(*_a, **_k):
        raise urllib.error.HTTPError(BASE + "/x", 503, "Service Unavailable", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(GrmpError) as info:
        _client().list_operations()
    msg = str(info.value)
    assert "HTTP 503" in msg and "grmp-new.internal:8080" in msg
