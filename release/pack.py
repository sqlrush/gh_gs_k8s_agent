"""打包时合成 release.yaml:`python3 -m release.pack --tag <TAG> --dist <包目录>`(package-images.sh 末尾调用)。

把人写的 k8s/release/changes.yaml 与本次的事实合在一起:标签、提交、各架构包里每个镜像的平台清单 digest
(发版台推完回读要对得上)、白名单全量(docs/delivery/scripts.sql 解析出的名字 + 正文 md5 + 参数)。
打包机上是系统自带的 python3(3.9,带 PyYAML),这里与它 import 的模块都要兼容 3.9。
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import subprocess
import sys
from typing import Dict, List, Optional

import yaml

from . import oci
from . import whitelist as wl

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = 1
VERSION = re.compile(r"^agent-v\d+\.\d+\.\d+$")
STEPS = {"rebuild_kb_import"}


class PackError(ValueError):
    pass


def version_of(tag: str) -> str:
    """agent-v1.4.4-oc1.18.27 → agent-v1.4.4"""
    m = re.match(r"^(agent-v\d+\.\d+\.\d+)(-oc[\d.]+)?$", tag)
    if not m:
        raise PackError("标签 %s 不是 agent-vX.Y.Z[-ocA.B.C] 的样子" % tag)
    return m.group(1)


def vkey(version: str) -> tuple:
    return tuple(int(x) for x in version[len("agent-v"):].split("."))


def load_changes(path: pathlib.Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    vs = doc.get("versions") or []
    if not vs:
        raise PackError("%s 里没有 versions" % path)
    seen = []
    for v in vs:
        ver = v.get("version", "")
        if not VERSION.match(ver):
            raise PackError("changes.yaml 版本号 %r 不对" % ver)
        for key in ("mixed_with", "required", "steps", "whitelist", "notes"):
            if not isinstance(v.get(key, []), list):
                raise PackError("%s 的 %s 要是列表" % (ver, key))
        bad = set(v.get("steps") or []) - STEPS
        if bad:
            raise PackError("%s 有不认识的步骤 %s(认识的:%s)" % (ver, sorted(bad), sorted(STEPS)))
        for r in v.get("required") or []:
            if not (isinstance(r, dict) and r.get("map") and r.get("key")):
                raise PackError("%s 的 required 每项要有 map 与 key" % ver)
        seen.append(ver)
    if seen != sorted(seen, key=vkey, reverse=True) or len(set(seen)) != len(seen):
        raise PackError("changes.yaml 的版本要从新到旧、不重复:%s" % seen)
    if not VERSION.match(doc.get("min_upgrade_from", "")):
        raise PackError("changes.yaml 缺 min_upgrade_from")
    return doc


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _skills() -> str:
    """agent/UPSTREAM 里最后一次同步记到的技能版本。"""
    hits = re.findall(r"skills-v[\d.]+", (ROOT / "agent" / "UPSTREAM").read_text(encoding="utf-8"))
    return hits[-1] if hits else ""


def compose(tag: str, dist: pathlib.Path, changes: dict, delivery: pathlib.Path, commit: str = "", skills: str = "",
            packed: str = "") -> dict:
    version = version_of(tag)
    top = changes["versions"][0]["version"]
    if top != version:
        raise PackError("changes.yaml 第一条是 %s,本次打的是 %s:发版前先把本版本的变化写进 changes.yaml" % (top, version))
    prefix = "gaussdb-agent-%s" % tag[len("agent-"):]
    k8s = dist / ("%s-k8s.tar.gz" % prefix)
    if not k8s.is_file():
        raise PackError("包目录里没有 %s" % k8s.name)
    files: Dict[str, object] = {"k8s": {"name": k8s.name, "sha256": _sha256(k8s)}}
    images: Dict[str, Dict[str, dict]] = {}
    arch_files: Dict[str, dict] = {}
    for f in sorted(dist.glob("%s-*.tar.gz" % prefix)):
        arch = f.name[len(prefix) + 1:-len(".tar.gz")]
        if arch == "k8s":
            continue
        arch_files[arch] = {"name": f.name, "sha256": _sha256(f)}
        for img in oci.read_images(str(f), arch):
            if img.tag != tag:
                raise PackError("%s 里 %s 的标签 %s 与本次 %s 不符" % (f.name, img.repo, img.tag, tag))
            images.setdefault(img.repo, {})[arch] = {"ref": img.source_ref, "digest": img.manifest_digest}
    if not arch_files:
        raise PackError("包目录里没有任何架构的镜像包")
    files["images"] = arch_files
    scripts = wl.parse_dml(delivery.read_text(encoding="utf-8"))
    history = [v for v in changes["versions"] if vkey(v["version"]) <= vkey(version)]
    return {"schema": SCHEMA, "version": version, "tag": tag, "skills": skills, "commit": commit, "packed": packed,
            "min_upgrade_from": changes["min_upgrade_from"], "files": files, "images": images,
            "whitelist": {"count": len(scripts), "scripts": wl.to_json(scripts)}, "history": history}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="合成离线包里的 release.yaml")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--dist", required=True, help="包目录(package-images.sh 的 dist/<包名>)")
    ap.add_argument("--date", default="")
    args = ap.parse_args(argv)
    dist = pathlib.Path(args.dist)
    try:
        doc = compose(args.tag, dist, load_changes(ROOT / "k8s" / "release" / "changes.yaml"),
                      ROOT / "docs" / "delivery" / "scripts.sql", commit=_commit(), skills=_skills(), packed=args.date)
    except (PackError, oci.OciError, wl.WhitelistError) as exc:
        print("合成 release.yaml 失败:%s" % exc, file=sys.stderr)
        return 1
    out = dist / "release.yaml"
    out.write_text("# 发版台读的版本说明(release/pack.py 生成,勿手改)\n"
                   + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    print("release.yaml:%s,%d 个镜像 × %d 个架构,白名单 %d 条,历史 %d 个版本"
          % (doc["version"], len(doc["images"]), len(doc["files"]["images"]), doc["whitelist"]["count"], len(doc["history"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
