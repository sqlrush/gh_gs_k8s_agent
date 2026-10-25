"""固定池化分配器(gateway/poolalloc.py,设计稿 §5)。

表驱动的用例对着设计稿的每一条规则;随机生成的池子核对不变量:不分到不能用的槽位、一人一槽、
16 人以内各自独占、满员判定、预留不被第三人抢、dirty 永不腾挪。
"""
import dataclasses
import pathlib
import random
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import poolalloc as pa  # noqa: E402

NOW = 1_800_000_000.0
POLICY = pa.Policy()
_KINDS = {"F": pa.FREE, "A": pa.ACTIVE, "B": pa.BINDING, "M": pa.RESUMING, "T": pa.RETAINING,
          "R": pa.RETAINED, "D": pa.RETAINED, "S": pa.RETAINED, "E": pa.EVICTING, "X": pa.DISABLED,
          "K": pa.BOOTING}


def pod(name, *tokens, reachable=True):
    """「A:u1」在用、「R:u2」保留、「D:」保留且 dirty、「S:」保留且 stale、F 空闲、B 绑定中、M 恢复中、
    T 转保留中、E 清理中、X 停用、K 握手前。"""
    slots = []
    for i, t in enumerate(tokens):
        k, _sep, user = t.partition(":")
        st = _KINDS[k]
        slots.append(pa.SlotView(name, i, st, user or None, last_seen=NOW - 60,
                                 retained_at=NOW - 3600 if st == pa.RETAINED else 0.0,
                                 dirty=k == "D", stale=k == "S"))
    return pa.PodView(name, tuple(slots), reachable)


def edit(pods, ref, **changes):
    out = []
    for p in pods:
        slots = tuple(dataclasses.replace(s, **changes) if s.ref == ref else s for s in p.slots)
        out.append(dataclasses.replace(p, slots=slots))
    return out


def decide(pods, user):
    return pa.decide(pods, user, NOW, POLICY)


# -- 规则 1:已在用 -----------------------------------------------------------------------------

def test_already_active_is_forwarded():
    pods = [pod("p0", "A:u1", "F"), pod("p1", "F", "F")]
    assert decide(pods, "u1") == pa.Decision(pa.FORWARD, target=("p0", 0))


@pytest.mark.parametrize("tok", ["B", "M", "T"])
def test_transient_states_wait(tok):
    d = decide([pod("p0", tok + ":u1", "F")], "u1")
    assert d.kind == pa.WAIT and d.target == ("p0", 0)


# -- 规则 2:回到保留槽位 --------------------------------------------------------------------------

def test_retained_with_a_quiet_home_resumes():
    d = decide([pod("p0", "R:u1", "F"), pod("p1", "F", "F")], "u1")
    assert (d.kind, d.target) == (pa.RESUME, ("p0", 0))


def test_retained_whose_home_is_busy_moves_to_an_empty_pod():
    d = decide([pod("p0", "R:u1", "A:u2"), pod("p1", "F", "F")], "u1")
    assert d.kind == pa.BIND and d.target[0] == "p1" and d.evict_after == ("p0", 0)


def test_retained_whose_home_is_busy_resumes_when_no_pod_is_empty():
    d = decide([pod("p0", "R:u1", "A:u2"), pod("p1", "A:u3", "F")], "u1")
    assert (d.kind, d.target) == (pa.RESUME, ("p0", 0))


def test_dirty_retained_always_goes_home():
    d = decide([pod("p0", "D:u1", "A:u2"), pod("p1", "F", "F")], "u1")
    assert (d.kind, d.target, d.reason) == (pa.RESUME, ("p0", 0), "dirty")


def test_stale_retained_is_ignored_and_the_user_gets_a_fresh_slot():
    d = decide([pod("p0", "S:u1", "F"), pod("p1", "F", "F")], "u1")
    assert d.kind == pa.BIND and d.target != ("p0", 0)


def test_retained_on_a_squatted_slot_moves_even_when_home_is_quiet():
    pods = edit([pod("p0", "R:u1", "F"), pod("p1", "F", "F")], ("p0", 0), listener="foreign:2001")
    d = decide(pods, "u1")
    assert d.kind == pa.BIND and d.target != ("p0", 0) and d.evict_after == ("p0", 0)


# -- 规则 3:新人 -----------------------------------------------------------------------------------

