"""发布包目录:发现离线包、核 sha256、读 release.yaml、核对镜像 digest。

包可以是一个子目录(package-images.sh 的 dist/<包名>/ 原样拷进来),也可以把文件平铺在发布包目录里 —— 按文件名前缀归组。
**v1.4.3 及更早的包没有 release.yaml**(第一次升级的起点、回滚到它时的目标):按「旧包」处理 —— 标签取自文件名,
镜像 digest 现读 OCI 目录,版本历史用更新的包里的 release.yaml(它累计了旧版本的变化),不核对白名单。
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import yaml

from . import oci

NAME = re.compile(r"^gaussdb-agent-(v\d+\.\d+\.\d+-oc[\d.]+)-(amd64|arm64|k8s)\.tar\.gz$")
SHA_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)\s*$")


@dataclass
class Package:
    tag: str                                    # agent-v1.4.4-oc1.18.27
    root: pathlib.Path
    files: Dict[str, pathlib.Path] = field(default_factory=dict)        # amd64 / arm64 / k8s → 路径
    release: Optional[dict] = None              # release.yaml;旧包为 None
    problems: List[str] = field(default_factory=list)
    checked: bool = False                       # sha256 已核过

    @property
    def version(self) -> str:
        return self.tag.split("-oc")[0]

    @property
    def legacy(self) -> bool:
        return self.release is None

    @property
    def arches(self) -> List[str]:
        return sorted(k for k in self.files if k != "k8s")

    @property
    def ok(self) -> bool:
        return not self.problems


def vkey(version: str) -> tuple:
    return tuple(int(x) for x in version.split("-oc")[0][len("agent-v"):].split("."))


def discover(root: pathlib.Path) -> List[Package]:
    """列出目录(及一级子目录)里的包,新版本在前。只看文件名与 release.yaml,不读大文件。"""
    found: Dict[str, Package] = {}
    for d in [root] + sorted(p for p in root.iterdir() if p.is_dir()):
        for f in sorted(d.iterdir()):
            m = NAME.match(f.name)
            if not m or not (f.is_file() or f.is_symlink()):
                continue
            tag, kind = "agent-" + m.group(1), m.group(2)
            pkg = found.setdefault(tag, Package(tag=tag, root=d))
            if not f.is_file():                 # 链接到发布包目录外:发版台以容器运行时只挂了这个目录,目标看不到
                pkg.problems.append("%s 是链接,指向的 %s 在发版台里看不到:把文件拷进发布包目录,不要用链接"
                                    % (f.name, os.readlink(str(f))))
                continue
            if kind in pkg.files:
                pkg.problems.append("%s 出现了两份(%s 与 %s)" % (f.name, pkg.files[kind].parent, d))
            pkg.files[kind] = f
    for pkg in found.values():
        _read_release(pkg)
        if "k8s" not in pkg.files:
            pkg.problems.append("缺 k8s 清单包(*-k8s.tar.gz)")
        if not pkg.arches:
            pkg.problems.append("缺镜像包(*-amd64.tar.gz / *-arm64.tar.gz)")
    return sorted(found.values(), key=lambda p: vkey(p.version), reverse=True)


def _read_release(pkg: Package) -> None:
    path = pkg.root / "release.yaml"
    if not path.is_file():
        return
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        pkg.problems.append("release.yaml 读不了:%s" % exc)
        return
    if doc.get("tag") != pkg.tag:
        pkg.problems.append("release.yaml 写的是 %s,文件名是 %s:别的版本的 release.yaml 拷错了目录" % (doc.get("tag"), pkg.tag))
        return
    if doc.get("schema") != 1:
        pkg.problems.append("release.yaml 的格式版本 %s 不认识:发版台需要先升级" % doc.get("schema"))
        return
    pkg.release = doc


def sha256_file(path: pathlib.Path, progress: Callable[[int], None] = lambda _n: None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
            progress(len(chunk))
    return h.hexdigest()


def verify(pkg: Package, progress: Callable[[int], None] = lambda _n: None) -> List[str]:
    """核对每个文件的 sha256(以同目录的 .sha256 为准;新包另与 release.yaml 里记的交叉核对)。返回问题列表(空 = 通过)。"""
    problems: List[str] = []
    paths = list(pkg.files.values()) + ([pkg.root / "release.yaml"] if pkg.release else [])
    recorded = {}
    if pkg.release:
        files = pkg.release.get("files") or {}
        recorded[(files.get("k8s") or {}).get("name")] = (files.get("k8s") or {}).get("sha256")
        for v in (files.get("images") or {}).values():
            recorded[v.get("name")] = v.get("sha256")
    for f in paths:
        side = f.with_name(f.name + ".sha256")
        if not side.is_file():
            problems.append("缺 %s" % side.name)
            continue
        m = SHA_LINE.match(side.read_text(encoding="utf-8").strip())
        if not m or m.group(2) != f.name:
            problems.append("%s 格式不对(应为「sha256  文件名」)" % side.name)
            continue
        got = sha256_file(f, progress)
        if got != m.group(1):
            problems.append("%s 的 sha256 不对:文件不完整或被改过,请重新拷贝" % f.name)
        elif f.name in recorded and recorded[f.name] != got:
            problems.append("%s 与 release.yaml 记的 sha256 不一致" % f.name)
    pkg.checked = True
    pkg.problems.extend(problems)
    return problems


def image_digests(pkg: Package, arch: str) -> Dict[str, str]:
    """这个架构包里各镜像的平台清单 digest。新包与 release.yaml 交叉核对,不一致直接报错。"""
    imgs = oci.read_images(str(pkg.files[arch]), arch)
    got = {i.repo: i.manifest_digest for i in imgs}
    if pkg.release:
        want = {repo: (per.get(arch) or {}).get("digest") for repo, per in (pkg.release.get("images") or {}).items()}
        if got != want:
            diff = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
            raise oci.OciError("%s 包里的镜像与 release.yaml 对不上:%s" % (arch, ", ".join(diff)))
    return got


def history(packages: List[Package]) -> Dict[str, dict]:
    """所有新包里 release.yaml 的 history 合在一起(同一版本以更新的包为准)→ {版本: 条目}。"""
    out: Dict[str, dict] = {}
    for pkg in sorted(packages, key=lambda p: vkey(p.version)):
        for h in (pkg.release or {}).get("history") or []:
            out[h["version"]] = h
    return out
