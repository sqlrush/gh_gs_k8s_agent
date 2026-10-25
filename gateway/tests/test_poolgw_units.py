"""网关的固定池化部分(2026-10-23):池子视图、分配落地、后台清扫、运维接口、管家客户端。

FakeCluster 同时扮演 k8s(Pod 列表、containerID、GRMP 令牌 Secret)与各 Pod 的管家(状态机的要点:
Pod 级 CAS、预留、独占、腾挪后换 containerID)。管家本身的语义在 docker/tests 里测;这里测网关把
「decide 的结论」落成调用、处理 409 与等待的那一层。客户端对着真的管家 HTTP 服务测签名与加密。
"""
import base64
import pathlib
import sys
import threading
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "docker"))

from gateway import poolalloc as pa  # noqa: E402
from gateway import poolclient, poolrouter, poolsweeper, pooltable  # noqa: E402
from gateway.poolclient import StewardError  # noqa: E402


class Slot:
    def __init__(self, i):
        self.i, self.state, self.user = i, pa.FREE, None
        self.password = self.reports_token = None
        self.last_active = self.busy_at = self.bound_at = self.retained_at = 0.0
        self.dirty = self.stale = self.offender = False
        self.listener = "ok"
        self.reserved_for, self.reserved_until = None, 0.0
        self.evict_cid = None


