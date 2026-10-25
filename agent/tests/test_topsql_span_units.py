"""Top SQL 的「某个时间段 / 最近 N 分钟」(span.py + topsql.py --from/--to/--window)。

用户 2026-10-18 定的原则:**覆盖住用户说的时间段即可,并告诉用户实际评估的是哪一段、为什么。**
端点怎么选在 test_timewin_units.py 里测;这里钉的是取数与相减这一层会悄悄算错的地方:
  ① 同一条语句按执行用户拆成多行(og5 实测 217 行里 29 行重复),覆盖而不是求和会算出凭空的增量;
  ② 快照端点要按 START_WIDTH 取宽,否则终点上的语句在起点里被截掉,被当成「新出现」整条计入;
  ③ 回看过去时只读快照,不碰此刻的累计值;结果存成时段榜,不顶掉大盘上的累计榜,链接带 run=。
时间轴取自 og5:故障快照 2981(10:46:29)/ 2986(11:06:55),之后 21:13:48 重启。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-topsql" / "scripts"

# topsql 与别的 skill 有同名模块(render),先清缓存再把它的 scripts 放最前面
for _m in ("delta", "span", "render", "topsql"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import delta as D  # noqa: E402
import span as SP  # noqa: E402
import topsql  # noqa: E402
from common import access, timewin  # noqa: E402


def t(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


SNAPS = {2980: "2026-10-17 10:45:29", 2981: "2026-10-17 10:46:29",
         2986: "2026-10-17 11:06:55", 2988: "2026-10-17 11:41:13"}


def stmt(sid, calls, total_sec, rows=0, query="select 1"):
    return {"unique_sql_id": sid, "query": query, "calls": str(calls), "total_sec": str(total_sec),
            "avg_ms": "0", "rows": str(rows)}


class FakeRunner:
    """按脚本名回放 og5 的形状。没准备的脚本抛 QueryError —— 与白名单里没导这条脚本时一样。"""

    def __init__(self, stmts=None, snaps=SNAPS, now="2026-10-18 20:24:31",
                 postmaster="2026-10-17 21:13:48"):
        self.stmts, self.snaps, self.now, self.pm = stmts or {}, snaps, now, postmaster
        self.calls = []

    def run(self, script, params):
        self.calls.append((script, dict(params)))
        if script == "health.live_counters" and self.now:
            return [{"item": "now", "value": self.now}, {"item": "postmaster_start", "value": self.pm},
                    {"item": "stats_reset", "value": "2026-10-18 09:00:00"}]
        if script == "wdr.snapshots" and self.snaps:
            return [{"snapshot_id": str(i), "start_ts": ts, "end_ts": ts} for i, ts in self.snaps.items()]
        if script == "topsql.snap_statement" and params.get("snap") in self.stmts:
            return self.stmts[params["snap"]]
        raise access.QueryError("script %s not prepared" % script)


# ---------------------------------------------------------------- 取数层

def test_rows_of_the_same_statement_from_different_users_are_summed():
    """dbe_perf.statement 按执行用户拆行。覆盖的话,两次采样可能留下不同用户的那一行。"""
    s = D.from_rows("t", [stmt("7", 10, 1.5, 3), stmt("7", 5, 0.5, 1), stmt("8", 1, 0.1)])
    assert s.rows["7"].calls == 15 and s.rows["7"].total_sec == pytest.approx(2.0) and s.rows["7"].rows == 4
    assert s.rows["8"].calls == 1


def test_from_rows_reads_by_column_name_and_does_not_choke_on_junk():
    s = D.from_rows("t", [{"unique_sql_id": "9", "calls": None, "total_sec": "abc", "rows": "", "query": None},
                          {"unique_sql_id": "", "calls": "3"}])
    assert list(s.rows) == ["9"], "没有 sql_id 的行丢掉"
    assert (s.rows["9"].calls, s.rows["9"].total_sec, s.rows["9"].query) == (0, 0.0, "")


def test_a_snapshot_endpoint_is_read_wide_from_snap_statement():
    r = FakeRunner(stmts={2981: [stmt("1", 1, 1.0)]})
    s = SP.fetch(r, timewin.Point("snapshot", t(SNAPS[2981]), 2981), SP.START_WIDTH, lambda: None)
    assert r.calls == [("topsql.snap_statement", {"snap": 2981, "limit": SP.START_WIDTH})]
    assert s.rows["1"].calls == 1


def test_sample_and_live_endpoints_do_not_query_snapshots():
    r = FakeRunner()
    mine = D.Sample(at="20261018T121000Z", rows={})
    assert SP.fetch(r, timewin.Point("sample", t("2026-10-18 20:10:00"), mine), 10, lambda: None) is mine
    live = D.Sample(at="now", rows={})
    assert SP.fetch(r, timewin.Point("live", t("2026-10-18 20:24:31"), None), 10, lambda: live) is live
    assert r.calls == []


def test_our_own_samples_are_shifted_from_container_utc_to_db_local_time():
    hist = [D.Sample(at="20261018T121000Z", rows={})]
    pts = SP.sample_points(hist, dt.timedelta(hours=8))
    assert pts[0].kind == "sample" and pts[0].at == t("2026-10-18 20:10:00")


def test_no_snapshot_and_no_sample_means_no_plan():
    r = FakeRunner(snaps={})
    ck = SP.clock(r)
    assert SP.plan(r, t("2026-10-18 20:14:31"), t("2026-10-18 20:24:31"), [], ck) is None


def test_the_statement_clock_ignores_pg_stat_reset():
    """语句累计值只随重启清零;pg_stat_reset 不动它,不能因此把可用的窗口判成跨过清零。"""
    ck = SP.clock(FakeRunner())
    assert ck.reset_at == t("2026-10-17 21:13:48")


# ---------------------------------------------------------------- 整条命令

@pytest.fixture()
def rdir(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(topsql.common.config, "resolved_name", lambda c: "og")
    return tmp_path


def _main(monkeypatch, runner, argv):
    monkeypatch.setattr(topsql.access, "for_conn", lambda *a, **k: runner)
    return topsql.main(["-c", "og"] + argv)


def test_a_past_window_is_the_difference_of_the_two_snapshots_around_it(rdir, monkeypatch, capsys):
    stmts = {2981: [stmt("100", 10, 1.0), stmt("100", 5, 0.5), stmt("200", 3, 9.0)],
             2986: [stmt("100", 40, 4.5), stmt("200", 3, 9.0), stmt("300", 2, 6.0, query="UPDATE fl.acct")]}
    r = FakeRunner(stmts=stmts)
    assert _main(monkeypatch, r, ["--from", "2026-10-17 10:46:29", "--to", "2026-10-17 11:06:30"]) == 0
    out, err = capsys.readouterr()
    assert [c for c in r.calls if c[0] == "topsql.snap_statement"] == [
        ("topsql.snap_statement", {"snap": 2981, "limit": SP.START_WIDTH}),
        ("topsql.snap_statement", {"snap": 2986, "limit": D.SAMPLE_WIDTH})]
    assert not any(c[0] == "topsql.top_sql" for c in r.calls), "终点在过去,不该读此刻的累计值"
    assert "快照 2981" in out and "快照 2986" in out
    assert "口径：你要看的是" in out, "实际评估的是哪一段必须跟着表走"
    assert "| 300 |" in out and "| 100 |" in out
    assert "| 200 |" not in out, "窗口内没有新耗时的语句不上榜"

    d = rdir / "topsql" / "og"
    files = [p.name for p in d.glob("*.time.span.json")]
    assert len(files) == 1
    assert not (d / "latest.json").exists(), "时段榜不顶掉 latest"
    rep = json.loads((d / files[0]).read_text(encoding="utf-8"))
    by_id = {x["sql_id"]: x for x in rep["rows"]}
    assert by_id["100"]["calls"] == 25 and by_id["100"]["total_sec"] == pytest.approx(3.0), "两个用户的行先求和再相减"
    assert by_id["300"]["is_new"] is True
    assert rep["mode"] == "span" and rep["complete"] is True
    assert rep["window"] == {"begin_ts": "2026-10-17 10:46:29", "end_ts": "2026-10-17 11:06:55"}
    idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
    assert idx[-1]["mode"] == "span" and idx[-1]["window"]["begin_ts"] == "2026-10-17 10:46:29"
    assert ("run=%s" % files[0]) in err, "大盘链接要钉住这一份时段榜"
    assert "from=2026-10-17T10%3A46" in err


def test_from_without_snapshots_or_clock_is_an_error_not_a_silent_fallback(rdir, monkeypatch, capsys):
    """--from 说的是一段具体的过去,没有快照就回看不了 —— 不能悄悄换成「此刻的累计值」。"""
    assert _main(monkeypatch, FakeRunner(now=""), ["--from", "2026-10-17 10:46"]) != 0
    _, err = capsys.readouterr()
    assert "error" in err and "快照" in err
    assert not (rdir / "topsql").exists()


def test_to_without_from_is_refused(rdir, monkeypatch, capsys):
    assert _main(monkeypatch, FakeRunner(), ["--to", "2026-10-17 11:00"]) == 2


@pytest.mark.parametrize("bad", [["--from", "2026-10-17 10:46", "--window", "10m"],
                                 ["--from", "2026-10-17 10:46", "--since-last"]])
def test_from_does_not_mix_with_the_other_window_flags(rdir, monkeypatch, bad):
    assert _main(monkeypatch, FakeRunner(), bad) == 2


def test_dash_link_pins_span_and_delta_reports_but_not_the_cumulative_ranking():
    from common import reports
    link = reports.dash_link("topsql", pathlib.Path("/r/20261018T124105Z.time.span.json"), "og", by="time")
    assert "run=20261018T124105Z.time.span.json" in link and "by=time" in link
    link = reports.dash_link("topsql", pathlib.Path("/r/20261018T121000Z.avg.delta.json"), "og", by="avg")
    assert "run=20261018T121000Z.avg.delta.json" in link
    assert reports.dash_link("topsql", pathlib.Path("/r/20261018T120000Z.time.json"), "og", by="time") == \
        "/dash/topsql?inst=og&by=time"
