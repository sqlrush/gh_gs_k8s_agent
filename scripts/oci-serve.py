#!/usr/bin/env python3
"""只读的最小镜像仓库:把一个 OCI 镜像目录(`ctr images export` / 离线包解开后的 index.json + blobs/)
按 Registry API v2 端出来,只支持拉取。只用标准库(Python 3.7 起可跑)。

为什么要有它(2026-11-17,Fuyao 适配):平台的模板构建服务要从镜像仓库拉我们的 runtime 镜像来构建 E2B 模板,
但它的容器里没有根证书,拉不了公网仓库;平台内部仓库要推送账号。构建服务跑在主机网络上,
而它用的拉取组件对本机地址(127.0.0.1 / localhost)走 HTTP、不校验证书 —— 在同一台主机上起这个只读仓库,
只听 127.0.0.1,构建服务就能从这里拉,不需要任何账号,也不改平台。用完即停。

用法: oci-serve.py <OCI 镜像目录> [--listen 127.0.0.1:5050] [--arch arm64]
拉取地址: <listen>/<镜像名>:<标签>,镜像名取 index.json 里镜像全名的最后一段(如 gaussdb-agent-runtime)。
多平台的索引按 --arch 选出对应的单平台清单再端出(构建服务只要本机架构)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_DEFAULT = "application/vnd.oci.image.manifest.v1+json"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PATH = re.compile(r"^/v2/(?P<name>[a-z0-9._/-]+)/(?P<kind>manifests|blobs)/(?P<ref>[A-Za-z0-9._:-]+)$")
CHUNK = 1 << 20


class Layout:
    def __init__(self, root: str, arch: str):
        self.root, self.arch = root, arch
        with open(os.path.join(root, "index.json"), encoding="utf-8") as f:
            index = json.load(f)
        self.tags: Dict[Tuple[str, str], dict] = {}
        for d in index.get("manifests") or []:
            ann = d.get("annotations") or {}
            full = ann.get("io.containerd.image.name") or ""
            tag = ann.get("org.opencontainers.image.ref.name") or (full.rsplit(":", 1)[1] if ":" in full else "")
            if not tag:
                continue
            name = full.rsplit(":", 1)[0].rsplit("/", 1)[-1] if full else "image"
            self.tags[(name, tag)] = self._single(d)

    def blob(self, digest: str) -> Optional[str]:
        if not _DIGEST.match(digest):
            return None
        p = os.path.join(self.root, "blobs", "sha256", digest.split(":", 1)[1])
        return p if os.path.isfile(p) else None

    def read(self, digest: str) -> dict:
        with open(self.blob(digest) or "", "rb") as f:
            return json.loads(f.read())

    def _single(self, d: dict) -> dict:
        """多平台索引 → 本架构的单平台清单描述符(只取目录里真有的那一个)。"""
        if d.get("mediaType") not in INDEX_TYPES:
            return d
        for m in self.read(d["digest"]).get("manifests") or []:
            plat = m.get("platform") or {}
            if plat.get("os") == "linux" and plat.get("architecture") == self.arch and self.blob(m["digest"]):
                return m
        raise SystemExit("镜像目录里没有 linux/%s 的清单" % self.arch)

    def manifest(self, name: str, ref: str) -> Optional[Tuple[str, str]]:
        """(媒体类型, 文件路径)。ref 是标签或摘要。"""
        if ref.startswith("sha256:"):
            p = self.blob(ref)
            if not p:
                return None
            with open(p, "rb") as f:
                mt = json.loads(f.read()).get("mediaType") or MANIFEST_DEFAULT
            return mt, p
        d = self.tags.get((name.rsplit("/", 1)[-1], ref))
        if d is None:
            return None
        return d.get("mediaType") or MANIFEST_DEFAULT, self.blob(d["digest"]) or ""


def handler(layout: Layout):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "oci-serve/1"

        def _empty(self, code: int, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Docker-Distribution-API-Version", "registry/2.0")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _file(self, path: str, ctype: str, digest: str, head: bool) -> None:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Docker-Distribution-API-Version", "registry/2.0")
            self.send_header("Content-Type", ctype)
            self.send_header("Docker-Content-Digest", digest)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if head:
                return
            with open(path, "rb") as f:
                while True:
                    buf = f.read(CHUNK)
                    if not buf:
                        break
                    self.wfile.write(buf)

        def _serve(self, head: bool) -> None:
            if self.path.rstrip("/") == "/v2":
                return self._empty(200)
            m = _PATH.match(self.path.split("?", 1)[0])
            if not m:
                return self._empty(404)
            name, kind, ref = m.group("name"), m.group("kind"), m.group("ref")
            if kind == "blobs":
                p = layout.blob(ref)
                return self._file(p, "application/octet-stream", ref, head) if p else self._empty(404)
            got = layout.manifest(name, ref)
            if not got or not got[1]:
                return self._empty(404)
            mt, p = got
            digest = "sha256:" + os.path.basename(p)
            return self._file(p, mt, digest, head)

        def do_GET(self):
            self._serve(head=False)

        def do_HEAD(self):
            self._serve(head=True)

        def log_message(self, fmt, *args):
            sys.stderr.write("oci-serve: %s %s\n" % (self.command, self.path))

    return H


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("layout")
    ap.add_argument("--listen", default="127.0.0.1:5050")
    ap.add_argument("--arch", default="arm64")
    a = ap.parse_args(argv)
    layout = Layout(a.layout, a.arch)
    host, port = a.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), handler(layout))
    print("oci-serve:%s 上端出 %s" % (a.listen, ", ".join("%s:%s" % k for k in sorted(layout.tags))), flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
