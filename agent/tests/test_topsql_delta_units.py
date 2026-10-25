"""Top SQL 的窗口增量(纯函数,不连库)。

**要钉死的是「累计拉平」这件事本身:** `dbe_perf.statement` 是自上次清零以来的累计值。
库跑了一个月、某条 SQL 最近十分钟冲高,它那十分钟被一个月的总量摊平,在累计榜上看不见。
用户问的恰恰是最近十分钟。所以这里每一条测试都对着一种会让增量算错、而且**错了看不出来**
的情形:新语句被丢掉、统计清零后相减出负数、排序维度没有原始值却照样排。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-topsql" / "scripts"

# topsql 与别的 skill 有同名模块(render),先清缓存再把它的 scripts 放最前面
for _m in ("delta", "render", "topsql"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import delta as D  # noqa: E402


def S(at, **rows):
    """造一刻采样:S("t1", a=(调用次数, 总秒数, 行数))。"""
    return D.Sample(at=at, rows={k: D.Counters(calls=v[0], total_sec=v[1], rows=v[2],
                                               query="sql " + k)
                                 for k, v in rows.items()})


# ---------------------------------------------------------------- 窗口写法

@pytest.mark.parametrize("text,secs", [("10m", 600), ("600s", 600), ("1h", 3600),
                                       (" 30m ", 1800), ("15S", 15)])
def test_parse_window_accepts_plain_units(text, secs):
    assert D.parse_window(text) == secs


@pytest.mark.parametrize("bad", ["", "10", "m", "0s", "5s", "7h", "-1m", "10 分钟", "abc", None])
def test_parse_window_refuses_anything_else(bad):
    """窗口长度会被用来 sleep,也会写进报告 —— 猜错一个单位,用户拿到的就是另一段时间的数。"""
    with pytest.raises(ValueError):
        D.parse_window(bad)


# ---------------------------------------------------------------- 增量算法

def test_delta_is_the_change_not_the_total():
    before = S("t1", a=(100, 1000.0, 10))
    after = S("t2", a=(110, 1012.0, 12))
    res = D.diff(before, after, 600)
    assert res.reset is False
    row = res.rows[0]
    assert row.calls == 10 and row.total_sec == pytest.approx(12.0) and row.rows == 2
    assert row.avg_ms == pytest.approx(1200.0)      # 12 秒 / 10 次
    assert row.is_new is False


def test_a_statement_that_appeared_inside_the_window_counts_in_full():
    """**这是 gaussdb-wdr 那个内连接 bug 的同一个教训。**

    拿两端取交集的话,窗口里新上来的语句(基线里没有它)会被整批丢掉 —— 而冲高的
    往往正是新上来的那批。它必须整条计入,并标成「新」。
    """
    before = S("t1", old=(100, 1000.0, 0))
    after = S("t2", old=(100, 1000.0, 0), spike=(40, 7.31, 40))
    res = D.diff(before, after, 11)
    assert [r.sql_id for r in res.rows] == ["spike"], "老语句没动过,不该进榜"
    assert res.rows[0].is_new is True
    assert res.rows[0].total_sec == pytest.approx(7.31)


def test_a_spike_buried_in_the_cumulative_board_surfaces_in_the_delta():
    """累计榜上 5963 秒的老语句排第一,而窗口里真正在动的是那条 7 秒的新语句。"""
    before = S("t1", heavy=(76, 5963.14, 0))
    after = S("t2", heavy=(76, 5963.14, 0), spike=(40, 7.31, 40))
    res = D.diff(before, after, 11, by="time")
    assert res.rows[0].sql_id == "spike"


def test_statements_that_did_not_move_are_left_out():
    before = S("t1", a=(10, 1.0, 1), b=(20, 2.0, 2))
    after = S("t2", a=(10, 1.0, 1), b=(21, 2.5, 2))
    assert [r.sql_id for r in D.diff(before, after, 60).rows] == ["b"]


def test_counters_going_backwards_is_reported_as_a_reset_not_as_zero():
    """`reset_unique_sql` 或实例重启之后相减得到负数,毫无意义。

    必须明说「这个窗口算不出增量」—— 把它当成 0 或者取绝对值,都会变成一句
    「最近没什么变化」,而那是错的。
    """
    before = S("t1", a=(100, 1000.0, 0))
    after = S("t2", a=(3, 12.0, 0))
    res = D.diff(before, after, 600)
    assert res.reset is True and res.rows == []
    assert "清零" in res.note and "不是" in res.note


def test_a_new_statement_alone_is_not_mistaken_for_a_reset():
    """只在后一次采样里出现的语句没有可比的前值 —— 不能拿它判成清零。"""
    before = S("t1", a=(10, 1.0, 0))
    after = S("t2", a=(12, 1.5, 0), brand_new=(1, 0.5, 0))
    assert D.diff(before, after, 60).reset is False


@pytest.mark.parametrize("by,first", [("time", "slow"), ("calls", "chatty"), ("avg", "slow")])
def test_sorting_happens_after_the_subtraction(by, first):
    """排序必须按**增量**排,不是按累计值排。"""
    before = S("t1", slow=(0, 0.0, 0), chatty=(0, 0.0, 0))
    after = S("t2", slow=(2, 100.0, 0), chatty=(5000, 5.0, 0))
    assert D.diff(before, after, 600, by=by).rows[0].sql_id == first


def test_delta_refuses_a_sort_key_whose_raw_column_is_not_collected():
    """`reads` 的块数列不在取数脚本的 SELECT 里,没有原始值就算不出增量 ——
    宁可明确拒绝,也不能悄悄按别的维度排序。"""
    with pytest.raises(ValueError) as e:
        D.diff(S("t1"), S("t2"), 60, by="reads")
    assert "reads" in str(e.value)


def test_limit_caps_the_rows():
    before = S("t1")
    after = S("t2", **{"s%d" % i: (1, float(i), 0) for i in range(20)})
    assert len(D.diff(before, after, 60, limit=5).rows) == 5


def test_note_says_the_numbers_are_deltas_and_how_wide_the_sample_was():
    res = D.diff(S("t1", a=(1, 1.0, 0)), S("t2", a=(2, 3.0, 0)), 600)
    assert "增量" in res.note and str(D.SAMPLE_WIDTH) in res.note


# ---------------------------------------------------------------- 基线存取

def test_baseline_round_trip(tmp_path):
    p = tmp_path / "topsql" / "inst" / D.BASELINE_NAME
    s = S("20261017T100905Z", a=(1, 2.0, 3))
    assert D.save_baseline(p, s) is True
    back, why = D.load_baseline(p)
    assert why == "" and back.at == "20261017T100905Z"
    assert back.rows["a"].total_sec == pytest.approx(2.0)


def test_no_reports_dir_means_no_baseline_and_says_so():
    """非容器环境没有存档目录 —— 要如实报错,不能悄悄退回累计榜冒充增量。"""
    assert D.baseline_path(None, "inst") is None
    sample, why = D.load_baseline(None)
    assert sample is None and "基线" in why


def test_first_run_has_no_baseline_and_says_so(tmp_path):
    sample, why = D.load_baseline(tmp_path / "nope.json")
    assert sample is None and "第一次" in why


def test_a_corrupt_baseline_does_not_crash_and_is_reported(tmp_path):
    p = tmp_path / D.BASELINE_NAME
    p.write_text("{不是 json", encoding="utf-8")
    sample, why = D.load_baseline(p)
    assert sample is None and why


def test_an_empty_baseline_is_treated_as_missing(tmp_path):
    p = tmp_path / D.BASELINE_NAME
    p.write_text(json.dumps({"at": "t", "rows": {}}), encoding="utf-8")
    sample, why = D.load_baseline(p)
    assert sample is None and why


def test_baseline_path_lives_next_to_the_archived_reports():
    p = D.baseline_path(pathlib.Path("/nas/me/reports"), "api_10-0-0-9-postgres")
    assert p == pathlib.Path("/nas/me/reports/topsql/api_10-0-0-9-postgres/_baseline.json")


def test_sample_skips_rows_without_a_sql_id():
    class R:
        def __init__(self, sid):
            self.sql_id, self.calls, self.total_sec, self.rows, self.query = sid, 1, 1.0, 1, "q"
    s = D.to_sample("t", [R("a"), R(""), R("b")])
    assert set(s.rows) == {"a", "b"}
