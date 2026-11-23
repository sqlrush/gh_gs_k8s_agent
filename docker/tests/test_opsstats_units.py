"""状态服务的运维统计(docker/opsstats.py,资源大盘,2026-11-18)。

要点:
- 统计接口只认网关的运维签名:沙箱令牌(哪怕是真的)调不通;签名绑定方法、路径、请求体与时刻,窗口内不能重放;
- 运维签名也打不开沙箱的接口(两把子密钥互不通用);
- 请求计数按「今天」累加、过零点清零;24 小时趋势半小时一点;一批里有一项不合法整批拒绝;
- 清扫器报的台数按时间点取最后一次(两个网关副本报的是同一份全量,不相加);
- 落盘后重启能恢复;昨天的计数不恢复;24 小时以外的点丢掉;
- NAS 用量不跟随链接,不存在的目录如实标出。
"""
import http.client
import json
import os
import pathlib
import sys
import threading
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import opsstats  # noqa: E402
import sbxtoken  # noqa: E402
import statesvc  # noqa: E402

KEY = b"k" * 32


@pytest.fixture()
def svc(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    s = statesvc.Service(str(nas), KEY, log=lambda m: None)
    srv = statesvc.ThreadingHTTPServer(("127.0.0.1", 0), statesvc.handler(s))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], nas, s
    srv.shutdown()
    srv.server_close()


def _call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, (json.loads(raw) if raw else {})


def _signed(port, method, path, body=None, key=KEY, now=None):
    data = json.dumps(body).encode() if body is not None else b""
    sig = sbxtoken.ops_sign(key, method, path, data, now=now)
    return _call(port, method, path, body, {sbxtoken.OPS_HEADER: sig})


# -- 鉴权 -------------------------------------------------------------------------------------------

def test_a_real_sandbox_token_cannot_read_or_write_the_ops_stats(svc):
    port, _nas, _s = svc
    tok = sbxtoken.issue(KEY, "sbx-1", "u1001", "runtime")
    for method, path, body in (("GET", "/v1/ops/stats", None), ("POST", "/v1/ops/requests", {"items": []}),
                               ("POST", "/v1/ops/fleet", {"running": 1, "paused": 0})):
        assert _call(port, method, path, body, {"Authorization": "Bearer " + tok})[0] == 401, "沙箱令牌在 Authorization 里"
        assert _call(port, method, path, body, {sbxtoken.OPS_HEADER: tok})[0] == 401, "沙箱令牌塞进运维签名头"
    for kind in sbxtoken.KINDS:
        tok = sbxtoken.issue(KEY, "sbx-2", "u1002", kind)
        assert _call(port, "GET", "/v1/ops/stats", None, {"Authorization": "Bearer " + tok})[0] == 401


def test_gateway_signature_works_and_is_bound_to_path_body_time_and_used_once(svc):
    port, _nas, _s = svc
    assert _signed(port, "GET", "/v1/ops/stats")[0] == 200
    assert _signed(port, "GET", "/v1/ops/stats")[0] == 200, "同一秒再签一次也是新签名(带随机数),两个网关副本不互相挡"
    assert _signed(port, "GET", "/v1/ops/stats", key=b"x" * 32)[0] == 401, "别的密钥签的不认"
    assert _signed(port, "GET", "/v1/ops/stats", now=time.time() - 600)[0] == 401, "过期的不认"
    body = {"running": 3, "paused": 1}
    sig = sbxtoken.ops_sign(KEY, "POST", "/v1/ops/fleet", json.dumps(body).encode())
    assert _call(port, "POST", "/v1/ops/requests", body, {sbxtoken.OPS_HEADER: sig})[0] == 401, "换接口"
    assert _call(port, "POST", "/v1/ops/fleet", {"running": 99, "paused": 1}, {sbxtoken.OPS_HEADER: sig})[0] == 401, "改请求体"
    assert _call(port, "POST", "/v1/ops/fleet", body, {sbxtoken.OPS_HEADER: sig})[0] == 200
    assert _call(port, "POST", "/v1/ops/fleet", body, {sbxtoken.OPS_HEADER: sig})[0] == 401, "同一个签名只认一次"


