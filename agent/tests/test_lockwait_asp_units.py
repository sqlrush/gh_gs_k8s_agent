"""时段锁回溯(lockwait --from/--to,aspchain.py)。

样本取自 og5 10-21 18:32–18:52(faultlab 第一轮)的真实 gs_asp 聚合结果,只保留链上的几对。
"""
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-lockwait" / "scripts"
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_SCRIPTS))

import pytest  # noqa: E402

import aspchain  # noqa: E402
import lockwait  # noqa: E402
from common.finding import Severity  # noqa: E402

BEGIN, END = "2026-10-21 18:32:00", "2026-10-21 18:52:00"

STATUS = [{"item": "asp_flush_rate", "value": "10"}, {"item": "asp_retention_days", "value": "2"},
          {"item": "asp_sample_interval", "value": "1"}, {"item": "enable_asp", "value": "on"},
          {"item": "samples_in_window", "value": "4680"}, {"item": "now", "value": "2026-10-22 08:48:59"}]


def _pair(w, h, ev, mode, obj, samples, first, last="2026-10-21 18:51:53", sql="3167133288",
          q="UPDATE fl.acct SET bal = bal + ? WHERE id = ?"):
    return {"waiter_sessionid": str(w), "holder_sessionid": str(h), "wait_event": ev, "waiter_mode": mode,
            "locktag": "", "lock_decoded": "", "lock_object": obj, "waiter_sql_id": sql, "waiter_query": q,
            "samples": str(samples), "first_seen": first, "last_seen": last}


T0 = "2026-10-21 18:32:34"
PAIRS = [
    _pair(5142, 5141, "transactionid", "ShareLock", "", 115, T0),
    _pair(5143, 5142, "transactionid", "ShareLock", "", 115, T0),
    _pair(5144, 5145, "tuple", "ExclusiveLock", "fl.acct", 115, T0),
    _pair(5145, 5143, "transactionid", "ShareLock", "", 115, T0),
    _pair(5146, 5144, "tuple", "ExclusiveLock", "fl.acct", 115, T0),
    _pair(5167, 5142, "tuple", "AccessExclusiveLock", "fl.acct", 105, "2026-10-21 18:34:15",
          sql="4168886180", q="SELECT id FROM fl.acct WHERE id BETWEEN ? AND ? ORDER BY id FOR UPDATE"),
    # 与上面不重叠的一对:时间上不相交,不该连进 5142 的链
    _pair(5141, 5300, "advisory", "ExclusiveLock", "", 2, "2026-10-21 18:10:00", last="2026-10-21 18:10:10"),
]
SESSIONS = [
    {"sessionid": "5141", "state": "idle in transaction", "last_event": "wait cmd", "sql_id": "",
     "query": "", "application_name": "fl:K1/root", "client_addr": "192.0.2.10",
     "xact_start": "2026-10-21 18:32:26", "samples": "115"},
    {"sessionid": "5142", "state": "active", "last_event": "transactionid", "sql_id": "3167133288",
     "query": "UPDATE fl.acct SET bal = bal + ? WHERE id = ?", "application_name": "fl:K1/mid1",
     "client_addr": "192.0.2.10", "xact_start": "2026-10-21 18:32:27", "samples": "115"},
]


class FakeRunner:
    def __init__(self, status=STATUS, pairs=PAIRS, sessions=SESSIONS):
        self.data = {aspchain.STATUS_SCRIPT: status, aspchain.PAIRS_SCRIPT: pairs,
                     aspchain.SESSIONS_SCRIPT: sessions}
        self.calls = []

    def run(self, script, values):
        self.calls.append((script, dict(values)))
        return self.data[script]


def test_chain_is_rebuilt_from_waiter_to_root():
    rep = aspchain.collect(FakeRunner(), BEGIN, END)
    assert rep.chains[0].path == (5146, 5144, 5145, 5143, 5142, 5141)
    assert rep.chains[0].lock_object == "fl.acct"
    assert rep.roots[0] == 5141
    # 5167 → 5142 → 5141 是另一条(5142 那段被长链包含,不单列)
    assert (5167, 5142, 5141) in [c.path for c in rep.chains]


def test_non_overlapping_pair_is_not_chained():
    rep = aspchain.collect(FakeRunner(), BEGIN, END)
    assert all(5300 not in c.path for c in rep.chains)


