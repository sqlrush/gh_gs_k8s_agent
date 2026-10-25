"""「最近 N 分钟」怎么回答:用基线回看,还是现场采一小段(纯函数,不连库)。

**这组测试守的是一个已经在生产里炸过的 bug。** 第一版 `--window 10m` 的实现是
采一次样 → `time.sleep(600)` → 再采一次。两个硬伤:

  ① **语义反了。** 用户问的是**过去**十分钟,这个实现测的是**未来**十分钟。
     库在过去十分钟里的冲高,它一个字也答不上。
  ② **把会话挂死。** 一条在对话里阻塞十分钟的命令,实测让 opencode 直接卡住不出结果。
     用户看到的是「发了问题,没反应」。

所以下面每条测试都钉住同一条纪律:**等待量不随用户问的时长增长**,
而「实际覆盖多久」必须和「用户问的多久」分开呈现 —— 这两个数不一样时,
拿前者当后者答,是在给一个没有依据的结论。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-topsql" / "scripts"

for _m in ("delta", "render", "topsql"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import delta as D  # noqa: E402


# ---------------------------------------------------------------- 绝不等满

@pytest.mark.parametrize("asked", [600, 1800, 3600, 6 * 3600])
def test_waiting_never_grows_with_the_question(asked):
    """**本组最重要的一条。** 问「最近一小时」不代表愿意等一小时 ——
    他要的是答案,不是一个挂住的会话。没有基线时也只采 MAX_LIVE_SAMPLE_S。"""
    src = D.choose_source(asked, None)
    assert src.wait_s <= D.MAX_LIVE_SAMPLE_S


def test_a_usable_baseline_means_no_waiting_at_all():
    src = D.choose_source(600, 510)
    assert src.kind == "baseline" and src.wait_s == 0.0


def test_the_span_reported_is_the_baseline_age_not_what_was_asked():
    """基线 8.5 分钟前留的,用户问的是 10 分钟 —— 呈现必须是 8.5。
    报成 10 分钟就是把一个估计值当成测量值。"""
    src = D.choose_source(600, 510)
    assert src.span_s == pytest.approx(510)
    assert "8.5 分钟" in src.note and "10 分钟" in src.note


# ---------------------------------------------------------------- 什么时候不用基线

def test_no_baseline_falls_back_to_a_short_live_sample_and_says_why():
    src = D.choose_source(600, None)
    assert src.kind == "live" and src.wait_s == D.MAX_LIVE_SAMPLE_S
    assert "没有可用基线" in src.note and "留下基线" in src.note


def test_a_baseline_much_older_than_asked_is_not_used():
    """**这正是要解决的问题本身。** 基线是 3 小时前留的、用户问最近 10 分钟:
    拿它相减得到的是这 3 小时的平均,最近的冲高照样被拉平 ——
    用「会把冲高拉平」的办法去回答「冲高在哪」,等于没回答。"""
    src = D.choose_source(600, 3 * 3600)
    assert src.kind == "live"
    assert "拉平" in src.note


def test_a_baseline_too_fresh_is_not_used():
    """上次采样才过 3 秒,这么短的间隔里采样抖动比信号还大。"""
    src = D.choose_source(600, 3)
    assert src.kind == "live"


def test_the_boundary_between_using_and_not_using_a_baseline():
    assert D.choose_source(600, 600 * D.BASELINE_MAX_RATIO - 1).kind == "baseline"
    assert D.choose_source(600, 600 * D.BASELINE_MAX_RATIO + 1).kind == "live"


def test_a_baseline_older_than_asked_but_within_tolerance_is_used():
    """问 10 分钟、基线 20 分钟前的 —— 还在容忍范围内,用它并如实说覆盖 20 分钟,
    比现场采 60 秒更接近用户想知道的事。"""
    src = D.choose_source(600, 1200)
    assert src.kind == "baseline" and src.span_s == pytest.approx(1200)


# ---------------------------------------------------------------- 边界与坏输入

def test_a_negative_baseline_age_is_clamped_not_trusted():
    """容器与库的时钟不一致会算出负数年龄。负数会一路当成「非常新鲜」,
    必须先夹到 0 —— 夹到 0 就落进「太新,改现场采」那一支,是安全的那一边。"""
    src = D.choose_source(600, -50)
    assert src.kind == "live"


def test_a_short_question_does_not_get_a_longer_sample_than_asked():
    """问「最近 20 秒」时不该采 60 秒 —— 采样不能比问题还宽。"""
    src = D.choose_source(20, None)
    assert src.wait_s == 20


def test_the_explicit_live_cap_is_bigger_than_the_automatic_one():
    """自动兜底要小(不能挂住对话);用户点名要盯着看时才放宽,且仍然有限。"""
    assert D.MAX_LIVE_SAMPLE_S < D.MAX_LIVE_EXPLICIT_S < D.MAX_WINDOW_S


def test_a_custom_live_cap_is_respected():
    assert D.choose_source(600, None, live_cap_s=30).wait_s == 30


# ---------------------------------------------------------------- 时长的说法

@pytest.mark.parametrize("secs,text", [(0, "0 秒"), (45, "45 秒"), (89, "89 秒"),
                                       (600, "10 分钟"), (510, "8.5 分钟"),
                                       (3600, "60 分钟"), (3 * 3600, "3 小时")])
def test_spans_read_like_a_person_wrote_them(secs, text):
    assert D.human_span(secs) == text


def test_a_whole_number_of_minutes_has_no_decimal_point():
    """「问的 10.0 分钟」读着像个测量值,而那是用户自己说的数。"""
    assert D.human_span(600) == "10 分钟"


# ---------------------------------------------------------------- 真正跑一遍 _run_delta

class _Runner:
    """假取数:每次调用都把累计值往上加一点,这样两次采样之间一定算得出增量。"""

    def __init__(self):
        self.n = 0

    def run(self, script, params=None):
        self.n += 1
        return [{"unique_sql_id": "s1", "query": "SELECT 1", "calls": 10 * self.n,
                 "total_sec": 1.5 * self.n, "avg_ms": 150, "rows": self.n}]


class _Args:
    def __init__(self, **kw):
        self.by, self.limit, self.format = "time", 10, "markdown"
        self.window, self.since_last, self.live_sample = "", False, ""
        self.include_system = False
        for k, v in kw.items():
            setattr(self, k, v)


def _load_topsql(monkeypatch, sleeps, tmp_path):
    """装载 topsql 并把 sleep 换成记账、把报告目录指到临时目录。"""
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    for _m in ("topsql",):
        sys.modules.pop(_m, None)
    import topsql as T
    monkeypatch.setattr(T.time, "sleep", lambda s: sleeps.append(s))
    return T


def test_asking_about_ten_minutes_does_not_sleep_ten_minutes(monkeypatch, tmp_path, capsys):
    """**这正是把 opencode 挂住的那条命令。**

    `--window 10m` 原先会 `time.sleep(600)`,用户发出问题后什么都等不到。
    现在最多睡 MAX_LIVE_SAMPLE_S,而且会如实说明实际覆盖了多久。
    """
    sleeps = []
    T = _load_topsql(monkeypatch, sleeps, tmp_path)
    rc = T._run_delta(_Runner(), _Args(window="10m"), "og5")
    assert rc == 0
    assert max(sleeps or [0]) <= D.MAX_LIVE_SAMPLE_S
    out = capsys.readouterr().out
    assert "口径：" in out and "现场采" in out


def test_the_second_ask_uses_the_baseline_and_does_not_sleep_at_all(monkeypatch, tmp_path, capsys):
    """第一次问留下基线,第二次问就能真的回看 —— 一秒都不用等。"""
    sleeps = []
    T = _load_topsql(monkeypatch, sleeps, tmp_path)
    r = _Runner()
    T._run_delta(r, _Args(window="10m"), "og5")          # 留基线
    sleeps.clear()
    # 把历史里每一刻的时间戳都往前挪 5 分钟,模拟「上次问过、过了一会儿再问」
    import datetime as _dt

    import delta as DD
    bpath = DD.baseline_path(pathlib.Path(str(tmp_path)), T.reports.instance_key("og5"))
    d = json.loads(bpath.read_text(encoding="utf-8"))
    back = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
    for i, smp in enumerate(d["samples"]):
        smp["at"] = (back + _dt.timedelta(seconds=i)).strftime("%Y%m%dT%H%M%SZ")
    bpath.write_text(json.dumps(d), encoding="utf-8")

    rc = T._run_delta(r, _Args(window="10m"), "og5")
    assert rc == 0 and sleeps == []
    out = capsys.readouterr().out
    assert "最近 5 分钟" in out


def test_an_explicit_live_sample_longer_than_the_cap_is_refused(monkeypatch, tmp_path, capsys):
    """能在对话里阻塞几小时的命令是个陷阱 —— 明确拒绝,并说清怎么看更长的一段。"""
    sleeps = []
    T = _load_topsql(monkeypatch, sleeps, tmp_path)
    rc = T._run_delta(_Runner(), _Args(live_sample="1h"), "og5")
    assert rc == 2 and sleeps == []
    assert "--since-last" in capsys.readouterr().err


def test_an_explicit_live_sample_within_the_cap_still_waits(monkeypatch, tmp_path):
    """用户点名要盯着采 30 秒,那就老老实实采 30 秒。"""
    sleeps = []
    T = _load_topsql(monkeypatch, sleeps, tmp_path)
    assert T._run_delta(_Runner(), _Args(live_sample="30s"), "og5") == 0
    assert sleeps == [30]


# ---------------------------------------------------------------- 采样历史

def _s(at, n=1):
    return D.Sample(at=at, rows={"a": D.Counters(calls=n, total_sec=float(n), rows=n,
                                                 query="SELECT 1")})


def test_saving_appends_instead_of_overwriting(tmp_path):
    """**实测抓到的问题:** 只留一刻的话,连着问两次「最近十分钟」,第二次读到的基线是
    第一次运行刚写下的(年龄 1 秒)—— 于是又退回现场采样,回看功能等于永远用不上。
    """
    p = tmp_path / D.BASELINE_NAME
    for i in range(3):
        D.save_baseline(p, _s("20261017T10%02d00Z" % i, i + 1))
    assert len(D.load_history(p)) == 3


def test_history_is_capped(tmp_path):
    p = tmp_path / D.BASELINE_NAME
    for i in range(D.HISTORY_MAX + 5):
        D.save_baseline(p, _s("20261017T10%02d00Z" % i, i + 1))
    assert len(D.load_history(p)) == D.HISTORY_MAX


def test_history_is_oldest_first_and_since_last_takes_the_newest(tmp_path):
    p = tmp_path / D.BASELINE_NAME
    D.save_baseline(p, _s("20261017T100000Z"))
    D.save_baseline(p, _s("20261017T101000Z"))
    hist = D.load_history(p)
    assert [s.at for s in hist] == ["20261017T100000Z", "20261017T101000Z"]
    newest, why = D.load_baseline(p)
    assert why == "" and newest.at == "20261017T101000Z"


def test_an_old_single_sample_file_is_still_read(tmp_path):
    """升级时已经存着的那一份不该被丢掉 —— 丢了的话升级后第一次问又没有基线。"""
    p = tmp_path / D.BASELINE_NAME
    p.write_text(json.dumps({"at": "20261017T100000Z",
                             "rows": {"a": {"calls": 1, "total_sec": 1.0, "rows": 1}}}),
                 encoding="utf-8")
    hist = D.load_history(p)
    assert len(hist) == 1 and hist[0].at == "20261017T100000Z"


def test_only_the_newest_sample_keeps_the_query_text(tmp_path):
    """历史几刻只参与相减,留着文本会让这个文件白白大十倍。"""
    p = tmp_path / D.BASELINE_NAME
    D.save_baseline(p, _s("20261017T100000Z"))
    D.save_baseline(p, _s("20261017T101000Z"))
    hist = D.load_history(p)
    assert hist[0].rows["a"].query == ""
    assert hist[-1].rows["a"].query == "SELECT 1"


@pytest.mark.parametrize("bad", ["", "{坏的", "[]", '{"samples": "不是表"}'])
def test_a_corrupt_history_file_is_empty_not_an_exception(tmp_path, bad):
    p = tmp_path / D.BASELINE_NAME
    p.write_text(bad, encoding="utf-8")
    assert D.load_history(p) == []


# ---------------------------------------------------------------- 挑哪一刻

_AGES = {"t_5s": 5, "t_60s": 60, "t_9m": 540, "t_11m": 660, "t_3h": 3 * 3600}


def _age_of(s):
    return _AGES.get(s.at)


def test_the_sample_closest_to_the_asked_window_is_chosen():
    """问「最近十分钟」,就该挑十分钟前那一刻 —— 不是最近那一刻,也不是最旧那一刻。"""
    samples = [_s(k) for k in _AGES]
    chosen, age = D.pick_baseline(samples, 600, _age_of)
    assert chosen.at == "t_9m" and age == 540


def test_samples_that_are_too_fresh_are_not_chosen():
    chosen, age = D.pick_baseline([_s("t_5s")], 600, _age_of)
    assert chosen is None and age == 5


def test_samples_that_are_too_old_are_not_chosen():
    chosen, age = D.pick_baseline([_s("t_3h")], 600, _age_of)
    assert chosen is None and age == 3 * 3600


def test_with_nothing_usable_the_newest_age_comes_back_to_explain_why():
    """带回最新那刻的年龄,好让 choose_source 分清「太新」还是「太旧」这两种原因。"""
    chosen, age = D.pick_baseline([_s("t_5s"), _s("t_3h")], 600, _age_of)
    assert chosen is None and age == 5


def test_an_empty_history_gives_no_age_at_all():
    chosen, age = D.pick_baseline([], 600, _age_of)
    assert chosen is None and age is None


def test_samples_whose_age_cannot_be_computed_are_skipped():
    chosen, age = D.pick_baseline([_s("解析不了"), _s("t_9m")], 600, _age_of)
    assert chosen.at == "t_9m"


def test_asking_a_shorter_window_picks_a_closer_sample():
    """同一份历史,问「最近一分钟」挑的是另一刻 —— 挑法要跟着问题走。"""
    samples = [_s(k) for k in _AGES]
    assert D.pick_baseline(samples, 60, _age_of)[0].at == "t_60s"


def test_history_is_sorted_by_time_not_by_write_order(tmp_path):
    """**实测存出过乱序历史**(123517 / 123516 / 123617):两次运行的写入顺序
    不一定就是时间顺序。而 `--since-last` 取「最后一刻」—— 不排序的话它可能拿到
    的不是最新那刻,算出来的增量连方向都是错的(可能变成负数而被判成「清零」)。
    """
    p = tmp_path / D.BASELINE_NAME
    p.write_text(json.dumps({"samples": [
        _sample_dict("20261017T123517Z"),
        _sample_dict("20261017T123516Z"),
        _sample_dict("20261017T123617Z"),
    ]}), encoding="utf-8")
    assert [s.at for s in D.load_history(p)] == ["20261017T123516Z",
                                                "20261017T123517Z",
                                                "20261017T123617Z"]
    newest, why = D.load_baseline(p)
    assert why == "" and newest.at == "20261017T123617Z"


def _sample_dict(at):
    return {"at": at, "rows": {"a": {"calls": 1, "total_sec": 1.0, "rows": 1}}}


def test_trimming_drops_the_oldest(tmp_path):
    """排序之后再裁,裁掉的必须是最旧的那几刻 —— 裁掉新的等于把刚采的样丢了。"""
    p = tmp_path / D.BASELINE_NAME
    p.write_text(json.dumps({"samples": [
        _sample_dict("20261017T10%02d00Z" % i) for i in range(D.HISTORY_MAX + 3)]}),
        encoding="utf-8")
    kept = [s.at for s in D.load_history(p)]
    assert len(kept) == D.HISTORY_MAX
    assert kept[-1] == "20261017T10%02d00Z" % (D.HISTORY_MAX + 2)
    assert kept[0] == "20261017T100300Z"
