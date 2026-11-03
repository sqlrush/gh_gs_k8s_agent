"""发版台随离线包发出:镜像单独一个 tar、release-console.sh 一条命令起(假 docker)。

钉住:
① 发版台的 tar 不会被 release/pack.py 与发版台的包识别当成「要推进客户镜像仓库的镜像包」;
② 启动前核 sha256,拷坏了不导入;
③ 起容器:宿主机网络、只听 127.0.0.1、用本机用户、目录按宿主机原路径挂(发布包目录只读),
   kubeconfig 引用的证书在别的目录也挂进去,用 exec 认证插件时提示;
④ 同一版本已在跑只打印地址;有发布正在执行时不替换。
"""
import fnmatch
import gzip
import hashlib
import os
import pathlib
import platform
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_SCRIPT = _ROOT / "scripts" / "release-console.sh"
_PACKAGER = _ROOT / "scripts" / "package-images.sh"
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]

_FAKE_DOCKER = r"""#!/bin/sh
echo "$@" >> "$DOCKER_CALLS"
case "$1" in
  load) cat >/dev/null; echo "Loaded image: gaussdb-agent-release:t2" ;;
  image) echo "sha256:new" ;;
  inspect)
    [ -n "$FAKE_EXISTING" ] || exit 1
    case "$*" in
      *Config.Image*) echo "$FAKE_EXISTING" ;;
      *.Image*) echo "${FAKE_EXISTING_ID:-sha256:old}" ;;
      *State.Running*) echo "${FAKE_RUNNING:-true}" ;;
      *Config.Env*) echo "RELEASE_LISTEN=127.0.0.1:8090" ;;
      *) echo "{}" ;;
    esac ;;
  exec) case "$*" in *runs/*) printf '%s' "$FAKE_BUSY" ;; esac ;;
esac
exit 0
"""


def test_console_tar_is_not_taken_for_an_image_package():
    from release import package
    body = _PACKAGER.read_text(encoding="utf-8")
    assert 'ROUT="$DIST/gaussdb-agent-release-${PKG#gaussdb-agent-}-$ARCH.tar.gz"' in body
    assert 'install -m 0755 "$HERE/scripts/release-console.sh" "$DIST/release-console.sh"' in body
    name = "gaussdb-agent-release-v1.4.4-oc1.18.27-amd64.tar.gz"
    assert not package.NAME.match(name), "发版台识别包时会把它当成 amd64 镜像包"
    assert not fnmatch.fnmatch(name, "gaussdb-agent-v1.4.4-oc1.18.27-*.tar.gz"), "pack.py 会把它的镜像写进 release.yaml、推进客户仓库"


@pytest.fixture
def env(tmp_path):
    """发布包目录 / 包目录(带发版台 tar 与 sha256)/ kubeconfig / 假 docker。"""
    releases = tmp_path / "releases"
    pkg = releases / "gaussdb-agent-v1.4.4-oc1.18.27"
    pkg.mkdir(parents=True)
    script = pkg / "release-console.sh"
    script.write_bytes(_SCRIPT.read_bytes())
    script.chmod(0o755)
    tar = pkg / ("gaussdb-agent-release-v1.4.4-oc1.18.27-%s.tar.gz" % _ARCH)
    tar.write_bytes(gzip.compress(b"fake image"))
    (pkg / (tar.name + ".sha256")).write_text("%s  %s\n" % (hashlib.sha256(tar.read_bytes()).hexdigest(), tar.name))
    kdir = tmp_path / "home" / ".kube"
    kdir.mkdir(parents=True)
    certs = tmp_path / "pki"
    certs.mkdir()
    (certs / "ca.crt").write_text("x")
    (kdir / "config").write_text("clusters:\n- cluster:\n    certificate-authority: %s/ca.crt\n" % certs)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(_FAKE_DOCKER)
    (bin_ / "docker").chmod(0o755)
    calls = tmp_path / "calls.txt"

    def run(*args, **extra):
        e = dict(os.environ, PATH="%s:%s" % (bin_, os.environ["PATH"]), DOCKER_CALLS=str(calls),
                 HOME=str(tmp_path / "home"), KUBECONFIG=str(kdir / "config"), **extra)
        p = subprocess.run(["bash", str(script), *args], env=e, capture_output=True, text=True, timeout=60)
        lines = calls.read_text().splitlines() if calls.exists() else []
        calls.unlink(missing_ok=True)
        return p, lines
    return {"run": run, "releases": releases, "pkg": pkg, "tar": tar, "kdir": kdir, "certs": certs, "home": tmp_path / "home"}


