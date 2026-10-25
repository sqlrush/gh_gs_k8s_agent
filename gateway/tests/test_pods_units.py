"""网关的 Pod 生命周期策略:建什么、什么时候不重建、活动时间怎么记。

两个必须钉死的行为,都是踩过或查出来的:

① **口令不能在重建时被轮换。** scripts/k8s/provision.py:81 无条件 `secrets.token_hex(24)`
   重新生成 pod-auth-<工号>。2026-09-21 我为了验证改动重建了三次 u9003 的 Pod,
   用户正开着页面 —— 口令变了,他那边下一次请求就是 401,而页面上看不出为什么。
   网关必须:Secret 已存在就沿用,只有不存在时才生成。

② **活动时间要节流。** 每个请求都去 PATCH 一次 Deployment 注解的话,一个用户开着
   SSE 每 10 秒一跳就是每 10 秒一次 k8s 写操作,几十个用户就把 apiserver 打满了。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import pods  # noqa: E402


class FakeKube:
    """只记下被调用了什么,不真连 k8s。"""

    def __init__(self, existing=None):
        self.objects = dict(existing or {})     # (kind, name) -> manifest
        self.applied = []
        self.deleted = []
        self.patched = []

    def get(self, api, kind, name):
        return self.objects.get((kind, name))

    def list(self, api, kind, label_selector="", field_selector=""):
        return {"items": [v for (k, _n), v in self.objects.items() if k == kind]}

    def apply(self, api, kind, name, manifest):
        self.applied.append((kind, name, manifest))
        self.objects[(kind, name)] = manifest
        return manifest

    def patch(self, api, kind, name, patch):
        self.patched.append((kind, name, patch))
        return {}

    def delete(self, api, kind, name):
        self.deleted.append((kind, name))
        return self.objects.pop((kind, name), None) is not None


def _mgr(kube, **kw):
    opts = dict(namespace="gaussdb-agent", image_tag="t1", pull_policy="IfNotPresent",
                templates=_ROOT / "k8s" / "templates")
    opts.update(kw)
    return pods.Manager(kube, **opts)


# --- 口令:重建时必须沿用 -------------------------------------------------------

def test_password_is_generated_when_absent():
    k = FakeKube()
    pw = _mgr(k).ensure_password("u1234")
    assert len(pw) >= 32
    assert ("secrets", "pod-auth-u1234") in [(kind, name) for kind, name, _ in k.applied]


def test_existing_password_is_reused_not_rotated():
    """**核心**:重建 Pod 不能让用户手上的口令失效。"""
    import base64
    existing = base64.b64encode(b"already-issued-password").decode()
    k = FakeKube({("secrets", "pod-auth-u1234"): {"data": {"OPENCODE_SERVER_PASSWORD": existing}}})
    assert _mgr(k).ensure_password("u1234") == "already-issued-password"
    assert not k.applied, "已有口令时不该再写 Secret"


def test_corrupt_password_secret_is_replaced():
    """Secret 在但内容坏了(手工改过 / key 写错):重新生成,不要抛异常把用户挡在外面。"""
    k = FakeKube({("secrets", "pod-auth-u1234"): {"data": {}}})
    assert len(_mgr(k).ensure_password("u1234")) >= 32
    assert k.applied


# --- 建 Pod -------------------------------------------------------------------

def test_normal_user_gets_runtime_only():
    k = FakeKube()
    _mgr(k).ensure("u1234", kb_admin=False)
    names = [name for kind, name, _ in k.applied if kind == "deployments"]
    assert names == ["runtime-u1234"]


def test_kb_admin_also_gets_kb_import():
    k = FakeKube()
    _mgr(k).ensure("u2001", kb_admin=True)
    names = sorted(name for kind, name, _ in k.applied if kind == "deployments")
    assert names == ["kb-import-u2001", "runtime-u2001"]


def test_ensure_is_idempotent_on_the_deployment():
    """server-side apply,重复调用不报错也不产生第二份。"""
    k = FakeKube()
    m = _mgr(k)
    m.ensure("u1234", kb_admin=False)
    m.ensure("u1234", kb_admin=False)
    assert len([n for kind, n, _ in k.applied if kind == "deployments"]) == 2   # 两次 apply
    assert len([key for key in k.objects if key[0] == "deployments"]) == 1      # 但只有一份对象


def test_delete_removes_pods_and_secrets_but_not_nas():
    k = FakeKube({("deployments", "runtime-u1234"): {}, ("services", "runtime-u1234"): {},
                  ("secrets", "pod-auth-u1234"): {}})
    _mgr(k).remove("u1234")
    kinds = {kind for kind, _ in k.deleted}
    assert kinds <= {"deployments", "services", "secrets"}
    assert not any("persistentvolumeclaims" == kind for kind, _ in k.deleted), "NAS 与 PVC 绝不能删"


# --- 活动时间 -----------------------------------------------------------------

def test_activity_is_recorded_as_an_annotation():
    k = FakeKube()
    _mgr(k).touch("u1234", now=1000.0)
    kind, name, patch = k.patched[0]
    assert kind == "deployments" and name == "runtime-u1234"
    assert pods.ANNOTATION_ACTIVITY in patch["metadata"]["annotations"]


def test_activity_writes_are_throttled():
    """**核心**:SSE 每 10 秒一跳,不节流就是每 10 秒一次 k8s 写。"""
    k = FakeKube()
    m = _mgr(k, activity_interval_s=60)
    m.touch("u1234", now=1000.0)
    m.touch("u1234", now=1005.0)          # 5 秒后,不该再写
    m.touch("u1234", now=1005.0)
    assert len(k.patched) == 1
    m.touch("u1234", now=1070.0)          # 过了节流窗口
    assert len(k.patched) == 2


def test_throttle_is_per_user():
    k = FakeKube()
    m = _mgr(k, activity_interval_s=60)
    m.touch("u1234", now=1000.0)
    m.touch("u5678", now=1001.0)
    assert len(k.patched) == 2, "一个人的活动不能把另一个人的记录压掉"


class _PatchFails(FakeKube):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def patch(self, api, kind, name, patch):
        self.patched.append((kind, name, patch))
        raise self.exc


def test_touch_on_a_deleted_deployment_says_the_environment_is_gone():
    """**2026-10-14 抓到。** 用户的 Deployment 被删(注销工号 / 卸载重装)时事件流还开着,
    touch 的 PATCH 拿到 404 没人接,异常一路冒到 _route 的兜底 —— 那时响应头早发出去了,
    兜底再写一个 500 进分块流,连接就坏了。404 要变成一个明确的「环境已被回收」。"""
    from gateway.kube import KubeError
    m = _mgr(_PatchFails(KubeError("k8s PATCH ... → 404:not found", 404)))
    with pytest.raises(pods.EnvironmentGone):
        m.touch("u1234", now=1000.0)


def test_touch_failing_for_other_reasons_is_logged_not_raised():
    """k8s 一时连不上 / 超时不该打断用户正在看的流:记一行日志,下个节流窗口再试。"""
    from gateway.kube import KubeError
    logs = []
    for exc in (KubeError("连不上 k8s API", 0), KubeError("k8s PATCH → 500", 500), TimeoutError("read timed out")):
        m = _mgr(_PatchFails(exc), log=logs.append)
        assert m.touch("u1234", now=1000.0) is False
    assert len(logs) == 3 and all("u1234" in x for x in logs)


# --- 前置条件:按人签发的 GRMP 令牌 --------------------------------------------

def test_missing_grmp_token_secret_is_named_explicitly():
    """**2026-09-21 真跑时踩到。**

    runtime 要本人的 grmp-token-<工号> Secret,那是客户中间件按人签发的,网关不创建它。
    缺了它 Pod 永远起不来,而网关原来只报「120 秒内未就绪」—— 把人往「启动慢 / 镜像大」
    的方向带,真原因(少一个 Secret)没人看得到。
    """
    k = FakeKube()
    with pytest.raises(pods.PrerequisiteMissing) as exc:
        _mgr(k).require_prerequisites("u1234", kb_admin=False)
    msg = str(exc.value)
    assert "grmp-token-u1234" in msg, "要点名缺的是哪个 Secret"
    assert "网关不创建" in msg, "要说明为什么不是网关的责任,否则运维会去找网关的 bug"


def test_prerequisites_pass_when_the_token_exists():
    k = FakeKube({("secrets", "grmp-token-u1234"): {"data": {"GRMP_AUTH_TOKEN": "x"}}})
    _mgr(k).require_prerequisites("u1234", kb_admin=False)      # 不抛就是通过


def test_pool_mode_builds_only_the_import_container():
    """固定池化:诊断容器来自池子。给管理员建导入容器时不能顺手建 runtime-<工号> ——
    按需 Pod 会抢走这个人的独占标记,池子就再也分不到槽位(2026-10-24 测试环境实测)。"""
    k = FakeKube()
    mgr = _mgr(k, runtime_on_demand=False)
    assert mgr.kinds_for(kb_admin=True) == [pods.KIND_KB_IMPORT] and mgr.kinds_for(kb_admin=False) == []
    mgr.require_prerequisites("u2001", kb_admin=True)          # 不建 runtime,就不要求 GRMP 令牌
    mgr.ensure("u2001", kb_admin=True)
    assert sorted(name for kind, name, _ in k.applied if kind == "deployments") == ["kb-import-u2001"]


def test_kb_admin_only_user_needs_no_grmp_token():
    """kb-import 不调中间件,所以没有令牌也能开 —— 但 kinds_for 里总有 runtime,
    所以这条实际上等价于「只要建 runtime 就必须有令牌」。这里钉住语义别被改坏。"""
    assert pods.KIND_RUNTIME in _mgr(FakeKube()).kinds_for(kb_admin=True)


def test_restarting_a_scaled_down_pod_starts_a_fresh_grace_period():
    """重新拉起被闲置回收过的 Pod 时,活动时间必须写成「现在」。

    2026-09-24 客户现场:scale 模式下 Deployment 保留,创建时间停在十几小时前;
    再登录时网关按模板 apply,模板里活动注解是空值,把原来的活动时间清掉 ——
    回收器于是按创建时间算出「闲置 15 小时」,下一轮扫描就把刚拉起、还在 ContainerCreating
    的 Pod 缩回 0。用户看到的是 504,网关日志反复「闲置回收:已缩容」。
    """
    from gateway import reaper
    k = FakeKube()
    now = 1_800_000_000.0
    _mgr(k).ensure("u1234", kb_admin=True, now=now)
    deps = [m for kind, _n, m in k.applied if kind == "deployments"]
    assert len(deps) == 2
    for d in deps:
        assert d["metadata"]["annotations"][reaper.ANNOTATION_ACTIVITY] == "%d" % int(now)
        # 十几小时前创建的 Deployment,刚拉起时也不能被判成闲置
        d = {**d, "metadata": {**d["metadata"], "creationTimestamp": "2026-09-23T00:00:00Z"},
             "spec": {**d["spec"], "replicas": 1}}
        assert not reaper.should_reap(d, now + 119, reaper.DEFAULT_IDLE_SECONDS)


# --- 镜像名:内网仓库前缀 --------------------------------------------------------

def _images(mgr, kind):
    docs = mgr._render(kind, "u1234")
    dep = [d for d in docs if d.get("kind") == "Deployment"][0]
    return [c["image"] for c in dep["spec"]["template"]["spec"]["containers"]]


def test_user_pod_image_has_no_prefix_by_default():
    assert _images(_mgr(FakeKube()), "runtime") == ["gaussdb-agent-runtime:t1"] * 2   # opencode 与凭据代理同一个镜像


def test_user_pod_image_carries_the_registry_prefix():
    """**原来没有这一项。** 镜像推在内网仓库时,不带前缀的名字会被节点当成 docker.io 上的镜像去拉,
    用户一登录就 ImagePullBackOff(504)。手册里对 k8s/templates/ 的 sed 改不到它:模板里是 ${IMAGE}。"""
    mgr = _mgr(FakeKube(), image_registry="registry.example/gaussdb-agent/")
    assert _images(mgr, "runtime") == ["registry.example/gaussdb-agent/gaussdb-agent-runtime:t1"] * 2
    assert _images(mgr, "kb-import") == ["registry.example/gaussdb-agent/gaussdb-agent-kb-import:t1"] * 2


# --- 知识库管理员名单:ConfigMap agent-roles ------------------------------------
# 部署手册 §9.1 / k8s-deploy.md:SSO 不传角色时,把管理员工号写进 agent-roles 的 kb-admins。
# 2026-10-14 才发现网关**从来不读**这张表 —— 照手册写进去的管理员拿不到导入容器,也没有任何报错。

def _roles_cm(text):
    return {("configmaps", "agent-roles"): {"data": {"kb-admins": text}}}


def test_kb_admins_are_read_one_per_line_case_insensitive_ignoring_comments():
    m = _mgr(FakeKube(_roles_cm("U2001\n  u2002  \n\n# u2003 已调岗\nu2004 # 临时\n")))
    assert m.kb_admins(now=1000.0) == frozenset({"u2001", "u2002", "u2004"})


def test_kb_admins_without_the_configmap_is_an_empty_list():
    assert _mgr(FakeKube()).kb_admins(now=1000.0) == frozenset()


def test_kb_admins_are_cached_so_every_request_is_not_a_k8s_read():
    class Counting(FakeKube):
        gets = 0

        def get(self, api, kind, name):
            Counting.gets += 1
            return super().get(api, kind, name)
    k = Counting(_roles_cm("u2001\n"))
    m = _mgr(k)
    for t in (1000.0, 1005.0, 1029.0):
        m.kb_admins(now=t)
    assert Counting.gets == 1
    k.objects[("configmaps", "agent-roles")] = {"data": {"kb-admins": "u2001\nu2009\n"}}
    assert m.kb_admins(now=1031.0) == frozenset({"u2001", "u2009"}), "过了缓存期要看到运维新加的人"


def test_kb_admins_read_failure_keeps_the_last_good_list_and_logs():
    """k8s 一时读不到不该让管理员突然变成普通用户(他会被当成没有导入容器)。"""
    from gateway.kube import KubeError

    class Flaky(FakeKube):
        fail = False

        def get(self, api, kind, name):
            if Flaky.fail:
                raise KubeError("k8s GET → 503", 503)
            return super().get(api, kind, name)
    logs = []
    m = _mgr(Flaky(_roles_cm("u2001\n")), log=logs.append)
    assert m.kb_admins(now=1000.0) == frozenset({"u2001"})
    Flaky.fail = True
    assert m.kb_admins(now=1100.0) == frozenset({"u2001"})
    assert logs and "agent-roles" in logs[0]



# --- 起不来时读原因 ------------------------------------------------------------

def test_diagnose_without_permission_logs_once_and_returns_none():
    """升级时漏了重新 apply 网关的 Role:读 Pod 403。不能每 6 秒刷一次日志,也不能让请求失败。"""
    from gateway.kube import KubeError

    class NoPods(FakeKube):
        def list(self, api, kind, label_selector="", field_selector=""):
            raise KubeError("pods is forbidden", 403)
    logs = []
    m = _mgr(NoPods(), log=logs.append)
    assert m.diagnose("u1234") is None and m.diagnose("u1234") is None
    assert len(logs) == 1 and "gateway.yaml" in logs[0]



def test_touch_can_record_activity_on_the_import_container_separately():
    k = FakeKube()
    m = _mgr(k, activity_interval_s=60)
    m.touch("u2001", now=1000.0)
    m.touch("u2001", now=1001.0, kind=pods.KIND_KB_IMPORT)
    assert [n for _k, n, _p in k.patched] == ["runtime-u2001", "kb-import-u2001"], "两个容器各自节流、各记各的"



def test_retire_deletes_one_container_and_nothing_else():
    k = FakeKube()
    _mgr(k).retire("u2001", pods.KIND_KB_IMPORT)
    assert sorted(k.deleted) == [("deployments", "kb-import-u2001"), ("services", "kb-import-u2001")], \
        "闲置回收不能删口令和平台签发的 grmp-token"
