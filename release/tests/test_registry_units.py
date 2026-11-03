"""镜像入库:读离线包的 OCI 目录、推进内网仓库(假仓库)。"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import oci, registry  # noqa: E402
from release.tests.fakes import FakeRegistry, digest_of, make_package  # noqa: E402

BASE = b"base-layer" * 1000             # 三个镜像共用的底层,和实物一样
IMAGES = {"gaussdb-agent-runtime": [BASE, b"runtime-app"], "gaussdb-agent-gateway": [BASE, b"gateway-app"],
          "gaussdb-agent-kbstd": [b"kbstd-only"]}


@pytest.fixture()
def pkg(tmp_path):
    path = tmp_path / "pkg-amd64.tar.gz"
    return str(path), make_package(path, "amd64", IMAGES)


@pytest.fixture()
def reg():
    r = FakeRegistry()
    yield r
    r.close()


def _client(r, **kw):
    return registry.Client(registry.Endpoint(prefix=r.host + "/gaussdb-agent", plain_http=True, **kw), log=lambda _m: None)


# -- 读包 ----------------------------------------------------------------------------------------

def test_reads_every_named_image_with_its_platform_manifest(pkg):
    path, digests = pkg
    imgs = {i.repo: i for i in oci.read_images(path, "amd64")}
    assert set(imgs) == set(IMAGES)
    rt = imgs["gaussdb-agent-runtime"]
    assert rt.tag == "agent-v1.4.4-oc1.18.27", "-amd64 后缀要去掉,推到仓库是不带架构的标签"
    assert rt.source_ref == "gaussdb-agent-runtime:agent-v1.4.4-oc1.18.27-amd64"
    assert rt.manifest_digest == digests["gaussdb-agent-runtime"] == digest_of(rt.manifest)
    assert [b.digest for b in rt.blobs[1:]] == [digest_of(BASE), digest_of(b"runtime-app")]


def test_the_packers_own_arch_has_no_suffix(tmp_path):
    path = tmp_path / "pkg-arm64.tar.gz"
    make_package(path, "arm64", {"gaussdb-agent-runtime": [b"x"]}, suffix=False)
    assert oci.read_images(str(path), "arm64")[0].tag == "agent-v1.4.4-oc1.18.27"


def test_a_package_missing_a_blob_is_refused_before_anything_is_pushed(tmp_path):
    path = tmp_path / "bad.tar.gz"
    make_package(path, "amd64", {"gaussdb-agent-runtime": [b"x", b"y"]}, drop_blob=digest_of(b"y"))
    with pytest.raises(oci.OciError, match="不在包里"):
        oci.read_images(str(path), "amd64")


def test_the_wrong_arch_is_named_not_guessed(pkg):
    with pytest.raises(oci.OciError, match="linux/arm64"):
        oci.read_images(pkg[0], "arm64")


def test_a_classic_docker_save_is_reported_clearly(tmp_path):
    import io
    import tarfile
    path = tmp_path / "classic.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        ti = tarfile.TarInfo("manifest.json")
        ti.size = 2
        tf.addfile(ti, io.BytesIO(b"[]"))
    with pytest.raises(oci.OciError, match="不是 OCI 目录格式"):
        oci.read_images(str(path), "amd64")


# -- 推送 ----------------------------------------------------------------------------------------

def _push(r, path, **kw):
    c = _client(r, **kw)
    imgs = oci.read_images(path, "amd64")
    return c, registry.push_images(c, path, imgs, lambda i: i.tag)


def test_push_uploads_each_blob_once_and_mounts_the_shared_base(reg, pkg):
    path, digests = pkg
    _c, results = _push(reg, path)
    by = {r.repo: r for r in results}
    assert reg.count("PUT", "/blobs/uploads/") == 7, "每个不同的 blob 只上传一次:3 配置 + 共用底层 + 3 个应用层"
    assert sum(r.mounted for r in results) == 1, "第二个用到 base 的镜像靠跨仓库挂载"
    for name, d in digests.items():
        data, ctype = reg.manifests[("gaussdb-agent/" + name, "agent-v1.4.4-oc1.18.27")]
        assert digest_of(data) == d and ctype == "application/vnd.oci.image.manifest.v1+json"
    assert by["gaussdb-agent/gaussdb-agent-kbstd"].uploaded == 2


def test_a_second_push_finds_everything_and_uploads_nothing(reg, pkg):
    path, _d = pkg
    _push(reg, path)
    before = reg.count("PUT", "/blobs/uploads/")
    _c, results = _push(reg, path)
    assert reg.count("PUT", "/blobs/uploads/") == before
    got = {r.repo.split("/")[-1]: (r.uploaded, r.mounted, r.existing) for r in results}
    assert got == {"gaussdb-agent-runtime": (0, 0, 3), "gaussdb-agent-gateway": (0, 0, 3), "gaussdb-agent-kbstd": (0, 0, 2)}


def test_basic_and_bearer_auth(pkg):
    for auth in ("basic", "bearer"):
        r = FakeRegistry(auth=auth)
        try:
            c = _client(r, username="u", password="p")
            c.ping()
            _push(r, pkg[0], username="u", password="p")
            assert ("gaussdb-agent/gaussdb-agent-runtime", "agent-v1.4.4-oc1.18.27") in r.manifests, auth
        finally:
            r.close()


def test_wrong_password_names_the_problem(pkg):
    r = FakeRegistry(auth="bearer")
    try:
        with pytest.raises(registry.RegistryError, match="账号密码"):
            _client(r, username="u", password="wrong").ping()
    finally:
        r.close()


def test_a_digest_that_does_not_match_the_package_fails_the_push(reg, pkg):
    reg.digest_override = "sha256:" + "f" * 64
    with pytest.raises(registry.RegistryError, match="不一致"):
        _push(reg, pkg[0])


def test_without_cross_repo_mount_each_repo_uploads_its_own_copy(pkg):
    r = FakeRegistry(mount=False)
    try:
        with pytest.raises(registry.RegistryError, match="跨仓库挂载"):
            _push(r, pkg[0])
    finally:
        r.close()
