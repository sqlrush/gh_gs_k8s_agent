"""Top SQL:把「等锁 / 等待」和「自身慢」分开(纯函数 + 假 runner,不连库)。

2026-10-24 faultlab(og5,快照 3148→3154):两张榜的榜首都是等锁语句 —— 总耗时榜首是 K1 链上的
UPDATE fl.acct(6778.9 秒,CPU 0),平均耗时榜首是 K2 的 FOR UPDATE(1081 秒,CPU 0);
高频中等慢的 S7 只排总耗时第 8,S3 / S4 也不在平均耗时榜首。

dbe_perf.statement 与 snapshot.snap_summary_statement 里**没有**锁等待时间列(og5 上逐列核对过),
有的是 cpu_time 与 data_io_time。于是:
  · 每条标出「等待占比」= 1 − (CPU + 数据 IO) / 总耗时 —— 等锁、等轻量锁、排队等 CPU 都在里面;
  · 新口径 `--by self` / `--by self_avg`:按 CPU + 数据 IO(自身耗时)排,不含任何等待。
下面的数都是那一轮的原值。
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-topsql" / "scripts"
for _m in ("delta", "render", "topsql", "span"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import delta as D  # noqa: E402
import topsql  # noqa: E402

# sql_id: (调用, 总秒, CPU 秒, IO 秒, 文本)
FL = {
    "3167133288": (75, 6778.9, 0.0, 0.0, "UPDATE fl.acct SET bal = bal + ? WHERE id = ?"),
    "4168886180": (1, 1081.2, 0.0, 0.0, "SELECT id FROM fl.acct WHERE id BETWEEN ? AND ? ORDER BY id FOR UPDATE"),
    "3091214944": (13, 1161.6, 678.2, 12.1, "WITH lvl1 AS ( SELECT i.order_id …"),           # S2
    "4198238080": (10057, 1140.0, 663.5, 0.8, "SELECT count(*) AS n … WHERE o.cust_code = ?"),  # S7
    "3509710879": (1, 879.6, 550.8, 0.0, "SELECT count(*) AS kept … NOT IN …"),               # S3
    "2643287733": (1, 315.2, 190.6, 0.0, "SELECT o.id, o.amount, (SELECT max(c.name) …"),     # S4
}


def _db_row(sid, calls, total, cpu, io, query, with_cpu=True):
    r = {"unique_sql_id": sid, "query": query, "calls": str(calls), "total_sec": str(total),
         "avg_ms": str(round(total * 1000 / calls, 2)), "rows": "0"}
    if with_cpu:
        r.update(cpu_sec=str(cpu), io_sec=str(io))
    return r


class FakeRunner:
    def __init__(self, with_cpu=True):
        self.with_cpu, self.calls = with_cpu, []

    def run(self, script, params=None):
        self.calls.append((script, dict(params or {})))
        rows = [_db_row(sid, *v, with_cpu=self.with_cpu) for sid, v in FL.items()]
        key = {"total_elapse_time DESC": lambda r: -float(r["total_sec"]),
               "cpu_time + data_io_time DESC": lambda r: -(float(r["cpu_sec"]) + float(r["io_sec"]))}
        rows.sort(key=key.get(params.get("order"), key["total_elapse_time DESC"]))
        return rows[:int(params.get("limit", 10))]


# ---------------------------------------------------------------- 白名单脚本

def _sql(name):
    return yaml.safe_load((_ROOT / "scripts" / "registry" / "topsql" / name).read_text(encoding="utf-8"))["sql"]


def test_top_sql_script_returns_cpu_and_io_and_can_order_by_them():
    sql = _sql("top_sql.yaml")
    assert "sum(cpu_time)" in sql and "sum(data_io_time)" in sql, "聚合子查询要带上这两列,{{order}} 才排得了"
    assert "AS cpu_sec" in sql and "AS io_sec" in sql
    for by in ("self", "self_avg"):
        for col in ("cpu_time", "data_io_time"):
            assert col in topsql._SORT_COLS[by]


def test_snapshot_script_returns_cpu_and_io_too():
    sql = _sql("snap_statement.yaml")
    assert "sum(snap_cpu_time)" in sql and "sum(snap_data_io_time)" in sql
    assert "AS cpu_sec" in sql and "AS io_sec" in sql


# ---------------------------------------------------------------- 累计榜

def test_rows_carry_self_time_and_wait_share():
    rows = {r.sql_id: r for r in topsql.top_sql(FakeRunner(), "time", 10)}
    k1, s3 = rows["3167133288"], rows["3509710879"]
    assert k1.self_sec == pytest.approx(0.0) and k1.wait_ratio == pytest.approx(1.0)
    assert s3.self_sec == pytest.approx(550.8) and s3.wait_ratio == pytest.approx(1 - 550.8 / 879.6)


def test_old_whitelist_without_cpu_columns_degrades_to_unknown_not_zero():
    """客户还没重导白名单时取数脚本没有 cpu_sec:不能当 0 —— 那会把每条都标成「全在等」。"""
    rows = topsql.top_sql(FakeRunner(with_cpu=False), "time", 10)
    assert all(r.self_sec is None and r.wait_ratio is None for r in rows)
    out = topsql.stmt_table("Top SQL by time", rows)
    assert "—" in out and "白名单" in out


def test_by_self_on_an_old_whitelist_says_to_re_register():
    from common import access

    class OldRunner:
        def run(self, script, params=None):
            raise access.QueryError('ERROR: column "cpu_time" does not exist (SQLSTATE 42703)')

    with pytest.raises(ValueError) as ei:
        topsql.top_sql(OldRunner(), "self", 10)
    assert "重新注册" in str(ei.value) and "--by time" in str(ei.value)
    with pytest.raises(access.QueryError):
        topsql.top_sql(OldRunner(), "time", 10)        # 别的维度的报错原样抛,不乱加提示


def test_by_self_puts_the_lock_waiters_at_the_bottom():
    r = FakeRunner()
    rows = topsql.top_sql(r, "self", 10)
    assert r.calls[0][1]["order"] == topsql._SORT_COLS["self"]
    assert [x.sql_id for x in rows[:3]] == ["3091214944", "4198238080", "3509710879"]   # S2 / S7 / S3


def test_cumulative_table_marks_wait_share_and_says_what_to_do():
    rows = topsql.top_sql(FakeRunner(), "time", 10)
    out = topsql.stmt_table("Top SQL by time", rows)
    assert "等待占比" in out and "CPU+IO_S" in out
    assert "| 100% |" in out
    assert "#1" in out and "--by self" in out and "gaussdb-lockwait" in out
    assert "没有" in out and "等锁" in out          # 说清楚:没有单条语句的锁等待列,分不出等的是不是锁


def test_no_wait_note_when_nothing_on_the_board_is_waiting():
    rows = [r for r in topsql.top_sql(FakeRunner(), "self", 10) if r.wait_ratio is not None and r.wait_ratio < 0.5]
    assert rows
    assert "gaussdb-lockwait" not in topsql.stmt_table("Top SQL by self", rows)


# ---------------------------------------------------------------- 窗口增量

def _sample(at, scale=1.0, with_cpu=True):
    return D.Sample(at=at, rows={sid: D.Counters(calls=int(v[0] * scale), total_sec=v[1] * scale, rows=0,
                                                 query=v[4],
                                                 cpu_sec=(v[2] * scale if with_cpu else None),
                                                 io_sec=(v[3] * scale if with_cpu else None))
                                 for sid, v in FL.items()})


def test_delta_self_ranking_matches_what_faultlab_expected():
    """窗口里新出现的整条计入;按自身耗时排,S7 / S2 在前、等锁的两条沉底;按平均自身耗时排,S3 / S4 在前。"""
    before = D.Sample(at="t0", rows={})
    after = _sample("t1")
    by_time = D.diff(before, after, 1288, by="time", limit=10)
    assert by_time.rows[0].sql_id == "3167133288"             # 原口径不变
    by_self = D.diff(before, after, 1288, by="self", limit=10)
    assert [r.sql_id for r in by_self.rows[:2]] == ["3091214944", "4198238080"]
    assert [r.sql_id for r in by_self.rows[-2:]] == ["3167133288", "4168886180"]
    by_self_avg = D.diff(before, after, 1288, by="self_avg", limit=10)
    assert [r.sql_id for r in by_self_avg.rows[:2]] == ["3509710879", "2643287733"]


def test_delta_self_is_the_change_not_the_total():
    res = D.diff(_sample("t0", 1.0), _sample("t1", 2.0), 600, by="self", limit=10)
    s7 = next(r for r in res.rows if r.sql_id == "4198238080")
    assert s7.self_sec == pytest.approx(664.3)


def test_baseline_without_cpu_makes_self_unknown_and_refuses_to_rank_by_it():
    """升级前留下的基线没有 CPU:那一端算不出自身耗时的增量 —— 标未知,按 self 排就明说排不了。"""
    res = D.diff(_sample("t0", 1.0, with_cpu=False), _sample("t1", 2.0), 600, by="time", limit=10)
    assert all(r.self_sec is None for r in res.rows)
    with pytest.raises(ValueError) as ei:
        D.diff(_sample("t0", 1.0, with_cpu=False), _sample("t1", 2.0), 600, by="self", limit=10)
    assert "CPU" in str(ei.value)


def test_baseline_roundtrip_keeps_cpu_and_io(tmp_path):
    path = tmp_path / "topsql" / "inst" / D.BASELINE_NAME
    assert D.save_baseline(path, _sample("20261024T014500Z"))
    back = D.load_history(path)[-1]
    assert back.rows["4198238080"].cpu_sec == pytest.approx(663.5)
    assert back.rows["4198238080"].io_sec == pytest.approx(0.8)


def test_old_baseline_file_loads_with_unknown_cpu(tmp_path):
    import json
    path = tmp_path / D.BASELINE_NAME
    path.write_text(json.dumps({"samples": [{"at": "t", "rows": {"a": {"calls": 1, "total_sec": 1.0, "rows": 1}}}]}))
    assert D.load_history(path)[-1].rows["a"].cpu_sec is None


def test_snapshot_rows_carry_cpu_and_io():
    s = D.from_rows("t", [_db_row("9", 3, 6.0, 2.5, 0.5, "q")])
    assert (s.rows["9"].cpu_sec, s.rows["9"].io_sec) == (pytest.approx(2.5), pytest.approx(0.5))
    old = D.from_rows("t", [_db_row("9", 3, 6.0, 0, 0, "q", with_cpu=False)])
    assert old.rows["9"].cpu_sec is None


def test_delta_table_shows_wait_share():
    res = D.diff(D.Sample(at="t0", rows={}), _sample("t1"), 1288, by="time", limit=10)
    out = topsql.delta_table("Top SQL(窗口)", res, "time")
    assert "窗口内CPU+IO(s)" in out and "等待占比" in out and "--by self" in out


def test_self_keys_are_accepted_in_window_mode():
    assert "self" in D.DELTA_SORT_KEYS and "self_avg" in D.DELTA_SORT_KEYS
    assert "self" in topsql.SORT_KEYS and "self_avg" in topsql.SORT_KEYS
