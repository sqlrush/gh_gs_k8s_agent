"""反向代理:把请求转给 runtime-<工号>:4096,顺手注入 Basic 口令并记活动。

三条硬约束,都是查出来的:

① **绝不缓冲。** opencode 的事件流是 SSE(`text/event-stream`),每 10 秒一跳心跳。
   任何一层把响应体攒起来再发,页面就一直转圈、消息迟迟不出来 —— 表现不像"断了",
   像"卡住了",最难排查。所以响应按块转发、每块 flush。
② **不设响应读超时。** SSE 是永不结束的连接。给它设读超时等于定期斩断它;
   前端会自动重连(带 Last-Event-ID,3 秒起退避),但重连瞬间的状态残留正是
   "已中断"的土壤(2026-09-21 查过)。连接超时该设,响应读取不设。
③ **活动时间在转发字节时更新,不是按请求计数。** 用户开着页面发呆时只有心跳,
   没有新请求;按请求计数判闲置会把正在用的人的 Pod 收掉。

口令只在网关内存里,从 Secret 读出来后注入 Authorization 头;客户的 SSO 层
永远看不到它,少一个泄露面。
"""
from __future__ import annotations

import base64
import http.client
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# 逐跳头:按 RFC 7230 不能转发。Content-Length 也要去掉 —— 我们按块转发,长度会变。
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
})
# 网关自己用的头,不往后端传。
_GATEWAY_ONLY = frozenset({"x-agent-trust", "authorization", "x-agent-target"})

CHUNK = 8192
UPSTREAM_USERNAME = "opencode"


def basic_auth(password: str, username: str = UPSTREAM_USERNAME) -> str:
    return "Basic " + base64.b64encode(("%s:%s" % (username, password)).encode("utf-8")).decode("ascii")


def forward_headers(incoming: Iterable[Tuple[str, str]], password: Optional[str],
                    user_id: str) -> List[Tuple[str, str]]:
    """过滤逐跳头与网关自用头,注入 Basic 口令与 X-Forwarded-User。

    password 为 None 时**不注入** Authorization:报告只读端口与大盘前端都没有口令,
    给它们发 opencode 的口令等于把口令递给不需要它的一方。
    """
    out = []
    for k, v in incoming:
        lk = k.lower()
        if lk in _HOP_BY_HOP or lk in _GATEWAY_ONLY:
            continue
        out.append((k, v))
    if password is not None:
        out.append(("Authorization", basic_auth(password)))
    # 后端(opencode)不认这个头,但它会进后端的访问日志,排查时能对上人。
    out.append(("X-Forwarded-User", user_id))
    return out


def response_headers(raw: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """回给客户端的头:同样去掉逐跳头。`x-accel-buffering: no` 若后端带了就保留 ——
    再往外还可能有一层客户的 nginx,那一层要靠它关缓冲。"""
    return [(k, v) for k, v in raw if k.lower() not in _HOP_BY_HOP]


CONNECT_RETRIES = 4
CONNECT_BACKOFF = 1.0


class UpstreamClosed(Exception):
    """上游在流的中途断开(opencode 被停掉 / 重启 / 槽位转保留)。与「客户端走了」分开:
    事件流还能给客户端一个干净的结尾,普通响应则要让客户端看出它被截断了。"""


class Upstream:
    """一次转发。connect_timeout 有值,**读响应不设超时**(SSE 永不结束)。"""

    retries = CONNECT_RETRIES

    def __init__(self, host: str, port: int = 4096, connect_timeout: float = 10.0, retries: int = CONNECT_RETRIES):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.retries = retries              # 固定池化直连 Pod IP,没有 DNS 生效的延迟,只试一次

    def send(self, method: str, path: str, headers: List[Tuple[str, str]],
             body: Optional[bytes]) -> http.client.HTTPResponse:
        """**第一次连接要重试。**

        Pod 报就绪(availableReplicas=1)和「Service 名字能解析、kube-proxy 规则已下发」
        不是同一时刻。新用户第一次进来时,Deployment 可能 3 秒就可用,而 CoreDNS 还没
        收到新 Service —— 这时直接回 502,用户看到的是「无法连接到您的环境」,再刷一次
        就好了。与其让用户去刷,不如在这里等几秒(2026-09-21 真跑时就是这么 502 的)。
        只对**连接阶段**重试:请求已经发出去就不能重发(可能不是幂等的)。
        """
        last = None
        for attempt in range(self.retries):
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.connect_timeout)
            try:
                conn.connect()
            except OSError as exc:
                last = exc
                conn.close()
                if attempt == self.retries - 1:
                    break
                time.sleep(CONNECT_BACKOFF * (attempt + 1))
                continue
            # **连上就把超时摘掉,而不是拿到响应头之后。** HTTPConnection 的 timeout 同时管
            # 读响应:同步的 POST /session/{id}/message 要等模型跑完才回响应头,10 秒一到
            # getresponse() 就 timed out → 502,而 Pod 里的活其实照跑(2026-09-22 e2e 抓到)。
            # connect_timeout 只该管连接;之后读的可能是一条几分钟才回的同步请求,或挂几小时的 SSE。
            if conn.sock is not None:
                conn.sock.settimeout(None)
            conn.request(method, path, body=body, headers=dict(headers))
            return conn.getresponse()
        raise OSError("连不上 %s:%s(重试 %d 次):%s" % (self.host, self.port, self.retries, last))


def pump(resp: http.client.HTTPResponse, write: Callable[[bytes], None],
         flush: Callable[[], None], on_bytes: Optional[Callable[[int], None]] = None) -> int:
    """按块转发响应体,**每块都 flush**。返回转发的字节数。

    两处都必须对,少一处 SSE 就废:

    ① **必须用 read1 而不是 read。** `http.client` 的 `read(n)` 在分块响应上会阻塞到
       攒满 n 字节**或流结束**;SSE 一条事件才几十字节,于是它一路等到上游结束 ——
       心跳全被攒住,页面一直转圈。`read1` 只取底层一次读到的东西,有多少给多少。
       (2026-09-21:第一版就是写成 read(CHUNK),被裸 socket 的时序测试抓出来。)
    ② **每块都 flush。** 不 flush 的话攒在本层的发送缓冲里,效果和 ① 一样。
    """
    total = 0
    while True:
        try:
            chunk = resp.read1(CHUNK)
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamClosed(str(exc)) from exc
        if not chunk:
            break
        write(chunk)
        flush()
        total += len(chunk)
        if on_bytes is not None:
            on_bytes(len(chunk))
    return total