def test_new_user_prefers_an_empty_pod_with_the_most_free_slots():
    pods = [pod("p0", "A:u2", "F", "F"), pod("p1", "R:u3", "F", "F"), pod("p2", "F", "F", "F")]
    d = decide(pods, "u1")
    assert d.kind == pa.BIND and d.target[0] == "p2" and d.evict_first is None


def test_new_user_without_empty_pods_goes_to_the_least_used_one():
    pods = [pod("p0", "A:u2", "A:u3", "F"), pod("p1", "A:u4", "F", "F")]
    assert decide(pods, "u1").target == ("p1", 1)


def test_equal_pods_are_ranked_by_rendezvous_hash_so_users_spread():
    pods = [pod("p%d" % i, "F", "F") for i in range(16)]
    picks = {decide(pods, "u%04d" % n).target[0] for n in range(40)}
    assert len(picks) > 8, "两个网关副本同时分配时,不同的人应大概率落到不同的 Pod"
    assert decide(pods, "u0001") == decide(list(reversed(pods)), "u0001"), "与列表顺序无关"


def test_a_pod_full_of_retained_slots_evicts_the_longest_unseen():
    pods = [pod("p0", "R:u2", "R:u3", "R:u4")]
    pods = edit(pods, ("p0", 1), last_seen=NOW - 9000)
    d = decide(pods, "u1")
    assert d.kind == pa.BIND and d.evict_first == d.target == ("p0", 1)


def test_eviction_prefers_slots_past_the_restart_backoff_window():
    pods = [pod("p0", "R:u2", "R:u3")]
    pods = edit(pods, ("p0", 0), last_seen=NOW - 9000, restarted_at=NOW - 30)      # 刚重启过:退避还没清零
    pods = edit(pods, ("p0", 1), last_seen=NOW - 5000, restarted_at=NOW - 3600)
    assert decide(pods, "u1").evict_first == ("p0", 1)


def test_dirty_retained_slots_are_never_taken():
    assert decide([pod("p0", "D:u2", "A:u3")], "u1").kind == pa.FULL


def test_unreachable_pods_and_squatted_or_offending_slots_are_skipped():
    pods = [pod("p0", "F", "F", reachable=False), pod("p1", "F", "A:u9")]
    pods = edit(pods, ("p1", 0), listener="foreign:2005")
    assert decide(pods, "u1").kind == pa.FULL
    pods = edit(pods, ("p1", 0), listener="ok", offender=True)
    assert decide(pods, "u1").kind == pa.FULL


# -- 规则 4:满员 ------------------------------------------------------------------------------------

def test_full_picks_the_most_idle_active_user_for_early_retain():
    pods = [pod("p0", "A:u2", "A:u3"), pod("p1", "A:u4", "B:u5")]
    pods = edit(pods, ("p0", 1), last_seen=NOW - 1200)
    pods = edit(pods, ("p1", 0), last_seen=NOW - 3000)
    d = decide(pods, "u1")
    assert d.kind == pa.FULL and d.early_retain == ("p1", 0)


def test_full_without_anyone_idle_long_enough_retains_nobody():
    d = decide([pod("p0", "A:u2", "A:u3")], "u1")
    assert d == pa.Decision(pa.FULL, reason="full")


def test_a_reserved_slot_goes_to_its_owner_and_not_to_a_third_person():
    pods = edit([pod("p0", "R:u2", "A:u3")], ("p0", 0), reserved_for="u1", reserved_until=NOW + 60)
    assert decide(pods, "u9").kind == pa.FULL, "第三个人抢不走"
    d = decide(pods, "u1")
    assert d.kind == pa.BIND and d.evict_first == ("p0", 0)
    assert decide(pods, "u2").kind == pa.RESUME, "被提前转保留的本人回来,照样回自己的槽位"


def test_a_reservation_wins_over_other_free_places():
    pods = edit([pod("p0", "R:u2", "A:u3"), pod("p1", "F", "A:u4")], ("p0", 0), reserved_for="u1",
                reserved_until=NOW + 60)
    assert decide(pods, "u1").target == ("p0", 0), "为他腾出来的位置不白占"


def test_a_reserved_slot_still_being_retained_means_wait_not_a_second_early_retain():
    pods = [pod("p0", "T:u2", "A:u3")]
    pods = edit(pods, ("p0", 0), reserved_for="u1", reserved_until=NOW + 60)
    pods = edit(pods, ("p0", 1), last_seen=NOW - 3000)
    d = decide(pods, "u1")
    assert (d.kind, d.target, d.reason) == (pa.WAIT, ("p0", 0), "reserved")


