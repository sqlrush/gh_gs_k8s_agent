"""打包时合成 release.yaml;changes.yaml 的守卫。"""
from __future__ import annotations

import pathlib
import re
import sys
import tarfile

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import pack  # noqa: E402
from release.tests.fakes import make_package  # noqa: E402

TAG = "agent-v1.4.4-oc1.18.27"
CHANGES = {"min_upgrade_from": "agent-v1.4.0", "versions": [
    {"version": "agent-v1.4.5", "mixed_with": ["agent-v1.4.4"], "required": [], "steps": [], "whitelist": [], "notes": []},
    {"version": "agent-v1.4.4", "mixed_with": ["agent-v1.4.3"], "required": [], "steps": [], "whitelist": [], "notes": []},
    {"version": "agent-v1.4.3", "mixed_with": [], "required": [], "steps": [], "whitelist": ["d.sql"], "notes": []},
]}


@pytest.fixture()
def dist(tmp_path):
    d = tmp_path / "gaussdb-agent-v1.4.4-oc1.18.27"
    d.mkdir()
    imgs = {"gaussdb-agent-runtime": [b"base", b"rt"], "gaussdb-agent-gateway": [b"base", b"gw"]}
    amd = make_package(d / "gaussdb-agent-v1.4.4-oc1.18.27-amd64.tar.gz", "amd64", imgs)
    arm = make_package(d / "gaussdb-agent-v1.4.4-oc1.18.27-arm64.tar.gz", "arm64", imgs, suffix=False)
    with tarfile.open(d / "gaussdb-agent-v1.4.4-oc1.18.27-k8s.tar.gz", "w:gz"):
        pass
    return d, amd, arm


def _changes(top="agent-v1.4.4"):
    return {"min_upgrade_from": CHANGES["min_upgrade_from"],
            "versions": [v for v in CHANGES["versions"] if pack.vkey(v["version"]) <= pack.vkey(top)]}


def test_compose_records_each_images_platform_digest_per_arch(dist):
    d, amd, arm = dist
    doc = pack.compose(TAG, d, _changes(), _ROOT / "docs" / "delivery" / "scripts.sql", commit="abc", packed="2026-11-10")
    assert doc["version"] == "agent-v1.4.4" and doc["tag"] == TAG and doc["schema"] == 1
    assert doc["images"]["gaussdb-agent-runtime"]["amd64"]["digest"] == amd["gaussdb-agent-runtime"]
    assert doc["images"]["gaussdb-agent-runtime"]["arm64"]["digest"] == arm["gaussdb-agent-runtime"]
    assert doc["images"]["gaussdb-agent-gateway"]["amd64"]["ref"] == "gaussdb-agent-gateway:%s-amd64" % TAG
    assert set(doc["files"]["images"]) == {"amd64", "arm64"} and len(doc["files"]["k8s"]["sha256"]) == 64
    assert doc["whitelist"]["count"] == 132 == len(doc["whitelist"]["scripts"])
    assert [v["version"] for v in doc["history"]] == ["agent-v1.4.4", "agent-v1.4.3"]


def test_packing_a_version_that_changes_yaml_does_not_describe_yet_is_refused(dist):
    with pytest.raises(pack.PackError, match="先把本版本的变化写进 changes.yaml"):
        pack.compose(TAG, dist[0], {"min_upgrade_from": "agent-v1.4.0", "versions": CHANGES["versions"][2:]},
                     _ROOT / "docs" / "delivery" / "scripts.sql")


def test_an_image_with_another_tag_in_the_package_is_refused(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    make_package(d / "gaussdb-agent-v1.4.5-oc1.18.27-amd64.tar.gz", "amd64", {"gaussdb-agent-runtime": [b"x"]})
    with tarfile.open(d / "gaussdb-agent-v1.4.5-oc1.18.27-k8s.tar.gz", "w:gz"):
        pass
    with pytest.raises(pack.PackError, match="标签"):
        pack.compose("agent-v1.4.5-oc1.18.27", d, _changes("agent-v1.4.5"), _ROOT / "docs" / "delivery" / "scripts.sql")


@pytest.mark.parametrize("mutate,msg", [
    (lambda c: c["versions"].reverse(), "从新到旧"),
    (lambda c: c["versions"][0].update(steps=["drop_everything"]), "不认识的步骤"),
    (lambda c: c["versions"][0].update(required=[{"key": "X"}]), "map 与 key"),
    (lambda c: c.pop("min_upgrade_from"), "min_upgrade_from"),
])
def test_changes_yaml_is_validated(tmp_path, mutate, msg):
    import copy
    c = copy.deepcopy(CHANGES)
    mutate(c)
    p = tmp_path / "changes.yaml"
    p.write_text(yaml.safe_dump(c, allow_unicode=True), encoding="utf-8")
    with pytest.raises(pack.PackError, match=msg):
        pack.load_changes(p)


def test_main_writes_a_readable_release_yaml(dist, monkeypatch):
    d = dist[0]
    monkeypatch.setattr(pack, "load_changes", lambda _p: _changes())
    assert pack.main(["--tag", TAG, "--dist", str(d), "--date", "2026-11-10"]) == 0
    doc = yaml.safe_load((d / "release.yaml").read_text(encoding="utf-8"))
    assert doc["version"] == "agent-v1.4.4" and doc["packed"] == "2026-11-10"


# -- 守卫:仓库里的 changes.yaml -----------------------------------------------------------------

def test_repo_changes_yaml_starts_at_the_version_the_manifests_ship():
    """发版时清单换了版本、changes.yaml 却没写这一版 —— 打包会失败;这里提前在单测里拦下。"""
    doc = pack.load_changes(_ROOT / "k8s" / "release" / "changes.yaml")
    cm = (_ROOT / "k8s" / "base" / "configmap-gateway.yaml").read_text(encoding="utf-8")
    tag = re.search(r'USER_IMAGE_TAG:\s*"([^"]+)"', cm).group(1)
    assert doc["versions"][0]["version"] == pack.version_of(tag)


def test_repo_changes_yaml_names_whitelist_files_that_exist():
    doc = pack.load_changes(_ROOT / "k8s" / "release" / "changes.yaml")
    for v in doc["versions"]:
        for f in v["whitelist"]:
            assert (_ROOT / "docs" / "delivery" / f).is_file(), (v["version"], f)