class FakeCluster:
    def __init__(self, pods=2, slots=2, now=1_800_000_000.0):
        self.now = now
        self.pods = {"pool-%d" % p: [Slot(i) for i in range(slots)] for p in range(pods)}
        self.cids = {(p, i): 1 for p in self.pods for i in range(slots)}
        self.down = set()
        self.owner = {}                     # 工号 → 持有者(「pool-x/i」或按需模式的 Pod 名)
        self.calls = []
        self.restart_delay = 0              # 腾挪后要被 fresh_meta 读几次才「重启完」
        self._pending = {}
        self.before_bind = None             # 测并发:bind 之前插一脚
        self.timeout_s = 1.0
        self.tokens = {}
        self.lock = threading.Lock()

    # -- k8s ------------------------------------------------------------------------------------
    def _pod(self, name):
        cs = [{"name": "slot-%d" % i, "containerID": "docker://%s-%d-%d" % (name, i, self.cids[(name, i)]),
               "state": {"running": {"startedAt": "2026-10-23T00:00:00Z"}}} for i in range(len(self.pods[name]))]
        cs.append({"name": "steward", "containerID": "docker://st", "state": {"running": {}}})
        return {"metadata": {"name": name}, "status": {"phase": "Running", "podIP": "10.0.0.%s" % name[-1],
                                                      "containerStatuses": cs}}

    def list(self, api, kind, label_selector="", field_selector=""):
        assert kind == "pods" and label_selector == "app=gaussdb-agent,role=pool"
        return {"items": [self._pod(n) for n in sorted(self.pods)]}

    def get(self, api, kind, name):
        if kind == "pods" and name not in self.pods:
            return None
        if kind == "pods":
            if self._pending.get(name, 0) > 0:
                self._pending[name] -= 1
                return self._pod_before_restart(name)
            return self._pod(name)
        if kind == "secrets" and name.startswith("grmp-token-"):
            tok = self.tokens.get(name[len("grmp-token-"):])
            return {"data": {"GRMP_AUTH_TOKEN": base64.b64encode(tok.encode()).decode()}} if tok else None
        return None

    def _pod_before_restart(self, name):
        pod = self._pod(name)
        for cs in pod["status"]["containerStatuses"]:
            for (p, i), old in self._old.items():
                if p == name and cs["name"] == "slot-%d" % i:
                    cs["containerID"] = old
        return pod

    # -- 管家 -----------------------------------------------------------------------------------
    def _in_use(self, pod):
        return sum(1 for s in self.pods[pod] if s.state in pa.IN_USE)

    def _check(self, pod):
        if pod in self.down:
            raise StewardError("down")

    def status(self, pod, ip):
        self._check(pod)
        return {"pod": pod, "in_use": self._in_use(pod), "slots": [
            {"slot": s.i, "state": s.state, "user_id": s.user, "last_active": s.last_active, "busy_at": s.busy_at,
             "bound_at": s.bound_at, "retained_at": s.retained_at, "dirty": s.dirty, "stale": s.stale,
             "listener": s.listener, "offender": s.offender, "reserved_for": s.reserved_for,
             "reserved_until": s.reserved_until, "evicting_since": 1.0 if s.state == pa.EVICTING else None,
             "ports": {"opencode": 4100 + 10 * s.i, "reports": 4101 + 10 * s.i}} for s in self.pods[pod]]}

    def _creds(self, s, idem=False):
        return 200, {"slot": s.i, "state": s.state, "user_id": s.user, "idempotent": idem,
                     "opencode": {"port": 4100 + 10 * s.i, "username": "opencode", "password": s.password},
                     "reports": {"port": 4101 + 10 * s.i, "token": s.reports_token}}

    def bind(self, pod, ip, slot, user, token, expect, cid):
        self._check(pod)
        self.calls.append(("bind", pod, slot, user, token))
        if self.before_bind:
            hook, self.before_bind = self.before_bind, None
            hook()
        s = self.pods[pod][slot]
        if s.state == pa.ACTIVE and s.user == user:
            return self._creds(s, idem=True)
        if expect != self._in_use(pod):
            return 409, {"reason": "pod_changed"}
        if s.state != pa.FREE:
            return 409, {"reason": "state"}
        if s.reserved_for and s.reserved_for != user and s.reserved_until > self.now:
            return 409, {"reason": "reserved"}
        holder = self.owner.get(user)
        if holder and holder != "%s/%d" % (pod, slot):
            return 409, {"reason": "owner_busy", "holder": holder}
        self.owner[user] = "%s/%d" % (pod, slot)
        s.state, s.user, s.bound_at, s.last_active = pa.ACTIVE, user, self.now, self.now
        s.password, s.reports_token = "pw-%s-%d" % (user, len(self.calls)), "rt-%s" % user
        s.lease = cid                      # 管家记下的租期(容器 ID)
        s.reserved_for, s.reserved_until = None, 0.0
        return self._creds(s)

    def resume(self, pod, ip, slot, user, token, expect, cid):
        self._check(pod)
        self.calls.append(("resume", pod, slot, user, token))
        s = self.pods[pod][slot]
        if s.state == pa.ACTIVE and s.user == user:
            return self._creds(s, idem=True)
        if expect != self._in_use(pod):
            return 409, {"reason": "pod_changed"}
        if s.state != pa.RETAINED or s.user != user:
            return 409, {"reason": "state"}
        if s.stale:
            return 409, {"reason": "stale"}
        if getattr(s, "nas_failing", False):
            # 与管家一样:NAS 上的数据不见了、用保留副本恢复时写回失败 → 继续持有独占、记 dirty,答 503
            s.dirty = True
            return 503, {"reason": "not_synced", "state": pa.RETAINED}
        if cid and getattr(s, "lease", "") and cid != s.lease:
            return 409, {"reason": "restarted"}     # 与管家一样:容器 ID 对不上就不恢复
        self.owner[user] = "%s/%d" % (pod, slot)
        s.state, s.bound_at, s.last_active = pa.ACTIVE, self.now, self.now
        s.password = "pw-%s-%d" % (user, len(self.calls))
        return self._creds(s)

    def retain(self, pod, ip, slot, user):
        self._check(pod)
        self.calls.append(("retain", pod, slot, user))
        s = self.pods[pod][slot]
        if s.state != pa.ACTIVE or s.user != user:
            return 409, {"reason": "state"}
        s.state, s.retained_at = pa.RETAINED, self.now
        self.owner.pop(user, None)
        return 200, {"state": pa.RETAINED, "dirty": False}

    def evict(self, pod, ip, slot, cid):
        self._check(pod)
        self.calls.append(("evict", pod, slot, cid))
        s = self.pods[pod][slot]
        if s.state not in (pa.RETAINED, pa.FREE, pa.DISABLED) or s.dirty:
            return 409, {"reason": "not_synced" if s.dirty else "state"}
        s.state, s.evict_cid = pa.EVICTING, cid
        self._old = getattr(self, "_old", {})
        self._old[(pod, slot)] = "docker://%s-%d-%d" % (pod, slot, self.cids[(pod, slot)])
        self.cids[(pod, slot)] += 1                     # 容器重启
        self._pending[pod] = self.restart_delay
        return 202, {"state": pa.EVICTING}

    def verify(self, pod, ip, slot, cid):
        self._check(pod)
        self.calls.append(("verify", pod, slot, cid))
        s = self.pods[pod][slot]
        if s.state != pa.EVICTING:
            return 409, {"reason": "state"}
        if not cid or cid == s.evict_cid:
            return 409, {"reason": "not_restarted"}
        keep = (s.reserved_for, s.reserved_until)
        self.pods[pod][slot] = Slot(slot)
        self.pods[pod][slot].reserved_for, self.pods[pod][slot].reserved_until = keep
        return 200, {"state": pa.FREE}

    def reserve(self, pod, ip, slot, user, seconds):
        self.calls.append(("reserve", pod, slot, user))
        s = self.pods[pod][slot]
        if s.reserved_for and s.reserved_for != user and s.reserved_until > self.now:
            return 409, {"reason": "reserved"}
        s.reserved_for, s.reserved_until = user, self.now + seconds
        return 200, {}

    def touch(self, pod, ip, items):
        self._check(pod)
        self.calls.append(("touch", pod, tuple(tuple(x) for x in items)))
        n = 0
        for slot, user, ts in items:
            s = self.pods[pod][slot]
            if s.state == pa.ACTIVE and s.user == user:
                s.last_active = max(s.last_active, ts)
                n += 1
        return 200, {"touched": n}

    def disable(self, pod, ip, slot, reason):
        self.calls.append(("disable", pod, slot, reason))
        self.pods[pod][slot].state = pa.DISABLED
        return 200, {}

    def verbs(self, *kinds):
        return [c for c in self.calls if c[0] in kinds]


