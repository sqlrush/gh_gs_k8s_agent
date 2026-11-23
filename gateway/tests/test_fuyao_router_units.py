"""Fuyao 运行模式的网关部分(2026-11-17):OpenSandbox 客户端、沙箱令牌、路由器、回收器、配置。

OpenSandbox 用真 HTTP 的替身(fake_fuyao.FakeOpenSandbox,按平台实测行为写);envd 用对象替身(它的协议在
test_fuyao_envd_units.py 里对着真 HTTP 测)。要点:
- 新用户:建沙箱 → 下发配置(含签名的沙箱令牌,集群服务能独立核对)→ 探活;第二次请求走缓存,不碰平台;
- 非交互请求不建、不恢复(开着的标签页不能把暂停的沙箱拉起来);
- 暂停的沙箱恢复后换了地址、平台还回旧地址:探活用本人口令,连不上就等,不会串到别人那里;
- 两个网关副本并发建出两台:只留最早的;
- 沙箱组满了回等待页;开通前提缺失当场报出来;
- 回收器:活动写进 metadata、闲置先收尾再暂停、有会话在跑不动、收尾失败不删、暂停超期删除、到期前续期;
- 换版:旧模板的沙箱闲置时收尾删除、暂停前已回写的直接重建,没回写成的不删。
"""
import pathlib
import sys
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import config as gwconfig  # noqa: E402
from gateway import fuyaorouter as fr  # noqa: E402
from gateway import fuyaosweeper as fs  # noqa: E402
from gateway import opensandbox as osb  # noqa: E402
from gateway import pods, poolrouter, sbxtoken  # noqa: E402
from gateway.tests.fake_fuyao import FakeEnvd, FakeOpenSandbox  # noqa: E402

KEY = b"k" * 32
POLICY = fr.Policy(templates={"runtime": "tpl-run", "kb-import": "tpl-imp"},
                   groups={"runtime": "g-run", "kb-import": "g-imp"}, wait_s=0.0, idle_s=600, retain_s=86400)


class Clock:
    def __init__(self, t=None):
        self.t = time.time() if t is None else t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture()
def env():
    fake = FakeOpenSandbox(capacity={"g-run": 3, "g-imp": 1})
    envd = FakeEnvd()
    clock = Clock()
    logs = []
    prereq_calls = []

    def prereq(user, kind):
        prereq_calls.append((user, kind))
        if user == "nobody":
            raise pods.PrerequisiteMissing("缺 Secret grmp-token-nobody")

    router = fr.FuyaoRouter(osb.Client(fake.url, timeout_s=5), envd, POLICY, KEY,
                            password_for=lambda u: "pw-" + u,
                            launch_env=lambda u, k: {"MODEL_ID": "m1", "EMPTY": "", "GSDB_STATE_URL": "http://10.0.0.1:8090"},
                            prereq=prereq, prober=envd.prober, log=logs.append, clock=clock, sleep=clock.sleep)
    yield fake, envd, router, clock, logs, prereq_calls
    fake.close()


def _platform_calls(fake):
    return [(m, p) for m, p, _b, _h in fake.requests]


# -- 路由器 ------------------------------------------------------------------------------------------

def test_a_new_user_gets_a_sandbox_with_signed_identity_and_the_second_request_uses_the_cache(env):
    fake, envd, router, clock, logs, prereq_calls = env
    t = router.target("u1001")
    assert len(fake.boxes) == 1
    bid, box = next(iter(fake.boxes.items()))
    assert box["metadata"] == {"gsdb-app": "gaussdb-agent", "gsdb-user": "u1001", "gsdb-kind": "runtime",
                               "gsdb-active": str(int(clock.t)), "gsdb-template": "tpl-run"}
    assert box["group"] == "g-run" and box["template"] == "tpl-run"
    create = [b for m, p, b, _h in fake.requests if m == "POST" and p == "/v1/sandboxes"][0]
    assert create["resourceLimits"] == {"cpu": "2", "memory": "4Gi"} and "volumes" not in create, \
        "平台要求带 resourceLimits;卷在 E2B 后端不生效,不发"
    assert t == poolrouter.Target(pod=bid, slot=0, host=box["ip"], port=4096, password="pw-u1001",
                                  reports_port=4097, reports_token=sbxtoken.reports_token(KEY, bid), since=clock.t)
    host, argv, user, envs = envd.runs[0]
    assert host == box["ip"] and argv[-1] == "fuyao-start" and user == "root"
    claims = sbxtoken.verify(KEY, envs["GSDB_SANDBOX_TOKEN"])
    assert (claims.sandbox_id, claims.user_id, claims.kind) == (bid, "u1001", "runtime")
    assert envs["OPENCODE_SERVER_PASSWORD"] == "pw-u1001" and envs["GSDB_USER_ID"] == "u1001"
    assert envs["MODEL_ID"] == "m1" and "EMPTY" not in envs, "空值不下发"
    assert prereq_calls == [("u1001", "runtime")]
    n = len(fake.requests)
    assert router.target("u1001") is t and len(fake.requests) == n, "已在用:走缓存,不碰平台"


def test_a_sandbox_token_does_not_verify_under_another_key_or_when_tampered(env):
    _fake, envd, router, *_ = env
    router.target("u1001")
    token = envd.runs[0][3]["GSDB_SANDBOX_TOKEN"]
    assert sbxtoken.verify(b"x" * 32, token) is None
    payload, sig = token[len(sbxtoken.PREFIX):].split(".")
    forged = sbxtoken.PREFIX + payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB") + "." + sig
    assert sbxtoken.verify(KEY, forged) is None


