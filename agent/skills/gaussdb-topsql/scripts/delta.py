"""窗口增量:两次采样相减,回答「最近这段时间谁在冲高」。

**为什么需要它。** `dbe_perf.statement` 是**自上次清零以来的累计值**。库跑了一个月、
某条 SQL 在最近十分钟突然冲高,它那十分钟的耗时被一个月的总量摊平,在累计榜上根本看不见。
用户问的恰恰是「最近十分钟怎么了」,而累计榜回答的是「这一个月总体谁最重」—— 两个问题
不一样,拿后者当前者答,会让一次真实的冲高被完全漏掉。

**「最近十分钟」只能靠基线回看,不能靠等。** 这是第一版踩过的坑:`--window 10m` 当时的实现
是采一次样、`sleep(600)`、再采一次。两个问题,都是硬伤 ——
  ① **语义反了**:用户问的是**过去**十分钟,这个实现测的是**未来**十分钟;
  ② 一条在对话里跑十分钟的命令等于把会话挂死,实测 opencode 直接卡住不出结果。
累计计数器没有历史,过去十分钟**只能**从十分钟前留下的基线里算出来。所以:
  · 有基线 → 拿它相减,如实说明实际覆盖了多久(可能是 8.5 分钟,不是整 10 分钟);
  · 没有基线 / 基线太旧 → **绝不等满用户要的时长**,现场采一小段(见 `MAX_LIVE_SAMPLE_S`)、
    如实标明这是最近多少秒,并留下基线 —— 下次问同样的问题就真能回看了。
  · 确实要盯着看一段时间,用 `--live-sample`(显式阻塞,有上限)。

**两种取法,对应两个真实问题:**
  · `--window 10m`  回看最近十分钟(基线优先,不阻塞)。
  · `--since-last`  与上一次运行留下的基线相减 → 「距上次巡检」。几乎零代价,适合日常巡检。
    基线由每次运行自动留下(与报告存档同一个目录),所以越跑越有用。

**三件必须说清、不能静默处理的事:**
  ① 窗口里**新出现**的语句,基线里没有它 —— 全部耗时都算进窗口(不是丢掉)。
     这正是 gaussdb-wdr 那个内连接 bug 的教训:拿两端取交集,新上来的语句会被整批丢掉,
     而故障期间冲高的恰恰是新上来的那批。
  ② 累计值**变小**说明统计被清零过(`reset_unique_sql`)或实例重启过。这时相减得到的是
     负数,毫无意义 —— 必须明说「这个窗口的增量不可用」,不能把负数或 0 当结论。
  ③ 采样是 `ORDER BY 累计值 LIMIT n` 取回来的**前 n 条**,不是全量。一条冲高的语句,它的
     累计值里已经含了这次冲高,所以通常在前 n 条里;但理论上存在「累计很小、刚冲高一点」
     而落在 n 之外的情况。采样宽度因此要明显大于最终呈现条数(见 SAMPLE_WIDTH),
     并在报告里写明这是「从前 n 条里算的增量」。
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import waits

# 采样宽度:算增量时先取这么多条累计榜,再在它们之间算增量、重新排序。
# 比最终呈现的条数宽得多 —— 累计榜靠后的语句完全可能是本窗口的冲高元凶。
SAMPLE_WIDTH = 500

# 窗口长度的上下限。下限 10 秒:再短的话采样本身的抖动比信号还大;
# 上限 6 小时:再长就该用 WDR 快照,不该让一条命令挂在那里等。
MIN_WINDOW_S = 10
MAX_WINDOW_S = 6 * 3600

# 回看不了的时候,现场最多采多久。**这是个硬上限,不受用户问的时长影响** ——
# 问「最近 10 分钟」不代表愿意等 10 分钟,他想要的是答案,不是一个挂住的会话。
MAX_LIVE_SAMPLE_S = 60

# 用户显式说「盯着采 N 秒」(--live-sample)时的上限。比自动的宽,但仍然有限:
# 一条能在对话里阻塞几小时的命令是个陷阱,要更长就分两次用 --since-last。
MAX_LIVE_EXPLICIT_S = 300

# 基线可用的年龄区间,相对用户问的窗口。
#   太新(< MIN_WINDOW_S)  采样抖动比信号大,不如现场采;
#   太旧(> 问的 × 这个倍数) 它覆盖的时间远宽于用户问的,拿它相减又会把最近的冲高拉平 ——
#                          那正是要解决的问题,不能用它来「解决」。
BASELINE_MAX_RATIO = 3.0

_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$", re.I)
_UNIT_S = {"s": 1, "m": 60, "h": 3600}

BASELINE_NAME = "_baseline.json"

# 增量模式下能排序的维度。`reads` 不在列:取数脚本的 SELECT 里没有块数列,
# 没有原始值就算不出增量 —— 宁可明确拒绝,也不能悄悄按别的维度排。
# self / self_avg 按 CPU + 数据 IO(自身耗时)排,见 waits.py。
DELTA_SORT_KEYS = ("time", "avg", "calls", "rows") + waits.SELF_KEYS


class Counters(NamedTuple):
    """一条语句在某一刻的累计值。cpu_sec / io_sec 为 None = 那一刻的取数没有这两列(老白名单 / 老基线)。"""
    calls: int
    total_sec: float
    rows: int
    query: str = ""
    cpu_sec: Optional[float] = None
    io_sec: Optional[float] = None


class Sample(NamedTuple):
    """某一刻的一批累计值。at 是数据库时间的字符串,只用于呈现,不参与计算。"""
    at: str
    rows: Dict[str, Counters]


class DeltaRow(NamedTuple):
    sql_id: str
    query: str
    calls: int          # 窗口内调用次数
    total_sec: float    # 窗口内总耗时
    avg_ms: float       # 窗口内平均耗时
    rows: int           # 窗口内返回行数
    is_new: bool        # 窗口里才出现的语句(基线里没有)
    self_sec: Optional[float] = None   # 窗口内自身耗时(CPU + 数据 IO);任一端缺这两列时为 None


class DeltaResult(NamedTuple):
    rows: List[DeltaRow]
    span_s: float       # 两次采样相隔多久
    reset: bool         # 统计被清零 / 实例重启 → rows 无意义
    note: str           # 给报告照抄的一句话
    system_sql: Optional[dict] = None   # 排除内核系统 SQL 的结果(common/syssql Screening.as_dict);没筛 = None


def parse_window(text: str) -> int:
    """`10m` / `600s` / `1h` → 秒。不合法就抛 ValueError,不猜。"""
    m = _WINDOW_RE.match(text or "")
    if not m:
        raise ValueError("窗口要写成 <数字><s|m|h>,例如 10m、600s、1h,给的是 %r" % (text,))
    n, unit = int(m.group(1)), m.group(2).lower()
    secs = n * _UNIT_S[unit]
    if secs < MIN_WINDOW_S or secs > MAX_WINDOW_S:
        raise ValueError("窗口要在 %d 秒 ~ %d 小时之间,给的是 %s"
                         % (MIN_WINDOW_S, MAX_WINDOW_S // 3600, text))
    return secs


class Source(NamedTuple):
    """「最近 N 分钟」这个问题该怎么回答。

    kind   "baseline" 拿已有基线相减(不等) / "live" 现场采一小段(等 wait_s 秒)
    span_s 结果实际覆盖多长时间 —— 呈现时必须用这个数,不是用户问的那个
    wait_s 这次要阻塞多久(baseline 时为 0)
    note   给用户看的一句话:为什么是这个口径
    """
    kind: str
    span_s: float
    wait_s: float
    note: str


def choose_source(requested_s: int, baseline_age_s: Optional[float],
                  live_cap_s: int = MAX_LIVE_SAMPLE_S) -> Source:
    """回看最近 requested_s 秒:用基线,还是现场采一小段?

    **绝不返回「等满 requested_s」** —— 用户问的是过去,等待量不该随他问的时长增长。
    `baseline_age_s=None` 表示没有可用基线。
    """
    req = max(1, int(requested_s))
    cap = max(MIN_WINDOW_S, int(live_cap_s))

    if baseline_age_s is None:
        return Source("live", float(min(req, cap)), float(min(req, cap)),
                      "没有可用基线,累计计数器里没有历史,过去这段时间无法回看;"
                      "改为现场采 %d 秒给出这一小段的增量。本次已留下基线,"
                      "之后再问「最近这段时间」就能真的回看了。" % min(req, cap))

    age = max(0.0, float(baseline_age_s))
    if age < MIN_WINDOW_S:
        return Source("live", float(min(req, cap)), float(min(req, cap)),
                      "上次采样只过了 %d 秒,这么短的间隔里采样抖动比信号还大;"
                      "改为现场采 %d 秒。" % (int(age), min(req, cap)))

    if age > req * BASELINE_MAX_RATIO:
        return Source("live", float(min(req, cap)), float(min(req, cap)),
                      "最近的基线是 %s 前留下的,比你问的 %s 宽得多 —— 拿它相减算出来的是"
                      "这一整段的平均,最近的冲高照样会被拉平;改为现场采 %d 秒,"
                      "并留下新基线。要精确盯住某一段,用 --live-sample。"
                      % (human_span(age), human_span(req), min(req, cap)))

    return Source("baseline", age, 0.0,
                  "实际覆盖的是最近 %s(上次采样到现在),不是你问的 %s —— "
                  "累计计数器只能从已有基线往回算。" % (human_span(age), human_span(req)))


def human_span(secs: float) -> str:
    """秒 → 「45 秒」「8.5 分钟」「3.2 小时」。只用于呈现。"""
    s = max(0.0, float(secs))
    if s < 90:
        return "%d 秒" % int(round(s))
    if s < 90 * 60:
        return _trim(s / 60.0) + " 分钟"
    return _trim(s / 3600.0) + " 小时"


def _trim(v: float) -> str:
    """10.0 → 「10」,8.47 → 「8.5」。整数不带小数点 —— 「问的 10.0 分钟」读着像个测量值。"""
    return "%d" % int(round(v)) if abs(v - round(v)) < 0.05 else "%.1f" % v


def to_sample(at: str, rows) -> Sample:
    """把取数结果转成一刻的采样。rows 里每项要有 sql_id / calls / total_sec / rows / query。

    **同一个 sql_id 出现多行时求和,不是覆盖。** dbe_perf.statement 按执行用户拆行
    (og5 实测 217 行里 29 行重复)。原来写的是 `out[sid] = …`,后一行覆盖前一行 ——
    两次采样留下的可能是不同用户的那一行,于是算出凭空的增量,或者负数被判成
    「统计清零」。取数脚本现在已经在 SQL 里聚合了,这里再求和一次是兜底:
    老版本的白名单(现场还没重导)仍然会送来重复行。
    """
    out: Dict[str, Counters] = {}
    for r in rows:
        sid = str(getattr(r, "sql_id", "") or "")
        if not sid:
            continue
        c = Counters(calls=int(getattr(r, "calls", 0) or 0),
                     total_sec=float(getattr(r, "total_sec", 0.0) or 0.0),
                     rows=int(getattr(r, "rows", 0) or 0),
                     query=str(getattr(r, "query", "") or ""),
                     cpu_sec=getattr(r, "cpu_sec", None), io_sec=getattr(r, "io_sec", None))
        prev = out.get(sid)
        out[sid] = c if prev is None else Counters(
            calls=prev.calls + c.calls, total_sec=prev.total_sec + c.total_sec,
            rows=prev.rows + c.rows, query=prev.query or c.query,
            cpu_sec=_add(prev.cpu_sec, c.cpu_sec), io_sec=_add(prev.io_sec, c.io_sec))
    return Sample(at=at, rows=out)


def _add(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a + b


class _Row(NamedTuple):
    sql_id: str
    calls: int
    total_sec: float
    rows: int
    query: str
    cpu_sec: Optional[float] = None
    io_sec: Optional[float] = None


def _opt_float(v) -> Optional[float]:
    """可选列:缺列 / 空串 / 解析不了 → None(老白名单没有这一列,不能当 0)。"""
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def from_rows(at: str, rows) -> Sample:
    """取数脚本的原始行(按列名)→ 采样。topsql.top_sql 与 topsql.snap_statement 列名一致,
    两边都走这一个函数 —— 不会一边按列名、一边按位置取值。"""
    def num(v, cast):
        try:
            return cast(float(v))
        except (TypeError, ValueError):
            return cast(0)
    return to_sample(at, [_Row(sql_id=str(r.get("unique_sql_id") or ""),
                               calls=num(r.get("calls"), int),
                               total_sec=num(r.get("total_sec"), float),
                               rows=num(r.get("rows"), int),
                               query=str(r.get("query") or ""),
                               cpu_sec=_opt_float(r.get("cpu_sec")),
                               io_sec=_opt_float(r.get("io_sec")))
                          for r in rows or []])


def _reset_detected(before: Sample, after: Sample) -> bool:
    """两端都在的语句里,只要有一条累计值变小,就说明统计被清零过。

    判「有没有清零」只看两端都在的语句 —— 只在 after 里出现的语句本来就没有可比的前值,
    拿它判断会把「新语句」误当成「清零」。
    """
    for sid, a in after.rows.items():
        b = before.rows.get(sid)
        if b is None:
            continue
        if a.calls < b.calls or a.total_sec < b.total_sec - 1e-6:
            return True
    return False


def _self_delta(a: Counters, b: Optional[Counters]) -> Optional[float]:
    """窗口内自身耗时。新出现的语句整条计入;任一端缺 CPU / IO 列就是未知(不当 0 —— 当 0 会把它标成全在等)。"""
    after_self = waits.self_sec(a.cpu_sec, a.io_sec)
    if after_self is None or b is None:
        return after_self
    before_self = waits.self_sec(b.cpu_sec, b.io_sec)
    return None if before_self is None else max(0.0, after_self - before_self)


def _self_avg_ms(r: "DeltaRow") -> float:
    return r.self_sec * 1000.0 / r.calls if r.self_sec is not None and r.calls > 0 else 0.0


# 自身耗时未知的行排在最后,不和「自身耗时为 0」的混在一起
_DELTA_KEYS = {"time": lambda r: -r.total_sec,
               "avg": lambda r: -r.avg_ms,
               "calls": lambda r: -r.calls,
               "rows": lambda r: -r.rows,
               "self": lambda r: (r.self_sec is None, -(r.self_sec or 0.0)),
               "self_avg": lambda r: (r.self_sec is None, -_self_avg_ms(r))}


def diff(before: Sample, after: Sample, span_s: float, by: str = "time",
         limit: int = 10, screen: Optional[Callable] = None) -> DeltaResult:
    """两次采样相减,按 by 排序取前 limit 条。

    screen(排好序的全部行, limit) → Screening:排除内核系统 SQL(topsql.py 传入)。**在截前 limit 条之前筛**,
    否则排除掉几条之后榜就短了。不传 = 不筛(保持原行为)。
    """
    if by not in DELTA_SORT_KEYS:
        raise ValueError("增量模式不支持按 %s 排序(取数脚本里没有这一列的原始值);"
                         "可用:%s" % (by, "、".join(DELTA_SORT_KEYS)))
    if _reset_detected(before, after):
        return DeltaResult(rows=[], span_s=span_s, reset=True,
                           note=("两次采样之间累计值变小了 —— 统计被清零(reset_unique_sql)"
                                 "或实例重启过。这个窗口算不出增量,**不是「没有变化」**;"
                                 "请重新采一次,或改用 WDR 快照窗口。"))
    out: List[DeltaRow] = []
    for sid, a in after.rows.items():
        b = before.rows.get(sid)
        # 基线里没有 = 窗口内新出现的语句,全部计入(不是丢掉 —— 见模块 docstring ①)
        d_calls = a.calls - (b.calls if b else 0)
        d_total = a.total_sec - (b.total_sec if b else 0.0)
        d_rows = a.rows - (b.rows if b else 0)
        if d_calls <= 0 and d_total <= 1e-6:
            continue                      # 这个窗口里没动过
        avg_ms = (d_total * 1000.0 / d_calls) if d_calls > 0 else 0.0
        out.append(DeltaRow(sql_id=sid, query=a.query or (b.query if b else ""),
                            calls=max(0, d_calls), total_sec=max(0.0, d_total),
                            avg_ms=avg_ms, rows=max(0, d_rows), is_new=b is None,
                            self_sec=_self_delta(a, b)))
    if by in waits.SELF_KEYS and out and all(r.self_sec is None for r in out):
        raise ValueError("按自身耗时(CPU + 数据 IO)排不了:这个窗口有一端没有 CPU / 数据 IO 列 —— 起点是升级前"
                         "留下的基线,或白名单里的 topsql.top_sql / topsql.snap_statement 还没更新。"
                         "改用 --by time / avg,或等下一次采样之后再看")
    out.sort(key=_DELTA_KEYS[by])
    note = ("窗口增量:%s 秒内的变化量(不是累计值)。窗口内新出现的语句整条计入;"
            "增量从累计榜前 %d 条里算,极少数「累计很小、刚冲高」的语句可能不在其中。"
            % (int(span_s), SAMPLE_WIDTH))
    want = max(1, int(limit))
    if screen is None:
        return DeltaResult(rows=out[:want], span_s=span_s, reset=False, note=note)
    screening = screen(out, want)
    return DeltaResult(rows=list(screening.kept), span_s=span_s, reset=False, note=note,
                       system_sql=screening.as_dict())


# ---------------------------------------------------------------- 基线存取

def baseline_path(base_dir: Optional[pathlib.Path], instance_key: str) -> Optional[pathlib.Path]:
    """基线放在报告存档目录里:`<dir>/topsql/<实例键>/_baseline.json`。

    没设存档目录(非容器环境)就没有基线可言 —— 返回 None,`--since-last` 会如实报错,
    而不是悄悄退回累计模式。
    """
    if base_dir is None or not instance_key:
        return None
    return base_dir / "topsql" / instance_key / BASELINE_NAME


# 基线里留几刻采样。**只留一刻是不够的** —— 实测:连着问两次「最近十分钟」,
# 第二次读到的基线是第一次运行刚写下的(年龄 1 秒),于是又退回现场采样,
# 回看功能等于永远用不上。留一串采样,才能按用户问的时长挑一刻最合适的。
# 12 刻:按日常问几次的节奏,足够覆盖到半小时以上的回看。
HISTORY_MAX = 12


def save_baseline(path: Optional[pathlib.Path], sample: Sample) -> bool:
    """把这一刻**追加**进采样历史(不是覆盖),供以后按不同时长回看。

    **为什么是追加。** 每次运行都覆盖的话,存下来的永远是「刚刚」—— 而「最近十分钟」
    需要的是十分钟前那一刻。覆盖式基线只答得上「距上次」,答不上「最近某段时间」。

    只有最新那一刻保留 query 文本:呈现用的是本次现采的那一刻,历史几刻只参与相减,
    留着文本会让这个文件白白大十倍。失败只返回 False —— 存不下不该让这次的报告失败。
    """
    if path is None:
        return False
    try:
        hist = [s for s in load_history(path) if s.at != sample.at]
        hist.append(sample)
        hist = hist[-HISTORY_MAX:]
        payload = {"samples": [_sample_to_dict(s, keep_query=(i == len(hist) - 1))
                               for i, s in enumerate(hist)]}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _sample_to_dict(s: Sample, keep_query: bool = True) -> dict:
    return {"at": s.at,
            "rows": {sid: {"calls": c.calls, "total_sec": c.total_sec, "rows": c.rows,
                           **({"query": c.query} if keep_query else {}),
                           **({"cpu_sec": c.cpu_sec, "io_sec": c.io_sec} if c.cpu_sec is not None else {})}
                     for sid, c in s.rows.items()}}


def _sample_from_dict(d) -> Optional[Sample]:
    if not isinstance(d, dict):
        return None
    rows = d.get("rows")
    if not isinstance(rows, dict) or not rows:
        return None
    out: Dict[str, Counters] = {}
    for sid, v in rows.items():
        if not isinstance(v, dict):
            continue
        try:
            out[str(sid)] = Counters(calls=int(v.get("calls") or 0),
                                     total_sec=float(v.get("total_sec") or 0.0),
                                     rows=int(v.get("rows") or 0),
                                     query=str(v.get("query") or ""),
                                     # 升级前留下的基线没有这两列 —— 读成 None,不是 0
                                     cpu_sec=_opt_float(v.get("cpu_sec")),
                                     io_sec=_opt_float(v.get("io_sec")))
        except (TypeError, ValueError):
            continue
    if not out:
        return None
    return Sample(at=str(d.get("at") or ""), rows=out)


def load_history(path: Optional[pathlib.Path]) -> List[Sample]:
    """读采样历史,**由旧到新**。读不出来就返回空表(调用方如实说明没有基线)。

    也认旧的单刻格式(`{"at":…,"rows":…}`)—— 升级时已经存着的那一份不该被丢掉。
    """
    if path is None or not path.exists():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(d, dict):
        return []
    raw = d.get("samples")
    if isinstance(raw, list):
        out = [s for s in (_sample_from_dict(x) for x in raw) if s is not None]
        # **按时刻排序,不信文件里的顺序。** 两次运行的写入顺序不一定就是时间顺序
        # (实测存出过 123517 / 123516 / 123617 这样的历史),而 `--since-last` 取的是
        # 「最后一刻」—— 不排序的话它可能拿到的不是最新那刻,算出来的增量方向都是错的。
        # utc_stamp(`YYYYMMDDTHHMMSSZ`)按字典序排就是按时间序。
        out.sort(key=lambda s: s.at)
        return out[-HISTORY_MAX:]
    one = _sample_from_dict(d)          # 旧格式:整个文件就是一刻
    return [one] if one is not None else []


def pick_baseline(samples: List[Sample], requested_s: int,
                  age_of) -> Tuple[Optional[Sample], Optional[float]]:
    """从历史里挑一刻最适合回答「最近 requested_s 秒」的采样。

    `age_of(sample) -> 秒` 由调用方给(时间戳格式是调用方的事)。

    挑法:在**年龄落在可用区间内**的采样里,选年龄离 requested_s 最近的那一刻。
    挑不出来返回 (None, 最新那刻的年龄) —— 把年龄也带回去,好让 choose_source
    说出「太新」还是「太旧」这两种不同的原因。
    """
    aged = []
    for s in samples or []:
        a = age_of(s)
        if a is None:
            continue
        aged.append((max(0.0, float(a)), s))
    if not aged:
        return None, None
    usable = [(a, s) for a, s in aged
              if a >= MIN_WINDOW_S and a <= requested_s * BASELINE_MAX_RATIO]
    if not usable:
        newest = min(aged, key=lambda t: t[0])[0]
        return None, newest
    a, s = min(usable, key=lambda t: abs(t[0] - requested_s))
    return s, a


def load_baseline(path: Optional[pathlib.Path]) -> Tuple[Optional[Sample], str]:
    """读**最新那一刻**采样(`--since-last` 用)。返回 (采样, 读不到的原因)。

    按时长回看用 `load_history` + `pick_baseline`:那边要的是「十分钟前那一刻」,
    不是「最近那一刻」。
    """
    if path is None:
        return None, "没有报告存档目录,无法保存/读取基线(--since-last 只在容器环境可用)"
    if not path.exists():
        return None, "还没有基线 —— 这是第一次对这个实例采样,本次会留下基线,下次就能算增量"
    hist = load_history(path)
    if not hist:
        return None, "基线文件里没有可用数据,本次重新留一份"
    return hist[-1], ""