def test_a_reservation_on_a_slot_not_yet_usable_does_not_pull_into_a_busy_pod():
    pods = edit([pod("p0", "A:u2", "F"), pod("p1", "F", "F")], ("p0", 0), reserved_for="u1",
                reserved_until=NOW + 60)
    assert decide(pods, "u1").target[0] == "p1", "预留的那位还没转保留,别处有空 Pod 就照常独占"


def test_an_expired_reservation_is_ignored():
    pods = edit([pod("p0", "R:u2", "A:u3")], ("p0", 0), reserved_for="u1", reserved_until=NOW - 1)
    assert decide(pods, "u9").kind == pa.BIND


# -- 后台 --------------------------------------------------------------------------------------------

def _plan(pods):
    return {(a.kind, a.ref, a.reason) for a in pa.plan(pods, NOW, POLICY)}


def test_plan_retains_idle_users_and_nothing_else_when_all_is_well():
    pods = [pod("p0", "A:u1", "A:u2", "F")]
    pods = edit(pods, ("p0", 0), last_seen=NOW - POLICY.idle_seconds - 1)
    assert _plan(pods) == {(pa.RETAIN, ("p0", 0), "idle")}


def test_plan_evicts_stale_elsewhere_and_expired_but_never_dirty():
    pods = [pod("p0", "S:u1", "R:u2", "D:u3", "R:u4", "F"), pod("p1", "A:u2", "F")]
    pods = edit(pods, ("p0", 3), retained_at=NOW - POLICY.retain_days * 86400 - 1)
    assert _plan(pods) == {(pa.EVICT, ("p0", 0), "stale"), (pa.EVICT, ("p0", 1), "active_elsewhere"),
                           (pa.EVICT, ("p0", 3), "retain_days")}


def test_plan_keeps_a_free_slot_per_pod():
    pods = [pod("p0", "A:u1", "R:u2", "R:u3")]
    pods = edit(pods, ("p0", 2), last_seen=NOW - 9999)
    assert _plan(pods) == {(pa.EVICT, ("p0", 2), "free_slots")}
    pods = edit(pods, ("p0", 2), reserved_for="u7", reserved_until=NOW + 60)
    assert _plan(pods) == {(pa.EVICT, ("p0", 1), "free_slots")}, "预留的槽位交给等它的人去腾"


def test_plan_verifies_evictions_and_handles_offenders():
    pods = [pod("p0", "E", "X", "X", "A:u1", "R:u2")]
    pods = edit(pods, ("p0", 2), evict_pending=True)
    pods = edit(pods, ("p0", 3), offender=True)
    pods = edit(pods, ("p0", 4), offender=True)
    assert _plan(pods) == {(pa.VERIFY, ("p0", 0), "verify"), (pa.VERIFY, ("p0", 2), "verify"),
                           (pa.RETAIN, ("p0", 3), "offender"), (pa.EVICT, ("p0", 4), "offender")}


# -- 整池模拟与随机不变量 --------------------------------------------------------------------------------

def _apply(pods, d, user):
    """按决定把人放进去(模拟管家照做)。"""
    if d.evict_first:
        pods = edit(pods, d.evict_first, state=pa.FREE, user=None, reserved_for=None, reserved_until=0.0)
    if d.kind in (pa.BIND, pa.RESUME):
        pods = edit(pods, d.target, state=pa.ACTIVE, user=user, last_seen=NOW, stale=False, dirty=False)
    if d.evict_after:
        pods = edit(pods, d.evict_after, state=pa.EVICTING, user=None)
    return pods


def test_sixteen_pods_of_eight_admit_128_exclusively_first_and_refuse_the_129th():
    pods = [pod("pool-%d" % i, *["F"] * 8) for i in range(16)]
    for n in range(128):
        d = decide(pods, "u%04d" % n)
        assert d.kind == pa.BIND, (n, d)
        if n < 16:
            assert next(p for p in pods if p.name == d.target[0]).in_use == 0, "16 人以内各自独占一个 Pod"
        pods = _apply(pods, d, "u%04d" % n)
    assert sorted(p.in_use for p in pods) == [8] * 16
    assert decide(pods, "u9999").kind == pa.FULL


SEEDS = range(300)


