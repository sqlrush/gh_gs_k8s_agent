"""经凭据代理访问 GRMP(2026-10-23):本进程里没有令牌、没有 SM2 私钥。

容器部署下,技能脚本与模型在同一个容器、同一个 uid 里跑。令牌与私钥要是放在这个容器的环境变量里,
模型执行一句 `env` 或读一下 `/proc/<pid>/environ` 就能看到 —— 挡在前面的只有提示词。所以改成:

    技能脚本 ──(Unix 套接字,不带令牌、不签名)──▶ 凭据代理 ──(加 auth、Appkey / Timestamp / Signature、本人工号)──▶ GRMP

代理在模型碰不到的地方:按需模式是同一个 Pod 里的伴生容器,固定池化是 Pod 的管家。它只往**自己配置的**中间件地址转发、
只转发 GRMP 诊断接口那一组路径、工号由它按本 Pod / 槽位绑定的人强制写入 —— 模型直接连套接字,
能做的也只是「以本人身份调白名单脚本」,和它经技能能做的一样,拿不到任何凭据。

环境变量 `GRMP_PROXY_SOCKET` 给出套接字路径;设了它就走代理,不再要求 `GRMP_AUTH_TOKEN`。
"""
from __future__ import annotations

import http.client
import os
import socket
from typing import Dict, Mapping, Optional, Tuple

ENV_SOCKET = "GRMP_PROXY_SOCKET"


def socket_path(env: Optional[Mapping[str, str]] = None) -> str:
    """配置了凭据代理就返回套接字路径,没配返回空串。"""
    return ((os.environ if env is None else env).get(ENV_SOCKET) or "").strip()


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client 走 Unix 套接字。Host 头写一个固定名字,代理不看它(只往自己配置的中间件转发)。"""

    def __init__(self, path: str, timeout: float):
        super().__init__("credproxy", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def post(path_on_socket: str, url_path: str, body: bytes, headers: Dict[str, str],
         timeout: float) -> Tuple[int, str, bytes]:
    """发一个 POST,返回 (状态码, 原因短语, 响应体)。连不上 / 被断开 / 超时抛 OSError 或 HTTPException。"""
    conn = _UnixHTTPConnection(path_on_socket, timeout)
    try:
        conn.request("POST", url_path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.reason or "", resp.read()
    finally:
        conn.close()
