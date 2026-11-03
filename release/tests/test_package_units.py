"""发布包目录:发现、核 sha256、digest 与 release.yaml 交叉核对、旧包、历史合并。"""
from __future__ import annotations

import hashlib
import pathlib
import sys
import tarfile

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import oci, pack  # noqa: E402
from release import package as pk  # noqa: E402
from release.tests.fakes import make_package  # noqa: E402

IMGS = {"gaussdb-agent-runtime": [b"base", b"rt"], "gaussdb-agent-gateway": [b"base", b"gw"]}


def _sha(path: pathlib.Path) -> None:
    path.with_name(path.name + ".sha256").write_text(
        "%s  %s\n" % (hashlib.sha256(path.read_bytes()).hexdigest(), path.name), encoding="utf-8")


def build(root: pathlib.Path, ver: str, with_release: bool = True, flat: bool = False) -> pathlib.Path:
    tag = "agent-%s-oc1.18.27" % ver
    d = root if flat else root / ("gaussdb-agent-%s-oc1.18.27" % ver)
    d.mkdir(parents=True, exist_ok=True)
    prefix = "gaussdb-agent-%s-oc1.18.27" % ver
    make_package(d / ("%s-arm64.tar.gz" % prefix), "arm64", {k: v + [ver.encode()] for k, v in IMGS.items()}, suffix=False)
    with tarfile.open(d / ("%s-k8s.tar.gz" % prefix), "w:gz"):
        pass
    for f in d.glob(prefix + "-*.tar.gz"):
        _sha(f)
    if with_release:
        changes = {"min_upgrade_from": "agent-v1.4.0", "versions": [
            {"version": "agent-%s" % ver, "mixed_with": [], "required": [], "steps": [], "whitelist": [], "notes": []},
            {"version": "agent-v1.4.3", "mixed_with": [], "required": [], "steps": [], "whitelist": [], "notes": ["旧"]}]}
        doc = pack.compose(tag, d, changes, _ROOT / "docs" / "delivery" / "scripts.sql")
        (d / "release.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        _sha(d / "release.yaml")
    return d


def test_discover_groups_subdirs_and_flat_files_newest_first(tmp_path):
    build(tmp_path, "v1.4.4")
    build(tmp_path, "v1.4.3", with_release=False, flat=True)
    pkgs = pk.discover(tmp_path)
    assert [p.tag for p in pkgs] == ["agent-v1.4.4-oc1.18.27", "agent-v1.4.3-oc1.18.27"]
    new, old = pkgs
    assert not new.legacy and old.legacy and new.arches == ["arm64"] and new.ok and old.ok
    assert new.version == "agent-v1.4.4"


def test_verify_passes_and_catches_a_corrupted_copy(tmp_path):
    d = build(tmp_path, "v1.4.4")
    assert pk.verify(pk.discover(tmp_path)[0]) == []
    f = next(d.glob("*-arm64.tar.gz"))
    data = bytearray(f.read_bytes())
    data[-10] ^= 0xFF
    f.write_bytes(bytes(data))
    probs = pk.verify(pk.discover(tmp_path)[0])
    assert any("sha256 不对" in p for p in probs), probs


def test_a_missing_checksum_file_is_reported(tmp_path):
    d = build(tmp_path, "v1.4.4")
    next(d.glob("*-k8s.tar.gz.sha256")).unlink()
    assert any("缺" in p and ".sha256" in p for p in pk.verify(pk.discover(tmp_path)[0]))


def test_digests_are_cross_checked_against_release_yaml(tmp_path):
    d = build(tmp_path, "v1.4.4")
    pkg = pk.discover(tmp_path)[0]
    assert set(pk.image_digests(pkg, "arm64")) == set(IMGS)
    doc = yaml.safe_load((d / "release.yaml").read_text(encoding="utf-8"))
    doc["images"]["gaussdb-agent-runtime"]["arm64"]["digest"] = "sha256:" + "0" * 64
    (d / "release.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(oci.OciError, match="gaussdb-agent-runtime"):
        pk.image_digests(pk.discover(tmp_path)[0], "arm64")


def test_a_release_yaml_from_another_version_is_refused(tmp_path):
    d = build(tmp_path, "v1.4.4")
    doc = yaml.safe_load((d / "release.yaml").read_text(encoding="utf-8"))
    doc["tag"] = "agent-v1.4.5-oc1.18.27"
    (d / "release.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    pkg = pk.discover(tmp_path)[0]
    assert pkg.legacy and any("拷错了目录" in p for p in pkg.problems)


def test_incomplete_packages_say_what_is_missing(tmp_path):
    d = build(tmp_path, "v1.4.4", with_release=False)
    next(d.glob("*-k8s.tar.gz")).unlink()
    assert any("k8s" in p for p in pk.discover(tmp_path)[0].problems)


def test_history_from_newer_packages_covers_the_legacy_ones(tmp_path):
    build(tmp_path, "v1.4.4")
    build(tmp_path, "v1.4.3", with_release=False)
    h = pk.history(pk.discover(tmp_path))
    assert set(h) == {"agent-v1.4.4", "agent-v1.4.3"} and h["agent-v1.4.3"]["notes"] == ["旧"]


def test_a_link_pointing_outside_is_listed_with_the_reason_not_dropped(tmp_path):
    """2026-11-03 发版台换成容器跑:演练包是指向 dist/ 的链接,容器里目标不在,包在列表里**直接消失**、没有任何说法。"""
    elsewhere = build(tmp_path / "dist", "v1.4.3", with_release=False)
    rel = tmp_path / "releases" / elsewhere.name
    rel.mkdir(parents=True)
    for f in elsewhere.iterdir():
        (rel / f.name).symlink_to(f)
    assert [p.ok for p in pk.discover(tmp_path / "releases")] == [True], "目标在:链接照常用"
    (elsewhere / ("%s-arm64.tar.gz" % elsewhere.name)).rename(tmp_path / "moved")
    pkgs = pk.discover(tmp_path / "releases")
    assert [p.tag for p in pkgs] == ["agent-v1.4.3-oc1.18.27"] and not pkgs[0].ok
    assert any("是链接,指向的" in x and "看不到" in x and "不要用链接" in x for x in pkgs[0].problems), pkgs[0].problems