def test_a_broken_copy_is_not_loaded(env):
    env["tar"].write_bytes(gzip.compress(b"damaged"))
    p, calls = env["run"]()
    assert p.returncode == 1 and "sha256 对不上" in p.stderr
    assert not any(c.startswith(("load", "run")) for c in calls)


def test_fresh_start_uses_host_network_loopback_own_user_and_same_paths(env):
    p, calls = env["run"]()
    assert p.returncode == 0, p.stderr
    run = next(c for c in calls if c.startswith("run "))
    rel, kdir, state = env["releases"], env["kdir"], env["home"] / "gaussdb-release-state"
    assert "--network host" in run and "--user %d:%d" % (os.getuid(), os.getgid()) in run
    assert "-e RELEASE_LISTEN=127.0.0.1:8090" in run and "-e RELEASE_DIR=%s" % rel in run and "-e RELEASE_STATE=%s" % state in run
    assert "-v %s:%s:ro" % (rel, rel) in run, "发布包目录只读、原路径"
    assert "-v %s:%s " % (state, state) in run and "-v %s:%s:ro" % (kdir, kdir) in run
    assert "-v %s/ca.crt:%s/ca.crt:ro" % (env["certs"], env["certs"]) in run, "kubeconfig 引用的证书在别的目录"
    assert run.rstrip().endswith("gaussdb-agent-release:t2"), "用 docker load 报出来的镜像名(另一架构的包标签带 -amd64)"
    assert oct(state.stat().st_mode & 0o777) == "0o700"
    assert "http://127.0.0.1:8090/" in p.stdout and "ssh -L 8090:127.0.0.1:8090" in p.stdout


def test_exec_auth_plugin_is_called_out(env):
    (env["kdir"] / "config").write_text("users:\n- user:\n    exec:\n      command: cce-auth\n")
    p, _ = env["run"]()
    assert p.returncode == 0 and "exec 认证插件" in p.stderr


def test_same_version_already_running_only_prints_the_address(env):
    p, calls = env["run"](FAKE_EXISTING="gaussdb-agent-release:t2", FAKE_EXISTING_ID="sha256:new")
    assert p.returncode == 0 and "已在运行" in p.stdout
    assert not any(c.startswith(("run ", "rm ")) for c in calls)


def test_same_tag_repacked_is_still_replaced(env):
    """同一标签重新打过包(热修)按镜像 ID 判断:不能因为名字一样就留着旧的跑。"""
    p, calls = env["run"](FAKE_EXISTING="gaussdb-agent-release:t2", FAKE_EXISTING_ID="sha256:old", FAKE_BUSY="")
    assert p.returncode == 0 and "替换旧发版台" in p.stdout and any(c.startswith("run ") for c in calls)


def test_a_release_in_progress_blocks_replacing_the_console(env):
    p, calls = env["run"](FAKE_EXISTING="gaussdb-agent-release:t1", FAKE_BUSY="7")
    assert p.returncode == 1 and "发布 #7 正在执行" in p.stderr
    assert not any(c.startswith(("run ", "rm ")) for c in calls)
    p, calls = env["run"]("--force", FAKE_EXISTING="gaussdb-agent-release:t1", FAKE_BUSY="7")
    assert p.returncode == 0 and any(c.startswith("rm -f gaussdb-release") for c in calls) and any(c.startswith("run ") for c in calls)


def test_an_idle_old_console_is_replaced_keeping_the_state_dir(env):
    p, calls = env["run"](FAKE_EXISTING="gaussdb-agent-release:t1", FAKE_BUSY="")
    assert p.returncode == 0, p.stderr
    assert "替换旧发版台(gaussdb-agent-release:t1 → gaussdb-agent-release:t2)" in p.stdout
    assert [c.split()[0] for c in calls if c.split()[0] in ("rm", "run")] == ["rm", "run"]