def test_non_interactive_requests_neither_create_nor_resume(env):
    fake, _envd, router, *_ = env
    with pytest.raises(poolrouter.NotActive):
        router.target("u1001", interactive=False)
    fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "u1002", "gsdb-kind": "runtime"}, state="Paused")
    with pytest.raises(poolrouter.NotActive):
        router.target("u1002", interactive=False)
    assert not [1 for m, p, *_ in fake.requests if m == "POST"], "没有建,也没有恢复"


def test_a_paused_sandbox_is_resumed_and_reached_at_its_new_address(env):
    fake, envd, router, clock, *_ = env
    bid = fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "u1001", "gsdb-kind": "runtime", "gsdb-paused": "1"},
                   state="Paused")
    old = fake.ip(bid)
    # 内存快照里 opencode 一直在:恢复后在虚拟机的「当前」地址上应答本人口令;
    # 恢复后沙箱里的同步向状态服务报到,网关从那里拿到新地址(恢复过的不用平台给的地址)
    router.prober = lambda host, pw: host == fake.ip(bid) and pw == "pw-u1001"
    router.locate = lambda sid, user, kind: (fake.ip(sid), 0.0) if fake.boxes[sid]["state"] == "Running" else None
    t = router.target("u1001")
    assert fake.boxes[bid]["state"] == "Running" and t.host == fake.ip(bid) != old
    assert "gsdb-paused" not in fake.boxes[bid]["metadata"]
    assert not envd.runs, "恢复的是内存快照,不再跑启动器"


def _victim_on_the_old_address(fake, envd, router):
    """u1001 的沙箱暂停着;它暂停前的地址已分给了 u2002 的新沙箱(Pod 网段地址会复用)。记下网关往哪儿送过口令。"""
    fake.stale_after_resume = True
    bid = fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "u1001", "gsdb-kind": "runtime"}, state="Paused")
    victim = fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "u2002", "gsdb-kind": "runtime"})
    fake.boxes[victim]["ip"] = fake.ip(bid)
    envd.ready.add(fake.ip(victim))
    probed = []
    real_prober = envd.prober
    router.prober = lambda host, pw: (probed.append((host, pw)), real_prober(host, pw) and pw == "pw-u2002")[1]
    router.locate = lambda sid, user, kind: None                   # 恢复后还没向状态服务报到
    return bid, fake.ip(victim), probed


def test_a_stale_address_after_resume_never_reaches_someone_else_and_ends_in_a_wait(env):
    fake, envd, router, clock, *_ = env
    router.policy = POLICY.__class__(**dict(POLICY.__dict__, wait_s=5.0))
    bid, old, probed = _victim_on_the_old_address(fake, envd, router)
    sleeps = []
    router.sleep = lambda s: (sleeps.append(s), clock.sleep(s))
    t0 = clock.t
    with pytest.raises(poolrouter.PoolWait) as ei:
        router.target("u1001")
    assert ei.value.reason == "address_stale"
    assert not [p for p in probed if p[0] == old], "本人口令不能送到平台给的旧地址(那里可能已是别人的沙箱)"
    assert not [r for r in envd.runs if r[0] == old], "不能在旧地址上跑启动器:会把本人令牌写进别人的沙箱"
    assert fake.boxes[bid]["metadata"]["gsdb-resumed"] == str(int(t0)), "恢复过的沙箱记下恢复时刻,别的副本 / 清扫器也只认之后报到的地址"
    assert len(sleeps) >= 5 and set(sleeps) == {0.5}, "等报到时每 0.5 秒问一次状态服务,不在旧地址上白等超时"


def test_a_resumed_sandbox_is_only_reached_at_the_address_it_reported(env):
    fake, envd, router, *_ = env
    bid, old, probed = _victim_on_the_old_address(fake, envd, router)
    with pytest.raises(poolrouter.PoolWait):
        router.target("u1001")                                     # 恢复(这一副本)
    router.forget("u1001")
    with pytest.raises(poolrouter.PoolWait) as ei:                 # 已在跑、带恢复标记:另一副本 / 下一次请求
        router.target("u1001")
    assert ei.value.reason == "address_stale" and not [p for p in probed if p[0] == old]
    router.locate = lambda sid, user, kind: (fake.ip(bid), 0.0) if sid == bid else None
    envd.ready.add(fake.ip(bid))
    real_prober = envd.prober
    router.prober = lambda host, pw: real_prober(host, pw) and pw == "pw-u1001"
    t = router.target("u1001")
    assert t.host == fake.ip(bid) != old and not [r for r in envd.runs if r[0] == old]


def test_a_report_from_before_the_pause_is_not_trusted_after_resume(env):
    fake, envd, router, clock, *_ = env
    bid, old, probed = _victim_on_the_old_address(fake, envd, router)
    router.locate = lambda sid, user, kind: (old, 20.0) if sid == bid else None   # 暂停前 20 秒的报到,仍在 30 秒新鲜期内
    with pytest.raises(poolrouter.PoolWait) as ei:
        router.target("u1001")
    assert ei.value.reason == "address_stale" and not [p for p in probed if p[0] == old], \
        "刚暂停就恢复时,暂停前的记录还「新鲜」,但那个地址可能已分给别人:只认恢复之后报上来的"