@pytest.fixture()
def cl():
    return FakeCluster()


def _stack(cl, policy=pa.Policy(), wait_s=5.0):
    table = pooltable.PoolTable(cl, cl, refresh_s=0, log=lambda m: None, clock=lambda: cl.now)
    table.refresh_s = 0
    logs = []
    router = poolrouter.PoolRouter(table, cl, policy, lambda u: cl.tokens.get(u, "tok-" + u), wait_s=wait_s,
                                   log=logs.append, clock=time.time, sleep=lambda s: None)
    router.clock = lambda: cl.now
    deadline = {"t": 0}

    def sleep(s):                       # 模拟时间流逝,让等待上限起作用
        deadline["t"] += 1
        cl.now += s
    router.sleep = sleep
    sweeper = poolsweeper.PoolSweeper(table, cl, router, policy, log=logs.append, clock=lambda: cl.now)
    return table, router, sweeper, logs


# -- 视图 ---------------------------------------------------------------------------------------

def test_snapshot_merges_k8s_and_steward_views(cl):
    cl.pods["pool-0"][1].busy_at = cl.now - 5
    cl.pods["pool-0"][1].last_active = cl.now - 100
    cl.pods["pool-0"][1].state, cl.pods["pool-0"][1].user = pa.ACTIVE, "u1"
    cl.down.add("pool-1")
    table, *_ = _stack(cl)
    snap = table.refresh()
    p0, p1 = snap.pods
    assert p0.reachable and not p1.reachable, "管家连不上的 Pod 这一轮不分配"
    s = p0.slots[1]
    assert s.last_seen == cl.now - 5 and s.restarted_at > 0, "会话忙也算活动;重启时间来自 k8s"
    assert snap.meta["pool-0"].ip == "10.0.0.0" and snap.meta["pool-0"].containers[1] == "docker://pool-0-1-1"
    assert snap.meta["pool-0"].ports[1] == (4110, 4111)


