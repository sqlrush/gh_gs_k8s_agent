"""网关问状态服务「沙箱现在在哪」(gateway/__main__.state_locator,2026-11-17):对着真的状态服务。

平台恢复暂停的沙箱后 /endpoints 回旧地址(端到端实测),网关改用状态服务最近看到的来源地址;太久没见到的不信。
"""
import pathlib
import sys
import threading
import time

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "docker"))

import remotesync as rs  # noqa: E402
import statesvc  # noqa: E402
from gateway import __main__ as gwmain  # noqa: E402
from gateway import sbxtoken  # noqa: E402

KEY = b"k" * 32


def test_the_locator_returns_a_fresh_address_and_ignores_stale_or_unknown_ones(tmp_path):
    clock = {"t": time.time()}
    svc = statesvc.Service(str(tmp_path), KEY, log=lambda m: None, clock=lambda: clock["t"])
    srv = statesvc.ThreadingHTTPServer(("127.0.0.1", 0), statesvc.handler(svc))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        locate = gwmain.state_locator(url, KEY)
        assert locate("sbx-1", "u1001", "runtime") is None, "没见过"
        rs.lease(rs.Client(url, sbxtoken.issue(KEY, "sbx-1", "u1001", "runtime")), "acquire")
        addr, age = locate("sbx-1", "u1001", "runtime")
        assert addr == "127.0.0.1" and 0 <= age < 2, "连距今秒数一起给:路由器据此判断是不是恢复后才报上来的"
        clock["t"] += gwmain.LOCATE_FRESH_S + 1
        assert locate("sbx-1", "u1001", "runtime") is None, "太久没见到:不信,退回平台给的地址"
    finally:
        srv.shutdown()
        srv.server_close()