def test_a_never_resumed_sandbox_still_falls_back_to_the_platform_address(env):
    fake, envd, router, *_ = env
    router.locate = lambda sid, user, kind: None
    t = router.target("u1001")                                     # 新建:平台给的地址就是它的
    router.forget("u1001")
    assert router.target("u1001").host == t.host == fake.ip(t.pod)


def test_the_sweeper_leaves_a_resumed_sandbox_alone_until_it_reports(env):
    fake, envd, router, clock, *_ = env
    bid, old, probed = _victim_on_the_old_address(fake, envd, router)
    logs = []
    sw = fs.FuyaoSweeper(router, sweep_s=30, busy_check=lambda h, p: (probed.append((h, p)), False)[1],
                         log=logs.append, clock=clock)
    fake.boxes[bid]["state"] = "Running"
    fake.boxes[bid]["metadata"]["gsdb-resumed"] = str(int(clock.t))
    clock.t += 601
    for b in fake.boxes.values():                                  # u2002 一直在用:不收尾它
        if b["metadata"].get("gsdb-user") == "u2002":
            b["metadata"]["gsdb-active"] = str(int(clock.t))
    out = sw.sweep()
    assert out["failed"] == 1 and fake.boxes[bid]["state"] == "Running"
    assert (old, "pw-u1001") not in probed and not [r for r in envd.runs if r[0] == old], \
        "收尾不能跑到别人的沙箱里(会放开别人的独占,还会据此删掉本人的沙箱)"
    assert sw.sweep()["failed"] == 1 and len([m for m in logs if "还没报到" in m]) == 1, "同一台只提醒一次"
    code, body = sw.act("stop", bid)
    assert code == 409 and "还没" in body["error"]
    assert sw.act("delete", bid)[0] == 409 and bid in fake.boxes
    code, body = sw.act("delete", bid, force=True)
    assert code == 200 and bid not in fake.boxes and body["synced"] is False, "强删不收尾(地址未知),如实写没回写"
    assert not [r for r in envd.runs if r[0] == old]


def test_after_resume_the_address_seen_by_the_state_service_is_used(env):
    fake, envd, router, clock, *_ = env
    fake.stale_after_resume = True
    bid = fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "u1001", "gsdb-kind": "runtime"}, state="Paused")
    seen = {}

    def locate(sid, user, kind):
        assert (user, kind) == ("u1001", "runtime")
        return (seen[sid], 0.0) if sid in seen else None
    router.locate = locate
    real_resume = fake.handle

    def handle(method, path, query, body):
        out = real_resume(method, path, query, body)
        if path.endswith("/resume"):                                   # 恢复后沙箱里的同步续独占:状态服务看到新地址
            seen[bid] = fake.ip(bid)
            envd.ready.add(fake.ip(bid))
        return out
    fake.handle = handle
    t = router.target("u1001")
    assert t.host == fake.ip(bid) != fake.boxes[bid]["old_ip"], "平台回旧地址,用状态服务看到的新地址"


def test_a_failing_locator_falls_back_to_the_platform_address(env):
    fake, envd, router, *_ = env
    router.locate = lambda sid, user, kind: (_ for _ in ()).throw(OSError("状态服务连不上"))
    t = router.target("u1001")
    assert t.host == fake.ip(t.pod)


def test_concurrent_creation_by_two_gateways_keeps_only_the_oldest(env):
    fake, envd, router, *_ = env
    md = {"gsdb-app": "gaussdb-agent", "gsdb-user": "u1001", "gsdb-kind": "runtime"}

    def other_gateway_was_faster(f, mine):
        f.on_create = None
        older = f.add(md, created=f.boxes[mine]["created"] - 5)
        other_gateway_was_faster.older = older
    fake.on_create = other_gateway_was_faster
    t = router.target("u1001")
    older = other_gateway_was_faster.older
    assert list(fake.boxes) == [older] and t.pod == older, "自己建的那台删掉,用最早的"
    assert envd.runs and envd.runs[-1][0] == fake.ip(older), "那台还没起来:envd 在,补起一次(启动器幂等)"


def test_extra_sandboxes_of_one_user_and_kind_are_removed(env):
    fake, envd, router, *_ = env
    md = {"gsdb-app": "gaussdb-agent", "gsdb-user": "u1001", "gsdb-kind": "runtime"}
    a = fake.add(md, created=100)
    b = fake.add(md, created=200)
    envd.ready.add(fake.ip(a))
    t = router.target("u1001")
    assert t.pod == a and b not in fake.boxes


def test_a_full_group_is_a_waiting_page_and_missing_prerequisites_are_reported(env):
    fake, _envd, router, *_ = env
    for i in range(3):
        fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "x%d" % i, "gsdb-kind": "runtime"}, group="g-run")
    with pytest.raises(poolrouter.PoolFull):
        router.target("u1001")
    with pytest.raises(pods.PrerequisiteMissing):
        router.target("nobody")


def test_a_failed_launch_becomes_a_wait_with_the_reason_logged_but_not_the_secrets(env):
    _fake, envd, router, _clock, logs, _p = env
    envd.fail_launch = True
    with pytest.raises(poolrouter.PoolWait) as ei:
        router.target("u1001")
    assert ei.value.reason == "launch_failed"
    text = "\n".join(logs)
    assert "启动失败" in text and "载入状态失败" in text
    assert "pw-u1001" not in text and sbxtoken.PREFIX not in text, "日志里不出现口令与令牌"


