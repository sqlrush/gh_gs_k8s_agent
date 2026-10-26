"""WDR 的 DB time 取数与 Top SQL 分母 —— 2026-10-17 压测抓到的两个静默错误的回归测试。

**这两个错为什么要专门钉住:** 它们都不报错、不缺数据，只给一个小得离谱的数，
然后把负载维度判成「健康」。客户现场拿到的会是一份看起来很正常的报告。

  · DB time 原来取 `wdr.load_profile`(snap_summary_statement 两端内连接)。那张表是
    滚动的语句历史，窗口中途才出现的语句被内连接整批丢掉 —— 22 分钟窗口实测算出
    2.67 秒，而内核 DB_TIME 增量是 23518 秒。现在取 `waitevent.instance_time`。
  · Top SQL 的「占 DB time%」原来拿本榜单合计当分母，于是 Top N 截断得越狠占比越高：
    一条跑了 1 秒的 TRUNCATE 被算成占 37.7%，还触发了「单条 SQL 占比过高」告警。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-wdr" / "scripts"

# wdr 与 health / sqlreview 等 skill 的模块**同名不同内容**(model / thresholds / util /
# collectors …)。全量跑测试时先 import 到的是哪一个取决于顺序,所以这里照 test_wdr_units.py
# 的做法:先把同名模块从缓存里清掉,再把 wdr 的 scripts 目录放到 sys.path 最前面。
for _m in ("model", "thresholds", "util", "collectors", "report",
           "native", "snaps", "interp", "recheck", "finalreport", "kbrefs", "appendix", "mdreport", "render", "ansi"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors as C                                   # noqa: E402
import model                                             # noqa: E402
import thresholds                                        # noqa: E402
from model import DimResult, Evidence, Options, Severity  # noqa: E402


class Runner:
    """按脚本名给预设结果;值一律是字符串 —— 协议回来的就是字符串。"""

    def __init__(self, **by_name):
        self.by_name = by_name
        self.calls = []

    def run(self, name, params=None):
        self.calls.append(name)
        out = self.by_name.get(name)
        if isinstance(out, Exception):
            raise out
        if out is None:
            raise AssertionError("测试没给 %s 的返回值" % name)
        return out


def _it(db_time, cpu_time, extra=()):
    """waitevent.instance_time 的行。十项里本模块只用两项,其余项不影响。"""
    rows = [{"stat_name": "DB_TIME", "delta_us": str(db_time)},
            {"stat_name": "CPU_TIME", "delta_us": str(cpu_time)}]
    rows.extend(extra)
    return rows


def _opt(**kw):
    o = Options(begin=2981, end=2987, top=10, thresholds=thresholds.default_thresholds())
    for k, v in kw.items():
        setattr(o, k, v)
    return o


# ---------------------------------------------------------------- DB time 取数

def test_window_db_time_reads_the_instance_time_counters():
    r = Runner(**{"waitevent.instance_time": _it(23_517_792_505, 3_506_381_360)})
    db, cpu, why = C.window_db_time(r, _opt())
    assert why == ""
    assert db == 23_517_792_505 and cpu == 3_506_381_360
    assert r.calls == ["waitevent.instance_time"], "必须走 instance_time,不能再用 wdr.load_profile"


def test_window_db_time_refuses_to_treat_a_null_delta_as_zero():
    """空值是「没取到」,不是「恰好为零」—— 当 0 处理就会报出一个假的轻负载。"""
    r = Runner(**{"waitevent.instance_time": [
        {"stat_name": "DB_TIME", "delta_us": ""},
        {"stat_name": "CPU_TIME", "delta_us": "123"}]})
    db, cpu, why = C.window_db_time(r, _opt())
    assert "空值" in why and db == 0


def test_window_db_time_reports_a_restart_instead_of_a_negative_number():
    """跨实例重启时计数器清零,增量为负 —— 这个窗口的算术结果是假的。"""
    r = Runner(**{"waitevent.instance_time": _it(-5000, 10)})
    db, cpu, why = C.window_db_time(r, _opt())
    assert "重启" in why and db == 0


def test_window_db_time_reports_missing_items():
    r = Runner(**{"waitevent.instance_time": [{"stat_name": "CPU_TIME", "delta_us": "9"}]})
    db, cpu, why = C.window_db_time(r, _opt())
    assert "DB_TIME" in why


def test_load_profile_degrades_when_db_time_is_unusable():
    """取不到 DB time 时整个维度降级,不能拿 0 当结论往下算。"""
    r = Runner(**{"waitevent.instance_time": _it(-1, -1)})
    d = C.collect_loadprofile(r, _opt())
    assert d.available is False and "重启" in d.note


def test_load_profile_publishes_db_time_for_top_sql():
    opt = _opt()
    r = Runner(**{"waitevent.instance_time": _it(23_000_000_000, 3_000_000_000),
                  "wdr.db_summary": [{"xact_commit": "1103253", "blks_read": "2037610",
                                      "blks_hit": "1289933185"}]})
    d = C.collect_loadprofile(r, opt)
    assert d.available is True
    assert opt.db_time_us == 23_000_000_000, "Top SQL 要靠这个值做分母"
    assert "23000.0s" in d.headline or "23000" in d.headline


# ---------------------------------------------------------------- Top SQL 分母

_TOPSQL_ROWS = [
    {"sid": "3832110783", "query": "TRUNCATE fl.wal_log", "calls": "1",
     "elapsed_us": "1010000", "cpu_us": "180000", "spill_kb": "0", "phys_blocks": "0"},
    {"sid": "4280455894", "query": "select 1", "calls": "18",
     "elapsed_us": "240000", "cpu_us": "100000", "spill_kb": "0", "phys_blocks": "0"},
]


def test_top_sql_percentage_uses_the_real_db_time_as_denominator():
    """压测里的真实数字:1.01 秒的 TRUNCATE,在 23518 秒的窗口里占 0.004%,不是 37.7%。"""
    opt = _opt(db_time_us=23_517_792_505)
    d = C.collect_topsql(Runner(**{"wdr.top_sql": _TOPSQL_ROWS}), opt)
    assert "占DB time%" in d.headers
    pct = float(d.rows[0][d.headers.index("占DB time%")])
    assert pct < 0.01, "拿真实 DB time 做分母,这条应该低到可以忽略,实际 %s" % pct
    assert not [f for f in d.findings if f.code == "WDR_TOPSQL_DBTIME"], \
        "占比这么低不该再报「单条 SQL 占 DB time 过高」"


def test_top_sql_says_so_when_it_has_to_fall_back_to_the_list_total():
    """拿不到真实 DB time 时改列名说明分母变了,不假装还是占 DB time。"""
    d = C.collect_topsql(Runner(**{"wdr.top_sql": _TOPSQL_ROWS}), _opt(db_time_us=0))
    assert "占本榜合计%" in d.headers
    assert "占DB time%" not in d.headers


def test_top_sql_still_flags_a_genuinely_dominant_statement():
    """修完不能走到另一个极端:真占了大半个窗口的语句还得报出来。"""
    rows = [dict(_TOPSQL_ROWS[0], elapsed_us="8000000"), _TOPSQL_ROWS[1]]
    opt = _opt(db_time_us=10_000_000)        # 10 秒窗口里占了 8 秒
    d = C.collect_topsql(Runner(**{"wdr.top_sql": rows}), opt)
    codes = [f.code for f in d.findings]
    assert "WDR_TOPSQL_DBTIME" in codes


# ---------------------------------------------------------------- 跨维度兜底

def _ev_with_loadprofile():
    ev = Evidence()
    ev.dims = [DimResult(dimension=model.DIM_LOADPROFILE, available=True, headline="h")]
    return ev


def test_consistency_check_flags_db_time_smaller_than_total_waits():
    """2026-10-17 的真实组合:DB time 2.67 秒、总等待 15371 秒,两者并存就说明前者是错的。"""
    ev = _ev_with_loadprofile()
    C._check_consistency(ev, _opt(db_time_us=2_670_000, wait_total_us=15_371_740_000))
    codes = [f.code for f in ev.findings]
    assert "WDR_DBTIME_INCONSISTENT" in codes
    assert "不可信" in ev.dims[0].note


def test_consistency_check_stays_quiet_on_a_sane_window():
    """修好之后的真实数字:DB time 23518 秒、锁等待 15371 秒 —— 合理,不该报。"""
    ev = _ev_with_loadprofile()
    C._check_consistency(ev, _opt(db_time_us=23_517_792_505, wait_total_us=15_371_740_000))
    assert [f.code for f in ev.findings] == []


def test_consistency_check_needs_both_numbers():
    """有一边没取到就不做这个判断 —— 那边已经各自降级说明过了。"""
    ev = _ev_with_loadprofile()
    C._check_consistency(ev, _opt(db_time_us=0, wait_total_us=15_371_740_000))
    assert ev.findings == []


# ---------------------------------------------------------------- 注册脚本守卫

def test_top_sql_registry_uses_a_left_join():
    """内连接会把窗口中途才出现的语句整批丢掉(实测 147 条只剩 55 条)。
    这条守卫防的是有人「顺手」把它改回内连接。"""
    sql = (_ROOT / "scripts" / "registry" / "wdr" / "top_sql.yaml").read_text(encoding="utf-8")
    assert "LEFT JOIN b USING (sid)" in sql
    assert "FROM e JOIN b USING (sid)" not in sql
    assert sql.count("COALESCE(b.") >= 5, "b 侧每一列都要 COALESCE,否则相减得 NULL"


def test_load_profile_registry_is_marked_deprecated_and_unused():
    """那条脚本算出来的 DB time 是错的。留着只为兼容已下发的白名单,代码里不许再调。"""
    sql = (_ROOT / "scripts" / "registry" / "wdr" / "load_profile.yaml").read_text(encoding="utf-8")
    assert "已弃用" in sql
    code = (_SCRIPTS / "collectors.py").read_text(encoding="utf-8")
    assert 'runner.run("wdr.load_profile"' not in code

# ------------------------------------------------- 「数对了但维度还是绿灯」的两处

def test_load_profile_flags_a_wait_dominated_window():
    """DB time 很大而 CPU 只占一小块 —— 时间花在等待上,不能还显示「健康」。

    2026-10-17 实测的那组数:DB time 23517 秒、CPU 占 14.9%、锁等待 15371 秒,
    而 Load Profile 维度一条判定都没有,于是永远是 🟢健康。
    """
    opt = _opt(window_s=1320)
    r = Runner(**{"waitevent.instance_time": _it(23_517_792_505, 3_506_381_360),
                  "wdr.db_summary": [{"xact_commit": "1103253", "blks_read": "2037610",
                                      "blks_hit": "1289933185"}]})
    d = C.collect_loadprofile(r, opt)
    codes = [f.code for f in d.findings]
    assert "WDR_DBTIME_WAIT_HEAVY" in codes
    # 平均并发活跃会话要算出来摆在表里:23517 秒 ÷ 1320 秒 ≈ 17.8
    assert "平均并发活跃会话" in d.headers
    assert float(d.rows[0][d.headers.index("平均并发活跃会话")]) == pytest.approx(17.8, abs=0.2)
    assert "平均并发活跃会话 17.8" in d.headline


def test_load_profile_stays_quiet_when_cpu_dominates():
    """CPU 占大头说明真的在干活,不该报「时间花在等待上」。"""
    opt = _opt(window_s=1320)
    r = Runner(**{"waitevent.instance_time": _it(1_000_000_000, 900_000_000),
                  "wdr.db_summary": [{"xact_commit": "1", "blks_read": "1", "blks_hit": "1"}]})
    d = C.collect_loadprofile(r, opt)
    assert [f.code for f in d.findings] == []


def test_load_profile_omits_avg_sessions_when_window_length_unknown():
    """窗口长度没传进来时不要瞎算一个 0.0 摆在表里。"""
    r = Runner(**{"waitevent.instance_time": _it(1_000_000, 900_000),
                  "wdr.db_summary": [{"xact_commit": "1", "blks_read": "1", "blks_hit": "1"}]})
    d = C.collect_loadprofile(r, _opt(window_s=0))
    assert "平均并发活跃会话" not in d.headers


def test_top_sql_flags_long_elapsed_with_near_zero_cpu_below_the_share_threshold():
    """**这是修复 DB time 之后带出来的回归。**

    榜首等了 5963 秒、CPU 0.03 秒,占真实 DB time 25.4% —— 低于 30% 的占比门槛。
    原来「高耗时但 CPU 极低」这条判定挂在同一个门槛后面,于是分母修对之后整个
    Top SQL 维度反而一条发现都不出了。被锁住等几千秒,和它占 DB time 多少无关。
    """
    rows = [{"sid": "3167133288", "query": "UPDATE fl.acct SET bal = bal + ? WHERE id = ?",
             "calls": "76", "elapsed_us": "5963140000", "cpu_us": "30000",
             "spill_kb": "0", "phys_blocks": "33"},
            {"sid": "648622204", "query": "SELECT pg_advisory_lock(?)", "calls": "4",
             "elapsed_us": "2882260000", "cpu_us": "0", "spill_kb": "0", "phys_blocks": "1"}]
    opt = _opt(db_time_us=23_517_792_505)
    d = C.collect_topsql(Runner(**{"wdr.top_sql": rows}), opt)
    top_pct = float(d.rows[0][d.headers.index("占DB time%")])
    assert top_pct < opt.thresholds.top_sql_dbtime_notice, "前提:占比确实没够门槛"
    assert "WDR_TOPSQL_LOW_CPU" in [f.code for f in d.findings], \
        "等了 5963 秒而 CPU 近零,必须报出来"


def test_top_sql_does_not_flag_a_short_cpu_light_statement():
    """几百毫秒的小语句 CPU 占比低是正常的,不要报成被锁住。"""
    rows = [{"sid": "1", "query": "select 1", "calls": "1", "elapsed_us": "300000",
             "cpu_us": "1000", "spill_kb": "0", "phys_blocks": "0"}]
    d = C.collect_topsql(Runner(**{"wdr.top_sql": rows}), _opt(db_time_us=23_517_792_505))
    assert "WDR_TOPSQL_LOW_CPU" not in [f.code for f in d.findings]