def test_the_gateway_signature_does_not_open_sandbox_endpoints(svc):
    port, _nas, _s = svc
    for method, path in (("GET", "/v1/state"), ("GET", "/v1/kb/manifest"), ("GET", "/v1/where")):
        assert _signed(port, method, path)[0] == 401
    st, _ = _signed(port, "POST", "/v1/lease", {"action": "acquire"})
    assert st == 401


def test_ops_requests_do_not_count_as_a_sandbox_seen_at_an_address(svc):
    port, _nas, s = svc
    _signed(port, "POST", "/v1/ops/fleet", {"running": 1, "paused": 0})
    assert s._where == {}


# -- 计数与趋势 -------------------------------------------------------------------------------------

def test_requests_and_fleet_reported_by_the_gateway_show_up_in_stats(svc):
    port, _nas, _s = svc
    assert _signed(port, "POST", "/v1/ops/requests", {"items": [{"u": "u1001", "k": "runtime", "n": 3},
                                                                 {"u": "u2001", "k": "kb-import", "n": 1}]})[0] == 200
    assert _signed(port, "POST", "/v1/ops/requests", {"items": [{"u": "u1001", "k": "runtime", "n": 2}]})[0] == 200
    assert _signed(port, "POST", "/v1/ops/fleet", {"running": 4, "paused": 2})[0] == 200
    st, got = _signed(port, "GET", "/v1/ops/stats")
    assert st == 200
    assert got["requests"]["total"] == 6 and got["requests"]["users"] == 2
    assert got["requests"]["by_user"][0] == {"user_id": "u1001", "kind": "runtime", "count": 5}
    assert len(got["trend"]) == opsstats.TREND_N and got["bucket_seconds"] == 1800
    last = got["trend"][-1]
    assert last["requests"] == 6 and last["running"] == 4 and last["paused"] == 2
    assert got["trend"][0]["running"] is None, "没报过的点是 None,不是 0(0 台和没数据不是一回事)"


def test_a_batch_with_one_bad_item_is_rejected_as_a_whole(svc):
    port, _nas, _s = svc
    bad = [{"u": "u1001", "k": "runtime", "n": 3}, {"u": "../x", "k": "runtime", "n": 1}]
    assert _signed(port, "POST", "/v1/ops/requests", {"items": bad})[0] == 400
    for item in ({"u": "u1001", "k": "other", "n": 1}, {"u": "u1001", "k": "runtime", "n": -1},
                 {"u": "u1001", "k": "runtime", "n": True}, {"u": "u1001", "k": "runtime", "n": "3"}):
        assert _signed(port, "POST", "/v1/ops/requests", {"items": [item]})[0] == 400, item
    assert _signed(port, "GET", "/v1/ops/stats")[1]["requests"]["total"] == 0, "不写半批"
    assert _signed(port, "POST", "/v1/ops/fleet", {"running": -1, "paused": 0})[0] == 400


def _stats(tmp_path, clock):
    nas = tmp_path / "nas"
    nas.mkdir(exist_ok=True)
    st = opsstats.OpsStats(os.open(str(nas), os.O_RDONLY | os.O_DIRECTORY), log=lambda m: None, clock=lambda: clock["t"])
    return st, nas, None


def test_counts_reset_after_midnight_and_the_trend_keeps_24_hours(tmp_path):
    t0 = time.mktime((2026, 11, 18, 23, 40, 0, 0, 0, -1))
    clock = {"t": t0}
    st, _nas, _ = _stats(tmp_path, clock)
    st.add_requests([{"u": "u1001", "k": "runtime", "n": 7}])
    st.set_fleet(5, 1)
    assert st.snapshot()["requests"]["total"] == 7 and st.snapshot()["date"] == "2026-11-18"
    clock["t"] = t0 + 40 * 60                          # 过零点
    snap = st.snapshot()
    assert snap["date"] == "2026-11-19" and snap["requests"]["total"] == 0, "过零点清零今天的计数"
    assert any(p["requests"] == 7 for p in snap["trend"]), "趋势跨天不清"
    clock["t"] = t0 + 25 * 3600
    assert all(p["requests"] == 0 and p["running"] is None for p in st.snapshot()["trend"]), "24 小时以外的点丢掉"