def test_cluster_service_names_are_resolved_before_they_reach_the_sandbox():
    from gateway.__main__ import ip_url
    seen = []

    def resolve(host):
        seen.append(host)
        if host == "gone.example.svc":
            raise OSError("Name or service not known")
        return "10.96.0.7"
    assert ip_url("http://statesvc.gaussdb-agent.svc:8090", resolve) == "http://10.96.0.7:8090"
    assert ip_url("http://statesvc.gaussdb-agent.svc", resolve) == "http://10.96.0.7"
    assert ip_url("http://10.96.0.8:8091", resolve) == "http://10.96.0.8:8091" and len(seen) == 2, "已是 IP 不解析"
    for bad in ("http://gone.example.svc:8090", ""):
        with pytest.raises(OSError):
            ip_url(bad, resolve)


def test_an_unresolvable_service_address_is_a_launch_failure_not_a_sandbox_with_a_dead_url(env):
    fake, envd, _router, clock, logs, _p = env

    def launch_env(u, k):
        raise fr.LaunchError("解析状态服务 / 凭据代理服务地址失败:Name or service not known")
    router = fr.FuyaoRouter(osb.Client(fake.url, timeout_s=5), envd, POLICY, KEY, password_for=lambda u: "pw-" + u,
                            launch_env=launch_env, prereq=lambda u, k: None, prober=envd.prober, log=logs.append,
                            clock=clock, sleep=clock.sleep)
    with pytest.raises(poolrouter.PoolWait) as ei:
        router.target("u1001")
    assert ei.value.reason == "launch_failed" and "解析状态服务" in "\n".join(logs)
    assert not [r for r in envd.runs if r[1][-1] == "fuyao-start"], "地址不对就不进沙箱启动"


def test_a_sandbox_whose_launcher_keeps_failing_is_reported_as_broken_instead_of_an_endless_wait(env):
    fake, envd, router, clock, logs, _p = env
    envd.fail_launch = True
    for _ in range(fr.MAX_LAUNCH_FAILS - 1):
        with pytest.raises(poolrouter.PoolWait):
            router.target("u1001")
        clock.t += fr.RELAUNCH_GAP_S + 1
    with pytest.raises(fr.SandboxBroken) as ei:
        router.target("u1001")
    assert "接连失败 %d 次" % fr.MAX_LAUNCH_FAILS in str(ei.value) and "/admin/fuyao" in str(ei.value)
    assert len(fake.boxes) == 1, "不自动重建:模板坏了重建只会循环"
    envd.fail_launch = False
    clock.t += fr.RELAUNCH_GAP_S + 1
    assert router.target("u1001").pod in fake.boxes, "修好之后照常起来"
    assert router._fails == {}


def test_an_unreachable_envd_is_not_counted_as_a_broken_sandbox(env):
    fake, envd, router, clock, *_ = env
    router.target("u1001")
    host = next(iter(envd.ready))
    router.forget("u1001")
    envd.ready.clear()
    envd.down.add(host)
    for _ in range(fr.MAX_LAUNCH_FAILS + 1):
        with pytest.raises(poolrouter.PoolWait):
            router.target("u1001")
        clock.t += fr.RELAUNCH_GAP_S + 1


def test_the_import_environment_is_its_own_sandbox_from_its_own_template_and_group(env):
    fake, envd, router, *_ = env
    router.target("u1001")
    t = router.target("u1001", kind="kb-import")
    boxes = {v["metadata"]["gsdb-kind"]: v for v in fake.boxes.values()}
    assert set(boxes) == {"runtime", "kb-import"}
    assert boxes["kb-import"]["template"] == "tpl-imp" and boxes["kb-import"]["group"] == "g-imp"
    assert sbxtoken.verify(KEY, envd.runs[-1][3]["GSDB_SANDBOX_TOKEN"]).kind == "kb-import"
    assert t.host == boxes["kb-import"]["ip"]


def test_forget_drops_the_cached_target(env):
    fake, _envd, router, *_ = env
    t = router.target("u1001")
    router.forget_sandbox(t.pod)
    n = len(fake.requests)
    router.target("u1001")
    assert len(fake.requests) > n, "缓存作废后重新查平台"


# -- 回收器 ------------------------------------------------------------------------------------------

def _sweeper(router, clock, busy=lambda h, p: False):
    return fs.FuyaoSweeper(router, sweep_s=30, busy_check=busy, log=lambda m: None, clock=clock)


def test_activity_is_written_into_sandbox_metadata_and_kept_when_writing_fails(env):
    fake, _envd, router, clock, *_ = env
    t = router.target("u1001")
    clock.t += 100
    router.note_activity("u1001")
    _sweeper(router, clock).sweep()
    assert fake.boxes[t.pod]["metadata"]["gsdb-active"] == str(int(clock.t))
    router.note_activity("u1001")
    fake.close()                                                            # 平台连不上
    _sweeper(router, clock).sweep()
    assert router.drain_activity(), "列举失败:活动放回去,下一轮再写"


def test_an_idle_sandbox_is_synced_then_paused_and_a_busy_one_is_left_alone(env):
    fake, envd, router, clock, *_ = env
    t = router.target("u1001")
    clock.t += 601
    out = _sweeper(router, clock, busy=lambda h, p: True).sweep()
    assert out["busy"] == 1 and fake.boxes[t.pod]["state"] == "Running"
    out = _sweeper(router, clock).sweep()
    assert out["retired"] == 1 and fake.boxes[t.pod]["state"] == "Paused"
    assert fake.boxes[t.pod]["metadata"]["gsdb-paused"] == str(int(clock.t))
    assert [r[1][-1] for r in envd.runs] == ["fuyao-start", "fuyao-stop"], "先收尾,再暂停(暂停不放开独占)"
    assert router.cached() == {}


