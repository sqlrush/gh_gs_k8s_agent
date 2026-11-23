"""网关 → 状态服务的运维上报(资源大盘,2026-11-18)。

- RequestCounter:代理入口每个**交互**请求(与记活动同一个口径:不含探活、事件流、大盘静态页)按「工号 × 环境」+1;
- Reporter:每 30 秒把攒下的增量报给状态服务(/v1/ops/requests)。报失败就并回去,下一轮一起报 ——
  只攒一份,不会无限涨(工号数有上限);网关重启时没报出去的那不到 30 秒的计数会丢,大盘上看到的是「约数」。
- StateOps:带运维签名(sbxtoken.ops_sign,控制密钥派生的另一把子密钥)调状态服务的三个统计接口。
  这几个接口只认这个签名,沙箱令牌调不通(docker/statesvc.py)。
"""
from __future__ import annotations

import http.client
import json
import threading
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from . import sbxtoken

REPORT_EVERY_S = 30
MAX_KEYS = 20000


class OpsError(Exception):
    pass


class RequestCounter:
    def __init__(self):
        self._lock = threading.Lock()
        self._n: Dict[Tuple[str, str], int] = {}

    def hit(self, user_id: str, kind: str) -> None:
        with self._lock:
            k = (user_id, kind)
            if k in self._n or len(self._n) < MAX_KEYS:
                self._n[k] = self._n.get(k, 0) + 1

    def drain(self) -> Dict[Tuple[str, str], int]:
        with self._lock:
            got, self._n = self._n, {}
        return got

    def restore(self, got: Dict[Tuple[str, str], int]) -> None:
        with self._lock:
            for k, n in got.items():
                if k in self._n or len(self._n) < MAX_KEYS:
                    self._n[k] = self._n.get(k, 0) + n


class StateOps:
    """状态服务的统计接口客户端。base_url 用集群域名即可(网关所在的 Pod 能解析)。"""

    def __init__(self, base_url: str, key: bytes, timeout: float = 5.0):
        u = urlsplit(base_url)
        if u.scheme != "http" or not u.hostname:
            raise ValueError("状态服务地址要是 http://主机:端口:%r" % base_url)
        self.host, self.port, self.key, self.timeout = u.hostname, u.port or 80, key, timeout

    def _call(self, method: str, path: str, doc: Optional[dict] = None) -> dict:
        body = json.dumps(doc, separators=(",", ":")).encode("utf-8") if doc is not None else b""
        headers = {sbxtoken.OPS_HEADER: sbxtoken.ops_sign(self.key, method, path, body)}
        if doc is not None:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, path, body=body if doc is not None else None, headers=headers)
            r = conn.getresponse()
            raw = r.read()
        except OSError as exc:
            raise OpsError("连不上状态服务:%s" % exc)
        finally:
            conn.close()
        if r.status != 200:
            raise OpsError("状态服务 %s %s → %d" % (method, path, r.status))
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise OpsError("状态服务回的不是 JSON")

    def report_requests(self, counts: Dict[Tuple[str, str], int]) -> dict:
        items = [{"u": u, "k": k, "n": n} for (u, k), n in sorted(counts.items()) if n > 0]
        return self._call("POST", "/v1/ops/requests", {"items": items})

    def report_fleet(self, running: int, paused: int) -> dict:
        return self._call("POST", "/v1/ops/fleet", {"running": int(running), "paused": int(paused)})

    def stats(self) -> dict:
        return self._call("GET", "/v1/ops/stats")


class Reporter:
    def __init__(self, counter: RequestCounter, ops: StateOps, every_s: int = REPORT_EVERY_S,
                 log: Callable[[str], None] = print):
        self.counter, self.ops, self.every_s, self.log = counter, ops, every_s, log
        self._stop = threading.Event()
        self._failing = False

    def flush(self) -> bool:
        got = self.counter.drain()
        if not got:
            return True
        try:
            self.ops.report_requests(got)
        except OpsError as exc:
            self.counter.restore(got)
            if not self._failing:
                self.log("运维上报:请求计数报不出去,下一轮再报(之后同样的失败不再重复记):%s" % exc)
            self._failing = True
            return False
        if self._failing:
            self.log("运维上报:恢复正常")
        self._failing = False
        return True

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.every_s):
                try:
                    self.flush()
                except Exception as exc:                # noqa: BLE001 —— 上报线程不能因为一次出错就停
                    self.log("运维上报:这一轮出错:%s" % exc)
        threading.Thread(target=loop, name="opsreport", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.flush()
        except Exception:                               # noqa: BLE001
            pass


def fleet_reporter(ops: StateOps, log: Callable[[str], None] = print):
    """给清扫器用的回调:每轮报一次运行 / 暂停台数。报失败只记一次日志(清扫器每 30 秒一轮,不刷屏)。"""
    state = {"failing": False}

    def report(running: int, paused: int) -> None:
        try:
            ops.report_fleet(running, paused)
        except OpsError as exc:
            if not state["failing"]:
                log("运维上报:沙箱台数报不出去:%s" % exc)
            state["failing"] = True
            return
        state["failing"] = False
    return report