def test_terminating_or_ip_less_pods_are_left_out():
    assert pooltable.pod_meta({"metadata": {"name": "p", "deletionTimestamp": "x"},
                               "status": {"phase": "Running", "podIP": "1.2.3.4"}}) is None
    assert pooltable.pod_meta({"metadata": {"name": "p"}, "status": {"phase": "Pending"}}) is None


# -- 分配落地 -------------------------------------------------------------------------------------

def test_new_user_is_bound_with_their_own_grmp_token_and_creds_are_cached(cl):
    cl.tokens["u1"] = "grmp-u1"
    _t, router, *_ = _stack(cl)
    t = router.target("u1")
    assert (t.host, t.port, t.reports_port) == ("10.0.0.%s" % t.pod[-1], 4100 + 10 * t.slot, 4101 + 10 * t.slot)
    assert cl.verbs("bind") == [("bind", t.pod, t.slot, "u1", "grmp-u1")]
    assert router.target("u1") == t and len(cl.verbs("bind")) == 1, "已在用:用缓存的凭证,不再调管家"


def test_first_users_are_exclusive_per_pod(cl):
    _t, router, *_ = _stack(cl)
    pods = {router.target("u%d" % n).pod for n in range(2)}
    assert pods == {"pool-0", "pool-1"}


def test_non_interactive_requests_never_allocate(cl):
    _t, router, *_ = _stack(cl)
    with pytest.raises(poolrouter.NotActive):
        router.target("u1", interactive=False)
    assert not cl.calls
    t = router.target("u1")
    assert router.target("u1", interactive=False) == t, "已在用时探活 / 事件流照常转发"


def test_after_a_gateway_restart_forward_fetches_creds_idempotently(cl):
    _t, router, *_ = _stack(cl)
    t = router.target("u1")
    _t2, router2, *_ = _stack(cl)
    t2 = router2.target("u1", interactive=False)
    assert (t2.pod, t2.slot, t2.password) == (t.pod, t.slot, t.password)
    assert cl.verbs("bind")[-1][4] == "", "取回凭证不需要令牌"


def test_retained_user_resumes_with_a_new_password(cl):
    _t, router, *_ = _stack(cl)
    t = router.target("u1")
    cl.retain(t.pod, "", t.slot, "u1")
    router.forget("u1")
    t2 = router.target("u1")
    assert (t2.pod, t2.slot) == (t.pod, t.slot) and t2.password != t.password
    assert cl.verbs("resume")


def test_cas_conflict_is_retried_after_a_refresh(cl):
    _t, router, *_ = _stack(cl)

    def sneak():                        # 另一个网关副本抢先把一个人塞进 u1 本来要去的 Pod
        _verb, pod, slot, _u, _tok = cl.verbs("bind")[0]
        other = cl.pods[pod][1 - slot]
        other.state, other.user = pa.ACTIVE, "u9"
        cl.sneaked = pod
    cl.before_bind = sneak
    t = router.target("u1")
    assert t.pod != cl.sneaked, "管家回 pod_changed:刷新后重来,换到还空着的 Pod"


