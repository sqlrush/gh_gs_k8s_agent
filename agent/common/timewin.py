"""时间段的覆盖解析 —— 用户说「最近 N 分钟」或「某个时间段」时,实际评估哪一段、为什么。

**原则(用户 2026-10-18 定):不追求精确,只要把用户说的时间段覆盖住;并且告诉用户
实际评估的是哪一段、为什么是这一段。**

累计计数器没有历史,任何「一段时间」都只能由两个时刻的累计值相减得到。可用的时刻有三类:

  snapshot  WDR 快照。库自己按 wdr_snapshot_interval 定时拍(og5 是每小时一个,
            保留 wdr_snapshot_retention_days 天)。最可靠,但粒度粗。
  sample    我们自己存的采样。有人问过才有,通常比快照更贴近用户要的时刻。
  live      此刻的实时值。用户的终点在最新快照之后(所有「最近 N 分钟」都是)时,
            只有它能当终点。

规则:
  起点 = 不晚于用户起点、离它最近的那个时刻;
  终点 = 不早于用户终点、离它最近的那个时刻(用户终点在此刻或之后 → 此刻)。
这样算出来的窗口**一定包住**用户要的时间段,代价是可能更宽 —— 宽多少、为什么,写进说明。

**起点与终点之间不能跨过实例重启或统计重置。** 计数器在那一刻清零,跨过去相减得到的是
负数或一个毫无意义的数。所以终点在重置之后时,起点只能从重置之后的时刻里选;
选不到「不晚于用户起点」的,就退而取重置之后最早的那个,并在说明里讲清楚少了哪一段 ——
**不悄悄只覆盖一部分**。

本模块只做选择与说明,不取数、不相减:每类计数器怎么读、怎么减,由各 skill 自己做。
时间一律用**数据库所在时区**的本地时间(快照时间就是这么存的,用户说的时间也按这个理解)。
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import List, NamedTuple, Optional, Tuple

# 用户给的时间。秒可有可无;T 或空格分隔都认。
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")
_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.I)
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# 窗口比用户要的宽多少以内不必专门提醒 —— 快照落点差个一两分钟,说了反而是噪音。
WIDEN_QUIET_S = 120
# 宽到这个倍数以上,提醒「冲高会被摊薄」。
DILUTE_FACTOR = 2.0


class Point(NamedTuple):
    """一个可以当窗口端点的时刻。

    kind  "snapshot" | "sample" | "live"
    at    数据库本地时间
    ref   snapshot → 快照 ID;sample → 调用方自己的采样对象;live → None
    """
    kind: str
    at: _dt.datetime
    ref: object = None

    def label(self) -> str:
        if self.kind == "snapshot":
            return "快照 %s（%s）" % (self.ref, self.at.strftime("%m-%d %H:%M"))
        if self.kind == "sample":
            return "上次采样（%s）" % self.at.strftime("%m-%d %H:%M:%S")
        return "此刻（%s）" % self.at.strftime("%m-%d %H:%M:%S")


class Coverage(NamedTuple):
    """解析结果。start/end 为 None 表示一个端点都选不出来(此时 complete 必为 False)。"""
    want_from: _dt.datetime
    want_to: _dt.datetime
    start: Optional[Point]
    end: Optional[Point]
    complete: bool          # 是否完整包住了用户要的时间段
    note: str               # 给用户的说明 —— 调用方**原样**放进报告

    @property
    def span_s(self) -> float:
        if self.start is None or self.end is None:
            return 0.0
        return max(0.0, (self.end.at - self.start.at).total_seconds())


# ---------------------------------------------------------------- 解析用户的说法

def parse_time(text: str, what: str) -> _dt.datetime:
    raw = (text or "").strip()
    for fmt in _TIME_FORMATS:
        try:
            return _dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("%s「%s」看不懂:写成 YYYY-MM-DD HH:MM(数据库所在时区),例如 2026-10-17 10:46"
                     % (what, raw))


def parse_window(text: str) -> int:
    """`10m` / `2h` / `1d` → 秒。不合法就抛 ValueError,不猜。"""
    m = _WINDOW_RE.match(text or "")
    if not m:
        raise ValueError("时间段要写成 <数字><s|m|h|d>,例如 10m、2h、1d,给的是 %r" % (text,))
    secs = int(m.group(1)) * _UNIT_S[m.group(2).lower()]
    if secs <= 0:
        raise ValueError("时间段必须大于 0,给的是 %s" % text)
    return secs


def parse_span(from_text: str, to_text: str, window_text: str,
               now: _dt.datetime) -> Tuple[_dt.datetime, _dt.datetime]:
    """用户的说法 → (起, 止)。

    `--window 10m` = (此刻 − 10 分钟, 此刻);`--from A [--to B]`,不给 B 就是到此刻。
    终点晚于此刻按此刻算 —— 未来的部分没有数据,而不是「出错」。
    """
    if window_text and (from_text or to_text):
        raise ValueError("--window 与 --from/--to 只能二选一")
    if window_text:
        secs = parse_window(window_text)
        return now - _dt.timedelta(seconds=secs), now
    if not from_text:
        raise ValueError("要给 --from(或 --window)")
    a = parse_time(from_text, "开始时间")
    b = parse_time(to_text, "结束时间") if to_text else now
    if a > b:
        raise ValueError("开始时间 %s 晚于结束时间 %s" % (from_text, to_text))
    if a > now:
        raise ValueError("开始时间 %s 在此刻(%s)之后,那段时间还没发生" % (from_text, now.strftime("%H:%M")))
    return a, min(b, now)


# ---------------------------------------------------------------- 选端点

def _human(secs: float) -> str:
    s = max(0.0, float(secs))
    if s < 90:
        return "%d 秒" % int(round(s))
    if s < 90 * 60:
        m = s / 60.0
        return ("%d 分钟" % round(m)) if abs(m - round(m)) < 0.05 else ("%.1f 分钟" % m)
    h = s / 3600.0
    return ("%d 小时" % round(h)) if abs(h - round(h)) < 0.05 else ("%.1f 小时" % h)


def _span_text(a: _dt.datetime, b: _dt.datetime) -> str:
    fmt = "%m-%d %H:%M" if a.date() != b.date() else "%H:%M"
    head = a.strftime("%m-%d %H:%M") if fmt == "%H:%M" else a.strftime(fmt)
    return "%s–%s（%s）" % (head, b.strftime(fmt), _human((b - a).total_seconds()))


def cover(want_from: _dt.datetime, want_to: _dt.datetime, points: List[Point],
          now: _dt.datetime, reset_at: Optional[_dt.datetime] = None) -> Coverage:
    """选出包住 [want_from, want_to] 的最小窗口。见模块说明里的规则。

    `points` 是快照与自存采样(不含 live,live 由这里自己补);`reset_at` 是实例重启 /
    统计重置的时刻(取不到给 None)。
    """
    want_to = min(want_to, now)
    live = Point("live", now)
    pts = sorted((p for p in points if p.kind != "live" and p.at <= now), key=lambda p: p.at)

    # 终点:不早于用户终点的最早一个时刻;一个都没有就是此刻
    end = next((p for p in pts if p.at >= want_to), live)

    # 起点候选:终点在重置之后时,只能从重置之后的时刻里选
    usable = [p for p in pts if p.at < end.at]
    crossed = bool(reset_at and end.at >= reset_at)
    if crossed:
        usable = [p for p in usable if p.at >= reset_at]
    before = [p for p in usable if p.at <= want_from]
    if before:
        start = before[-1]
        return Coverage(want_from, want_to, start, end, True,
                        _note_full(want_from, want_to, start, end))

    # 包不住起点:退而取能用的最早一个,并说清少了哪一段、为什么
    if usable:
        start = usable[0]
        why = _why_short(want_from, start, pts, reset_at, crossed)
        return Coverage(want_from, want_to, start, end, False,
                        "你要看的是 %s。只能评估 %s → %s：%s，%s 到 %s 这一段算不了。"
                        % (_span_text(want_from, want_to), start.label(), end.label(), why,
                           want_from.strftime("%H:%M"), start.at.strftime("%H:%M")))
    why = ("实例在 %s 重启 / 统计重置之后还没有任何快照或采样" % reset_at.strftime("%m-%d %H:%M")
           if crossed else "没有任何快照或历史采样（WDR 快照未开启或已全部过期）")
    return Coverage(want_from, want_to, None, end, False,
                    "你要看的是 %s。这段时间取不到起点：%s，没有可相减的前值。"
                    % (_span_text(want_from, want_to), why))


def _why_short(want_from, start, pts, reset_at, crossed) -> str:
    if crossed and any(p.at <= want_from for p in pts):
        return ("实例在 %s 重启 / 统计重置过，计数器在那一刻清零，"
                "重启之前的快照不能和之后的值相减" % reset_at.strftime("%m-%d %H:%M"))
    return "更早的快照已按保留期清理（最早只到 %s）" % start.at.strftime("%m-%d %H:%M")


def _note_full(want_from, want_to, start: Point, end: Point) -> str:
    """完整覆盖时给用户的说明:实际是哪一段、为什么是这一段、宽了多少意味着什么。"""
    got_s = (end.at - start.at).total_seconds()
    want_s = max(1.0, (want_to - want_from).total_seconds())
    head = ("你要看的是 %s。实际评估的是 %s → %s（%s）"
            % (_span_text(want_from, want_to), start.label(), end.label(), _human(got_s)))
    why = []
    if start.kind == "snapshot":
        why.append("起点用的是不晚于 %s 的最近一个快照" % want_from.strftime("%H:%M"))
    elif start.kind == "sample":
        why.append("起点用的是 %s 那次采样" % start.at.strftime("%H:%M:%S"))
    if end.kind == "live":
        why.append("终点是此刻")
    elif end.kind == "snapshot":
        why.append("终点用的是不早于 %s 的最近一个快照" % want_to.strftime("%H:%M"))
    else:
        why.append("终点用的是 %s 那次采样" % end.at.strftime("%H:%M:%S"))
    out = head + "：" + "，".join(why)
    extra = got_s - want_s
    if extra > WIDEN_QUIET_S:
        out += "，把你要的时间段完整包含在内，比你要的宽 %s" % _human(extra)
        if got_s > DILUTE_FACTOR * want_s:
            out += ("。结果是这 %s的总增量，你要的那段里的冲高会被摊薄一部分"
                    "——累计计数器只能在已有的时刻之间相减" % _human(got_s))
    return out + "。"


def to_local(utc_stamp: str, offset: _dt.timedelta) -> Optional[_dt.datetime]:
    """自存采样的时间戳(`YYYYMMDDTHHMMSSZ`,容器时钟 UTC)→ 数据库本地时间。

    快照时间是数据库本地时间,两类时刻要放在同一根时间轴上比较才有意义。
    """
    try:
        return _dt.datetime.strptime(utc_stamp, "%Y%m%dT%H%M%SZ") + offset
    except (TypeError, ValueError):
        return None


def db_offset(db_now: _dt.datetime, utc_now: _dt.datetime) -> _dt.timedelta:
    """数据库本地时间相对 UTC 的偏移,取整到 15 分钟(时区偏移都是 15 分钟的整数倍)。

    两个时钟读数之间有几百毫秒的取数延迟,不取整的话偏移会带上这段噪音。
    """
    secs = (db_now - utc_now).total_seconds()
    return _dt.timedelta(seconds=round(secs / 900.0) * 900)