def test_wait_is_samples_times_flush_step():
    rep = aspchain.collect(FakeRunner(), BEGIN, END)
    assert rep.status.step_s == 10.0
    assert rep.chains[0].wait_s == 1150.0
    # 链的时长按链头算:5167 采到 105 次,不是链上最长那一对的 115 次
    short = next(c for c in rep.chains if c.path[0] == 5167)
    assert short.wait_s == 1050.0 and short.first == "2026-10-21 18:34:15"


def test_unsampled_root_says_why():
    """ASP 不采纯空闲会话:拿着会话级 advisory 锁的根一次也采不到,要说明,不能只写「未采到」。"""
    pairs = [_pair(5190, 5185, "advisory", "ExclusiveLock", "", 93, "2026-10-21 18:36:16",
                   sql="648622204", q="SELECT pg_advisory_lock(?)")]
    rep = aspchain.collect(FakeRunner(pairs=pairs, sessions=[]), BEGIN, END)
    md = aspchain.render_markdown(rep, aspchain.judge(rep, "Locks", 5, 60), "og")
    assert "采样不记空闲会话" in md


def test_judge_long_wait_and_idle_root():
    rep = aspchain.collect(FakeRunner(), BEGIN, END)
    fs = aspchain.judge(rep, lockwait.DIM, lockwait.WAIT_WARN_S, lockwait.WAIT_CRIT_S)
    codes = {f.code: f for f in fs}
    assert codes["LOCK_WAIT_LONG"].severity == Severity.CRITICAL
    assert codes["LOCK_WAIT_LONG"].value == "约 1150s"
    assert codes["LOCK_ROOT_IDLE_XACT"].value == "会话 5141"
    assert "挡住 6 个会话" in codes["LOCK_ROOT_IDLE_XACT"].evidence
    assert "fl:K1/root" in codes["LOCK_ROOT_IDLE_XACT"].evidence
    # 2026-10-22 实测:模型对过去时段的根会话建议 pg_terminate_backend —— 证据里要写明不据此 kill
    assert "不要据此 kill" in codes["LOCK_ROOT_IDLE_XACT"].evidence


def test_asp_off_is_not_reported_as_no_waits():
    st = [dict(r) for r in STATUS]
    st[3] = {"item": "enable_asp", "value": "off"}
    r = FakeRunner(status=st)
    rep = aspchain.collect(r, BEGIN, END)
    assert "enable_asp=off" in rep.unavailable
    assert [c[0] for c in r.calls] == [aspchain.STATUS_SCRIPT]
    assert aspchain.judge(rep, "Locks", 5, 60) == []


def test_expired_window_says_retention():
    st = [dict(r) for r in STATUS]
    st[4] = {"item": "samples_in_window", "value": "0"}
    st[5] = {"item": "now", "value": "2026-10-25 08:00:00"}
    rep = aspchain.collect(FakeRunner(status=st), BEGIN, END)
    assert "保留期" in rep.unavailable


def test_no_waits_in_samples_is_explicit():
    rep = aspchain.collect(FakeRunner(pairs=[]), BEGIN, END)
    md = aspchain.render_markdown(rep, [], "og")
    assert "没有采到锁等待" in md and "4680" in md


def test_markdown_has_chain_roots_and_no_kill(monkeypatch):
    rep = aspchain.collect(FakeRunner(), BEGIN, END)
    fs = aspchain.judge(rep, "Locks", 5, 60)
    md = aspchain.render_markdown(rep, fs, "og")
    assert "5146 → 5144 → 5145 → 5143 → 5142 → **5141（根，idle in transaction）**" in md
    assert "pg_terminate_session(" not in md and "pg_cancel_session(" not in md
    assert "估算" in md


def test_cli_span_json(monkeypatch, capsys):
    monkeypatch.setattr(lockwait.access, "for_conn", lambda *a, **k: FakeRunner())
    monkeypatch.setattr(lockwait.cli, "apply_session_arg", lambda args: None)
    rc = lockwait.main(["--from", "2026-10-21 18:32", "--to", "2026-10-21 18:52", "--format", "json"])
    assert rc == 0
    d = json.loads(capsys.readouterr().out)
    assert d["skill"] == "gaussdb-lockwait"
    assert d["asp"]["chains"][0]["path"] == [5146, 5144, 5145, 5143, 5142, 5141]
    assert {f["code"] for f in d["findings"]} >= {"LOCK_WAIT_LONG", "LOCK_ROOT_IDLE_XACT"}