def test_full_pool_early_retains_reserves_and_hands_the_slot_over(cl):
    _t, router, *_ = _stack(cl)
    for n in range(4):
        router.target("u%d" % n)
    idle = cl.pods["pool-1"][0]
    idle.last_active = idle.bound_at = cl.now - 4000            # 这位闲置超过 10 分钟
    t = router.target("newbie")
    assert (t.pod, t.slot) == ("pool-1", 0)
    assert [c[0] for c in cl.calls][-5:] == ["reserve", "retain", "evict", "verify", "bind"]
    assert cl.verbs("reserve")[0][3] == "newbie", "腾出来的槽位预留给等的人"
    assert cl.pods["pool-1"][0].user == "newbie"


def test_a_slot_handed_over_after_an_eviction_can_be_resumed_later(cl):
    """先腾挪再绑定:绑定要带腾挪之后的容器 ID,否则管家记下的租期是旧的,下次恢复会被当成「容器换过了」。"""
    _t, router, *_ = _stack(cl)
    for n in range(4):
        router.target("u%d" % n)
    idle = cl.pods["pool-1"][0]
    idle.last_active = idle.bound_at = cl.now - 4000
    t = router.target("newbie")
    assert cl.pods[t.pod][t.slot].lease == "docker://%s-%d-2" % (t.pod, t.slot), "租期是重启后的容器"
    cl.retain(t.pod, "", t.slot, "newbie")
    router.forget("newbie")
    t2 = router.target("newbie")
    assert (t2.pod, t2.slot) == (t.pod, t.slot) and cl.verbs("resume")[-1][3] == "newbie"


def test_full_pool_without_anyone_idle_is_full(cl):
    _t, router, *_ = _stack(cl)
    for n in range(4):
        router.target("u%d" % n)
    with pytest.raises(poolrouter.PoolFull) as exc:
        router.target("newbie")
    assert exc.value.retry_after == 30


def test_a_stale_view_that_looks_full_is_rechecked_before_saying_full(cl):
    """网关刚起来时管家都还连不上:缓存的视图里一个能用的槽位都没有。不能凭它就说满员。"""
    table, router, *_ = _stack(cl)
    table.refresh_s = 3600
    cl.down.update(cl.pods)
    table.refresh()                                         # 缓存下一份「全都连不上」的视图
    cl.down.clear()
    t = router.target("u1")                                 # 现读之后有空位
    assert t.pod in cl.pods


def test_unreachable_stewards_mean_wait_not_full(cl):
    _t, router, *_ = _stack(cl)
    for n in range(2):
        router.target("u%d" % n)                            # pool-0、pool-1 各一人
    for pod, user in (("pool-1", "u8"), ("pool-0", "u9")):   # 另两位刚有过操作:没有可提前转保留的人
        s = cl.pods[pod][1]
        s.state, s.user, s.last_active = pa.ACTIVE, user, cl.now
    with pytest.raises(poolrouter.PoolFull):
        router.target("newbie")                             # 真满了
    cl.pods["pool-1"][1].state, cl.pods["pool-1"][1].user = pa.FREE, None
    cl.down.add("pool-1")                                   # pool-1 有空位,但它的管家这会儿连不上
    with pytest.raises(poolrouter.PoolWait) as exc:
        router.target("newbie")
    assert exc.value.reason == "steward_unreachable" and exc.value.retry_after == 10


def test_a_resume_that_cannot_write_the_nas_back_yet_waits_quietly_and_never_evicts(cl):
    """管家用保留副本恢复 NAS、写回失败时答 503 not_synced(继续持有独占、后台重试)。网关按「稍后再来」处理:
    不记「失败」、不腾挪那份副本(唯一完整的一份),到等待上限转等待页;写回补上之后照常恢复到同一个槽位。"""
    _t, router, _sw, logs = _stack(cl, wait_s=3.0)
    t = router.target("u1")
    cl.retain(t.pod, "", t.slot, "u1")
    router.forget("u1")
    s = cl.pods[t.pod][t.slot]
    s.nas_failing = True
    with pytest.raises(poolrouter.PoolWait) as exc:
        router.target("u1")
    assert exc.value.reason == "not_synced"
    assert not cl.verbs("evict", "bind")[1:], "不腾挪、也不换个槽位重新载入"
    assert not any("失败" in m for m in logs), logs
    s.nas_failing, s.dirty = False, False          # 后台重试把写回补上了
    t2 = router.target("u1")
    assert (t2.pod, t2.slot) == (t.pod, t.slot) and cl.pods[t.pod][t.slot].state == pa.ACTIVE


