"""kubectl 的薄封装。发版台跑在集群外,kubeconfig 的各种认证方式、kustomize、diff、exec、port-forward 都交给 kubectl。

**这里只做传输**:拼参数、跑子进程、解析 JSON。要改什么、什么时候改,在 render / steps / pool 里。
密钥只走标准输入(apply_secret 把 Secret 清单从 stdin 交给 kubectl),不进命令行参数 —— 命令行在进程列表里谁都看得到;
命令日志只记参数,不记标准输入。
"""
from __future__ import annotations

import contextlib
import json
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

import yaml


class KubeError(Exception):
    def __init__(self, message: str, code: int = 0, stderr: str = ""):
        super().__init__(message)
        self.code, self.stderr = code, stderr


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str


Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class Kube:
    namespace: str
    kubeconfig: str = ""
    context: str = ""
    kubectl: str = "kubectl"
    runner: Runner = subprocess.run
    log: Callable[[str], None] = lambda _m: None
    commands: List[List[str]] = field(default_factory=list)       # 跑过的命令(只有参数),给「执行的命令」面板

    def base(self, namespaced: bool = True) -> List[str]:
        cmd = [self.kubectl]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            cmd += ["--context", self.context]
        if namespaced:
            cmd += ["-n", self.namespace]
        return cmd

    def run(self, args: Sequence[str], stdin: str = "", timeout: float = 60, namespaced: bool = True,
            check: bool = True) -> Result:
        cmd = self.base(namespaced) + list(args)
        self.commands.append(list(args))
        try:
            p = self.runner(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise KubeError("kubectl %s 超过 %d 秒没有返回" % (" ".join(args[:3]), timeout))
        except FileNotFoundError:
            raise KubeError("找不到 kubectl(%s)" % self.kubectl)
        res = Result(p.returncode, p.stdout or "", p.stderr or "")
        if check and res.code != 0:
            raise KubeError("kubectl %s 失败:%s" % (" ".join(args[:3]), _first_line(res.err) or res.code), res.code, res.err)
        return res

    # -- 读 ---------------------------------------------------------------------------------------

    def get(self, kind: str, name: str, namespaced: bool = True) -> Optional[Dict[str, Any]]:
        res = self.run(["get", kind, name, "-o", "json"], namespaced=namespaced, check=False)
        if res.code != 0:
            if "NotFound" in res.err or "not found" in res.err:
                return None
            raise KubeError("读 %s/%s 失败:%s" % (kind, name, _first_line(res.err)), res.code, res.err)
        return json.loads(res.out)

    def list(self, kind: str, selector: str = "", namespaced: bool = True) -> List[Dict[str, Any]]:
        args = ["get", kind, "-o", "json"] + (["-l", selector] if selector else [])
        return json.loads(self.run(args, namespaced=namespaced).out).get("items") or []

    def can_i(self, verb: str, resource: str, namespaced: bool = True) -> bool:
        res = self.run(["auth", "can-i", verb, resource], namespaced=namespaced, check=False)
        return res.out.strip() == "yes"

    def version(self) -> Dict[str, int]:
        """kubectl 与集群各自的小版本号(1.31 → 31)。连不上集群时只有 client;读不出来就是空。"""
        res = self.run(["version", "-o", "json"], namespaced=False, check=False, timeout=30)
        try:
            doc = json.loads(res.out or "{}")
        except ValueError:
            return {}
        out: Dict[str, int] = {}
        for side, key in (("client", "clientVersion"), ("server", "serverVersion")):
            m = re.match(r"\d+", str((doc.get(key) or {}).get("minor") or ""))      # 托管集群常见 "27+"
            if m:
                out[side] = int(m.group())
        return out

    def logs(self, target: str, container: str = "", tail: int = 200) -> str:
        args = ["logs", target, "--tail", str(tail)] + (["-c", container] if container else [])
        return self.run(args, check=False).out

    # -- 写 ---------------------------------------------------------------------------------------

    def kustomize(self, path: str) -> str:
        return self.run(["kustomize", path], namespaced=False).out

    def diff(self, manifest: str) -> str:
        """kubectl diff:退出码 0 = 没变化,1 = 有变化(正常),其余 = 出错。返回差异文本。"""
        res = self.run(["diff", "-f", "-"], stdin=manifest, check=False, timeout=120)
        if res.code not in (0, 1):
            raise KubeError("变更预览失败:%s" % _first_line(res.err), res.code, res.err)
        return res.out

    def apply(self, manifest: str) -> str:
        return self.run(["apply", "-f", "-"], stdin=manifest, timeout=180).out

    def apply_secret(self, name: str, data: Dict[str, str], labels: Optional[Dict[str, str]] = None) -> None:
        doc = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
               "metadata": {"name": name, "namespace": self.namespace, "labels": labels or {}}, "stringData": data}
        self.run(["apply", "-f", "-"], stdin=yaml.safe_dump(doc, allow_unicode=True))

    def patch(self, kind: str, name: str, patch: Dict[str, Any], ptype: str = "strategic") -> None:
        self.run(["patch", kind, name, "--type", ptype, "-p", json.dumps(patch, ensure_ascii=False)])

    def delete(self, kind: str, name: str = "", selector: str = "", wait: bool = True) -> None:
        args = ["delete", kind] + ([name] if name else ["-l", selector]) + ["--ignore-not-found", "--wait=%s" % str(wait).lower()]
        self.run(args, timeout=300)

    def rollout_status(self, target: str, timeout_s: int = 300) -> None:
        self.run(["rollout", "status", target, "--timeout=%ds" % timeout_s], timeout=timeout_s + 30)

    def exec(self, pod: str, container: str, argv: Sequence[str], stdin: str = "", timeout: float = 60) -> Result:
        args = ["exec", "-i" if stdin else "", pod] + (["-c", container] if container else []) + ["--"] + list(argv)
        return self.run([a for a in args if a], stdin=stdin, timeout=timeout, check=False)

    # -- 端口转发 ---------------------------------------------------------------------------------

    @contextlib.contextmanager
    def port_forward(self, target: str, remote_port: int, popen: Callable[..., Any] = subprocess.Popen,
                     wait_s: float = 20) -> Iterator[int]:
        """kubectl port-forward <target> :<端口>,读出本机端口后交给调用方;退出时结束子进程。"""
        cmd = self.base() + ["port-forward", target, ":%d" % remote_port, "--address", "127.0.0.1"]
        proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        got: Dict[str, int] = {}
        done = threading.Event()

        def read() -> None:
            for line in proc.stdout:
                m = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
                if m:
                    got["port"] = int(m.group(1))
                    done.set()
            done.set()
        threading.Thread(target=read, daemon=True).start()
        try:
            if not done.wait(wait_s) or "port" not in got:
                err = proc.stderr.read() if proc.poll() is not None else ""
                raise KubeError("port-forward %s 没有建立:%s" % (target, _first_line(err) or "超时"))
            yield got["port"]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""