def _random_pool(rng):
    users = ["u%03d" % i for i in range(rng.randint(1, 30))]
    pods = []
    for pi in range(rng.randint(1, 5)):
        slots = []
        for si in range(rng.randint(1, 4)):
            st = rng.choice([pa.FREE, pa.FREE, pa.ACTIVE, pa.RETAINED, pa.RETAINED, pa.BINDING, pa.EVICTING,
                             pa.DISABLED, pa.BOOTING, pa.RETAINING])
            user = rng.choice(users) if st not in (pa.FREE, pa.EVICTING, pa.DISABLED, pa.BOOTING) else None
            res = rng.choice(users) if rng.random() < 0.15 else None
            slots.append(pa.SlotView("p%d" % pi, si, st, user, last_seen=NOW - rng.randint(0, 5000),
                                     retained_at=NOW - rng.randint(0, 900000) if st == pa.RETAINED else 0.0,
                                     dirty=st == pa.RETAINED and rng.random() < 0.2,
                                     stale=st == pa.RETAINED and rng.random() < 0.2,
                                     listener=rng.choice(["ok"] * 8 + ["down", "foreign:2003"]),
                                     offender=rng.random() < 0.05, restarted_at=NOW - rng.randint(0, 3000),
                                     reserved_for=res, reserved_until=NOW + rng.choice([-10, 60]) if res else 0.0))
        pods.append(pa.PodView("p%d" % pi, tuple(slots), reachable=rng.random() > 0.1))
    return pods, users


def _slot(pods, ref):
    return next(s for p in pods for s in p.slots if s.ref == ref)


@pytest.mark.parametrize("seed", SEEDS)
def test_random_pools_never_break_the_invariants(seed):
    rng = random.Random(seed)
    pods, users = _random_pool(rng)
    reach = {p.name for p in pods if p.reachable}
    for user in users + ["newbie"]:
        d = decide(pods, user)
        mine = [s for p in pods if p.reachable for s in p.slots if s.user == user]
        if any(s.state == pa.ACTIVE for s in mine):
            assert d.kind == pa.FORWARD
        if d.kind == pa.BIND:
            s = _slot(pods, d.target)
            assert d.target[0] in reach
            assert not s.listener.startswith("foreign") and not s.offender
            assert not s.reserved_for_other(user, NOW), "预留给别人的不能分"
            if d.evict_first:
                assert d.evict_first == d.target and s.state == pa.RETAINED and not s.dirty
            else:
                assert s.state == pa.FREE
            empties = [p for p in pods if p.reachable and p.in_use == 0 and p.name != (d.evict_after or ("",))[0]
                       and any(pa._usable_in(p, user, NOW))]
            reserved = any(pa._reserved_for(x, user, NOW) for p in pods if p.reachable
                           for x in sum(pa._usable_in(p, user, NOW), []))
            if empties and not reserved:
                assert next(p for p in pods if p.name == d.target[0]).in_use == 0, "有空 Pod 就独占"
        if d.kind == pa.RESUME:
            s = _slot(pods, d.target)
            assert s.state == pa.RETAINED and s.user == user and not s.stale
        if d.kind == pa.FULL:
            for p in pods:
                if p.reachable:
                    free, ev = pa._usable_in(p, user, NOW)
                    assert not free and not ev, "还有能用的槽位就不是满员"
            if d.early_retain:
                s = _slot(pods, d.early_retain)
                assert s.state == pa.ACTIVE and NOW - s.last_seen > POLICY.early_idle_seconds
    for a in pa.plan(pods, NOW, POLICY):
        s = _slot(pods, a.ref)
        assert a.ref[0] in reach
        if a.kind == pa.EVICT:
            assert s.state in (pa.RETAINED, pa.FREE) and not s.dirty, "dirty 与在用的永不腾挪"
        if a.kind == pa.RETAIN:
            assert s.state == pa.ACTIVE
        if a.kind == pa.VERIFY:
            assert s.state in (pa.EVICTING, pa.DISABLED)
    refs = [a.ref for a in pa.plan(pods, NOW, POLICY)]
    assert len(refs) == len(set(refs)), "同一个槽位一轮只做一件事"


def test_a_user_active_in_a_pod_whose_steward_is_unreachable_is_still_forwarded():
    """管家重启不该打断对话:网关直连槽位,不经管家。"""
    pods = [pod("p0", "A:u1", "F", reachable=False), pod("p1", "F", "F")]
    assert decide(pods, "u1") == pa.Decision(pa.FORWARD, target=("p0", 0))


def test_retained_in_an_unreachable_pod_is_not_resumed_there():
    pods = [pod("p0", "R:u1", "F", reachable=False), pod("p1", "F", "F")]
    d = decide(pods, "u1")
    assert d.kind == pa.BIND and d.target[0] == "p1"