def test_owner_held_outside_the_pool_waits(cl):
    cl.owner["u1"] = "runtime-u1-5d8f"               # 还没回收的按需模式 Pod
    _t, router, *_ = _stack(cl)
    with pytest.raises(poolrouter.PoolWait) as exc:
        router.target("u1")
    assert exc.value.reason.startswith("held_elsewhere")


def test_a_slow_restart_turns_into_wait_and_the_next_request_finishes_it(cl):
    _t, router, *_ = _stack(cl, wait_s=3.0)
    for n in range(4):
        router.target("u%d" % n)
    for name, slots in cl.pods.items():
        for s in slots:
            cl.retain(name, "", s.i, s.user)          # 全部转保留:新人得先腾挪一个
    cl.restart_delay = 100
    with pytest.raises(poolrouter.PoolWait) as exc:
        router.target("newbie")
    assert exc.value.reason == "evicting"
    cl.restart_delay = 0
    cl._pending.clear()
    t = router.target("newbie")
    assert cl.pods[t.pod][t.slot].user == "newbie"


def test_moving_to_an_empty_pod_evicts_the_old_retained_slot_afterwards(cl):
    _t, router, *_ = _stack(cl)
    t1 = router.target("u1")
    cl.retain(t1.pod, "", t1.slot, "u1")
    router.forget("u1")
    s = cl.pods[t1.pod][1 - t1.slot]
    s.state, s.user = pa.ACTIVE, "u2"                 # 他的 Pod 来了别人,另一个 Pod 是空的
    t = router.target("u1")
    assert t.pod != t1.pod
    deadline = time.time() + 5
    while time.time() < deadline and not cl.verbs("evict"):
        time.sleep(0.02)
    assert cl.verbs("evict")[0][1:3] == (t1.pod, t1.slot)


# -- 后台清扫 -------------------------------------------------------------------------------------

def test_sweeper_reports_activity_before_judging_idleness(cl):
    policy = pa.Policy(idle_seconds=600)
    _t, router, sweeper, _logs = _stack(cl, policy)
    t = router.target("u1")
    s = cl.pods[t.pod][t.slot]
    s.last_active = s.bound_at = cl.now - 700
    router.note_activity("u1")                      # 本副本刚转发过他的请求
    sweeper.sweep_once(wait=True)
    assert s.state == pa.ACTIVE and cl.verbs("touch"), "先报活动,再判闲置"
    cl.now += 700
    sweeper.sweep_once(wait=True)
    assert s.state == pa.RETAINED and cl.verbs("retain")[-1][3] == "u1"


def test_sweeper_evicts_and_verifies_with_a_fresh_container_id(cl):
    _t, router, sweeper, _logs = _stack(cl)
    t = router.target("u1")
    cl.retain(t.pod, "", t.slot, "u1")
    cl.pods[t.pod][t.slot].stale = True
    sweeper.sweep_once(wait=True)
    assert cl.verbs("evict")[-1][1:3] == (t.pod, t.slot)
    sweeper.sweep_once(wait=True)
    v = cl.verbs("verify")[-1]
    assert v[3] == "docker://%s-%d-2" % (t.pod, t.slot), "核验用重启后的 containerID"
    assert cl.pods[t.pod][t.slot].state == pa.FREE


def test_sweeper_puts_activity_back_when_the_steward_is_unreachable(cl):
    _t, router, sweeper, _logs = _stack(cl)
    t = router.target("u1")
    router.note_activity("u1")
    cl.down.add(t.pod)
    sweeper.flush_activity(sweeper.table.snapshot())
    assert "u1" in router.drain_activity(), "报不出去的活动下一轮再报"


