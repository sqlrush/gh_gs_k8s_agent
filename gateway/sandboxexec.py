"""在沙箱里执行命令:E2B 的 envd(端口 49983,Connect 协议 + JSON 编码)。只用标准库。

用途只有两个:沙箱建好(或恢复)后下发配置、启动服务(`podctl fuyao-start`);删除 / 暂停前做收尾同步
(`podctl fuyao-stop`)。平台实测(2026-11-17):
- `process.Process/Start` 是服务端流:请求与应答都是「1 字节标志 + 4 字节长度 + JSON」的信封;
- 进程配置里的 envs 按命令生效(创建沙箱时给的 env 不进虚拟机,只能走这里);
- 用户由 Basic 认证头的用户名指定(root / 普通用户),口令为空;
- 它会等**所有**子进程结束才收尾 —— 后台服务要用虚拟机里的 systemd-run 起,不能 `&`;
- stdout / stderr 是 base64;退出码为 0 时 JSON 里不出现 exitCode(proto3 省略零值)。

envd 本身没有认证(平台前提见设计文档 §9);这里不传任何密钥给日志,环境变量的值不打印。
"""
from __future__ import annotations

import base64
import http.client
import json
import struct
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional

ENVD_PORT = 49983
_END_STREAM = 0x02
_MAX_OUT = 256 * 1024


class ExecError(Exception):
    """连不上 envd、协议不对、超时。调用方把这台沙箱当成暂时不可用。"""


@dataclass(frozen=True)
class Result:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _auth(user: str) -> str:
    return "Basic " + base64.b64encode(("%s:" % user).encode("utf-8")).decode("ascii")


def _envelope(msg: dict) -> bytes:
    data = json.dumps(msg).encode("utf-8")
    return struct.pack(">BI", 0, len(data)) + data


def _read_exact(resp, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = resp.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


class Envd:
    def __init__(self, port: int = ENVD_PORT, connect_timeout_s: float = 5.0):
        self.port, self.connect_timeout_s = port, connect_timeout_s

    def healthy(self, host: str) -> bool:
        conn = http.client.HTTPConnection(host, self.port, timeout=self.connect_timeout_s)
        try:
            conn.request("GET", "/health")
            r = conn.getresponse()
            r.read()
            return r.status in (200, 204)
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def run(self, host: str, argv: List[str], user: str = "root", envs: Optional[Mapping[str, str]] = None,
            cwd: Optional[str] = None, timeout_s: float = 120.0) -> Result:
        if not argv:
            raise ValueError("命令不能为空")
        proc = {"cmd": argv[0], "args": list(argv[1:]), "envs": dict(envs or {})}
        if cwd:
            proc["cwd"] = cwd
        conn = http.client.HTTPConnection(host, self.port, timeout=timeout_s)
        out: List[str] = []
        err: List[str] = []
        exit_code: Optional[int] = None
        size = 0
        try:
            conn.request("POST", "/process.Process/Start", body=_envelope({"process": proc}),
                         headers={"Content-Type": "application/connect+json", "Connect-Protocol-Version": "1",
                                  "Authorization": _auth(user)})
            resp = conn.getresponse()
            if resp.status != 200:
                raise ExecError("envd 回了 HTTP %d:%s" % (resp.status, resp.read()[:200].decode("utf-8", "replace")))
            while True:
                head = _read_exact(resp, 5)
                if len(head) < 5:
                    break
                flags, n = struct.unpack(">BI", head)
                payload = _read_exact(resp, n)
                try:
                    msg = json.loads(payload or b"{}")
                except ValueError as exc:
                    raise ExecError("envd 的应答不是 JSON") from exc
                if flags & _END_STREAM:
                    if isinstance(msg, dict) and msg.get("error"):
                        e = msg["error"]
                        raise ExecError("envd 报错:%s" % (e.get("message") if isinstance(e, dict) else e))
                    break
                ev = (msg.get("event") or {}) if isinstance(msg, dict) else {}
                data = ev.get("data") or {}
                for key, sink in (("stdout", out), ("stderr", err)):
                    if key in data and size < _MAX_OUT:
                        text = base64.b64decode(data[key]).decode("utf-8", "replace")
                        size += len(text)
                        sink.append(text)
                if "end" in ev:
                    end = ev["end"] or {}
                    exit_code = int(end.get("exitCode", 0) or 0)
        except (OSError, http.client.HTTPException) as exc:
            raise ExecError("在沙箱(%s)里执行命令失败:%s" % (host, exc)) from exc
        finally:
            conn.close()
        if exit_code is None:
            raise ExecError("envd 没有给出进程结束事件(%s)" % host)
        return Result(exit_code=exit_code, stdout="".join(out), stderr="".join(err))


def env_from(mapping: Mapping[str, object]) -> Dict[str, str]:
    """只保留字符串化后非空的值(envd 的 envs 是 map<string,string>)。"""
    return {str(k): str(v) for k, v in mapping.items() if v is not None and str(v) != ""}