def test_without_pause_a_sandbox_whose_final_sync_failed_is_not_deleted(env):
    fake, envd, router, clock, *_ = env
    router.policy = POLICY.__class__(**dict(POLICY.__dict__, pause=False))
    t = router.target("u1001")
    clock.t += 601
    envd.fail_stop = True
    sw = _sweeper(router, clock)
    sw.policy = router.policy
    assert sw.sweep()["failed"] == 1 and t.pod in fake.boxes, "删了会丢最后一个同步周期"
    envd.fail_stop = False
    assert sw.sweep()["retired"] == 1 and t.pod not in fake.boxes
    assert envd.runs[-1][1][-2:] == ["fuyao-stop", "--release"], "删除之前放开独占,新沙箱不用等它过期"


def test_a_sandbox_paused_beyond_the_retention_is_deleted_only_if_synced_before_pause(env):
    fake, _envd, router, clock, *_ = env
    logs = []
    sw = fs.FuyaoSweeper(router, sweep_s=30, busy_check=lambda h, p: False, log=logs.append, clock=clock)
    long_ago = str(int(clock.t - 86401))
    ok = _box(fake, "u1001", "tpl-run", **{"gsdb-paused": long_ago, "gsdb-synced": "1"})
    bad = _box(fake, "u1002", "tpl-run", **{"gsdb-paused": long_ago, "gsdb-synced": "0"})
    unknown = _box(fake, "u1003", "tpl-run", **{"gsdb-paused": long_ago})     # 没记回写结果:按没回写成处理
    out = sw.sweep()
    assert out["deleted"] == 1 and ok not in fake.boxes
    assert out["kept"] == 2 and bad in fake.boxes and unknown in fake.boxes, "删了会丢暂停前最后一段没回写的数据"
    warned = [m for m in logs if "没回写成" in m]
    assert len(warned) == 2 and any(bad in m for m in warned) and any(unknown in m for m in warned)
    assert sw.sweep()["kept"] == 2 and len([m for m in logs if "没回写成" in m]) == 2, "同一台只提醒一次,不每轮刷日志"
    s = sw.summary()
    assert s["unsynced_over_retention"] == 2
    assert {x["sandbox"]: x["synced_before_pause"] for x in s["sandboxes"]} == {bad: False, unknown: False}


def test_admin_delete_refuses_when_the_final_sync_fails_unless_forced(env):
    fake, envd, router, clock, *_ = env
    t = router.target("u1001")
    sw = _sweeper(router, clock)
    envd.fail_stop = True
    code, body = sw.act("delete", t.pod)
    assert code == 409 and t.pod in fake.boxes and body["synced"] is False, "回写没成功默认不删"
    assert "force" in body["error"]
    code, body = sw.act("delete", t.pod, force=True)
    assert code == 200 and t.pod not in fake.boxes and body["synced"] is False and body["forced"] is True
    assert "最后一次成功同步" in body["note"] and "收尾同步把最后的状态写回" not in body["note"], "不能说数据都在 NAS 上"


def test_admin_delete_of_a_paused_sandbox_follows_its_sync_before_pause(env):
    fake, envd, router, clock, *_ = env
    now = str(int(clock.t))
    ok = _box(fake, "u1001", "tpl-run", **{"gsdb-paused": now, "gsdb-synced": "1"})
    bad = _box(fake, "u1002", "tpl-run", **{"gsdb-paused": now, "gsdb-synced": "0"})
    sw = _sweeper(router, clock)
    code, body = sw.act("delete", ok)
    assert code == 200 and ok not in fake.boxes and body["synced"] is True and body["forced"] is False
    code, body = sw.act("delete", bad)
    assert code == 409 and bad in fake.boxes and body["synced"] is False
    assert sw.act("delete", bad, force=True)[0] == 200 and bad not in fake.boxes
    assert envd.runs == [], "暂停的沙箱连不上,不收尾,只看暂停前的回写结果"


def test_admin_delete_of_a_sandbox_neither_running_nor_paused_needs_force(env):
    fake, _envd, router, clock, *_ = env
    odd = fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "u1001", "gsdb-kind": "runtime"}, state="Pending")
    sw = _sweeper(router, clock)
    code, body = sw.act("delete", odd)
    assert code == 409 and odd in fake.boxes and body["synced"] is None and "Pending" in body["error"]
    assert sw.act("delete", odd, force=True)[0] == 200 and odd not in fake.boxes


def test_by_default_no_platform_lifetime_is_requested(env):
    fake, _envd, router, _clock, *_ = env
    t = router.target("u1001")
    create = [b for m, p, b, _h in fake.requests if m == "POST" and p == "/v1/sandboxes"][0]
    assert "timeout" not in create, "不带 timeout:平台不设到期时间(实测),生命周期由网关管"
    assert "gsdb-expires" not in fake.boxes[t.pod]["metadata"]


def _with_lifetime(router, seconds=3600):
    router.policy = POLICY.__class__(**dict(POLICY.__dict__, sandbox_timeout_s=seconds))
    return router.policy


