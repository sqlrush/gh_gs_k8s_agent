#!/usr/bin/env python3
"""构建并注册 Fuyao(E2B)模板(2026-11-17)。在平台的模板构建服务所在的主机上以 root 运行;只用标准库(Python 3.7 起)。

    build-e2b-template.py --tag agent-v1.5.0-oc1.18.27 [--registry ghcr.io/sqlrush] [--kind runtime|kb-import|all]
                          [--cpu 2 --memory 4096] [--reserve-mb 6144] [--e2b-api http://IP:3000] [--e2b-config /root/.e2b/config.json]
                          [--from-oci <OCI 目录或 tar>]

每种模板四步(设计文档 docs/specs/2026-11-17-fuyao-sandbox-design.md §8):
  1. buildkit 按 docker/Dockerfile.e2b 构建(加磁盘占位文件;诊断模板装进官方文档集),导出成 OCI 目录;
  2. scripts/oci-serve.py 只听 127.0.0.1 端出 —— 构建服务没有根证书、拉不了公网仓库,对本机地址走 HTTP(平台实测);
  3. E2B 接口:POST /v3/templates(名字、vCPU、内存)→ POST /v2/templates/{tid}/builds/{bid}(fromImage 指向本机);
  4. 等构建就绪,停掉临时仓库。
--from-oci:跳过第 1 步,用别处构建好的 OCI 包(目录或 tar,可同时含两种模板镜像 gaussdb-agent-*-e2b:<tag>)——
构建服务所在的主机不能上网时,镜像在能上网的机器上按 Dockerfile.e2b 构建好带过去。
模板名 = gsdb-<种类>-<版本>(如 gsdb-runtime-v1-5-0),填进 gateway-config 的 FUYAO_RUNTIME_TEMPLATE / FUYAO_IMPORT_TEMPLATE。
--template-name:同一版本改虚拟机规格(--cpu / --memory)时另起一个名字,要配 --kind runtime 或 kb-import(一次一种)。
同名模板重复注册的平台行为没有实测:改规格时换新名字,不要覆盖在用的模板。发版台按版本推导模板名,下一次升级会回到默认名字
(届时按新规格注册目标版本的模板)。
团队 API Key 从 --e2b-config 读,只在本进程里用,不打印。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
KINDS = {"runtime": ("runtime-e2b", "gaussdb-agent-runtime"), "kb-import": ("kbimport-e2b", "gaussdb-agent-kb-import")}
LISTEN = "127.0.0.1:5050"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")      # 与默认名字同一种字符:小写字母、数字、连字符


def log(msg: str) -> None:
    print("模板构建:%s" % msg, flush=True)


def template_name(kind: str, tag: str) -> str:
    m = re.match(r"^agent-v(\d+)\.(\d+)\.(\d+)", tag)
    ver = "v%s-%s-%s" % m.groups() if m else re.sub(r"[^a-z0-9-]+", "-", tag.lower()).strip("-")
    return "gsdb-%s-%s" % ("runtime" if kind == "runtime" else "import", ver)


def chosen_name(kind: str, args) -> str:
    """--template-name 给了就用它,否则按版本推导(默认行为不变)。"""
    return getattr(args, "template_name", "") or template_name(kind, args.tag)


def e2b_api(explicit: str) -> str:
    if explicit:
        return explicit.rstrip("/")
    ip = subprocess.check_output(["kubectl", "-n", "e2b", "get", "svc", "api", "-o", "jsonpath={.spec.clusterIP}"]).decode()
    return "http://%s:3000" % ip.strip()


class E2B:
    def __init__(self, base: str, key: str):
        self.base, self.key = base, key

    def call(self, method: str, path: str, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-API-Key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:500].decode("utf-8", "replace")

    def build(self, name: str, cpu: int, memory: int, image: str, wait_s: int = 1800) -> dict:
        code, r = self.call("POST", "/v3/templates", {"name": name, "cpuCount": cpu, "memoryMB": memory})
        if code not in (200, 201, 202) or not isinstance(r, dict):
            raise SystemExit("建模板 %s 失败(HTTP %s):%s" % (name, code, r))
        tid, bid = r["templateID"], r["buildID"]
        code, r2 = self.call("POST", "/v2/templates/%s/builds/%s" % (tid, bid), {"fromImage": image})
        if code not in (200, 201, 202, 204):
            raise SystemExit("开始构建 %s 失败(HTTP %s):%s" % (name, code, r2))
        t0, off = time.time(), 0
        while time.time() - t0 < wait_s:
            code, st = self.call("GET", "/templates/%s/builds/%s/status?logsOffset=%d" % (tid, bid, off))
            if code == 200 and isinstance(st, dict):
                off += len(st.get("logs") or [])
                if st.get("status") == "ready":
                    return {"name": name, "templateID": tid, "buildID": bid, "seconds": int(time.time() - t0)}
                if st.get("status") == "error":
                    raise SystemExit("模板 %s 构建失败:%s" % (name, json.dumps(st.get("reason"), ensure_ascii=False)[:500]))
            time.sleep(5)
        raise SystemExit("模板 %s 构建超时(%d 秒)" % (name, wait_s))


def served_ref(layout: str, arch: str, name: str = "", tag: str = "") -> str:
    """OCI 目录在 oci-serve 上的「名字:标签」—— 直接用 oci-serve 自己的解析,不另写一份。
    给了 name / tag 就只认匹配的那一个(一个目录里可以有几个镜像)。"""
    spec = importlib.util.spec_from_file_location("oci_serve", str(ROOT / "scripts" / "oci-serve.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tags = sorted(k for k in mod.Layout(layout, arch).tags if (not name or k[0] == name) and (not tag or k[1] == tag))
    if len(tags) != 1:
        raise SystemExit("OCI 目录 %s 里应正好一个 %s:%s,实际 %s" % (layout, name or "*", tag or "*", tags))
    return "%s:%s" % tags[0]


def prebuilt_ref(layout: str, args, kind: str) -> str:
    """离线包里非本机架构的镜像标签带「-<架构>」后缀(scripts/package-images.sh),两种写法都认。"""
    name = KINDS[kind][1] + "-e2b"
    for tag in (args.tag, "%s-%s" % (args.tag, args.arch)):
        try:
            return served_ref(layout, args.arch, name, tag)
        except SystemExit:
            continue
    return served_ref(layout, args.arch, name, args.tag)     # 都没有:按原标签报错


def prebuilt_layout(path: str, work: str) -> str:
    """--from-oci:目录直接用;tar 解到工作目录(只解一次,两种模板共用)。"""
    if os.path.isdir(path):
        return path
    out = os.path.join(work, "prebuilt")
    if not os.path.isdir(out):
        os.makedirs(out)
        subprocess.run(["tar", "-xf", path, "-C", out], check=True)
    return out


def build_command(target: str, tar: str, args, kind: str) -> list:
    reg = args.registry.rstrip("/")
    return ["nerdctl", "build", "--platform", "linux/arm64" if args.arch == "arm64" else "linux/amd64",
            "-f", str(ROOT / "docker" / "Dockerfile.e2b"), "--target", target,
            "--build-arg", "RUNTIME_IMAGE=%s/gaussdb-agent-runtime:%s" % (reg, args.tag),
            "--build-arg", "KBIMPORT_IMAGE=%s/gaussdb-agent-kb-import:%s" % (reg, args.tag),
            "--build-arg", "KBSTD_IMAGE=%s/gaussdb-agent-kbstd:%s" % (reg, args.tag),
            "--build-arg", "RESERVE_MB=%d" % args.reserve_mb,
            "-t", "%s-e2b:%s" % (KINDS[kind][1], args.tag), "-o", "type=oci,dest=%s" % tar, str(ROOT)]


def build_oci(target: str, out_dir: str, args, kind: str) -> str:
    """buildkit 构建 docker/Dockerfile.e2b 的 target,导出 OCI 目录。返回 oci-serve 上的「名字:标签」。"""
    tar = out_dir + ".tar"
    log("构建 %s(%s)" % (target, args.tag))
    subprocess.run(build_command(target, tar, args, kind), check=True)
    os.makedirs(out_dir, exist_ok=True)
    subprocess.run(["tar", "-xf", tar, "-C", out_dir], check=True)
    os.unlink(tar)
    return served_ref(out_dir, args.arch)


def serve_and_build(layout: str, ref: str, kind: str, args, api: E2B) -> dict:
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "oci-serve.py"), layout, "--listen", LISTEN,
                             "--arch", args.arch], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen("http://%s/v2/" % LISTEN, timeout=1):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        name = chosen_name(kind, args)
        log("注册模板 %s(%s/%s,%d vCPU / %d MB),构建服务拉取并制作快照,通常几分钟"
            % (name, LISTEN, ref, args.cpu, args.memory))
        got = api.build(name, args.cpu, args.memory, "%s/%s" % (LISTEN, ref))
        log("模板 %s 就绪(%d 秒,templateID %s)" % (name, got["seconds"], got["templateID"]))
        return got
    finally:
        proc.terminate()
        proc.wait(10)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建并注册 Fuyao(E2B)模板")
    ap.add_argument("--tag", required=True, help="版本标签,如 agent-v1.5.0-oc1.18.27")
    ap.add_argument("--registry", default="ghcr.io/sqlrush", help="三个镜像的仓库前缀(构建机要拉得到)")
    ap.add_argument("--kind", choices=("runtime", "kb-import", "all"), default="all")
    ap.add_argument("--cpu", type=int, default=2)
    ap.add_argument("--memory", type=int, default=4096, help="MB")
    ap.add_argument("--reserve-mb", type=int, default=6144)
    ap.add_argument("--arch", choices=("arm64", "amd64"), default="arm64")
    ap.add_argument("--e2b-api", default="")
    ap.add_argument("--e2b-config", default="/root/.e2b/config.json")
    ap.add_argument("--workdir", default="")
    ap.add_argument("--from-oci", default="", help="不构建,用现成的 OCI 包(目录或 tar)")
    ap.add_argument("--template-name", default="",
                    help="指定模板名(同一版本改规格时用;要配 --kind runtime 或 kb-import;建议换新名字,不要覆盖在用的)")
    a = ap.parse_args(argv)
    if a.template_name:
        if a.kind == "all":
            raise SystemExit("--template-name 一次只能给一种模板:加 --kind runtime 或 --kind kb-import")
        if not NAME_RE.match(a.template_name):
            raise SystemExit("--template-name=%r 不对:小写字母、数字、连字符,字母或数字开头,至多 63 个字符" % a.template_name)
    key = json.loads(pathlib.Path(a.e2b_config).read_text())["teamApiKey"]
    api = E2B(e2b_api(a.e2b_api), key)
    work = a.workdir or tempfile.mkdtemp(prefix="gsdb-e2b-")
    done = []
    try:
        for kind in (("runtime", "kb-import") if a.kind == "all" else (a.kind,)):
            if a.from_oci:
                layout = prebuilt_layout(a.from_oci, work)
                ref = prebuilt_ref(layout, a, kind)
            else:
                layout = os.path.join(work, kind)
                ref = build_oci(KINDS[kind][0], layout, a, kind)
            done.append(serve_and_build(layout, ref, kind, a, api))
    finally:
        if not a.workdir:
            shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"templates": done}, ensure_ascii=False, indent=1))
    log("把模板名填进 gateway-config:FUYAO_RUNTIME_TEMPLATE / FUYAO_IMPORT_TEMPLATE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