def test_cli_span_passes_seconds_to_whitelist(monkeypatch, capsys):
    r = FakeRunner()
    monkeypatch.setattr(lockwait.access, "for_conn", lambda *a, **k: r)
    monkeypatch.setattr(lockwait.cli, "apply_session_arg", lambda args: None)
    lockwait.main(["--from", "2026-10-21 18:32", "--to", "2026-10-21 18:52", "--format", "json"])
    assert r.calls[0][1] == {"begin_time": BEGIN, "end_time": END}


@pytest.mark.parametrize("bad", ["10-21 18:32", "2026/10/21 18:32", "昨晚"])
def test_cli_span_rejects_bad_time(monkeypatch, capsys, bad):
    monkeypatch.setattr(lockwait.access, "for_conn", lambda *a, **k: FakeRunner())
    monkeypatch.setattr(lockwait.cli, "apply_session_arg", lambda args: None)
    assert lockwait.main(["--from", bad]) == 2
    assert "格式不对" in capsys.readouterr().err


def test_pairs_are_fetched_in_full_not_by_display_limit(monkeypatch, capsys):
    """只取前 20 对会把链从中间截断,断点被当成根(2026-10-22 第二轮实测)。"""
    r = FakeRunner()
    monkeypatch.setattr(lockwait.access, "for_conn", lambda *a, **k: r)
    monkeypatch.setattr(lockwait.cli, "apply_session_arg", lambda args: None)
    lockwait.main(["--from", "2026-10-21 18:32", "--to", "2026-10-21 18:52", "--limit", "2"])
    pairs_call = next(v for s, v in r.calls if s == aspchain.PAIRS_SCRIPT)
    assert pairs_call["limit"] == aspchain.FETCH_LIMIT
    out = capsys.readouterr().out
    assert "共 7 对，按采到次数列了前 2 对" in out
    assert "5146 → 5144 → 5145 → 5143 → 5142 → **5141" in out, "展示截断不影响连链"


# ---------------------------------------------------------------- 内存里的样本(dbe_perf.local_active_session)
#
# 2026-10-24 faultlab 第二轮:时段 09:46–10:06 在 10:07 回看,gs_asp 一个样本都没有,而
# dbe_perf.local_active_session 里有 5.3 万条覆盖整个窗口的样本 —— 脚本却报「回看不了 …
# 采样当时没开」(实际 enable_asp=on)。原因:ASP 先把样本放在内存里(最多 asp_sample_num 个),
# **内存写满才**把其中每 asp_flush_rate 个落 1 个进 gs_asp;og5 上那一段直到 11:25 才落表。

def _status(table=0, memory=0, memory_first="2026-10-24 09:40:00", memory_last="2026-10-24 10:07:20",
            table_last="2026-10-24 09:30:00", now="2026-10-24 10:07:29", flush_mode="table"):
    rows = [{"item": "asp_flush_rate", "value": "10"}, {"item": "asp_retention_days", "value": "2"},
            {"item": "asp_sample_interval", "value": "1"}, {"item": "enable_asp", "value": "on"},
            {"item": "asp_flush_mode", "value": flush_mode}, {"item": "asp_sample_num", "value": "100000"},
            {"item": "samples_in_window", "value": str(table)},
            {"item": "memory_samples_in_window", "value": str(memory)},
            {"item": "memory_first", "value": memory_first}, {"item": "memory_last", "value": memory_last},
            {"item": "table_last", "value": table_last}, {"item": "now", "value": now}]
    return rows


W_BEGIN, W_END = "2026-10-24 09:46:00", "2026-10-24 10:06:00"


def test_window_only_in_memory_is_read_from_memory_samples():
    rep = aspchain.collect(FakeRunner(status=_status(table=0, memory=4618)), W_BEGIN, W_END)
    assert rep.unavailable == ""
    assert rep.status.samples == 4618 and rep.status.memory_samples == 4618
    assert rep.chains and rep.chains[0].path[-1] == 5141
    note = aspchain.basis_note(rep)
    assert "local_active_session" in note and "4618" in note
    assert "09:40:00" in note and "10:07:20" in note          # 说清内存样本覆盖到哪