def test_with_a_platform_lifetime_the_sandbox_is_synced_released_and_deleted_before_it_expires(env):
    fake, envd, router, clock, *_ = env
    policy = _with_lifetime(router)
    t = router.target("u1001")
    create = [b for m, p, b, _h in fake.requests if m == "POST" and p == "/v1/sandboxes"][0]
    assert create["timeout"] == 3600
    assert fake.boxes[t.pod]["metadata"]["gsdb-expires"] == str(int(clock.t + 3600)), "列举不回 expiresAt,自己记"
    sw = _sweeper(router, clock, busy=lambda h, p: True)
    sw.policy = policy
    clock.t += 3600 - 900 - 5
    router.note_activity("u1001")
    assert sw.sweep()["expired"] == 0 and t.pod in fake.boxes
    clock.t += 10
    out = sw.sweep()
    assert out["busy"] == 1 and t.pod in fake.boxes, "有会话在跑:等到最后三分之一"
    clock.t += 600
    assert sw.sweep()["expired"] == 1 and t.pod not in fake.boxes, "最后五分钟:有会话也收尾删除(平台到点会直接删)"
    assert envd.runs[-1][1][-2:] == ["fuyao-stop", "--release"]
    assert router.target("u1001").pod != t.pod, "下次请求按新沙箱重建"


def test_a_sandbox_whose_final_sync_fails_near_expiry_is_kept_for_another_try(env):
    fake, envd, router, clock, *_ = env
    sw = _sweeper(router, clock)
    sw.policy = _with_lifetime(router)
    t = router.target("u1001")
    clock.t += 3600 - 60
    envd.fail_stop = True
    assert sw.sweep()["failed"] == 1 and t.pod in fake.boxes, "收尾没成不删,下一轮再试"


def test_paused_sandboxes_near_expiry_are_deleted_only_if_synced_before_pause(env):
    fake, _envd, router, clock, *_ = env
    logs = []
    sw = fs.FuyaoSweeper(router, sweep_s=30, busy_check=lambda h, p: False, log=logs.append, clock=clock)
    sw.policy = _with_lifetime(router)
    soon = str(int(clock.t + 300))
    ok = _box(fake, "u1001", "tpl-run", **{"gsdb-synced": "1", "gsdb-paused": str(int(clock.t)), "gsdb-expires": soon})
    bad = _box(fake, "u1002", "tpl-run", **{"gsdb-synced": "0", "gsdb-paused": str(int(clock.t)), "gsdb-expires": soon})
    later = _box(fake, "u1003", "tpl-run", **{"gsdb-synced": "1", "gsdb-paused": str(int(clock.t)),
                                              "gsdb-expires": str(int(clock.t + 7200))})
    out = sw.sweep()
    assert out["expired"] == 1 and ok not in fake.boxes and later in fake.boxes
    assert out["failed"] == 1 and bad in fake.boxes and "没回写成" in "\n".join(logs)
    assert sw.summary()["sandboxes"][0]["expires_at"] is not None


def test_admin_summary_and_actions(env):
    fake, envd, router, clock, *_ = env
    t = router.target("u1001")
    foreign = fake.add({"owner": "someone-else"})
    sw = _sweeper(router, clock)
    s = sw.summary()
    assert [x["user_id"] for x in s["sandboxes"]] == ["u1001"] and s["states"] == {"Running": 1}
    assert sw.act("pause", t.pod)[0] == 200 and fake.boxes[t.pod]["state"] == "Paused"
    assert sw.act("resume", t.pod)[0] == 200 and fake.boxes[t.pod]["state"] == "Running"
    assert fake.boxes[t.pod]["metadata"].get("gsdb-resumed"), "运维恢复也先打恢复标记"
    assert sw.act("delete", t.pod)[0] == 409 and t.pod in fake.boxes, "恢复后还没报到:地址未知,不收尾不删"
    router.locate = lambda sid, user, kind: (fake.ip(sid), 0.0)     # 沙箱报到了
    code, body = sw.act("delete", t.pod)
    assert code == 200 and body["synced"] is True and t.pod not in fake.boxes
    assert sw.act("delete", foreign)[0] == 404, "不是本智能体的沙箱不动"
    assert sw.act("explode", "x")[0] == 404


# -- 换版(发版换了模板) ------------------------------------------------------------------------------

def _new_templates(router, **kinds):
    router.policy = POLICY.__class__(**dict(POLICY.__dict__, templates=dict(POLICY.templates, **kinds)))
    return router.policy


def _box(fake, user, template, **md):
    return fake.add(dict({"gsdb-app": "gaussdb-agent", "gsdb-user": user, "gsdb-kind": "runtime",
                          "gsdb-template": template}, **md), state="Paused")


def test_an_idle_old_sandbox_is_synced_released_and_deleted_and_the_next_login_uses_the_new_template(env):
    fake, envd, router, clock, *_ = env
    t = router.target("u1001")
    assert fake.boxes[t.pod]["metadata"]["gsdb-template"] == "tpl-run", "建沙箱时记下模板"
    sw = _sweeper(router, clock)
    sw.policy = _new_templates(router, runtime="tpl-run-2")
    assert sw.summary()["outdated"] == 1
    clock.t += 300
    assert sw.sweep()["upgraded"] == 0 and t.pod in fake.boxes, "在跑、没闲置的旧版沙箱不打断"
    clock.t += 301
    assert sw.sweep()["upgraded"] == 1 and t.pod not in fake.boxes
    assert envd.runs[-1][1][-2:] == ["fuyao-stop", "--release"], "收尾并放开独占后才删"
    t2 = router.target("u1001")
    assert t2.pod != t.pod and fake.boxes[t2.pod]["template"] == "tpl-run-2"
    assert fake.boxes[t2.pod]["metadata"]["gsdb-template"] == "tpl-run-2" and sw.summary()["outdated"] == 0


