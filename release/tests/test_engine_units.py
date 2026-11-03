"""执行引擎:顺序、跳过、关卡(含重启后接着等)、暂停、失败续跑、取消。"""
from __future__ import annotations

import pathlib
import sys
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release.engine import Engine, StepFailed  # noqa: E402
from release.store import Store  # noqa: E402

PLAN = [{"id": "a", "subs": ["一", "二"]}, {"id": "b", "skip": True}, {"id": "c"}, {"id": "d"}]


def _until(cond, s=5):
    end = time.time() + s
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "release.db")
    yield s
    s.close()


def make(store, steps):
    return Engine(store, steps, sleep=lambda _s: time.sleep(0.01), poll_s=0.01)


def test_steps_run_in_order_skipping_the_skipped_and_sharing_data(store):
    seen = []

    def a(ctx):
        ctx.sub(0, "done", "好")
        ctx.share(x=1)
        seen.append("a")
    eng = make(store, {"a": a, "c": lambda ctx: seen.append(("c", ctx.shared()["x"], ctx.plan["to"])),
                       "d": lambda ctx: seen.append("d")})
    rid = eng.start("upgrade", "v3", "v4", PLAN, {"to": "v4"}, None)
    eng.wait()
    r = store.run(rid)
    assert seen == ["a", ("c", 1, "v4"), "d"] and r["status"] == "done"
    assert [s["status"] for s in r["steps"]] == ["done", "skipped", "done", "done"]
    assert r["steps"][0]["subs"][0] == {"label": "一", "st": "done", "note": "好"}


def test_a_gate_blocks_until_the_operator_chooses(store):
    got = []
    eng = make(store, {"a": lambda ctx: got.append(ctx.gate("review", {"n": 3})), "c": lambda c: None, "d": lambda c: None})
    rid = eng.start("upgrade", "v3", "v4", PLAN, {}, None)
    assert _until(lambda: store.run(rid)["status"] == "waiting")
    assert store.run(rid)["gate"]["payload"] == {"n": 3} and store.run(rid)["steps"][0]["status"] == "waiting"
    eng.resolve(rid, "ok")
    eng.wait()
    assert got == ["ok"] and store.run(rid)["status"] == "done"
    assert any(a["action"] == "关卡 · review" for a in store.audits())


def test_failure_stops_and_resume_reruns_the_failed_step(store):
    tries = []

    def c(ctx):
        tries.append(1)
        if len(tries) == 1:
            raise StepFailed("网关 180 秒没就绪")
    eng = make(store, {"a": lambda ctx: None, "c": c, "d": lambda ctx: None})
    rid = eng.start("upgrade", "v3", "v4", PLAN, {}, None)
    eng.wait()
    r = store.run(rid)
    assert r["status"] == "failed" and r["steps"][2]["status"] == "failed" and "180 秒" in r["error"]
    eng.resume(rid)
    eng.wait()
    r = store.run(rid)
    assert r["status"] == "done" and len(tries) == 2 and r["steps"][2]["tries"] == 2
    assert r["steps"][0]["tries"] == 1, "做完的步骤续跑时不重做"


def test_an_unexpected_error_keeps_the_traceback_in_the_log_only(store):
    eng = make(store, {"a": lambda ctx: 1 / 0})
    rid = eng.start("upgrade", "v3", "v4", PLAN, {}, None)
    eng.wait()
    r = store.run(rid)
    assert r["error"].startswith("ZeroDivisionError") and "Traceback" not in r["error"]
    assert any("Traceback" in l["text"] for l in store.logs(rid))


def test_pause_inside_a_long_wait_and_resume(store):
    state = {"n": 0}

    def a(ctx):
        while state["n"] < 1000:
            ctx.check()
            state["n"] += 1
            time.sleep(0.005)
    eng = make(store, {"a": a, "c": lambda ctx: None, "d": lambda ctx: None})
    rid = eng.start("upgrade", "v3", "v4", PLAN, {}, None)
    assert _until(lambda: state["n"] > 3)
    eng.pause(rid)
    eng.wait()
    assert store.run(rid)["status"] == "paused" and store.run(rid)["steps"][0]["status"] == "paused"
    state["n"] = 999
    eng.resume(rid)
    eng.wait()
    assert store.run(rid)["status"] == "done"


def test_cancel_at_a_gate_ends_the_release(store):
    eng = make(store, {"a": lambda ctx: ctx.gate("review"), "c": lambda c: None, "d": lambda c: None})
    rid = eng.start("upgrade", "v3", "v4", PLAN, {}, None)
    assert _until(lambda: store.open_gate(rid) is not None)
    eng.resolve(rid, "cancel")
    eng.wait()
    assert store.run(rid)["status"] == "cancelled"


def test_only_one_release_at_a_time(store):
    eng = make(store, {"a": lambda ctx: ctx.gate("review"), "c": lambda c: None, "d": lambda c: None})
    rid = eng.start("upgrade", "v3", "v4", PLAN, {}, None)
    with pytest.raises(RuntimeError, match="进行中"):
        eng.start("upgrade", "v3", "v4", PLAN, {}, None)
    assert _until(lambda: store.open_gate(rid) is not None)
    eng.resolve(rid, "ok")
    eng.wait()


def test_after_a_restart_the_open_gate_is_waited_on_again(tmp_path):
    s1 = Store(tmp_path / "r.db")
    eng = make(s1, {"a": lambda ctx: ctx.gate("review", {"v": 1}), "c": lambda c: None, "d": lambda c: None})
    rid = eng.start("upgrade", "v3", "v4", PLAN, {}, None)
    assert _until(lambda: s1.open_gate(rid) is not None)
    gid = s1.open_gate(rid)["id"]
    eng.pause(rid)                                  # 模拟进程退出:线程停下
    eng.wait()
    s2 = Store(tmp_path / "r.db")
    s2.mark_interrupted()
    eng2 = make(s2, {"a": lambda ctx: ctx.gate("review", {"v": 2}), "c": lambda c: None, "d": lambda c: None})
    eng2.resume(rid)
    assert _until(lambda: s2.run(rid)["status"] == "waiting")
    assert s2.open_gate(rid)["id"] == gid, "接着等原来那个关卡,不新开一个"
    eng2.resolve(rid, "ok")
    eng2.wait()
    assert s2.run(rid)["status"] == "done"


def test_a_release_waiting_at_a_gate_is_reattached_after_a_restart(tmp_path, monkeypatch):
    """进程重启后,停在关卡上的发布要有线程接着等;否则界面上点了确认也不会往下走。"""
    from release import app as appmod
    s1 = Store(tmp_path / "s" / "release.db")
    eng = make(s1, {"a": lambda ctx: ctx.gate("review"), "c": lambda c: None, "d": lambda c: None})
    rid = eng.start("upgrade", "v3", "v4", PLAN, {}, None)
    assert _until(lambda: s1.run(rid)["status"] == "waiting")
    eng._pause[rid] = True                          # 让旧线程退出,模拟进程没了(状态留在 waiting)
    eng.wait()
    s1.update_run(rid, status="waiting")
    a = appmod.App(Store(tmp_path / "s" / "release.db"), tmp_path, tmp_path / "s")
    a.engine.steps = {"a": lambda ctx: ctx.gate("review"), "c": lambda c: None, "d": lambda c: None}
    a.engine.sleep, a.engine.poll_s = (lambda _s: time.sleep(0.01)), 0.01
    assert a.reattach() == [rid]
    assert _until(lambda: a.store.open_gate(rid) is not None)
    a.engine.resolve(rid, "ok")
    a.engine.wait()
    assert a.store.run(rid)["status"] == "done"
