"""时间段的覆盖解析(common/timewin.py,纯函数)。

**用户定的原则(2026-10-18):不追求精确,只要把用户说的时间段覆盖住,并告诉用户
实际评估的是哪一段、为什么。** 所以这组测试钉住的是三件事:
  ① 选出来的窗口**一定包住**用户要的时间段(包不住时必须明说,不许悄悄只覆盖一部分);
  ② 窗口不能跨过实例重启 / 统计重置(跨过去相减得到的数毫无意义);
  ③ 说明里写清实际评估的是哪一段、为什么、宽了多少。

时间轴全部取自 og5 的真实情况:faultlab 那次故障的快照(2981 于 10:46:29、2986 于 11:06:55、
2987 于 11:08:02)、10-17 21:13:48 那次重启、之后每小时一个的快照。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import timewin as T  # noqa: E402


def t(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def snap(i, s):
    return T.Point("snapshot", t(s), i)


# og5 的真实快照
FAULT = [snap(2980, "2026-10-17 10:45:29"), snap(2981, "2026-10-17 10:46:29"),
         snap(2985, "2026-10-17 11:02:38"), snap(2986, "2026-10-17 11:06:55"),
         snap(2987, "2026-10-17 11:08:02"), snap(2988, "2026-10-17 11:41:13")]
RESTART = t("2026-10-17 21:13:48")
HOURLY = [snap(3019, "2026-10-18 19:14:03"), snap(3020, "2026-10-18 20:14:04")]
# 重启前 20:14 一个;重启(21:13:48)之后 21:14:30、22:14:01 各一个 —— og5 的快照钉在每小时第 14 分
ALL = FAULT + [snap(3005, "2026-10-17 20:14:00"), snap(3006, "2026-10-17 21:14:30"),
               snap(3007, "2026-10-17 22:14:01")] + HOURLY
NOW = t("2026-10-18 20:24:31")


# ---------------------------------------------------------------- 一定包住

def test_a_past_window_is_covered_by_the_snapshots_around_it():
    """用户要 10:46:29–11:06:30:起点 2981(10:46:29,恰好),终点 2986(11:06:55,晚 25 秒)。"""
    c = T.cover(t("2026-10-17 10:46:29"), t("2026-10-17 11:06:30"), ALL, NOW)
    assert c.complete and c.start.ref == 2981 and c.end.ref == 2986
    assert c.start.at <= c.want_from and c.end.at >= c.want_to


def test_the_last_ten_minutes_ends_at_now():
    """所有「最近 N 分钟」的终点都在最新快照之后 —— 只有此刻能当终点。"""
    a, b = T.parse_span("", "", "10m", NOW)
    c = T.cover(a, b, ALL, NOW)
    assert c.complete and c.end.kind == "live"
    assert c.start.ref == 3020                       # 20:14:04 <= 20:14:31
    assert c.start.at <= a and c.end.at >= b


def test_with_hourly_snapshots_the_window_widens_back_to_the_previous_hour():
    """最近 5 分钟,而最新快照在 10 分钟前 —— 窗口只能从那个快照起算,比要的宽。"""
    a, b = T.parse_span("", "", "5m", NOW)           # 20:19:31–20:24:31
    c = T.cover(a, b, ALL, NOW)
    assert c.complete and c.start.ref == 3020
    assert "宽" in c.note


@pytest.mark.parametrize("want_from,want_to", [
    ("2026-10-17 10:50:00", "2026-10-17 10:55:00"),
    ("2026-10-17 10:46:29", "2026-10-17 11:08:02"),
    ("2026-10-18 19:00:00", "2026-10-18 20:00:00"),
    ("2026-10-18 20:00:00", "2026-10-18 20:20:00"),
])
def test_whatever_is_asked_the_chosen_window_contains_it(want_from, want_to):
    """**本组最重要的一条。** 不管问哪一段,选出来的窗口都必须包住它 ——
    用户定的唯一硬要求。"""
    c = T.cover(t(want_from), t(want_to), ALL, NOW)
    assert c.complete
    assert c.start.at <= t(want_from) and c.end.at >= t(want_to)


def test_a_sample_tighter_than_any_snapshot_is_preferred():
    """自存采样比快照更贴近时用采样 —— 窗口越贴近,冲高越不容易被摊薄。"""
    s = T.Point("sample", t("2026-10-18 20:13:00"), object())
    a, b = T.parse_span("", "", "10m", NOW)
    c = T.cover(a, b, ALL + [s], NOW)
    assert c.start is s or c.start.ref == 3020       # 3020 更晚更贴近时仍用快照
    s2 = T.Point("sample", t("2026-10-18 20:14:20"), object())
    assert T.cover(a, b, ALL + [s2], NOW).start is s2


# ---------------------------------------------------------------- 不跨重启

def test_a_window_spanning_the_restart_starts_after_it_and_says_what_is_missing():
    """要 10-17 20:00–22:00,而实例 21:13:48 重启过:20:14 的快照不能和 22:14 的相减。
    起点只能挪到重启之后,并说清 20:00 到那里这一段算不了。"""
    c = T.cover(t("2026-10-17 20:00:00"), t("2026-10-17 22:00:00"), ALL, NOW, reset_at=RESTART)
    assert not c.complete
    assert c.start.at >= RESTART
    assert "重启" in c.note and "算不了" in c.note


def test_when_the_only_post_restart_point_is_the_end_there_is_no_start():
    """重启之后只有终点那一个快照 —— 一个时刻减不出增量,不许拿重启前的快照凑。"""
    pts = FAULT + [snap(3005, "2026-10-17 20:14:00"), snap(3007, "2026-10-17 22:14:01")]
    c = T.cover(t("2026-10-17 20:00:00"), t("2026-10-17 22:00:00"), pts, NOW, reset_at=RESTART)
    assert c.start is None and not c.complete
    assert "重启" in c.note


def test_a_window_entirely_before_the_restart_is_unaffected():
    """faultlab 那段整个在重启之前,两端都是重启前的快照 —— 照常可算。"""
    c = T.cover(t("2026-10-17 10:46:29"), t("2026-10-17 11:06:30"), ALL, NOW, reset_at=RESTART)
    assert c.complete and c.start.ref == 2981


def test_a_window_entirely_after_the_restart_is_unaffected():
    c = T.cover(t("2026-10-18 19:30:00"), t("2026-10-18 20:20:00"), ALL, NOW, reset_at=RESTART)
    assert c.complete and c.start.ref == 3019


def test_no_point_after_the_restart_means_no_window_and_it_says_why():
    """重启之后还一个快照都没有、也没有采样 —— 没有前值可减,明说。"""
    c = T.cover(t("2026-10-17 21:20:00"), t("2026-10-17 21:30:00"), FAULT, t("2026-10-17 21:30:00"),
                reset_at=RESTART)
    assert c.start is None and not c.complete
    assert "重启" in c.note


# ---------------------------------------------------------------- 包不住的情形

def test_a_start_older_than_every_snapshot_is_reported_not_silently_trimmed():
    """快照按保留期清理过,用户要的起点比最早的快照还早 —— 退到最早的那个,
    并**明说**前面那一段算不了。不许悄悄只覆盖一部分。"""
    c = T.cover(t("2026-10-10 00:00:00"), t("2026-10-17 11:00:00"), ALL, NOW)
    assert not c.complete
    assert c.start.ref == 2980
    assert "算不了" in c.note


def test_no_snapshot_at_all_says_so():
    a, b = T.parse_span("", "", "10m", NOW)
    c = T.cover(a, b, [], NOW)
    assert c.start is None and "WDR" in c.note


# ---------------------------------------------------------------- 说明文字

def test_the_note_names_both_the_asked_and_the_evaluated_window():
    c = T.cover(t("2026-10-17 10:46:29"), t("2026-10-17 11:06:30"), ALL, NOW)
    assert "你要看的是" in c.note and "实际评估的是" in c.note
    assert "快照 2981" in c.note and "快照 2986" in c.note


def test_a_window_much_wider_than_asked_warns_about_dilution():
    """要 5 分钟、评估了 70 分钟:冲高会被摊薄 —— 这一句必须在,否则用户会把
    这一小时的平均值当成那 5 分钟的实况。"""
    c = T.cover(t("2026-10-18 20:15:00"), t("2026-10-18 20:20:00"), ALL, NOW)
    assert "摊薄" in c.note


def test_a_window_barely_wider_than_asked_does_not_nag():
    """落点差几十秒不必专门提醒 —— 每次都说就成了噪音。"""
    c = T.cover(t("2026-10-17 10:46:29"), t("2026-10-17 11:06:30"), ALL, NOW)
    assert "摊薄" not in c.note


# ---------------------------------------------------------------- 用户的说法

@pytest.mark.parametrize("text,secs", [("10m", 600), ("2h", 7200), ("1d", 86400), (" 30m ", 1800)])
def test_window_forms(text, secs):
    assert T.parse_window(text) == secs


@pytest.mark.parametrize("bad", ["", "10", "m", "0m", "-5m", "十分钟", None])
def test_bad_windows_are_refused_not_guessed(bad):
    with pytest.raises(ValueError):
        T.parse_window(bad)


def test_from_without_to_means_until_now():
    a, b = T.parse_span("2026-10-18 20:00", "", "", NOW)
    assert b == NOW


def test_an_end_in_the_future_is_clamped_to_now():
    """未来那部分没有数据 —— 按此刻算,而不是报错。"""
    a, b = T.parse_span("2026-10-18 20:00", "2026-10-18 23:00", "", NOW)
    assert b == NOW


@pytest.mark.parametrize("f,to,w", [
    ("2026-10-18 21:00", "2026-10-18 20:00", ""),     # 起点晚于终点
    ("2026-10-19 09:00", "", ""),                     # 起点在未来
    ("2026-10-18 20:00", "", "10m"),                  # 两种说法混用
    ("", "", ""),                                     # 什么都没给
    ("昨天下午", "", ""),
])
def test_contradictory_or_unreadable_requests_are_refused(f, to, w):
    with pytest.raises(ValueError):
        T.parse_span(f, to, w, NOW)


# ---------------------------------------------------------------- 时钟对齐

def test_sample_stamps_are_moved_onto_the_database_clock():
    """自存采样用容器时钟(UTC)打戳,快照是数据库本地时间(og5 是 +08)——
    不换到同一根时间轴上,「哪个更贴近」的比较就是错的。"""
    off = T.db_offset(t("2026-10-18 20:24:31"), t("2026-10-18 12:24:30"))
    assert off == dt.timedelta(hours=8)
    assert T.to_local("20261018T121400Z", off) == t("2026-10-18 20:14:00")


def test_a_bad_stamp_is_none_not_a_crash():
    assert T.to_local("坏的", dt.timedelta(hours=8)) is None
