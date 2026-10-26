"""WDR 的三件「让结论能接着往下走」的事(纯函数,不连库)。

都来自 2026-10-17 压测后对模型输出的复核:
  · 表格里显示的是 SQL 开头的**注释**,看着像注释、认不出是哪条语句,用户以为报告里没有慢 SQL;
  · 锁受害者(CPU≈0)和真在干活的语句(CPU 占主导)混在一张榜上,而两者的下一步方向相反;
  · SKILL.md 早就要求「带 sql_id 的先 sqltune 实证」,可模型那一轮 8 条建议一条都没实证 ——
    规则写在提示词里只是「希望它做」,落成报告里的一个段落才看得见有没有做。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-wdr" / "scripts"

for _m in ("model", "thresholds", "util", "collectors", "report", "native", "snaps",
           "interp", "recheck", "finalreport", "kbrefs", "appendix", "mdreport", "render", "ansi"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors as C                                   # noqa: E402
import finalreport as FR                                 # noqa: E402
import model                                             # noqa: E402
import thresholds                                        # noqa: E402
from model import DimResult, Finding, Options, Severity   # noqa: E402


# ---------------------------------------------------------------- 剥开头注释

@pytest.mark.parametrize("raw,starts", [
    ("-- S7 高频点查。单次零点几秒。\n--\n-- 成因:没索引\nSELECT count(*) FROM fl.orders", "SELECT count(*)"),
    ("/* app=order-svc */ SELECT 1", "SELECT 1"),
    ("/* a */\n-- b\nUPDATE t SET x = 1", "UPDATE t"),
    ("\n\n   -- 一行注释\n  DELETE FROM t", "DELETE FROM t"),
    ("SELECT 1", "SELECT 1"),
])
def test_leading_comments_are_stripped_for_display(raw, starts):
    assert C._strip_lead_comments(raw).startswith(starts)


def test_comments_in_the_middle_are_kept():
    """中间的注释可能是 hint 或业务标记,不能动。"""
    out = C._strip_lead_comments("SELECT /* +index(t idx) */ 1 FROM t")
    assert "/* +index(t idx) */" in out


def test_a_statement_that_is_all_comment_still_shows_something():
    """整段都是注释时返回原文,免得表格里显示成空白 —— 空白让人以为取数失败。"""
    assert C._strip_lead_comments("-- 只有注释,没有语句").strip() != ""


def test_stripping_happens_line_wise_not_after_collapsing_whitespace():
    """**第一版的 bug:** 先把换行压成空格再剥,行注释的边界就没了 ——
    只剥掉第一个 `--` 和紧跟的一个词,后面几行注释照样留在显示里。"""
    raw = "-- 第一行注释\n-- 第二行注释\n-- 第三行注释\nSELECT 42"
    assert C._strip_lead_comments(raw) == "SELECT 42"


def test_full_text_is_archived_unstripped():
    """存档里的全文一个字都不剥 —— 要靠它去对现场的 SQL。"""
    raw = "/* app=svc */ SELECT 1"
    assert C._clip_sql(raw).startswith("/* app=svc */")


def test_full_text_keeps_line_breaks_so_line_comments_stay_comments():
    """压成一行之后,`-- 说明` 会把后面整条 SQL 都吃成注释,存档的全文就跑不起来了。"""
    raw = "-- 说明一\n--   说明二\nSELECT   a,\n       b\n\n  FROM t"
    clipped = C._clip_sql(raw)
    assert clipped == "-- 说明一\n-- 说明二\nSELECT a,\nb\nFROM t"
    assert [ln for ln in clipped.splitlines() if not ln.startswith("--")] == ["SELECT a,", "b", "FROM t"]


# ---------------------------------------------------------------- 两类语句分开

class Runner:
    def __init__(self, rows):
        self.rows = rows

    def run(self, name, params=None):
        return self.rows


def _row(sid, elapsed_s, cpu_s, spill_kb=0, calls=1, query="SELECT 1"):
    return {"sid": sid, "query": query, "calls": str(calls),
            "elapsed_us": str(int(elapsed_s * 1e6)), "cpu_us": str(int(cpu_s * 1e6)),
            "spill_kb": str(spill_kb), "phys_blocks": "0"}


def _opt(**kw):
    o = Options(begin=1, end=2, top=10, thresholds=thresholds.default_thresholds())
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_a_cpu_bound_statement_is_flagged_as_doing_real_work():
    d = C.collect_topsql(Runner([_row("plan1", 1142.0, 698.0)]), _opt(db_time_us=23_000_000_000))
    codes = [f.code for f in d.findings]
    assert "WDR_TOPSQL_CPU_HEAVY" in codes
    f = next(f for f in d.findings if f.code == "WDR_TOPSQL_CPU_HEAVY")
    assert f.sql_id == "plan1"
    assert "不是被锁住" in f.evidence


def test_a_lock_victim_is_not_flagged_as_doing_real_work():
    """CPU≈0 是被锁住,**不能**往执行计划的方向指 —— 方向错了用户会白查半天。"""
    d = C.collect_topsql(Runner([_row("lock1", 5963.0, 0.03)]), _opt(db_time_us=23_000_000_000))
    codes = [f.code for f in d.findings]
    assert "WDR_TOPSQL_CPU_HEAVY" not in codes
    assert "WDR_TOPSQL_LOW_CPU" in codes


def test_cpu_bound_statements_are_found_even_when_they_are_not_at_the_top():
    """**实测的真实形态:** 按耗时排序时锁受害者霸占前几名(它们等得最久),
    真在干活的排在 6~8 位。只看前 3 名就一条都报不出来。"""
    rows = [_row("lock%d" % i, 5000.0 - i, 0.0) for i in range(5)]
    rows.append(_row("plan1", 1142.0, 698.0))
    d = C.collect_topsql(Runner(rows), _opt(db_time_us=23_000_000_000))
    heavy = [f.sql_id for f in d.findings if f.code == "WDR_TOPSQL_CPU_HEAVY"]
    assert heavy == ["plan1"]


def test_at_most_two_cpu_bound_findings():
    """一张榜刷出十条发现会把重点冲淡。"""
    rows = [_row("p%d" % i, 1000.0, 700.0) for i in range(6)]
    d = C.collect_topsql(Runner(rows), _opt(db_time_us=23_000_000_000))
    heavy = [f for f in d.findings if f.code == "WDR_TOPSQL_CPU_HEAVY"]
    assert len(heavy) == C.CPU_HEAVY_MAX == 2


def test_a_short_cpu_bound_statement_is_not_flagged():
    """几百毫秒的小语句 CPU 占比高是正常的。"""
    d = C.collect_topsql(Runner([_row("tiny", 0.3, 0.25)]), _opt(db_time_us=23_000_000_000))
    assert "WDR_TOPSQL_CPU_HEAVY" not in [f.code for f in d.findings]


# ------------------------------------------------- 报告里不许出现命令行

def _f(code, sql_id=""):
    return Finding("Top SQL", code, Severity.NOTICE, "m", "v", "t", "e", sql_id=sql_id)


def _report_model(findings=(), texts=None, plans=None):
    m = FR._ReportModel()
    m.findings = list(findings)
    m.sql_texts = dict(texts or {})
    m.plans = dict(plans or {})
    return m


def test_the_section_only_says_what_was_not_covered():
    """**第一版在这里列的是 shell 命令,那是错的。**

    报告逐字呈现给用户:给他一行 `python3 …/explain.py --sql-id 123`,
    他在聊天窗口里跑不了,而模型渲染完报告就收工了、那条命令也不会被执行 —— 两头落空。
    这一段只剩一件事:说清楚这份报告没看哪些东西。
    """
    out = FR._emit_next_steps(_report_model())
    assert "python3" not in out
    assert "膨胀" in out and "已完成" in out


def test_no_shell_command_survives_anywhere_in_the_rendered_report():
    """守门测试:整份报告的任何一段都不许冒出命令行。

    附录、图示、发现、没覆盖的 —— 任何一处加回一行 `python3 …` 都会被这条拦住。
    """
    m = _report_model(findings=[_f("WDR_TOPSQL_CPU_HEAVY", "s1")],
                      texts={"s1": "SELECT 1"})
    out = FR._emit_next_steps(m) + FR._emit_sql_appendix(m)
    for bad in ("python3", "scripts/explain.py", "scripts/sqltune.py", "gsql "):
        assert bad not in out, bad


# ------------------------------------------------- 附录里放计划本身

def test_the_appendix_shows_the_plan_the_model_fetched():
    """用户要的是执行计划,不是取执行计划的命令。"""
    m = _report_model(findings=[_f("WDR_TOPSQL_CPU_HEAVY", "s1")],
                      texts={"s1": "SELECT 1"},
                      plans={"s1": "Seq Scan on fl.orders  (cost=0.00..1.00 rows=1)"})
    out = FR._emit_sql_appendix(m)
    assert "Seq Scan on fl.orders" in out
    assert "python3" not in out


@pytest.mark.parametrize("plans", [None, {"s1": ""}, {"s1": "   "}, {"other": "x"}])
def test_a_missing_plan_is_stated_not_left_blank(plans):
    """取不到就如实写一行。空着会让人以为这条语句没有计划可看。"""
    m = _report_model(findings=[_f("WDR_TOPSQL_CPU_HEAVY", "s1")],
                      texts={"s1": "SELECT 1"}, plans=plans)
    out = FR._emit_sql_appendix(m)
    assert "未取到" in out


def test_a_very_long_plan_is_clipped():
    m = _report_model(findings=[_f("WDR_TOPSQL_CPU_HEAVY", "s1")],
                      texts={"s1": "SELECT 1"},
                      plans={"s1": "x" * (FR.APPENDIX_PLAN_MAX + 500)})
    out = FR._emit_sql_appendix(m)
    assert len(out) < FR.APPENDIX_PLAN_MAX + 400
    assert "计划更长" in out



# ------------------------------------------------- 附录的计划在采集时就取(2026-10-21 测试)

_HIST = {"plan": "Sort  (cost=1.00..2.00 rows=1) (actual time=124133.645..124167.730 rows=197939 loops=1)",
         "start_time": "2026-10-21 18:51:02", "db_time_ms": "124287.3", "rows": "197939"}


def test_the_appendix_prefers_the_real_plan_from_the_window():
    m = _report_model(findings=[_f("WDR_TOPSQL_CPU_HEAVY", "s1")], texts={"s1": "SELECT 1"},
                      plans={"s1": "Seq Scan on fl.orders  (cost=0.00..1.00 rows=1)"})
    m.hist_plans = {"s1": _HIST}
    out = FR._emit_sql_appendix(m)
    assert "真实执行计划" in out and "2026-10-21 18:51:02" in out and "actual time=124133.645" in out
    assert "Seq Scan on fl.orders" not in out and "未取到" not in out


def test_a_missing_history_plan_says_why():
    m = _report_model(findings=[_f("WDR_TOPSQL_CPU_HEAVY", "s1")], texts={"s1": "SELECT 1"})
    m.hist_plans = {"s1": {"plan": "", "why": "SQL 历史里这个窗口内没有记下它的执行计划"}}
    assert "本次未取到（SQL 历史里这个窗口内没有记下它的执行计划）" in FR._emit_sql_appendix(m)


class _PlanRunner:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc, self.calls = rows, exc, []

    def run(self, script, values):
        self.calls.append((script, dict(values)))
        if self.exc:
            raise self.exc
        return self.rows


def _ev_with(sids):
    ev = model.Evidence()
    ev.window = model.Window(begin_id=1, end_id=2, begin_ts="2026-10-21 18:32", end_ts="2026-10-21 18:52")
    ev.findings = [_f("WDR_TOPSQL_CPU_HEAVY", s) for s in sids]
    ev.sql_texts = {s: "SELECT 1" for s in sids}
    return ev


def test_collect_plans_asks_for_the_window_and_strips_the_node_line():
    r = _PlanRunner([{"start_time": "2026-10-21 18:51:02", "db_time_ms": "124287.3", "n_returned_rows": "197939",
                      "query_plan": "Datanode Name: og5\nSort  (cost=1.00..2.00 rows=1)\n"}])
    out = C.collect_plans(r, _ev_with(["3091214944"]))
    assert r.calls == [(C.PLAN_SCRIPT, {"sid": 3091214944, "begin_time": "2026-10-21 18:32:00",
                                        "end_time": "2026-10-21 18:52:59"})]
    assert out["3091214944"]["plan"] == "Sort  (cost=1.00..2.00 rows=1)"
    assert out["3091214944"]["start_time"] == "2026-10-21 18:51:02"


def test_collect_plans_without_the_whitelist_script_records_why_once():
    r = _PlanRunner(exc=C.access.QueryError("脚本不存在"))
    out = C.collect_plans(r, _ev_with(["1", "2"]))
    assert len(r.calls) == 1, "第一条失败后不再逐条试"
    assert set(out) == {"1", "2"} and all("脚本不存在" in v["why"] for v in out.values())


def test_collect_plans_round_trips_through_the_evidence_file():
    ev = _ev_with(["1"])
    ev.sql_plans = {"1": _HIST}
    assert model.Evidence.from_dict(ev.to_dict()).sql_plans == {"1": _HIST}


# ------------------------------------------------- 死锁证据要讲清自己的边界

def test_the_deadlock_evidence_admits_it_cannot_name_the_statements():
    """**次数是硬数据,「哪几条语句成环」不是。** 快照里只有一个计数器。

    2026-10-17 压测那次模型报出的「死锁对」推对了,只因为 faultlab 只让 fl.acct 上的
    UPDATE 互相打架、候选只有一条;日志里实际还有**三方环**(id=20 → 21 → 22),
    按 Top SQL 反推只会得出「两条 UPDATE 在抢」。证据必须把这个边界写死,
    否则模型会把推断当证据陈述 —— 而那时输出看起来和推对时一模一样。
    """
    rows = [{"datname": "postgres", "deadlocks": "24", "xact_commit": "100",
             "xact_rollback": "1", "temp_bytes": "0", "blks_read": "0",
             "blks_hit": "0", "tup_returned": "0", "tup_fetched": "0"}]
    d = C.collect_dbstat(Runner(rows), _opt())
    ev = next(f.evidence for f in d.findings if f.code == "WDR_DEADLOCK")
    assert "硬数据" in ev
    assert "pg_log" in ev and "deadlock detected" in ev
    assert "候选" in ev and "不是证据" in ev
