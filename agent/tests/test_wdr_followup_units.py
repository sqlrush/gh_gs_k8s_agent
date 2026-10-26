"""WDR 的「下一步」:给**模型**执行的计划,走标准错误,不进用户看的报告(纯函数,不连库)。

**这组测试守着一个设计错误的修正。** 第一版把下一步写成报告里的一段:

    ## 下一步
    - 这条在真干活——看执行计划
      `python3 ../gaussdb-explain/scripts/explain.py --sql-id 4198238080`

报告是逐字呈现给用户的最终输出,于是:
  · 用户拿到的是一行 shell,而他要的是执行计划本身,在聊天窗口里也跑不了;
  · 报告既然是「最终输出」,模型渲染完就收工 —— 那几条命令**一条都不会被执行**。
两头落空。改成在 collect 阶段从标准错误交代给模型,模型跑完把结论写进 interp.json。

下面几条分别钉住:命令要带对窗口、只反映当下的检查不许套在历史窗口上、填不出参数的命令不发。
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-wdr" / "scripts"

for _m in ("followup", "model", "thresholds", "util", "collectors", "report",
           "native", "snaps", "interp", "recheck", "finalreport", "kbrefs", "appendix", "mdreport", "render", "ansi"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import followup as F                                  # noqa: E402
from model import Finding, Severity, Window           # noqa: E402


def _f(code, sql_id=""):
    return Finding("Top SQL", code, Severity.WARN, "m", "v", "t", "e", sql_id=sql_id)


_W = Window(begin_id=2981, end_id=2987, begin_ts="2026-10-17 10:46:29",
            end_ts="2026-10-17 11:06:30")


def _cmds(steps):
    return [s.cmd for s in steps]


# ---------------------------------------------------------------- 窗口要带对

def test_the_wait_event_command_carries_this_windows_snapshots():
    """**第一版的实打实的 bug:** 发的是裸 `waitevent.py` —— 它默认取最近几个快照,
    跑出来是「现在」的等待构成,而报告分析的是另一段时间。命令对、窗口错,
    输出看着完全正常,这种错最难发现。"""
    steps = F.plan([_f("WDR_WAIT_CLASS_SKEW")], _W)
    cmd = next(c for c in _cmds(steps) if "waitevent" in c)
    assert "--begin 2981" in cmd and "--end 2987" in cmd


def test_a_window_without_snapshot_ids_drops_the_command_instead_of_guessing():
    """填不出快照号就不发 —— 不带窗口的 waitevent 会给出另一段时间的答案。"""
    steps = F.plan([_f("WDR_WAIT_CLASS_SKEW")], Window())
    assert not any("waitevent" in c for c in _cmds(steps))


def test_the_sql_id_is_filled_in():
    steps = F.plan([_f("WDR_TOPSQL_CPU_HEAVY", "4198238080")], _W)
    cmds = _cmds(steps)
    assert any("--sql-id 4198238080" in c for c in cmds)
    assert not any("{sql_id}" in c or "<sql_id>" in c for c in cmds)


def test_a_command_whose_sql_id_is_unknown_is_not_sent():
    """甩一个带占位符的命令出去,跑起来必然报错。"""
    steps = F.plan([_f("WDR_TOPSQL_CPU_HEAVY", "")], _W)
    assert not any("explain" in c for c in _cmds(steps))


def test_the_spill_culprit_comes_from_the_top_sql_table():
    """溢出那条发现来自库级计数器,本身不知道是哪条语句落的盘;
    从 Top SQL 的 spill 列里取最大的那条,才填得出 sql_id。"""
    headers = ["sql_id", "calls", "elapsed_s", "cpu_s", "spill_MB", "query"]
    rows = [["a", "1", "10", "1", "12.00", "q"],
            ["b", "1", "10", "1", "4483.68", "q"],
            ["c", "1", "10", "1", "0.00", "q"]]
    steps = F.plan([_f("WDR_TEMP_SPILL")], _W, headers, rows)
    assert any("sqltune.py b" in c for c in _cmds(steps))


def test_a_malformed_top_sql_table_does_not_crash_the_plan():
    steps = F.plan([_f("WDR_TEMP_SPILL")], _W, ["没有这列"], [[None]])
    assert isinstance(steps, list)


# ---------------------------------------------------------------- 当下 vs 历史

def test_lock_victims_are_pointed_away_from_plan_tuning():
    steps = F.plan([_f("WDR_TOPSQL_LOW_CPU", "x")], _W)
    assert any("lockwait" in s.cmd for s in steps)
    why = " ".join(s.why for s in steps)
    assert "不要" in why and "执行计划" in why


def test_now_only_checks_are_withheld_on_a_stale_window():
    """**这条是为了不让一个「看起来有根据」的错误结论成立。**

    阻塞链、死元组、正在执行的 SQL 都是「此刻」的;拿它们去解释两小时前那个窗口,
    得到的结论与当时毫无关系 —— 那比老实说「查不到」更糟。
    """
    steps = F.plan([_f("WDR_TOPSQL_LOW_CPU", "x"), _f("WDR_DEADLOCK")], _W, fresh=False)
    assert not any("lockwait" in c for c in _cmds(steps))
    assert not any("vacuum" in c for c in _cmds(steps))


def test_window_independent_checks_survive_a_stale_window():
    """执行计划是**语句的**属性,不是那个窗口的 —— 窗口旧了照样该看。"""
    steps = F.plan([_f("WDR_TOPSQL_CPU_HEAVY", "s1")], _W, fresh=False)
    assert any("explain" in c for c in _cmds(steps))


def test_the_two_things_wdr_never_covers_are_always_checked():
    """膨胀/回收与「没跑完的 SQL」每次都要查 —— 不查就等于让用户以为查全了。"""
    cmds = _cmds(F.plan([], _W))
    assert any("vacuum" in c for c in cmds)
    assert any("slowsql" in c for c in cmds)


def test_the_same_command_is_not_sent_twice():
    steps = F.plan([_f("WDR_DEADLOCK"), _f("WDR_TOPSQL_LOW_CPU", "x")], _W)
    assert _cmds(steps).count("python3 ../gaussdb-lockwait/scripts/lockwait.py") == 1


# ---------------------------------------------------------------- 新鲜度

def _now(ts):
    return _dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def test_staleness_is_the_gap_from_the_window_end():
    assert F.staleness_s("2026-10-17 11:06:30",
                         _now("2026-10-17 11:16:30")) == pytest.approx(600)


@pytest.mark.parametrize("ts", ["2026-10-17 11:06", "2026-10-17 11:06:30",
                                "2026-10-17 11:06:30.123", " 2026-10-17 11:06:30 "])
def test_both_snapshot_timestamp_shapes_parse(ts):
    """快照时间两种长度都会出现(`…11:06:30` 与 `…11:06`)。只认一种会一个都解析不出来,
    然后静默地按「不知道」处理 —— 大盘链接那次就是这么错的。"""
    assert F.staleness_s(ts, _now("2026-10-17 11:16:30")) is not None


@pytest.mark.parametrize("bad", ["", None, "昨天下午", "2026/10/17 11:06"])
def test_an_unparsable_timestamp_is_unknown_not_zero(bad):
    """返回 0 就等于「刚刚结束」,会让只反映当下的检查照样发出去。"""
    assert F.staleness_s(bad, _now("2026-10-17 11:16:30")) is None


def test_a_future_timestamp_is_clamped_to_zero():
    """容器与库的时钟不一致会算出负数。负数不会被任何上限拦住。"""
    assert F.staleness_s("2026-10-17 12:00:00", _now("2026-10-17 11:00:00")) == 0.0


# ---------------------------------------------------------------- 交给模型的文本

def test_the_text_tells_the_model_not_to_paste_the_commands():
    out = F.render_plan(F.plan([_f("WDR_TOPSQL_CPU_HEAVY", "s1")], _W), 10.0)
    assert "不要把命令贴给用户" in out
    assert "interp.json" in out


def test_a_stale_window_explains_why_fewer_checks_were_sent():
    out = F.render_plan(F.plan([_f("WDR_DEADLOCK")], _W, fresh=False),
                        3 * 3600)
    assert "已过 180 分钟" in out
    assert "现场复测" in out


def test_an_unknown_freshness_says_so():
    out = F.render_plan(F.plan([], _W, fresh=False), None)
    assert "算不出窗口距今多久" in out


def test_nothing_to_do_prints_nothing():
    """宁可不打,也不要打一段空的「下一步」。"""
    assert F.render_plan([], 10.0) == ""


# ---------------------------------------------------------------- 死锁:给去处,不给猜测

def test_deadlock_sends_no_command_because_lockwait_cannot_see_past_rings():
    """**lockwait 只能抓正在发生的环。** 它是对 pg_thread_wait_status 的边做环检测，
    而 og5 的 `deadlock_timeout` 是 1 秒 —— 内核 1 秒内就杀掉牺牲者解开环。
    等命令跑起来环早没了，发它只会让模型拿到一份「当前无死锁」然后说不出话。
    """
    steps = F.plan([_f("WDR_DEADLOCK")], _W)
    assert not any("lockwait" in c for c in _cmds(steps))


def test_deadlock_tells_the_user_where_to_actually_look():
    """给不出命令时给**精确的去处** —— 那比给一个看起来有根据的猜测有用得多。"""
    notes = F.manual_notes([_f("WDR_DEADLOCK")])
    assert len(notes) == 1
    note = notes[0]
    assert "pg_log" in note and "deadlock detected" in note
    assert "initial account" in note          # 说明为什么本工具读不到
    assert "候选" in note                      # 反推时必须标候选


def test_the_manual_note_reaches_the_text_given_to_the_model():
    out = F.render_plan(F.plan([_f("WDR_DEADLOCK")], _W), 10.0,
                        manual=F.manual_notes([_f("WDR_DEADLOCK")]))
    assert "deadlock detected" in out


def test_a_manual_note_shows_up_even_when_there_is_no_command_to_run():
    """死锁是唯一发现、而它不产生任何命令时,这段去处仍然必须出现 ——
    否则模型什么都拿不到,只会说「死锁 24 次」然后停住。"""
    out = F.render_plan([], None, manual=F.manual_notes([_f("WDR_DEADLOCK")]))
    assert "pg_log" in out


def test_manual_notes_are_deduplicated():
    notes = F.manual_notes([_f("WDR_DEADLOCK"), _f("WDR_DEADLOCK")])
    assert len(notes) == 1


def test_findings_without_a_manual_note_add_nothing():
    assert F.manual_notes([_f("WDR_TOPSQL_CPU_HEAVY", "x")]) == []


def test_the_deadlock_note_asks_for_the_log_and_names_the_parser():
    """用户贴回日志段之后要有地方接 —— 去处里点名 lockwait 的日志解析,不只是「去日志里看」。"""
    note = F.manual_notes([_f("WDR_DEADLOCK")])[0]
    assert "贴回对话里" in note and "--deadlock-log -" in note and "不连库" in note