def test_fleet_counts_take_the_last_report_in_a_bucket_not_the_sum(tmp_path):
    clock = {"t": time.mktime((2026, 11, 18, 10, 0, 0, 0, 0, -1))}
    st, _nas, _ = _stats(tmp_path, clock)
    st.set_fleet(4, 2)
    clock["t"] += 10
    st.set_fleet(4, 2)                                 # 另一个网关副本报的同一份全量
    last = st.snapshot()["trend"][-1]
    assert (last["running"], last["paused"]) == (4, 2)


def test_saved_stats_survive_a_restart_but_yesterdays_counts_do_not(tmp_path):
    clock = {"t": time.mktime((2026, 11, 18, 10, 5, 0, 0, 0, -1))}
    st, nas, _ = _stats(tmp_path, clock)
    st.add_requests([{"u": "u1001", "k": "runtime", "n": 4}])
    st.set_fleet(2, 1)
    assert st.save() is True and st.save() is False, "没变化不重写"
    assert (nas / ".ops" / "usage.json").is_file()
    again, _nas, _ = _stats(tmp_path, clock)
    snap = again.snapshot()
    assert snap["requests"]["by_user"] == [{"user_id": "u1001", "kind": "runtime", "count": 4}]
    assert snap["trend"][-1]["running"] == 2
    clock["t"] += 24 * 3600                            # 第二天才重启
    tomorrow, _nas, _ = _stats(tmp_path, clock)
    assert tomorrow.snapshot()["requests"]["total"] == 0


def test_a_corrupt_state_file_starts_from_zero_instead_of_failing(tmp_path):
    (tmp_path / "nas" / ".ops").mkdir(parents=True)
    (tmp_path / "nas" / ".ops" / "usage.json").write_text("{not json")
    logs = []
    nas = tmp_path / "nas"
    st = opsstats.OpsStats(os.open(str(nas), os.O_RDONLY | os.O_DIRECTORY), log=logs.append)
    assert st.snapshot()["requests"]["total"] == 0 and any("从零开始" in m for m in logs)


# -- NAS 用量 ---------------------------------------------------------------------------------------

def test_nas_usage_counts_entries_files_and_bytes_without_following_links(tmp_path):
    nas = tmp_path / "nas"
    (nas / "users" / "u1001" / "sessions").mkdir(parents=True)
    (nas / "users" / "u1002").mkdir(parents=True)
    (nas / "users" / "u1001" / "sessions" / "a.db").write_bytes(b"x" * 1000)
    (nas / "users" / "u1002" / "b.txt").write_bytes(b"y" * 24)
    (nas / "kb").mkdir()
    (nas / "kb" / "c.md").write_bytes(b"z" * 10)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"0" * 100000)
    os.symlink(str(outside), str(nas / "users" / "u1002" / "trap"))
    os.symlink(str(outside / "big.bin"), str(nas / "kb" / "trap.bin"))
    st = opsstats.OpsStats(os.open(str(nas), os.O_RDONLY | os.O_DIRECTORY), log=lambda m: None)
    got = st.refresh_nas()["dirs"]
    assert got["users"] == {"exists": True, "entries": 2, "files": 2, "bytes": 1024}
    assert got["kb"]["files"] == 1 and got["kb"]["bytes"] == 10, "链接不跟随"
    assert got["admins"]["exists"] is False
    assert st.snapshot()["nas"]["dirs"]["users"]["bytes"] == 1024


def test_the_stats_endpoint_reports_nas_usage_once_it_is_computed(svc):
    port, nas, s = svc
    (nas / "admins" / "u2001").mkdir(parents=True)
    assert _signed(port, "GET", "/v1/ops/stats")[1]["nas"] is None, "还没算过就如实给 None"
    s.ops.refresh_nas()
    got = _signed(port, "GET", "/v1/ops/stats")[1]["nas"]
    assert got["dirs"]["admins"]["entries"] == 1 and "took_s" in got