def test_admin_summary_has_no_secrets_and_actions_use_fresh_container_ids(cl):
    table, router, _s, _l = _stack(cl)
    t = router.target("u1")
    adm = poolsweeper.PoolAdmin(table, cl, router)
    text = repr(adm.summary())
    assert t.password not in text and t.reports_token not in text and "u1" in text
    assert adm.act("retain", t.pod, t.slot)[0] == 200
    code, _body = adm.act("evict", t.pod, t.slot)
    assert code == 202 and cl.verbs("evict")[-1][3] == "docker://%s-%d-1" % (t.pod, t.slot)
    assert adm.act("nope", t.pod, t.slot)[0] == 404 and adm.act("evict", "pool-9", 0)[0] == 404


# -- 管家客户端(对着真的管家 HTTP 服务) ------------------------------------------------------------

@pytest.fixture()
def real_steward(tmp_path):
    import poolslot
    import poolsteward
    users = tmp_path / "users"
    users.mkdir()
    for i in range(2):
        for sub in ("ctl", "state"):
            (tmp_path / "slots" / str(i) / sub).mkdir(parents=True)
    key = b"c" * 32
    cfg = poolsteward.Config(key=key, pod="pool-0", slots=2, users_dir=users, slots_dir=tmp_path / "slots",
                             priv_dir=tmp_path / "priv", port=0, settings=poolslot.Settings(pod="pool-0"),
                             slot_ports=lambda i: (6100 + 10 * i, 6101 + 10 * i))
    st = poolsteward.Steward(cfg, log=lambda m: None, verdict=lambda p, u: "ok")
    for s in st.slots:
        s._set(state=poolslot.FREE, boot="b%d" % s.i, quiet=True)
    srv = poolsteward.make_server(st, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = poolclient.StewardClient(key, port=srv.server_address[1])
    yield st, client
    srv.shutdown()
    st.close()


def test_client_signs_requests_and_seals_the_grmp_token(real_steward):
    st, client = real_steward
    assert len(client.status("pool-0", "127.0.0.1")["slots"]) == 2
    st.slots[0]._set(state="active", user="u1234", password="p" * 24, reports_token="r" * 24)
    code, body = client.bind("pool-0", "127.0.0.1", 0, "u1234", "grmp-plain-token", 1, "c")
    assert code == 200 and body["idempotent"], "令牌是密文,管家解得开(明文会被 400 bad_token)"
    assert client.reserve("pool-0", "127.0.0.1", 1, "u9", 30)[0] == 200


def test_client_rejects_answers_without_the_steward_signature(real_steward):
    _st, client = real_steward
    with pytest.raises(StewardError):
        client.status("pool-9", "127.0.0.1")        # 签给别的 Pod 名:管家 401 且不签名
    other = poolclient.StewardClient(b"x" * 32, port=client.port)
    with pytest.raises(StewardError):
        other.status("pool-0", "127.0.0.1")
    with pytest.raises(StewardError):
        poolclient.StewardClient(b"c" * 32, port=1).status("pool-0", "127.0.0.1")


def test_an_unreachable_steward_keeps_its_last_slots_and_active_users_keep_working(cl):
    table, router, *_ = _stack(cl)
    t = router.target("u1")
    table.refresh()                     # 例行的刷新(清扫、别的请求都会刷)
    cl.down.add(t.pod)
    snap = table.refresh()
    p = next(x for x in snap.pods if x.name == t.pod)
    assert not p.reachable and any(s.user == "u1" for s in p.slots), "沿用上一次看到的槽位"
    n = len(cl.calls)
    assert router.target("u1") == t and len(cl.calls) == n, "缓存的凭证照常用,不经管家"
    with pytest.raises(poolrouter.NotActive):
        router.target("u2", interactive=False)