def test_an_old_sandbox_whose_final_sync_fails_is_paused_as_unsynced_and_later_resumed_not_rebuilt(env):
    fake, envd, router, clock, *_ = env
    t = router.target("u1001")
    sw = _sweeper(router, clock)
    sw.policy = _new_templates(router, runtime="tpl-run-2")
    clock.t += 601
    envd.fail_stop = True
    assert sw.sweep()["retired"] == 1
    box = fake.boxes[t.pod]
    assert box["state"] == "Paused" and box["metadata"]["gsdb-synced"] == "0", "收尾没成:暂停保住内存,不删"
    assert sw.sweep()["upgraded"] == 0 and t.pod in fake.boxes, "没回写成的旧版沙箱清扫器也不删"
    with pytest.raises(poolrouter.PoolWait):                       # 恢复后换了地址,替身里探不通:等待
        router.target("u1001")
    assert fake.boxes[t.pod]["state"] == "Running" and "gsdb-synced" not in fake.boxes[t.pod]["metadata"]
    assert len([b for b in fake.boxes.values() if b["metadata"].get("gsdb-user") == "u1001"]) == 1


def test_a_paused_old_sandbox_synced_before_pause_is_rebuilt_at_login_or_removed_by_the_sweeper(env):
    fake, _envd, router, clock, *_ = env
    old = _box(fake, "u1001", "tpl-run-0", **{"gsdb-synced": "1", "gsdb-paused": str(int(clock.t))})
    with pytest.raises(poolrouter.NotActive):
        router.target("u1001", interactive=False)
    assert old in fake.boxes, "非交互请求不建、不删"
    t = router.target("u1001")
    assert old not in fake.boxes and fake.boxes[t.pod]["template"] == "tpl-run", "同一个请求里直接按新模板重建"
    gone = _box(fake, "u1002", "tpl-run-0", **{"gsdb-synced": "1", "gsdb-paused": str(int(clock.t))})
    kept = _box(fake, "u1003", "tpl-run", **{"gsdb-synced": "1", "gsdb-paused": str(int(clock.t))})
    sw = _sweeper(router, clock)
    assert sw.summary()["outdated"] == 1
    out = sw.sweep()
    assert out["upgraded"] == 1 and gone not in fake.boxes and kept in fake.boxes, "当前版本的暂停沙箱照常保留"


def test_pausing_records_whether_the_final_sync_succeeded(env):
    fake, envd, router, clock, *_ = env
    t = router.target("u1001")
    sw = _sweeper(router, clock)
    assert sw.act("pause", t.pod)[0] == 200 and fake.boxes[t.pod]["metadata"]["gsdb-synced"] == "1"
    assert sw.act("resume", t.pod)[0] == 200 and "gsdb-synced" not in fake.boxes[t.pod]["metadata"]


# -- 配置 --------------------------------------------------------------------------------------------

BASE = {"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t", "GATEWAY_TRUST_SECRET": "s" * 32}
FUYAO = dict(BASE, GATEWAY_RUNTIME_MODE="fuyao", GATEWAY_POOL_CONTROL_KEY="c" * 32, GATEWAY_ADMIN_TOKEN="a" * 32,
             GATEWAY_FUYAO_API="http://opensandbox-server.flux-system.svc:80",
             GATEWAY_FUYAO_RUNTIME_TEMPLATE="gsdb-runtime-v1-5-0", GATEWAY_FUYAO_RUNTIME_GROUP="sg-gsdb-run",
             GATEWAY_FUYAO_IMPORT_TEMPLATE="gsdb-import-v1-5-0", GATEWAY_FUYAO_IMPORT_GROUP="sg-gsdb-imp",
             GATEWAY_FUYAO_STATE_URL="http://10.96.0.20:8090", GATEWAY_FUYAO_CREDPROXY_URL="http://10.96.0.21:8091")


def test_fuyao_config_parses_and_other_modes_ignore_it():
    c = gwconfig.load(FUYAO)
    assert c.runtime_mode == "fuyao" and c.fuyao_pause is True and c.fuyao_idle_seconds == 1800
    assert c.fuyao_state_url == "http://10.96.0.20:8090"
    assert gwconfig.load(BASE).runtime_mode == "ondemand", "不在 fuyao 模式时缺这些不拒绝"


def test_fuyao_mode_refuses_to_start_without_the_admin_token():
    """Fuyao 模式的运维(/admin/fuyao:回写、暂停、删除,发版台的换版进度与冒烟)全靠运维密钥;没有它运维接口整体 404,
    出了问题只能去平台上删沙箱(不回写)。所以缺了就不启动,并说清楚去哪补(2026-11-18)。其他模式照旧可选。"""
    for bad in ("", "   "):
        with pytest.raises(gwconfig.ConfigError) as ei:
            gwconfig.load(dict(FUYAO, GATEWAY_ADMIN_TOKEN=bad))
        assert "ADMIN_TOKEN" in str(ei.value) and "gateway-auth" in str(ei.value)
    no_key = {k: v for k, v in FUYAO.items() if k != "GATEWAY_ADMIN_TOKEN"}
    with pytest.raises(gwconfig.ConfigError):
        gwconfig.load(no_key)
    assert gwconfig.load(dict(BASE, GATEWAY_ADMIN_TOKEN="")).admin_token == "", "按需模式不强制"


