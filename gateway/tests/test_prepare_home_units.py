"""新工号首次登录时的个人目录(v1.1.6 待修第 1 项)。

**现场形态(2026-09-24 客户、2026-10-15 全新 NAS 实测):** 新工号第一次登录时 `users/<工号>` 还不存在,
主容器的 subPath 挂载让 kubelet 以 **root** 把它建出来;Pod 以 uid 1000 运行 → 写不进 → opencode 反复崩溃重启。
以前只能靠 `runtime-doctor.sh <工号> --fix` 预先建目录绕过。

修法是 runtime / kb-import 两个模板里的初始化容器 prepare-home:以 uid 1000 挂上**父目录**(users/ 或 admins/)
把工号目录建好并确认可写。这里两类测试:
① 清单:挂的是父目录不是工号目录(挂到工号这一级,kubelet 一样先以 root 建出来 —— 等于没修);
   与主容器同样的只读根 / 不提权;失败原因能被网关读到(terminationMessagePolicy)。
② 行为:把渲染出来的脚本拿出来,在临时目录里真跑三种情况。
"""
import os
import pathlib
import string
import subprocess

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TEMPLATES = {"runtime": "users", "kb-import": "admins"}


def _render(kind: str, uid: str = "u1234") -> dict:
    text = (_ROOT / "k8s" / "templates" / ("%s.yaml" % kind)).read_text(encoding="utf-8")
    out = string.Template(text).substitute(USER_ID=uid, IMAGE="img:t", IMAGE_PULL_POLICY="IfNotPresent",
                                           SUBPATH_ROOT=_TEMPLATES[kind])
    return next(d for d in yaml.safe_load_all(out) if d and d.get("kind") == "Deployment")


def _init(kind: str) -> dict:
    spec = _render(kind)["spec"]["template"]["spec"]
    inits = {c["name"]: c for c in spec.get("initContainers") or []}
    assert "prepare-home" in inits, "%s 模板没有 prepare-home 初始化容器" % kind
    return inits["prepare-home"]


@pytest.mark.parametrize("kind", sorted(_TEMPLATES))
def test_prepare_home_mounts_the_parent_dir_not_the_user_dir(kind):
    mounts = _init(kind)["volumeMounts"]
    assert [m.get("subPath") for m in mounts] == [_TEMPLATES[kind]], \
        "只能挂父目录 %s/:挂到 %s/<工号> 的话 kubelet 会先以 root 建出它,初始化容器就来不及了" % (_TEMPLATES[kind],) * 2


@pytest.mark.parametrize("kind", sorted(_TEMPLATES))
def test_prepare_home_is_as_locked_down_as_the_main_container(kind):
    dep = _render(kind)
    spec = dep["spec"]["template"]["spec"]
    init = _init(kind)
    main = spec["containers"][0]
    assert spec["securityContext"]["runAsUser"] == 1000 and spec["securityContext"]["runAsNonRoot"] is True
    assert init["securityContext"] == main["securityContext"], "初始化容器不能比主容器权限大"
    assert init["image"] == main["image"], "用同一个镜像,不额外拉一个"
    assert init.get("terminationMessagePolicy") == "FallbackToLogsOnError", "失败原因要能被网关从 Pod 状态里读到"
    assert "limits" in init.get("resources", {})


@pytest.mark.parametrize("kind", sorted(_TEMPLATES))
def test_main_container_still_sees_only_its_own_dir(kind):
    """隔离不变:模型所在的主容器只挂本人那一份;整个 users/ 只有初始化容器挂得到。"""
    main = _render(kind)["spec"]["template"]["spec"]["containers"][0]
    me = [m for m in main["volumeMounts"] if m["mountPath"] == "/nas/me"]
    assert me and me[0]["subPath"] == "%s/u1234" % _TEMPLATES[kind]


def _script(kind: str, root: pathlib.Path) -> str:
    cmd = _init(kind)["command"]
    assert cmd[:2] == ["sh", "-c"]
    assert "/nas/root" in cmd[2]
    return cmd[2].replace("/nas/root", str(root))


def _run(kind: str, root: pathlib.Path):
    return subprocess.run(["sh", "-c", _script(kind, root)], capture_output=True, text=True, timeout=20)


skip_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 无视目录权限,测不出")


@pytest.mark.parametrize("kind", sorted(_TEMPLATES))
def test_new_user_dir_is_created_and_left_clean(kind, tmp_path):
    r = _run(kind, tmp_path)
    assert r.returncode == 0, r.stderr
    d = tmp_path / "u1234"
    assert d.is_dir() and os.access(d, os.W_OK)
    assert list(d.iterdir()) == [], "探针文件要删掉,不能留在用户目录里"
    assert _run(kind, tmp_path).returncode == 0, "第二次登录(目录已在)要照常通过"


@skip_root
def test_unwritable_parent_fails_with_a_message_that_says_what_to_do(tmp_path):
    tmp_path.chmod(0o555)
    try:
        r = _run("runtime", tmp_path)
    finally:
        tmp_path.chmod(0o755)
    assert r.returncode != 0
    msg = r.stderr
    assert "users/u1234" in msg and "users/" in msg
    assert "1000" in msg and "runtime-doctor.sh u1234 --fix" in msg, msg


@skip_root
def test_existing_root_owned_user_dir_fails_and_points_at_the_fix(tmp_path):
    """之前某次失败的登录已让 kubelet 以 root 建出了目录:uid 1000 改不了属主,只能指给运维。"""
    d = tmp_path / "u1234"
    d.mkdir()
    d.chmod(0o555)
    try:
        r = _run("runtime", tmp_path)
    finally:
        d.chmod(0o755)
    assert r.returncode != 0
    assert "不可写" in r.stderr and "runtime-doctor.sh u1234 --fix" in r.stderr, r.stderr
    # 属主不是 root(这里是跑测试的账号)时不能瞎猜「被 kubelet 以 root 建的」—— 2026-10-16 测试环境上就猜错过
    assert "kubelet" not in r.stderr
