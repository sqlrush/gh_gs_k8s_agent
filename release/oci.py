"""离线包里的镜像:读 `docker save` 产出的 OCI 目录(oci-layout / index.json / blobs/),不需要 Docker。

package-images.sh 在打包机(containerd 镜像存储)上 `docker save | gzip`,一个架构一个 tar.gz,五个镜像共用 blobs。
顶层 index.json 里每个命名条目(注解 `io.containerd.image.name`)是一个单架构 index:平台清单 + buildx 证明清单。
推送只用平台清单(见 docs/specs/2026-11-03-release-console-design.md §2.3)。

tar.gz 不能随机访问,读两遍:第一遍收 index.json 与所有小 blob(清单、配置都在里面),第二遍按需把大 blob 流给调用方。
"""
from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import dataclass
from typing import Callable, Dict, IO, Iterable, List, Optional, Tuple

NAME_ANNOTATION = "io.containerd.image.name"
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
SMALL_BLOB = 1 << 20                    # 清单与配置都远小于 1 MiB;更大的只可能是层


class OciError(Exception):
    pass


@dataclass(frozen=True)
class Blob:
    digest: str
    size: int
    media_type: str = ""


@dataclass(frozen=True)
class Image:
    """一个镜像在本架构包里的平台清单。repo / tag 已去掉 docker.io/library/ 前缀与 -<arch> 后缀。"""
    repo: str
    tag: str
    source_ref: str                     # 包里的原名,例如 gaussdb-agent-runtime:agent-v1.4.3-oc1.18.27-amd64
    arch: str
    manifest: bytes
    manifest_digest: str
    media_type: str
    blobs: Tuple[Blob, ...]             # 配置在前、层按顺序


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _blob_member(name: str) -> Optional[str]:
    parts = name.lstrip("./").split("/")
    if len(parts) == 3 and parts[0] == "blobs" and parts[1] == "sha256" and len(parts[2]) == 64:
        return "sha256:" + parts[2]
    return None


def _strip_name(full: str, arch: str) -> Tuple[str, str]:
    """docker.io/library/gaussdb-agent-runtime:agent-v1.4.3-oc1.18.27-amd64 → (gaussdb-agent-runtime, agent-v1.4.3-oc1.18.27)"""
    ref = full
    for prefix in ("docker.io/library/", "docker.io/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    if ":" not in ref.rsplit("/", 1)[-1]:
        raise OciError("镜像名没有标签:%s" % full)
    repo, tag = ref.rsplit(":", 1)
    if tag.endswith("-" + arch):
        tag = tag[: -len(arch) - 1]
    return repo, tag


def _first_pass(path: str) -> Tuple[dict, Dict[str, bytes], Dict[str, int]]:
    index: Optional[dict] = None
    small: Dict[str, bytes] = {}
    sizes: Dict[str, int] = {}
    with tarfile.open(path, "r:gz") as tf:
        for m in tf:
            if not m.isfile():
                continue
            name = m.name.lstrip("./")
            digest = _blob_member(name)
            if digest:
                sizes[digest] = m.size
                if m.size <= SMALL_BLOB:
                    small[digest] = tf.extractfile(m).read()
            elif name == "index.json":
                index = json.loads(tf.extractfile(m).read())
    if index is None:
        raise OciError("%s 里没有 index.json:不是 OCI 目录格式(打包机可能用的是 Docker 经典存储)" % path)
    return index, small, sizes


def _json_blob(small: Dict[str, bytes], digest: str, what: str) -> Tuple[bytes, dict]:
    raw = small.get(digest)
    if raw is None:
        raise OciError("%s %s 不在包里" % (what, digest))
    if sha256_digest(raw) != digest:
        raise OciError("%s %s 内容与 digest 不符" % (what, digest))
    return raw, json.loads(raw)


def read_images(path: str, arch: str) -> List[Image]:
    """列出本架构包里的全部镜像(平台清单 + 它引用的 blob),并核对每个 blob 都在包里、大小对得上。"""
    index, small, sizes = _first_pass(path)
    out: List[Image] = []
    for entry in index.get("manifests") or []:
        full = (entry.get("annotations") or {}).get(NAME_ANNOTATION)
        if not full:
            continue                    # 没有名字的是证明清单的附属条目
        repo, tag = _strip_name(full, arch)
        raw, doc = _json_blob(small, entry["digest"], "镜像 %s 的清单" % full)
        if doc.get("mediaType") in INDEX_TYPES or "manifests" in doc:
            plats = [d for d in doc.get("manifests") or []
                     if (d.get("platform") or {}).get("os") == "linux" and (d.get("platform") or {}).get("architecture") == arch]
            if len(plats) != 1:
                raise OciError("镜像 %s 里 linux/%s 的平台清单有 %d 个" % (full, arch, len(plats)))
            mdigest = plats[0]["digest"]
            raw, doc = _json_blob(small, mdigest, "镜像 %s 的 linux/%s 清单" % (full, arch))
        else:
            mdigest = entry["digest"]
        blobs = tuple(Blob(d["digest"], int(d["size"]), d.get("mediaType", ""))
                      for d in [doc["config"]] + list(doc.get("layers") or []))
        for b in blobs:
            if b.digest not in sizes:
                raise OciError("镜像 %s 引用的 %s 不在包里(包不完整)" % (full, b.digest))
            if sizes[b.digest] != b.size:
                raise OciError("镜像 %s 的 %s 大小不符:清单 %d,包里 %d" % (full, b.digest, b.size, sizes[b.digest]))
        out.append(Image(repo=repo, tag=tag, source_ref=full.split("/")[-1], arch=arch, manifest=raw,
                         manifest_digest=mdigest, media_type=doc.get("mediaType", "application/vnd.oci.image.manifest.v1+json"),
                         blobs=blobs))
    if not out:
        raise OciError("%s 里没有带名字的镜像" % path)
    return out


def stream_blobs(path: str, wanted: Iterable[str], consume: Callable[[str, int, IO[bytes]], None]) -> None:
    """第二遍:按包内顺序把需要的 blob 交给 consume(digest, size, 可读流)。每个 blob 只读一次。"""
    want = set(wanted)
    with tarfile.open(path, "r:gz") as tf:
        for m in tf:
            if not want:
                return
            digest = _blob_member(m.name) if m.isfile() else None
            if digest in want:
                consume(digest, m.size, tf.extractfile(m))
                want.discard(digest)
    if want:
        raise OciError("包里缺 blob:%s" % ", ".join(sorted(want)[:3]))
