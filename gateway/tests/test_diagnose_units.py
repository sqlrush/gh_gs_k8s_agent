"""用户 Pod 起不来时的原因归类(v1.1.6 待修第 2 项)。

**现场(2026-09-24):** 用户等满 120 秒只拿到「您的环境正在启动,请稍后重试」,网关日志里没有原因;
客户靠 `kubectl describe pod` 才看到是 NAS / 镜像 / 目录属主中的哪一个。等下去也不会好的那几类
(拉不到镜像、配置缺失、个人目录初始化失败)还让用户白等了两分钟。

classify 是纯函数:Pod 对象 + 它的事件 → 一类原因、给运维的完整说明、是否「等下去也不会好」。
给用户看的 category 不能带内部资源名(Secret 名、镜像仓库地址),那些只进 detail。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import diagnose as dg  # noqa: E402
from gateway.kube import KubeError  # noqa: E402


def _pod(name="runtime-u1-abc", phase="Pending", init=None, main=None, conditions=None, created="2026-10-16T01:00:00Z", deleting=False):
    meta = {"name": name, "creationTimestamp": created}
    if deleting:
        meta["deletionTimestamp"] = "2026-10-16T01:05:00Z"
    st = {"phase": phase}
    if init is not None:
        st["initContainerStatuses"] = [dict(name="prepare-home", **init)]
    if main is not None:
        st["containerStatuses"] = [dict(name="opencode", **main)]
    if conditions is not None:
        st["conditions"] = conditions
    return {"metadata": meta, "status": st}


def _ev(reason, message, kind="Warning", at="2026-10-16T01:01:00Z"):
    return {"type": kind, "reason": reason, "message": message, "lastTimestamp": at}


def test_image_pull_backoff_is_permanent_and_hides_the_registry_from_the_user():
    p = _pod(main={"state": {"waiting": {"reason": "ImagePullBackOff",
                                         "message": 'Back-off pulling image "registry.corp/gaussdb-agent-runtime:t1"'}}})
    d = dg.classify(p, [])
    assert d.category == dg.IMAGE and d.permanent
    assert "registry.corp" in d.detail and "registry.corp" not in d.category


def test_first_err_image_pull_is_reported_but_not_yet_given_up_on():
    d = dg.classify(_pod(main={"state": {"waiting": {"reason": "ErrImagePull", "message": "timeout"}}}), [])
    assert d.category == dg.IMAGE and not d.permanent


def test_missing_secret_key_is_a_config_error_and_permanent():
    p = _pod(main={"state": {"waiting": {"reason": "CreateContainerConfigError",
                                         "message": 'secret "model-api" not found'}}})
    d = dg.classify(p, [])
    assert d.category == dg.CONFIG and d.permanent
    assert "model-api" in d.detail and "model-api" not in d.category


def test_prepare_home_failure_carries_its_own_message():
    """初始化容器失败时它的终止消息(terminationMessagePolicy=FallbackToLogsOnError)就是原因原文。"""
    msg = "个人目录 users/u1 不可写(属主 0:0、权限 drwxr-xr-x)…或执行 k8s/tools/runtime-doctor.sh u1 --fix"
    for st in ({"state": {"terminated": {"exitCode": 1, "reason": "Error", "message": msg}}},
               {"state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {"terminated": {"exitCode": 1, "reason": "Error", "message": msg}}}):
        d = dg.classify(_pod(init=st), [])
        assert d.category == dg.HOME and d.permanent
        assert "runtime-doctor.sh u1 --fix" in d.detail


def test_unschedulable_is_reported_but_may_still_resolve():
    p = _pod(conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                          "message": "0/3 nodes are available: 3 Insufficient memory."}])
    d = dg.classify(p, [])
    assert d.category == dg.SCHEDULE and not d.permanent and "Insufficient memory" in d.detail


def test_failed_mount_event_means_the_nas():
    d = dg.classify(_pod(), [_ev("FailedMount", "MountVolume.SetUp failed for volume \"nas-pv\" : mount failed: exit status 32")])
    assert d.category == dg.MOUNT and "exit status 32" in d.detail


def test_crash_loop_in_the_main_container_shows_the_last_exit():
    p = _pod(phase="Running", main={"state": {"waiting": {"reason": "CrashLoopBackOff"}}, "restartCount": 4,
                                    "lastState": {"terminated": {"exitCode": 1, "reason": "Error"}}})
    d = dg.classify(p, [])
    assert d.category == dg.CRASH and "退出码 1" in d.detail and "4" in d.detail
    assert not d.permanent, "独占标记被上一个 Pod 占着时会自己恢复(90 秒后过期),不能提前放弃"


def test_plain_slow_start_is_still_starting():
    d = dg.classify(_pod(phase="Pending", init={"state": {"running": {}}}), [_ev("Pulling", "Pulling image", kind="Normal")])
    assert d.category == dg.STARTING and not d.permanent


def test_warning_events_are_appended_newest_last_and_capped():
    evs = [_ev("BackOff", "m%d" % i, at="2026-10-16T01:0%d:00Z" % i) for i in range(6)]
    d = dg.classify(_pod(), evs)
    assert "m5" in d.detail and "m0" not in d.detail


# --- 读取层 -------------------------------------------------------------------

class _Kube:
    def __init__(self, pods=None, events=None, fail=None):
        self.pods, self.events, self.fail, self.calls = pods or [], events or {}, fail, []

    def list(self, api, kind, label_selector="", field_selector=""):
        self.calls.append((kind, label_selector, field_selector))
        if self.fail and kind in self.fail:
            raise self.fail[kind]
        if kind == "pods":
            return {"items": self.pods}
        name = field_selector.split("=", 1)[1]
        return {"items": self.events.get(name, [])}


def test_reader_picks_the_newest_live_pod_of_this_user_and_kind():
    old = _pod("runtime-u1-old", created="2026-10-16T00:00:00Z", main={"state": {"waiting": {"reason": "CreateContainerConfigError"}}})
    gone = _pod("runtime-u1-zzz", created="2026-10-16T02:00:00Z", deleting=True)
    new = _pod("runtime-u1-new", created="2026-10-16T01:00:00Z", main={"state": {"waiting": {"reason": "ImagePullBackOff"}}})
    k = _Kube([old, gone, new])
    d = dg.read(k, "u1", "runtime")
    assert d.category == dg.IMAGE and "runtime-u1-new" in d.detail
    assert ("pods", "app=gaussdb-agent,role=runtime,user=u1", "") in k.calls
    assert ("events", "", "involvedObject.name=runtime-u1-new") in k.calls


def test_reader_without_any_pod_says_so():
    d = dg.read(_Kube([]), "u1", "runtime")
    assert d.category == dg.NO_POD and not d.permanent


def test_reader_still_classifies_when_events_are_not_readable():
    p = _pod(main={"state": {"waiting": {"reason": "ImagePullBackOff"}}})
    d = dg.read(_Kube([p], fail={"events": KubeError("forbidden", 403)}), "u1", "runtime")
    assert d.category == dg.IMAGE


def test_reader_without_pod_permission_raises_for_the_caller_to_fall_back():
    with pytest.raises(KubeError):
        dg.read(_Kube(fail={"pods": KubeError("forbidden", 403)}), "u1", "runtime")
