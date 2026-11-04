"""离线包里某个架构的镜像重新打过之后(行内把根规范模板换成正式规范,scripts/rootspec-overlay.sh),刷新 release.yaml。

只改「事实」部分:该架构各镜像的平台清单 digest 与原名、该架构镜像包的 sha256;版本、历史、白名单原样不动。
镜像标签必须与 release.yaml 的 tag 一致(换根规范不换版本);再写 release.yaml.sha256 与镜像包的 .sha256。

    python3 -m release.repack <包目录> --arch amd64 [--note "根规范:…"]
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
from typing import List, Optional

import yaml

from . import oci


class RepackError(Exception):
    pass


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def refresh(pkg_dir: pathlib.Path, arch: str, note: str = "") -> List[str]:
    """返回改了 digest 的镜像名。"""
    rel_path = pkg_dir / "release.yaml"
    if not rel_path.is_file():
        raise RepackError("包目录里没有 release.yaml:%s" % pkg_dir)
    rel = yaml.safe_load(rel_path.read_text(encoding="utf-8"))
    tag = rel["tag"]
    tar = pkg_dir / ("gaussdb-agent-%s-%s.tar.gz" % (tag[len("agent-"):], arch))
    if not tar.is_file():
        raise RepackError("包目录里没有 %s" % tar.name)
    imgs = oci.read_images(str(tar), arch)
    changed = []
    for img in imgs:
        if img.tag != tag:
            raise RepackError("%s 里 %s 的标签 %s 与包的 %s 不符:换根规范不能换标签" % (tar.name, img.repo, img.tag, tag))
        per = rel.setdefault("images", {}).setdefault(img.repo, {})
        if (per.get(arch) or {}).get("digest") != img.manifest_digest:
            changed.append(img.repo)
        per[arch] = {"ref": img.source_ref, "digest": img.manifest_digest}
    missing = sorted(r for r, per in rel.get("images", {}).items() if arch in per and r not in {i.repo for i in imgs})
    if missing:
        raise RepackError("%s 里少了镜像:%s" % (tar.name, ", ".join(missing)))
    files = rel.setdefault("files", {}).setdefault("images", {})
    files[arch] = {"name": tar.name, "sha256": _sha256(tar)}
    if note:
        rel["rootspec"] = note
    rel_path.write_text(yaml.safe_dump(rel, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for f in (tar, rel_path):
        f.with_name(f.name + ".sha256").write_text("%s  %s\n" % (_sha256(f), f.name), encoding="utf-8")
    return changed


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="镜像重新打过后刷新离线包的 release.yaml")
    ap.add_argument("package", type=pathlib.Path)
    ap.add_argument("--arch", required=True, choices=("amd64", "arm64"))
    ap.add_argument("--note", default="", help="记进 release.yaml 的说明(例如换了哪几份根规范)")
    a = ap.parse_args(argv)
    try:
        changed = refresh(a.package, a.arch, a.note)
    except (RepackError, oci.OciError, OSError, KeyError) as exc:
        print("刷新失败:%s" % exc, file=sys.stderr)
        return 1
    print("release.yaml 已刷新(%s):%s" % (a.arch, "、".join(changed) or "镜像 digest 没变"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
