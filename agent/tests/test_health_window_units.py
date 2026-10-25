"""时段体检(gaussdb-health --from/--to/--window,window.py)。

数字取自 og5 两段真实窗口:
  · faultlab 故障窗口 2981(10:46:29)→ 2986(11:06:55):DB time 23510.33 秒、死锁 24、溢出 7.8G、
    LOCK_EVENT 15369.74 秒 —— 与同一窗口的 WDR 逐项一致;
  · 2026-10-18 那轮测试里的空闲小时窗口:每小时 DB time 0.2 秒,LWLOCK 等待却比 DB time 还长
    (等待统计含后台线程)。当时健康检查把「LWLOCK 占 DB time 171.8%」判成 🟠 总体告警 —— 是误报。
钉住:等待只算占总等待、不除以 DB time;空闲窗口不判比例;空闲等待(STATUS)剔除;
过去的窗口里瞬时维度明列「回看不了」,不留空白;时段报告不顶掉大盘上的此刻巡检。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"

for _m in ("model", "thresholds", "util", "collectors", "report", "health", "aggregate", "render",
           "window", "counters"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import aggregate  # noqa: E402
import health  # noqa: E402
import window as W  # noqa: E402
from common import access, timewin  # noqa: E402
from model import DimResult, HealthEvidence, Severity  # noqa: E402


def t(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def kv(**c):
    return [{"item": k, "value": str(v)} for k, v in c.items()]


def waits(*rows):
    return [{"wait_class": c, "event": e, "waits": str(n), "wait_us": str(us)} for c, e, n, us in rows]


BASE = dict(db_time_us=9_000_000_000, cpu_time_us=2_000_000_000, xact_commit=5_000_000, xact_rollback=100,
            deadlocks=3, temp_bytes=1 << 30, blks_read=10_000_000, blks_hit=900_000_000, ckpt_timed=500, ckpt_req=20)


def plus(base, **d):
    return {k: base[k] + d.get(k, 0) for k in base}


# 故障窗口:两端之差就是 WDR 报的那组数
FAULT_END = plus(BASE, db_time_us=23_510_330_000, cpu_time_us=3_501_100_000, xact_commit=1_102_964,
                 xact_rollback=69, deadlocks=24, temp_bytes=8_375_186_227, blks_read=1_895_003,
                 blks_hit=1_270_000_000, ckpt_timed=18, ckpt_req=3)
FAULT_W0 = waits(("LOCK_EVENT", "transactionid", 10, 1_000_000), ("STATUS", "wait cmd", 5, 9_000_000))
FAULT_W1 = waits(("LOCK_EVENT", "transactionid", 8_010, 5_607_700_000), ("LOCK_EVENT", "relation", 6_000, 4_493_200_000),
                 ("LOCK_EVENT", "tuple", 6_171, 5_269_840_000), ("IO_EVENT", "BufHashTableSearch", 1_381_148_036, 1_874_280_000),
                 ("LWLOCK_EVENT", "WALFlushWait", 785_155, 623_140_000),
                 ("STATUS", "wait cmd", 9_999, 33_181_920_000), ("NONE", "none", 1, 5_000_000))

# 空闲小时窗口:DB time 0.2 秒,LWLOCK 等待 0.34 秒 —— 比 DB time 还长
IDLE_END = plus(BASE, db_time_us=200_000, cpu_time_us=80_000, xact_commit=900, blks_hit=5_000, blks_read=40)
IDLE_W0 = waits(("LWLOCK_EVENT", "WALWriteLock", 10, 1_000_000))
IDLE_W1 = waits(("LWLOCK_EVENT", "WALWriteLock", 30, 1_340_000), ("IO_EVENT", "DataFileWrite", 5, 1_010_000))

SNAPS = {2981: "2026-10-17 10:46:29", 2986: "2026-10-17 11:06:55",
         3019: "2026-10-18 19:14:03", 3020: "2026-10-18 20:14:04"}


class FakeRunner:
    def __init__(self, counters, waits_, live=None, live_waits=None, now="2026-10-18 20:24:31", snaps=SNAPS,
                 top_sql=None):
        self.counters, self.waits, self.now, self.snaps = counters, waits_, now, snaps
        self.top_sql = top_sql
        self.live, self.live_waits = live or BASE, live_waits or []
        self.calls = []

    def run(self, script, params):
        self.calls.append((script, dict(params)))
        if script == "health.live_counters" and self.now:
            return kv(now=self.now, postmaster_start="2026-10-17 21:13:48",
                      stats_reset="2026-10-17 21:13:50", **self.live)
        if script == "health.live_waits":
            return self.live_waits
        if script == "wdr.snapshots" and self.snaps:
            return [{"snapshot_id": str(i), "start_ts": s, "end_ts": s} for i, s in self.snaps.items()]
        if script == "health.snap_counters" and params.get("snap") in self.counters:
            return kv(**self.counters[params["snap"]])
        if script == "health.snap_waits" and params.get("snap") in self.waits:
            return self.waits[params["snap"]]
        if script == "wdr.top_sql" and self.top_sql is not None:
            return self.top_sql
        raise access.QueryError("script %s not prepared" % script)


def cov_of(a, b, ta, tb):
    return timewin.Coverage(want_from=t(ta), want_to=t(tb), start=timewin.Point("snapshot", t(ta), a),
                            end=timewin.Point("snapshot", t(tb), b), complete=True, note="(说明)")


FAULT_COV = cov_of(2981, 2986, SNAPS[2981], SNAPS[2986])
IDLE_COV = cov_of(3019, 3020, SNAPS[3019], SNAPS[3020])


def collect(cov, runner):
    return W.finish(W.collect(runner, cov))


def dim(ev, name):
    return next(d for d in ev.dims if d.dimension == name)


# ---------------------------------------------------------------- 判定

def test_the_fault_window_says_what_wdr_says():
    ev = collect(FAULT_COV, FakeRunner({2981: BASE, 2986: FAULT_END}, {2981: FAULT_W0, 2986: FAULT_W1}))
    got = {f.code: f.severity for f in ev.findings}
    assert got == {"WIN_DEADLOCK": Severity.WARN, "WIN_TEMP_SPILL": Severity.WARN,
                   "WIN_WAIT_HEAVY": Severity.NOTICE, "WIN_WAIT_SKEW": Severity.NOTICE}
    assert ev.overall == Severity.WARN
    load = dim(ev, W.DIM_WIN_LOAD)
    assert ["DB time", "23510.33 秒"] in load.rows and ["平均活跃会话", "19.176"] in load.rows
    assert ["死锁", "24"] in dim(ev, W.DIM_WIN_TXN).rows
    waits_ = dim(ev, W.DIM_WIN_WAITS)
    assert waits_.rows[0][0] == "LOCK_EVENT" and waits_.rows[0][2] == "15369.74" and waits_.rows[0][3] == "86.0%"


def test_idle_waits_are_dropped_even_if_the_script_did_not():
    """og5 故障窗口里 STATUS「wait cmd」占全部等待 65%,不剔除就盖过真正的锁等待。
    脚本里已剔除;老版本白名单送来的行在这里再剔一次。"""
    ev = collect(FAULT_COV, FakeRunner({2981: BASE, 2986: FAULT_END}, {2981: FAULT_W0, 2986: FAULT_W1}))
    classes = [r[0] for r in dim(ev, W.DIM_WIN_WAITS).rows]
    assert "STATUS" not in classes and "NONE" not in classes


def test_an_idle_window_judges_no_ratios():
    """2026-10-18 的误报:空闲库上 0.03 秒的后台锁等待「占」一大半,被判成总体 🟠。"""
    ev = collect(IDLE_COV, FakeRunner({3019: BASE, 3020: IDLE_END}, {3019: IDLE_W0, 3020: IDLE_W1}))
    assert ev.findings == [] and ev.overall == Severity.OK
    assert "窗口几乎空闲" in dim(ev, W.DIM_WIN_LOAD).headline


def test_waits_are_shares_of_all_waits_never_of_db_time():
    """空闲窗口里等待(0.34+0.01 秒)比 DB time(0.2 秒)还长 —— 除以 DB time 就会超过 100%。"""
    ev = collect(IDLE_COV, FakeRunner({3019: BASE, 3020: IDLE_END}, {3019: IDLE_W0, 3020: IDLE_W1}))
    w = dim(ev, W.DIM_WIN_WAITS)
    shares = [float(r[3].rstrip("%")) for r in w.rows]
    assert sum(shares) == pytest.approx(100.0, abs=0.2) and max(shares) <= 100
    assert "不是占 DB time" in w.headline


def test_deadlocks_are_judged_even_in_an_idle_window():
    """死锁次数是硬数据,不是比例 —— 空闲窗口里出一次也要报。"""
    end = dict(IDLE_END, deadlocks=BASE["deadlocks"] + 1)
    ev = collect(IDLE_COV, FakeRunner({3019: BASE, 3020: end}, {3019: IDLE_W0, 3020: IDLE_W1}))
    assert [f.code for f in ev.findings] == ["WIN_DEADLOCK"]
    assert "deadlock detected" in ev.findings[0].evidence, "下一步要指到「把日志段贴给我」"


def test_a_counter_going_down_means_a_reset_not_zero_activity():
    end = dict(FAULT_END, xact_commit=1)
    ev = collect(FAULT_COV, FakeRunner({2981: BASE, 2986: end}, {2981: FAULT_W0, 2986: FAULT_W1}))
    load = dim(ev, W.DIM_WIN_LOAD)
    assert load.available is False and "统计被重置过" in load.note and "不是「没有活动」" in load.note
    assert ev.findings == []


def test_the_rollback_rate_reads_the_same_in_headline_and_table():
    ev = collect(FAULT_COV, FakeRunner({2981: BASE, 2986: FAULT_END}, {2981: FAULT_W0, 2986: FAULT_W1}))
    txn = dim(ev, W.DIM_WIN_TXN)
    assert ["回滚率", "0.01%"] in txn.rows and "（0.01%）" in txn.headline


# ---------------------------------------------------------------- 取不到的情形

def test_no_clock_or_no_snapshot_is_said_plainly():
    with pytest.raises(ValueError, match="health.live_counters"):
        W.plan(FakeRunner({}, {}, now=""), "2026-10-17 10:46", "", "")
    with pytest.raises(ValueError, match="快照"):
        W.plan(FakeRunner({}, {}, snaps={}), "2026-10-17 10:46", "", "")


def test_the_top_sql_dimension_asks_for_the_evaluated_window(monkeypatch):
    """传的是实际评估窗口(快照时刻),不是用户说的时间 —— 两个维度用的是同一段。"""
    seen = []

    class P:
        returncode, stdout = 0, json.dumps({"rows": [{"sql_id": "1", "calls": 2, "total_sec": 3.0,
                                                      "avg_ms": 1500, "is_new": True, "query": "q"}]})

    monkeypatch.setattr(W.subprocess, "run", lambda argv, **k: seen.append(argv) or P())
    cov = timewin.Coverage(want_from=t("2026-10-17 10:47:00"), want_to=t("2026-10-17 11:06:30"),
                           start=FAULT_COV.start, end=FAULT_COV.end, complete=True, note="")
    d = W.topsql_dim(cov, 10, "/x/topsql.py", 30, "og")
    assert seen[0][seen[0].index("--from") + 1] == "2026-10-17 10:46:29"
    assert seen[0][seen[0].index("--to") + 1] == "2026-10-17 11:06:55"
    assert d.rows[0][:2] == ["1", "2"] and d.rows[0][4] == "新"
    live = cov._replace(end=timewin.Point("live", t("2026-10-18 20:24:31"), None))
    W.topsql_dim(live, 10, "/x/topsql.py", 30)
    assert "--to" not in seen[1], "终点是此刻时不传 --to"



# ---------------------------------------------------------------- 锁阻塞(活跃会话采样)

ASP_OUT = {"skill": "gaussdb-lockwait",
           "findings": [{"dimension": "Locks", "code": "LOCK_WAIT_LONG", "severity": 3,
                         "metric": "最长锁等待（时段回看）", "value": "约 1150s", "threshold": ">=60s",
                         "evidence": "e", "sql_id": "", "skill": "gaussdb-lockwait"}],
           "asp": {"unavailable": "", "basis": "来源：活跃会话采样（gs_asp）", "samples": 4680, "step_s": 10.0,
                   "chains": [{"path": [5146, 5144, 5145, 5143, 5142, 5141], "wait_s": 1150.0,
                               "first": "2026-10-21 18:32:34", "last": "2026-10-21 18:51:53",
                               "lock": "tuple ExclusiveLock", "lock_object": "fl.acct",
                               "sql_id": "3167133288", "query": "UPDATE fl.acct SET bal = bal + ? WHERE id = ?"}],
                   "roots": [{"sessionid": 5141, "state": "idle in transaction", "blocked": 5}],
                   "pairs": [{"waiter": w} for w in (5142, 5143, 5144, 5145, 5146)]}}


def _proc(rc, out="", err=""):
    return type("P", (), {"returncode": rc, "stdout": out, "stderr": err})()


def test_the_lock_dimension_is_rebuilt_from_asp(monkeypatch):
    seen = []
    monkeypatch.setattr(W.subprocess, "run", lambda argv, **k: seen.append(argv) or _proc(0, json.dumps(ASP_OUT)))
    d = W.lock_dim(FAULT_COV, "/x/lockwait.py", 30, "og")
    assert seen[0][seen[0].index("--from") + 1] == "2026-10-17 10:46:29"
    assert seen[0][seen[0].index("--to") + 1] == "2026-10-17 11:06:55"
    assert d.dimension == "Transaction Locks & Blocking Chains" and d.available
    assert d.rows[0][0] == "5146 → 5144 → 5145 → 5143 → 5142 → 5141"
    assert d.rows[0][1] == "idle in transaction" and d.rows[0][3] == "1150s"
    assert "5 个会话被挡" in d.headline and "会话 5141" in d.headline
    assert [f.code for f in d.findings] == ["LOCK_WAIT_LONG"]


def test_asp_off_is_lookback_unavailable_not_no_waits(monkeypatch):
    out = dict(ASP_OUT, findings=[], asp={"unavailable": "数据库没开活跃会话采样（enable_asp=off）"})
    monkeypatch.setattr(W.subprocess, "run", lambda argv, **k: _proc(0, json.dumps(out)))
    d = W.lock_dim(FAULT_COV, "/x/lockwait.py", 30)
    assert not d.available and d.headline == "回看不了" and "enable_asp=off" in d.note


def test_missing_whitelist_script_says_why(monkeypatch):
    monkeypatch.setattr(W.subprocess, "run",
                        lambda argv, **k: _proc(2, err="error: 时段锁回溯取数失败(脚本不存在)"))
    d = W.lock_dim(FAULT_COV, "/x/lockwait.py", 30)
    assert not d.available and d.headline == "不可用" and "脚本不存在" in d.note


def test_no_waits_in_window_is_said_plainly(monkeypatch):
    out = dict(ASP_OUT, findings=[], asp=dict(ASP_OUT["asp"], chains=[], roots=[], pairs=[]))
    monkeypatch.setattr(W.subprocess, "run", lambda argv, **k: _proc(0, json.dumps(out)))
    d = W.lock_dim(FAULT_COV, "/x/lockwait.py", 30)
    assert d.available and "没有采到锁等待" in d.headline and not d.findings


# ---------------------------------------------------------------- 整条命令

@pytest.fixture()
def rdir(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(health.common.config, "resolved_name", lambda c: "og")
    monkeypatch.setattr(W, "topsql_dim", lambda *a, **k: DimResult(dimension=W.DIM_WIN_TOPSQL, available=True))
    monkeypatch.setattr(W, "lock_dim", lambda *a, **k: DimResult(dimension=W.DIM_LOCKS, available=True,
                                                                  headline="活跃会话采样回看：桩"))
    return tmp_path


def test_a_past_window_lists_what_cannot_be_looked_back(rdir, monkeypatch, capsys):
    r = FakeRunner({2981: BASE, 2986: FAULT_END}, {2981: FAULT_W0, 2986: FAULT_W1})
    monkeypatch.setattr(health.access, "for_conn", lambda *a, **k: r)
    monkeypatch.setattr(health.aggregate, "run_sub_skill",
                        lambda *a, **k: pytest.fail("回看过去时不跑只看此刻的子 skill"))
    assert health.main(["-c", "og", "--from", "2026-10-17 10:46:29", "--to", "2026-10-17 11:06:30"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# 时段体检 — og")
    assert W.PAST_SUB_NOTE in out
    assert "--include/--exclude 排除了全部" not in out, "没跑子 skill 的原因要说真的"
    assert "## Connections\n\n> 不可用：这段时间当时没有采集，回看不了" in out
    assert "活跃会话采样回看：桩" in out, "锁阻塞改从 ASP 回看,不再列进回看不了"
    assert "锁释放后不留痕迹" not in out
    d = rdir / "health" / "og"
    assert len(list(d.glob("*.span.json"))) == 1
    assert not (d / "latest.json").exists(), "时段报告不顶掉大盘上的此刻巡检"
    idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
    assert idx[-1]["mode"] == "span" and idx[-1]["window"]["end_ts"] == "2026-10-17 11:06:55"


def test_a_window_ending_now_adds_current_values_marked_as_current(rdir, monkeypatch, capsys):
    r = FakeRunner({3020: BASE}, {3020: IDLE_W0}, live=IDLE_END, live_waits=IDLE_W1)
    monkeypatch.setattr(health.access, "for_conn", lambda *a, **k: r)
    ran = []
    monkeypatch.setattr(health.aggregate, "run_sub_skill",
                        lambda skill, *a, **k: ran.append(skill) or aggregate.SubSkillResult(skill=skill, ok=True))
    asked = []

    def fake_run_health(runner, include, *a, **k):
        asked.append(include)
        return HealthEvidence(dims=[DimResult(dimension="Connections", available=True, headline="共 12")])

    monkeypatch.setattr(health, "run_health", fake_run_health)
    assert health.main(["-c", "og", "--window", "10m"]) == 0
    out = capsys.readouterr().out
    assert ran == ["gaussdb-lockwait", "gaussdb-vacuum"] and asked == [["conn", "xact", "repl", "storage"]]
    assert "【当前值，不是时段内的】共 12" in out
    assert "快照 3020" in out and "此刻" in out


def test_window_mode_refuses_include_and_exclude(rdir, monkeypatch):
    monkeypatch.setattr(health.access, "for_conn", lambda *a, **k: FakeRunner({}, {}))
    assert health.main(["-c", "og", "--window", "10m", "--include", "conn"]) == 2


# ---------------------------------------------------------------- 溢出归到语句(2026-10-19 测试补)

def _stmt(sid, spill_mb, elapsed_s=1.0):
    return {"sid": sid, "query": "q", "calls": "1", "elapsed_us": str(int(elapsed_s * 1e6)), "cpu_us": "0",
            "spill_kb": str(int(spill_mb * 1024)), "phys_blocks": "0"}


def test_the_window_spill_is_attributed_to_the_statement_that_spilled():
    """测试里时段体检只说「库级 7.8G」,模型点了两条聚合查询当候选;真正落盘 87% 的是 2858996927。"""
    top = [_stmt("3167133288", 0, 5963), _stmt("4198238080", 0, 1142), _stmt("511666082", 682.8, 709),
           _stmt("2858996927", 4483.68, 259)]
    r = FakeRunner({2981: BASE, 2986: FAULT_END}, {2981: FAULT_W0, 2986: FAULT_W1}, top_sql=top)
    ev = collect(FAULT_COV, r)
    f = next(x for x in ev.findings if x.code == "WIN_TEMP_SPILL")
    assert f.sql_id == "2858996927", "下一步调优要能直接拿到这条的 sql_id"
    assert "占语句级的 87%" in f.evidence and "占库级的 56%" in f.evidence, f.evidence
    assert "归不到具体语句" in f.evidence
    assert ("wdr.top_sql", {"b": 2981, "e": 2986, "top": W.SPILL_WIDTH}) in r.calls


def test_a_window_ending_now_says_the_spill_cannot_be_attributed():
    live_cov = FAULT_COV._replace(end=timewin.Point("live", t("2026-10-18 20:24:31"), None))
    r = FakeRunner({2981: BASE}, {2981: FAULT_W0}, live=FAULT_END, live_waits=FAULT_W1, top_sql=[])
    f = next(x for x in collect(live_cov, r).findings if x.code == "WIN_TEMP_SPILL")
    assert f.sql_id == "" and "终点是此刻" in f.evidence and "WDR" in f.evidence
    assert not any(c[0] == "wdr.top_sql" for c in r.calls)


def test_a_failed_statement_lookup_does_not_break_the_report():
    ev = collect(FAULT_COV, FakeRunner({2981: BASE, 2986: FAULT_END}, {2981: FAULT_W0, 2986: FAULT_W1}))
    f = next(x for x in ev.findings if x.code == "WIN_TEMP_SPILL")
    assert "归不到具体语句" in f.evidence and f.severity == Severity.WARN
