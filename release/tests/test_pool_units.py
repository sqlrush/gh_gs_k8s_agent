"""池子替换:假池子按 11-03 实验观察到的行为模拟(腾挪后要等重启、核验要等 containerID 变、换管家会转保留在用的槽位)。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import pool as pl  # noqa: E402

OLD, NEW = "reg/gaussdb-agent-runtime:agent-v1.4.3-oc1.18.27", "reg/gaussdb-agent-runtime:agent-v1.4.4-oc1.18.27"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0
        self.hooks = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s
        for h in list(self.hooks):
            h(self.t)


class FakePool:
    """槽位状态 + 容器。patch 镜像后容器先停一拍(restart_polls 次读到「没在运行」),再以新 ID 运行。"""

    def __init__(self, clock, pods=1, slots=2):
        self.clock = clock
        self.slots = {("p%d" % p, i): {"state": "free", "busy": False, "busy_at": 0.0, "dirty": False, "user_id": None}
                      for p in range(pods) for i in range(slots)}
        self.ctr = {("p%d" % p, n): {"image": OLD, "cid": "docker://%s-%s-0" % (p, n), "running": True, "pending": 0}
                    for p in range(pods) for n in ["slot-%d" % i for i in range(slots)] + ["steward"]}
        self.uid = {"p%d" % p: "uid-0" for p in range(pods)}
        self.calls = []
        self.reachable = {"p%d" % p: True for p in range(pods)}
        self.retain_aborts = set()

    # -- 网关 admin
    def slots_view(self):
        return {k: dict(v, reachable=self.reachable[k[0]]) for k, v in self.slots.items()}

    def call(self, action, pod, i):
        self.calls.append((action, pod, i))
        s = self.slots[(pod, i)]
        if action == "retain":
            if s["state"] != "active":
                return 409, {"reason": "state"}
            s.update(state="retained", busy=False)
            return 200, {"state": "retained", "dirty": s["dirty"], "aborted_running": (pod, i) in self.retain_aborts,
                         "synced": {"files": 3}}
        if action == "evict":
            if s["state"] not in ("retained", "free", "disabled") or s["dirty"]:
                return 409, {"reason": "state"}
            s["state"], s["evict_cid"] = "evicting", self.ctr[(pod, "slot-%d" % i)]["cid"]
            return 202, {"state": "evicting"}
        if action == "verify":
            c = self.ctr[(pod, "slot-%d" % i)]
            if s["state"] != "evicting" or not c["running"] or c["cid"] == s.get("evict_cid"):
                return 409, {"reason": "not_restarted"}
            s.update(state="free", user_id=None)
            return 200, {"state": "free"}
        raise AssertionError(action)

    # -- k8s
    def get(self, kind, name, namespaced=True):
        assert kind == "pod"
        if name not in self.uid:
            return None
        conts, stats = [], []
        for (p, n), c in sorted(self.ctr.items()):
            if p != name:
                continue
            if c["pending"]:
                c["pending"] -= 1
                if not c["pending"]:
                    c.update(running=True, cid=c["cid"].rsplit("-", 1)[0] + "-%d" % (int(c["cid"].rsplit("-", 1)[1]) + 1))
            conts.append({"name": n, "image": c["image"]})
            stats.append({"name": n, "containerID": c["cid"], "state": {"running": {}} if c["running"] else {"waiting": {}}})
        return {"metadata": {"uid": self.uid[name]}, "spec": {"containers": conts}, "status": {"containerStatuses": stats}}

    def patch(self, kind, name, patch, ptype="strategic"):
        for c in patch["spec"]["containers"]:
            self.calls.append(("patch", name, c["name"]))
            ctr = self.ctr[(name, c["name"])]
            ctr.update(image=c["image"], running=False, pending=2)
            if c["name"] == "steward":                    # 管家收到 SIGTERM:在用的全部转保留
                for (p, i), s in self.slots.items():
                    if p == name and s["state"] == "active":
                        s.update(state="retained", busy=False)
                self.reachable[name] = False
                self.clock.hooks.append(lambda t, n=name: self.reachable.__setitem__(n, True))

    def delete(self, kind, name="", selector="", wait=True):
        self.calls.append(("delete", name))
        self.uid[name] = "uid-1"
        for (p, n), c in self.ctr.items():
            if p == name:
                c.update(image=NEW, running=True, cid=c["cid"] + "x")
        for (p, i), s in self.slots.items():
            if p == name:
                s.update(state="free", user_id=None, busy=False)


class FakeAdmin:
    def __init__(self, fp):
        self.fp = fp

    def slots(self):
        return self.fp.slots_view()

    def call(self, action="", pod="", slot=0):
        return self.fp.call(action, pod, slot)


@pytest.fixture()
def env():
    clock = Clock()
    fp = FakePool(clock)
    events, gates, logs = [], [], []
    sw = pl.Swapper(kube=fp, admin=FakeAdmin(fp), image=NEW, clock=clock, sleep=clock.sleep,
                    log=lambda lv, m: logs.append((lv, m)), progress=lambda p, s, st, v: events.append((p, s, st)),
                    gate=lambda k, p: gates.append((k, p)) or "wait", busy_timeout_s=600, ready_timeout_s=120)
    return clock, fp, sw, events, gates, logs


def test_a_free_slot_is_evicted_then_patched_then_verified(env):
    clock, fp, sw, events, _g, _l = env
    assert sw.swap_slot("p0", 0) is True
    order = [c for c in fp.calls if c[0] in ("evict", "patch", "verify")]
    assert order[0] == ("evict", "p0", 0) and order[1] == ("patch", "p0", "slot-0"), "先腾挪再换镜像,不能直接 patch 空闲槽位"
    assert fp.slots[("p0", 0)]["state"] == "free" and fp.ctr[("p0", "slot-0")]["image"] == NEW
    assert [e[2] for e in events] == ["restart", "verify", "free"]


def test_a_slot_already_on_the_target_image_is_left_alone(env):
    _c, fp, sw, _e, _g, _l = env
    fp.ctr[("p0", "slot-0")]["image"] = NEW
    assert sw.swap_slot("p0", 0) is False and fp.calls == []


def test_an_active_slot_waits_until_it_has_been_quiet_for_a_while(env):
    clock, fp, sw, _e, _g, _l = env
    s = fp.slots[("p0", 0)]
    s.update(state="active", busy=True, busy_at=clock.t, user_id="u1")
    end = clock.t + 30
    clock.hooks.append(lambda t: s.update(busy=False, busy_at=end) if t >= end and s["busy"] else None)
    sw.swap_slot("p0", 0)
    retain_at = next(i for i, c in enumerate(fp.calls) if c[0] == "retain")
    assert retain_at == 0 and clock.t >= end + sw.quiet_s, "忙结束后还要再等 quiet_s 才转保留"
    assert fp.slots[("p0", 0)]["state"] == "free"


def test_a_request_that_never_ends_goes_to_the_operator_who_can_force(env):
    clock, fp, sw, _e, gates, logs = env
    fp.slots[("p0", 0)].update(state="active", busy=True, busy_at=clock.t, user_id="u1")
    clock.hooks.append(lambda t: fp.slots[("p0", 0)].__setitem__("busy_at", t) if fp.slots[("p0", 0)]["busy"] else None)
    choices = iter(["wait", "force"])
    sw.gate = lambda k, p: gates.append((k, p)) or next(choices)
    fp.retain_aborts.add(("p0", 0))
    sw.swap_slot("p0", 0)
    assert [g[0] for g in gates] == ["drain", "drain"] and gates[0][1]["slots"] == [0]
    assert sw.aborted == ["p0/0"] and any("强制" in m for _lv, m in logs)


def test_a_retained_slot_with_an_unclean_writeback_waits_for_the_steward(env):
    clock, fp, sw, _e, _g, _l = env
    s = fp.slots[("p0", 0)]
    s.update(state="retained", dirty=True, user_id="u1")
    due = clock.t + 20
    clock.hooks.append(lambda t: s.__setitem__("dirty", False) if t > due else None)
    sw.swap_slot("p0", 0)
    assert fp.calls[0][0] == "evict" and clock.t > due, "写回干净之前不能腾挪(管家会拒 not_synced)"
    assert fp.slots[("p0", 0)]["state"] == "free"


def test_the_steward_goes_last_and_only_when_the_whole_pod_is_quiet(env):
    clock, fp, sw, events, _g, _l = env
    sw.swap_pod_online("p0")
    patched = [c[2] for c in fp.calls if c[0] == "patch"]
    assert patched == ["slot-0", "slot-1", "steward"]
    assert all(fp.ctr[("p0", n)]["image"] == NEW for n in ("slot-0", "slot-1", "steward"))
    assert events[-1] == ("p0", -1, "ok")


def test_swapping_the_steward_retains_whoever_is_active(env):
    clock, fp, sw, _e, _g, _l = env
    for n in ("slot-0", "slot-1"):
        fp.ctr[("p0", n)]["image"] = NEW
    fp.slots[("p0", 1)].update(state="active", busy=False, busy_at=clock.t - 100, user_id="u1")
    assert sw.swap_steward("p0", [0, 1]) is True
    assert fp.slots[("p0", 1)]["state"] == "retained", "管家 SIGTERM 会转保留在用的人,下一句自动恢复"


def test_pod_replacement_retains_active_slots_then_deletes_the_pod(env):
    clock, fp, sw, events, _g, _l = env
    fp.slots[("p0", 0)].update(state="active", busy=False, busy_at=clock.t - 100, user_id="u1")
    sw.replace_pod("p0")
    kinds = [c[0] for c in fp.calls]
    assert kinds.index("retain") < kinds.index("delete")
    assert all(fp.slots[("p0", i)]["state"] == "free" for i in (0, 1))
    assert ("p0", -1, "ok") in events


def test_a_verification_that_never_passes_is_an_error_not_a_hang(env):
    clock, fp, sw, _e, _g, _l = env
    fp.call = lambda a, p, i: (409, {"reason": "not_restarted"}) if a == "verify" else FakePool.call(fp, a, p, i)
    with pytest.raises(pl.PoolError, match="核验"):
        sw.swap_slot("p0", 0)


# -- 经 kubectl exec 调网关 ------------------------------------------------------------------------

class ExecKube:
    def __init__(self, out, code=0):
        self.out, self.code, self.stdin = out, code, None

    def list(self, kind, selector="", namespaced=True):
        return [{"metadata": {"name": "gw-0"}, "status": {"containerStatuses": [{"ready": False}]}},
                {"metadata": {"name": "gw-1"}, "status": {"containerStatuses": [{"ready": True}]}}]

    def exec(self, pod, container, argv, stdin="", timeout=60):
        from release.kube import Result
        self.pod, self.stdin = pod, stdin
        return Result(self.code, self.out, "boom" if self.code else "")


def test_gateway_admin_uses_a_ready_gateway_and_never_sees_the_token():
    body = {"pods": [{"name": "p0", "reachable": True, "slots": [{"slot": 0, "state": "free", "busy": False}]}]}
    k = ExecKube(json.dumps({"code": 200, "body": body}) + "\n")
    adm = pl.GatewayAdmin(k)
    assert adm.slots()[("p0", 0)]["state"] == "free" and k.pod == "gw-1"
    assert "GATEWAY_ADMIN_TOKEN" in pl.ADMIN and "TOKEN" not in k.stdin, "密钥在网关容器里读,不经发版台"
    with pytest.raises(pl.PoolError, match="boom"):
        pl.GatewayAdmin(ExecKube("", code=1), sleep=lambda _s: None).call()


def test_an_old_gateway_without_busy_falls_back_to_last_seen(env):
    clock, fp, sw, _e, _g, _l = env
    assert not sw._quiet({"state": "active", "last_seen": clock.t - 20}), "旧网关摘要没有 busy:20 秒前还有动静就不算安静"
    assert sw._quiet({"state": "active", "last_seen": clock.t - 31})


def test_gateway_admin_skips_terminating_pods_and_retries(monkeypatch):
    from release.kube import Result

    class K:
        def __init__(self):
            self.tried = []

        def list(self, kind, selector="", namespaced=True):
            return [{"metadata": {"name": "gw-old", "deletionTimestamp": "x"}, "status": {"containerStatuses": [{"ready": True}]}},
                    {"metadata": {"name": "gw-a"}, "status": {"containerStatuses": [{"ready": True}]}},
                    {"metadata": {"name": "gw-b"}, "status": {"containerStatuses": [{"ready": True}]}}]

        def exec(self, pod, container, argv, stdin="", timeout=60):
            self.tried.append(pod)
            if len(self.tried) == 1:
                return Result(137, "", "command terminated with exit code 137")
            return Result(0, json.dumps({"code": 200, "body": {"pods": []}}) + "\n", "")
    k = K()
    assert pl.GatewayAdmin(k, sleep=lambda _s: None).call()[0] == 200
    assert "gw-old" not in k.tried and k.tried == ["gw-a", "gw-b"], "退出中的旧网关不选;失败换一个副本重试"