def test_samples_from_both_sources_are_counted():
    rep = aspchain.collect(FakeRunner(status=_status(table=300, memory=200)), W_BEGIN, W_END)
    assert rep.status.samples == 500
    note = aspchain.basis_note(rep)
    assert "gs_asp 300" in note and "local_active_session 200" in note


def test_window_between_flushed_and_memory_samples_states_coverage_not_a_guess():
    """两处都没有:说清两处各覆盖到哪,不猜「采样没开」(enable_asp 明明是 on)。"""
    st = _status(memory_first="2026-10-24 11:25:41", memory_last="2026-10-24 14:00:00",
                 table_last="2026-10-24 09:30:00", now="2026-10-24 14:00:05")
    rep = aspchain.collect(FakeRunner(status=st), W_BEGIN, W_END)
    assert rep.unavailable
    assert "没开" not in rep.unavailable
    assert "09:30:00" in rep.unavailable and "11:25:41" in rep.unavailable


def test_window_covered_by_memory_without_samples_is_no_activity_not_unavailable():
    """内存采样完整覆盖这段时间、一个样本也没有 = 当时没有活跃会话,也就没有锁等待 —— 不是「回看不了」。"""
    r = FakeRunner(status=_status(table=0, memory=0))
    rep = aspchain.collect(r, W_BEGIN, W_END)
    assert rep.unavailable == "" and rep.pairs == ()
    md = aspchain.render_markdown(rep, [], "og")
    assert "一个活跃会话都没采到" in md
    assert [c[0] for c in r.calls] == [aspchain.STATUS_SCRIPT]


def test_partial_coverage_says_which_part_is_missing():
    st = _status(table=0, memory=900, memory_first="2026-10-24 09:55:00", table_last="2026-10-24 09:30:00")
    rep = aspchain.collect(FakeRunner(status=st), W_BEGIN, W_END)
    assert rep.unavailable == ""
    note = aspchain.basis_note(rep)
    assert "09:46:00" in note and "09:55:00" in note and "看不到" in note


def test_flush_to_file_is_named_from_the_parameter():
    st = _status(flush_mode="file", memory_first="2026-10-24 11:25:41", now="2026-10-24 14:00:05")
    rep = aspchain.collect(FakeRunner(status=st), W_BEGIN, W_END)
    assert "asp_flush_mode=file" in rep.unavailable


def test_old_whitelist_status_without_memory_rows_says_so():
    """客户没重导白名单:旧版 asp_status 不返回内存样本那几行 —— 说清是脚本旧了,不说「没开」。"""
    st = [r for r in _status(table=0, memory=0) if r["item"] not in ("memory_samples_in_window", "memory_first", "memory_last",
                                             "table_last", "asp_flush_mode", "asp_sample_num")]
    rep = aspchain.collect(FakeRunner(status=st), W_BEGIN, W_END)
    assert "lockwait.asp_status" in rep.unavailable and "没开" not in rep.unavailable


def test_json_carries_the_sources():
    rep = aspchain.collect(FakeRunner(status=_status(table=300, memory=200)), W_BEGIN, W_END)
    d = aspchain.to_dict(rep)
    assert d["sources"] == {"table": 300, "memory": 200, "memory_first": "2026-10-24 09:40:00",
                            "memory_last": "2026-10-24 10:07:20", "table_last": "2026-10-24 09:30:00"}


@pytest.mark.parametrize("name", ["lockwait.asp_pairs", "lockwait.asp_sessions", "lockwait.asp_status"])
def test_registry_reads_memory_samples_with_the_same_step(name):
    """三条脚本都要同时读落表的 gs_asp 与内存里的 local_active_session;内存那一侧只取
    need_flush_sample 的样本 —— 与将来落表的是同一批,步长不变,同一时段今天查和落表后查结果一样。"""
    from common.grmp.script import load_script
    sql = load_script(_ROOT / "scripts" / "registry" / "lockwait" / (name.split(".")[1] + ".yaml")).script_content
    assert "gs_asp" in sql and "dbe_perf.local_active_session" in sql
    assert "need_flush_sample" in sql
