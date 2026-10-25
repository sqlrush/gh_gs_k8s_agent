"""按 uid 认出端口上的监听者(P0 摸底 S3:同 Pod 别的槽位能在空档抢占端口冒充本人)。"""
import pathlib
import socket
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import netcheck  # noqa: E402

TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1004 00000000:0000 0A 00000000:00000000 00:00000000 00000000  2000        0 111 1 0 100 0 0 10 0
   1: 0100007F:1005 00000000:0000 0A 00000000:00000000 00:00000000 00000000  2001        0 112 1 0 100 0 0 10 0
   2: 0100007F:1004 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 113 1 0 100 0 0 10 0
   3: garbage line
"""
TCP6 = """  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000000000000:1004 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  2003        0 114 1 0 100 0 0 10 0
"""


def test_parse_keeps_only_listening_sockets():
    got = netcheck.parse(TCP)
    assert [(x.port, x.uid, x.inode) for x in got] == [(4100, 2000, 111), (4101, 2001, 112)]


def test_judge_says_ok_down_or_who_is_squatting(tmp_path):
    t, t6 = tmp_path / "tcp", tmp_path / "tcp6"
    t.write_text(TCP)
    t6.write_text(TCP6)
    assert netcheck.verdict(4101, 2001, [str(t)]) == "ok"
    assert netcheck.verdict(4110, 2001, [str(t)]) == "down"
    assert netcheck.verdict(4100, 2000, [str(t)]) == "ok"
    # 同一个端口 IPv6 上还有 uid 2003 在听:哪怕本人也在,也是冒充
    assert netcheck.verdict(4100, 2000, [str(t), str(t6)]) == "foreign:2003"
    assert netcheck.verdict(4100, 2000, [str(t), str(tmp_path / "missing")]) == "ok"


def test_a_real_listener_is_seen_with_our_uid():
    import os
    if not pathlib.Path("/proc/net/tcp").exists():
        import pytest
        pytest.skip("没有 /proc/net/tcp(macOS)")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        assert netcheck.verdict(port, os.getuid()) == "ok"
        assert netcheck.verdict(port, os.getuid() + 1).startswith("foreign:")
    finally:
        s.close()
