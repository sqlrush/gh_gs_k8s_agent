"""build-images.sh 的推送闸。

2026-09-22 客户在 x86 服务器上拉 GHCR 上的镜像,三个标签全报:

    no matching manifest for linux/amd64/v3 in the manifest list entries

原因不是权限也不是标签写错 —— 标签在,只是那个 manifest list 里**只有 arm64**。
镜像是在 Mac(aarch64)上 `docker tag 本地镜像 → docker push` 推上去的,而本地那个
plain 标签按打包约定就是本机架构(amd64 那份在本地叫 `<TAG>-amd64`,从没推过)。

推送方式本身没有任何报错:push 成功、标签能解析、在 Mac 上 pull 回来也正常 ——
只有客户那台 x86 机器会失败。所以闸门放在「推之前」:
  · `--push` 必须给 `--registry`,否则 tag 没有仓库前缀,推到哪里去都说不清;
  · `--push` 必须是多架构,除非显式写 `--single-arch` 认下这件事。
"""
import os
import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "build-images.sh"
_FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$DOCKER_CALLS"
exit 0
"""


@pytest.fixture
def run(tmp_path):
    """用假 docker 跑脚本,返回 (完成对象, 每次 docker 调用的参数行)。"""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake = bin_ / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    calls = tmp_path / "calls.txt"

    def _run(*args):
        env = dict(os.environ)
        env["PATH"] = "%s:%s" % (bin_, env.get("PATH", ""))
        env["DOCKER_CALLS"] = str(calls)
        proc = subprocess.run([str(_SCRIPT), *args], env=env,
                              capture_output=True, text=True)
        lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return proc, lines
    return _run


def test_bash_is_available():
    assert shutil.which("bash"), "这组测试要 bash"


def test_push_without_registry_is_refused(run):
    """没有仓库前缀的 `--push`:tag 是 `gaussdb-agent-runtime:<TAG>`,
    推到 docker.io/library 去 —— 要么失败,要么推到一个谁都不该有权限的地方。"""
    proc, calls = run("t1", "--push")
    assert proc.returncode != 0
    assert "registry" in (proc.stderr + proc.stdout).lower()
    assert not calls, "拒绝之前不应该已经调过 docker:%s" % calls


def test_single_arch_push_is_refused_unless_acknowledged(run):
    """**这就是客户那次的形态。** 不带 --platform 的 --push 只推本机架构。"""
    proc, calls = run("t1", "--push", "--registry", "ghcr.io/sqlrush")
    assert proc.returncode != 0
    out = proc.stderr + proc.stdout
    assert "--platform" in out and "single-arch" in out
    assert not calls


def test_single_arch_push_goes_through_when_acknowledged(run):
    proc, calls = run("t1", "--push", "--registry", "ghcr.io/sqlrush", "--single-arch")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 3, calls          # runtime / kb-import / gateway
    for line in calls:
        assert "ghcr.io/sqlrush/gaussdb-agent-" in line and "--push" in line


def test_multiarch_push_tags_carry_the_registry(run):
    proc, calls = run("t1", "--push", "--registry", "ghcr.io/sqlrush",
                      "--platform", "linux/amd64,linux/arm64")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 3, calls
    for name, line in zip(("runtime", "kb-import", "gateway"), calls):
        assert "-t ghcr.io/sqlrush/gaussdb-agent-%s:t1" % name in line, line
        assert "--platform linux/amd64,linux/arm64" in line
        assert "--push" in line and "--load" not in line


def test_local_build_keeps_plain_tags_and_loads(run):
    """本地构建不变:不带仓库前缀、--load 进本机镜像库。"""
    proc, calls = run("t1")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 3, calls
    for line in calls:
        assert "--load" in line and "--push" not in line
        assert "-t gaussdb-agent-" in line and "ghcr.io" not in line


def test_kbstd_is_built_only_from_a_directory_that_holds_a_pack(run, tmp_path):
    """标准知识库镜像的包不在代码仓库里:给了 --kbstd 才构建第 5 个,目录里没有包就拒绝;不给就照旧三个并说明。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    proc, calls = run("t1", "--kbstd", str(empty))
    assert proc.returncode == 2 and "kb-std-" in proc.stderr and not calls
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "kb-std-G25132C-abc.tar.gz").write_bytes(b"x")
    proc, calls = run("t1", "--kbstd", str(packs))
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 4 and "Dockerfile.kbstd" in calls[-1] and "--build-context kbstd=%s" % packs in calls[-1]
    assert "-t gaussdb-agent-kbstd:t1" in calls[-1] and "--load" in calls[-1]
    proc, calls = run("t2")
    assert "没构建 gaussdb-agent-kbstd" in proc.stdout
