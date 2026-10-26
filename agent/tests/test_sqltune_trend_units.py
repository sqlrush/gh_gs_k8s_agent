"""SQL 优化第 ① 段「现状与趋势」与第 ③ 段「前后对比」(2026-10-26 回答模板化 R4)。

钉住:快照相减按小时归并、计数器清零那一段丢弃;「突然变慢」由脚本判定(≥ 3 倍且 ≥ 100 ms,要有足够的基线);
计划指纹变了还要节点结构真变了才算「计划变了」(绑定值不同不算);任何一条脚本取不到都只落到说明里;
三节在调优报告的最前面;改写验证给出 SQL 文本差异与前后两棵计划。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _m in ("render", "sqltrend", "sqltrendmd", "rewritediff"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))
sys.path.insert(0, str(_ROOT))

import rewritediff  # noqa: E402
import sqltrend as T  # noqa: E402
import sqltrendmd as TM  # noqa: E402
from common.grmp.registry import RegistryError  # noqa: E402


def _snap(i, ts, calls, total_ms):
    return {"snapshot_id": str(i), "snap_ts": ts, "calls": str(calls), "total_us": str(int(total_ms * 1000))}


def _steady_then_jump():
    """10-24 00:00 起每小时 100 次、每次 20 ms;10-25 10:00 那一小时每次 200 ms。"""
    rows, calls, total, i = [], 0, 0.0, 1
    rows.append(_snap(i, "2026-10-24 00:00:05", calls, total))
    for h in range(1, 35):
        i += 1
        per = 200.0 if h == 34 else 20.0
        calls, total = calls + 100, total + 100 * per
        day, hour = divmod(h, 24)
        rows.append(_snap(i, "2026-10-%02d %02d:00:05" % (24 + day, hour), calls, total))
    return rows


def test_hourly_deltas_and_counter_reset():
    rows = [_snap(1, "2026-10-24 09:00:00", 1000, 1000), _snap(2, "2026-10-24 10:00:00", 1100, 3000),
            _snap(3, "2026-10-24 10:30:00", 1100, 3000), _snap(4, "2026-10-24 11:00:00", 50, 500),
            _snap(5, "2026-10-24 12:00:00", 150, 2500)]
    points, resets = T.hourly(rows)
    assert [(p.hour, p.calls, round(p.avg_ms, 1)) for p in points] == [
        ("2026-10-24 10:00", 100, 20.0), ("2026-10-24 12:00", 100, 20.0)]
    assert resets == ("2026-10-24 11:00",)         # 1100 → 50:中间清零过,那一段不计


def test_jump_needs_a_real_jump_and_enough_baseline():
    points, _ = T.hourly(_steady_then_jump())
    j = T.find_jump(points)
    assert j and j.hour == "2026-10-25 10:00" and round(j.ratio, 1) == 10.0
    assert T.find_jump(points[:3]) is None                         # 基线不够 3 个点
    small = [T.HourPoint("2026-10-24 %02d:00" % h, 10, 10 * (50.0 if h == 5 else 1.0)) for h in range(6)]
    assert T.find_jump(small) is None                              # 涨了 50 倍但只有 50 ms,不报


def test_plan_shape_ignores_numbers_and_bind_values():
    a = ("Seq Scan on orders o  (cost=0.00..1234.50 rows=10 width=8) (actual time=0.1..90.2 rows=3 loops=1)\n"
         "  Filter: ((cust_code)::text = 'C001'::text)\n")
    b = a.replace("1234.50", "99.00").replace("C001", "C777").replace("90.2", "12.0")
    c = "Index Scan using idx_orders_cust on orders o  (cost=0.43..8.45 rows=1 width=8)\n"
    assert T.plan_shape(a) == T.plan_shape(b) and T.plan_shape(a) != T.plan_shape(c)


def test_plan_changes_only_when_structure_changes():
    seq = "Seq Scan on orders o  (cost=0.00..1.00 rows=1 width=8)"
    seq2 = seq + "\n  Filter: ((cust_code)::text = 'C777'::text)"
    idx = "Index Scan using idx_c on orders o  (cost=0.43..8.45 rows=1 width=8)"
    hist = T.parse_history([
        {"hour": "2026-10-24 09:00", "runs": "5", "avg_ms": "90", "max_ms": "120", "plan_sig": "a", "query_plan": seq},
        {"hour": "2026-10-24 10:00", "runs": "5", "avg_ms": "90", "max_ms": "130", "plan_sig": "a", "query_plan": ""},
        {"hour": "2026-10-24 11:00", "runs": "5", "avg_ms": "90", "max_ms": "125", "plan_sig": "b", "query_plan": seq2},
        {"hour": "2026-10-24 12:00", "runs": "5", "avg_ms": "3", "max_ms": "4", "plan_sig": "c", "query_plan": idx},
    ])
    changes = T.plan_changes(hist)
    assert [(c.hour, c.before_hour) for c in changes] == [("2026-10-24 12:00", "2026-10-24 11:00")]
    assert "Index Scan" in changes[0].after_plan and "Seq Scan" in changes[0].before_plan


class _Runner:
    def __init__(self, data, missing=()):
        self.data, self.missing, self.calls = data, set(missing), []

    def run(self, script, values):
        self.calls.append((script, dict(values)))
        if script in self.missing:
            raise RegistryError("未注册脚本 %s" % script)
        return self.data.get(script, [])


def test_collect_uses_the_three_scripts_and_degrades_per_section():
    stat = [{"calls": "2641", "avg_ms": "106.8", "max_ms": "507", "min_ms": "64.2", "total_ms": "281934.9",
             "cpu_ms": "196071.2", "io_ms": "0.4"}]
    r = T.collect(_Runner({T.STAT_SCRIPT: stat, T.TREND_SCRIPT: _steady_then_jump()}, missing={T.HISTORY_SCRIPT}),
                  "4198238080")
    assert r.stats.calls == 2641 and r.jump is not None
    assert r.plan_note.startswith("暂不可得") and "未注册脚本" in r.plan_note
    out = TM.report(r)
    assert out.index("## Current Stats") < out.index("## Trend") < out.index("## Plan Changes")
    assert "2641" in out and "**突然变慢**:2026-10-25 10:00" in out and "执行计划历史暂不可得" in out


def test_collect_without_sql_id_does_not_touch_the_database():
    runner = _Runner({})
    r = T.collect(runner, "")
    assert runner.calls == [] and "没有 sql_id" in TM.report(r)


def test_correlation_between_jump_and_plan_change_is_stated():
    points, _ = T.hourly(_steady_then_jump())
    hist = T.parse_history([
        {"hour": "2026-10-25 08:00", "runs": "3", "avg_ms": "20", "max_ms": "25", "plan_sig": "a",
         "query_plan": "Index Scan using i on t  (cost=1..2 rows=1 width=4)"},
        {"hour": "2026-10-25 10:00", "runs": "3", "avg_ms": "200", "max_ms": "260", "plan_sig": "b",
         "query_plan": "Seq Scan on t  (cost=0..900 rows=9 width=4)"},
    ])
    r = T.TrendReport(points=points, jump=T.find_jump(points), history=hist, changes=T.plan_changes(hist))
    out = TM.plan_section(r)
    assert "与执行计划变化(2026-10-25 10:00)吻合" in out and "### 2026-10-25 10:00 起执行计划变了" in out


def test_sql_diff_and_comparison_section():
    assert rewritediff.sql_diff("SELECT a\n FROM t;", "SELECT  a\nFROM t") == ""
    d = rewritediff.sql_diff("SELECT a FROM t\nWHERE to_char(d,'YYYY') = '2026'",
                             "SELECT a FROM t\nWHERE d >= '2026-01-01'")
    assert "-WHERE to_char(d,'YYYY') = '2026'" in d and "+WHERE d >= '2026-01-01'" in d
    md = rewritediff.comparison_md(d, "Seq Scan on t", "Index Scan using i on t")
    assert md.index("### SQL 文本差异") < md.index("### 执行计划(改写前 / 改写后)")
    assert "Seq Scan on t" in md and "Index Scan using i on t" in md
    assert "本次没取到执行计划" in rewritediff.comparison_md("", "", "")


def test_verify_report_carries_the_comparison():
    sys.modules.pop("verify", None)
    import verify
    v = verify.RewriteVerdict(100.0, 10.0, 10.0, True, True, "", "", "-a\n+b", "Seq Scan", "Index Scan")
    out = verify.verify_report(v)
    assert "### SQL 文本差异" in out and "Seq Scan" in out and "Index Scan" in out


def test_tune_report_puts_current_stats_first(monkeypatch):
    sys.modules.pop("sqltune", None)
    import sqltune
    from types import SimpleNamespace
    ev = SimpleNamespace(search_path="", search_path_note="")
    tr = SimpleNamespace(sql_id="1", source="statement_history", schema="", substitution=None, evidence=ev,
                         verified_indexes=[], index_verify_note="", derivation_report="",
                         trend=T.TrendReport(stats=T.Stats(10, 5.0, 9.0, 1.0, 50.0, 40.0, 0.0)))
    monkeypatch.setattr(sqltune, "_substitution_section", lambda _s: "## Placeholder Substitution\n")
    monkeypatch.setattr(sqltune, "evidence_report", lambda _e: "## Execution Plan\n")
    monkeypatch.setattr(sqltune, "kb_section", lambda _t: ("", []))
    out = sqltune.sqltune_report(tr)
    assert out.index("## Current Stats") < out.index("## Placeholder Substitution") < out.index("## Execution Plan")