def test_the_two_service_urls_follow_the_gateway_namespace_when_not_given():
    """没填(或填空)时按网关所在命名空间推导:换命名空间部署不用再手工改两个地址(2026-11-18)。显式填了的照用。"""
    c = gwconfig.load(dict(FUYAO, GATEWAY_FUYAO_STATE_URL="", GATEWAY_FUYAO_CREDPROXY_URL=" "))
    assert c.fuyao_state_url == "http://statesvc.ns.svc:8090"
    assert c.fuyao_credproxy_url == "http://credproxy.ns.svc:8091"
    bare = {k: v for k, v in FUYAO.items() if k not in ("GATEWAY_FUYAO_STATE_URL", "GATEWAY_FUYAO_CREDPROXY_URL")}
    c = gwconfig.load(dict(bare, GATEWAY_NAMESPACE="gaussdb-agent-fy"))
    assert (c.fuyao_state_url, c.fuyao_credproxy_url) == ("http://statesvc.gaussdb-agent-fy.svc:8090",
                                                          "http://credproxy.gaussdb-agent-fy.svc:8091")
    assert gwconfig.load(FUYAO).fuyao_credproxy_url == "http://10.96.0.21:8091", "显式填了的照用"


@pytest.mark.parametrize("patch,needle", [
    ({"GATEWAY_FUYAO_API": ""}, "GATEWAY_FUYAO_API"),
    ({"GATEWAY_FUYAO_STATE_URL": "statesvc:8090"}, "GATEWAY_FUYAO_STATE_URL"),
    ({"GATEWAY_POOL_CONTROL_KEY": "short"}, "GATEWAY_POOL_CONTROL_KEY"),
    ({"GATEWAY_FUYAO_RUNTIME_GROUP": "Bad_Group"}, "沙箱组名"),
    ({"GATEWAY_FUYAO_API": "opensandbox:80"}, "http(s)://"),
    ({"GATEWAY_FUYAO_MEMORY": "four"}, "资源量"),
    ({"GATEWAY_FUYAO_PAUSE": "maybe"}, "GATEWAY_FUYAO_PAUSE"),
    ({"GATEWAY_FUYAO_IDLE_SECONDS": "60"}, "太小"),
])
def test_fuyao_config_fails_closed(patch, needle):
    with pytest.raises(gwconfig.ConfigError) as ei:
        gwconfig.load(dict(FUYAO, **patch))
    assert needle in str(ei.value)


# -- 客户端细节 --------------------------------------------------------------------------------------

def test_client_errors_and_parsing():
    fake = FakeOpenSandbox(capacity={"g": 0})
    try:
        c = osb.Client(fake.url, api_key="k1", timeout_s=5)
        with pytest.raises(osb.CapacityError):
            c.create("tpl", "g", {}, "1", "1Gi", 600)
        assert fake.requests[-1][3].get("OPEN-SANDBOX-API-KEY") == "k1", "平台开认证后带 API Key"
        assert c.get("nope") is None and c.delete("nope") is False
        bid = fake.add({"a": "1", "b": "2"})
        fake.add({"a": "1", "b": "3"})
        assert [s.id for s in c.list({"a": "1", "b": "2"})] == [bid]
        assert c.address(bid) == fake.ip(bid)
        c.patch_metadata(bid, {"b": None, "c": "9"})
        assert fake.boxes[bid]["metadata"] == {"a": "1", "c": "9"}
    finally:
        fake.close()
    with pytest.raises(osb.OpenSandboxError):
        osb.Client(fake.url, timeout_s=1).get("x")
    with pytest.raises(ValueError):
        osb.Client("opensandbox:80")


def test_a_5xx_from_create_is_treated_as_the_platform_being_full_not_an_error(env):
    """鲲鹏 2026-11-18:沙箱组满时 OpenSandbox 把调度器的 503「no available agent」包成 500。
    网关按「平台暂时没有空位」让用户等待重试(等待页),平台原话记日志;不回「联系管理员」。"""
    fake, _envd, router, _clock, logs, _p = env
    fake.create_error = (500, {"detail": {"message": "RuntimeError: flux create_sandbox failed: 503 no available agent"}})
    with pytest.raises(poolrouter.PoolWait) as ei:
        router.target("u1001")
    assert ei.value.reason == "platform_busy" and ei.value.retry_after > 0
    assert any("平台回 500" in m and "no available agent" in m for m in logs)
    fake.create_error = (502, {"detail": "bad gateway"})
    with pytest.raises(poolrouter.PoolWait):
        router.target("u1002")


def test_a_4xx_from_create_is_still_an_error(env):
    fake, _envd, router, *_ = env
    fake.create_error = (400, {"detail": {"message": "template not found"}})
    with pytest.raises(osb.OpenSandboxError) as ei:
        router.target("u1001")
    assert ei.value.status == 400 and not isinstance(ei.value, osb.CapacityError)


def test_paused_sandboxes_do_not_take_platform_capacity(env):
    """实测:每个承载 Pod 2 台、1 个承载 Pod;暂停一台后,新沙箱马上建在同一个承载 Pod 上。"""
    fake, _envd, router, *_ = env
    for i in range(2):
        fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "x%d" % i, "gsdb-kind": "runtime"}, group="g-run",
                 state="Paused")
    fake.add({"gsdb-app": "gaussdb-agent", "gsdb-user": "x9", "gsdb-kind": "runtime"}, group="g-run")
    assert router.target("u1001").pod in fake.boxes, "容量 3,运行 1 + 暂停 2:还能建"
