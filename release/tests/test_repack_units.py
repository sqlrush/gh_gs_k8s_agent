"""根规范换过之后刷新离线包的 release.yaml(scripts/rootspec-overlay.sh 的最后一步)。"""
from __future__ import annotations

import hashlib
import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import package, repack  # noqa: E402
from release.tests.fakes import make_package  # noqa: E402

TAG = "agent-v1.4.5-oc1.18.27"
NAME = "gaussdb-agent-v1.4.5-oc1.18.27"


def _pkg(tmp_path, layers):
    d = tmp_path / NAME
    d.mkdir()
    tar = d / ("%s-arm64.tar.gz" % NAME)
    dig = make_package(tar, "arm64", {"gaussdb-agent-runtime": layers, "gaussdb-agent-gateway": [b"base", b"gw"]}, suffix=False, tag=TAG)
    rel = {"tag": TAG, "version": "agent-v1.4.5", "images": {k: {"arm64": {"ref": "%s:%s" % (k, TAG), "digest": v}} for k, v in dig.items()},
           "files": {"images": {"arm64": {"name": tar.name, "sha256": hashlib.sha256(tar.read_bytes()).hexdigest()}}},
           "whitelist": {"count": 128}}
    (d / "release.yaml").write_text(yaml.safe_dump(rel, allow_unicode=True), encoding="utf-8")
    return d, tar


def test_refresh_records_the_new_digest_and_sha256(tmp_path):
    d, tar = _pkg(tmp_path, [b"base", b"rt"])
    old = yaml.safe_load((d / "release.yaml").read_text(encoding="utf-8"))
    make_package(tar, "arm64", {"gaussdb-agent-runtime": [b"base", b"rt", b"rootspec"], "gaussdb-agent-gateway": [b"base", b"gw"]},
                 suffix=False, tag=TAG)                                 # runtime 多了一层根规范,网关不变
    assert repack.refresh(d, "arm64", note="根规范:a.md b.md c.md") == ["gaussdb-agent-runtime"]
    new = yaml.safe_load((d / "release.yaml").read_text(encoding="utf-8"))
    assert new["images"]["gaussdb-agent-gateway"] == old["images"]["gaussdb-agent-gateway"]
    assert new["images"]["gaussdb-agent-runtime"]["arm64"]["digest"] != old["images"]["gaussdb-agent-runtime"]["arm64"]["digest"]
    assert new["files"]["images"]["arm64"]["sha256"] == hashlib.sha256(tar.read_bytes()).hexdigest()
    assert new["whitelist"] == old["whitelist"] and new["rootspec"] == "根规范:a.md b.md c.md"
    for f in (tar, d / "release.yaml"):
        line = (f.parent / (f.name + ".sha256")).read_text(encoding="utf-8")
        assert line.split()[0] == hashlib.sha256(f.read_bytes()).hexdigest()
    pkg = [p for p in package.discover(tmp_path) if p.tag == TAG][0]
    assert package.image_digests(pkg, "arm64")["gaussdb-agent-runtime"] == new["images"]["gaussdb-agent-runtime"]["arm64"]["digest"], \
        "发版台核对包里的 digest 与 release.yaml:刷新后要对得上"


def test_a_retagged_image_is_refused(tmp_path):
    d, tar = _pkg(tmp_path, [b"base", b"rt"])
    import release.oci as oci
    real = oci.read_images

    def retagged(path, arch):
        return [i.__class__(**dict(i.__dict__, tag="agent-v9.9.9-oc1.18.27")) if i.repo.endswith("runtime") else i
                for i in real(path, arch)]
    with pytest.MonkeyPatch.context() as m:
        m.setattr(repack.oci, "read_images", retagged)
        with pytest.raises(repack.RepackError, match="换根规范不能换标签"):
            repack.refresh(d, "arm64")
