"""DB time 分解与判定 —— 纯函数，不连库。

**为什么这里只有平铺、没有两层树：** 设计文档原打算把 DB_TIME 画成两层树
（解析阶段 + 执行阶段，执行阶段下面再挂 CPU/IO/NET）。`tools/probe_dbtime_
containment.py` 用 5 个真实快照窗口实测过这两条包含关系：

  - EXECUTION_TIME+PARSE_TIME+PLAN_TIME+REWRITE_TIME<=DB_TIME —— 5/5 窗口成立
  - CPU_TIME+DATA_IO_TIME+NET_SEND_TIME<=EXECUTION_TIME —— 5/5 窗口**不成立**，
    超出 EXECUTION_TIME 15%~24%，不是舍入误差

两条关系一真一假时画树最危险：读者会拿子项减父项去算"其余"，在不成立的
那一半上得到一个没有意义的数字，而且没有任何报错提示。所以哪怕只画验证
过的那一半（关系 2）也不画——`breakdown()` 把全部 9 项平铺返回，`Breakdown.
note` 里说明这个决定，不留给报告层去猜。

三个陷阱，本文件逐一覆盖：
  1. NULL 是空串不是 None；delta_us 缺值/为空不能当 0，DB_TIME 为 0 不能除零。
  2. 任一 delta 为负 → 跨了一次实例重启，这个窗口的比例是假的，`restarted=True`
     且不产生除 DBTIME_RESTART 外的任何 finding。
  3. waitevent.events 的 HAVING SUM(...)>0 会在重启窗口里悄悄滤掉负增量行——
     判定层不能反过来用"waits 是不是空"去猜有没有重启，只认 bd.restarted。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-waitevent" / "scripts"))

from common.finding import Severity  # noqa: E402
from dbtime import Breakdown, breakdown, judge_dbtime  # noqa: E402

# 一个"正常"窗口的十项基线，值全是字符串——协议把所有列值都渲染成字符串，
# NULL 渲染成空串，这里刻意不用 int，免得测试悄悄依赖了协议不保证的类型。
_BASE = {
    "DB_TIME": "1000000",
    "EXECUTION_TIME": "800000",
    "CPU_TIME": "500000",
    "DATA_IO_TIME": "100000",
    "NET_SEND_TIME": "50000",
    "PARSE_TIME": "10000",
    "PLAN_TIME": "20000",
    "REWRITE_TIME": "5000",
    "PL_EXECUTION_TIME": "40000",
    "PL_COMPILATION_TIME": "8000",
}


def _rows(overrides=None, drop=None):
    values = dict(_BASE)
    if overrides:
        values.update(overrides)
    if drop:
        for k in drop:
            values.pop(k, None)
    return [{"stat_name": k, "delta_us": v} for k, v in values.items()]


def _codes(findings):
    return [f.code for f in findings]


# --- breakdown(): 占比计算 ----------------------------------------------------

def test_normal_window_computes_shares_of_db_time():
    bd = breakdown(_rows())
    assert bd.db_time_us == 1000000
    assert bd.restarted is False
    shares = {name: share for name, _, share in bd.items}
    assert shares["CPU_TIME"] == pytest.approx(0.5)
    assert shares["DATA_IO_TIME"] == pytest.approx(0.1)
    assert shares["NET_SEND_TIME"] == pytest.approx(0.05)
    assert shares["EXECUTION_TIME"] == pytest.approx(0.8)
    # delta_us 本身也要能读回来，不只是比例
    deltas = {name: delta for name, delta, _ in bd.items}
    assert deltas["CPU_TIME"] == 500000


def test_breakdown_returns_a_note_explaining_the_flattening():
    """Breakdown 必须自带说明串——报告层不该重新猜"哪条关系验证过"。"""
    bd = breakdown(_rows())
    assert bd.note
    assert "DB_TIME" in bd.note


def test_items_are_flat_not_nested():
    """items 是平铺 list[tuple]，没有父子结构可言——这本身就是断言的一部分：
    9 项各自独立出现一次，互不嵌套、互不派生。"""
    bd = breakdown(_rows())
    names = [name for name, _, _ in bd.items]
    assert len(names) == len(set(names)) == 9
    assert "DB_TIME" not in names  # DB_TIME 是分母，不是 items 里的一项


# --- breakdown(): 除零与缺值 ---------------------------------------------------

def test_zero_db_time_returns_empty_items_without_dividing_by_zero():
    bd = breakdown(_rows({"DB_TIME": "0"}))
    assert bd.db_time_us == 0
    assert bd.items == []
    assert bd.restarted is False
    assert bd.note


def test_null_delta_is_not_silently_treated_as_zero():
    """NULL 在协议里是空串，不是 None——空串不能被 as_int 的默认值悄悄吃掉，
    必须报错，而不是把"取数出了问题"伪装成"这一项耗时为零"。"""
    with pytest.raises(ValueError):
        breakdown(_rows({"DATA_IO_TIME": ""}))


def test_missing_item_is_not_silently_treated_as_zero():
    """整行都没返回（不是 NULL，是缺行）同样不能当 0。"""
    with pytest.raises(ValueError):
        breakdown(_rows(drop=["NET_SEND_TIME"]))


# --- breakdown(): 跨实例重启 ---------------------------------------------------

def test_negative_delta_marks_restarted_and_produces_no_items():
    bd = breakdown(_rows({"CPU_TIME": "-500"}))
    assert bd.restarted is True
    assert bd.items == []
    assert bd.note


def test_restarted_note_is_distinct_from_the_normal_flattening_note():
    """重启窗口的 note 说的是"这个窗口不可用"，不是"关系没验证过"——
    两件事不能用同一句话带过，否则报告层分不清是数据坏了还是口径问题。"""
    normal = breakdown(_rows())
    restarted = breakdown(_rows({"DATA_IO_TIME": "-1"}))
    assert normal.note != restarted.note


# --- judge_dbtime(): 阈值判定 --------------------------------------------------

def test_data_io_time_at_35_percent_triggers_io_heavy_warn():
    bd = breakdown(_rows({
        "DATA_IO_TIME": "350000",   # 35%
        "CPU_TIME": "300000",       # 30%，低于 70% 阈值
        "NET_SEND_TIME": "20000",   # 2%，低于 30% 阈值
    }))
    findings = judge_dbtime(bd, [])
    assert _codes(findings) == ["DBTIME_IO_HEAVY"]
    f = findings[0]
    assert f.severity == Severity.WARN
    assert f.dimension == "DB Time"


def test_cpu_time_at_70_percent_triggers_cpu_heavy_notice():
    bd = breakdown(_rows({
        "CPU_TIME": "700000",     # 70%
        "DATA_IO_TIME": "10000",  # 1%
        "NET_SEND_TIME": "10000",
    }))
    findings = judge_dbtime(bd, [])
    assert _codes(findings) == ["DBTIME_CPU_HEAVY"]
    assert findings[0].severity == Severity.NOTICE


def test_net_send_time_at_30_percent_triggers_net_heavy_warn():
    bd = breakdown(_rows({
        "NET_SEND_TIME": "300000",  # 30%
        "CPU_TIME": "100000",
        "DATA_IO_TIME": "10000",
    }))
    findings = judge_dbtime(bd, [])
    assert _codes(findings) == ["DBTIME_NET_HEAVY"]
    assert findings[0].severity == Severity.WARN


def test_lock_event_at_15_percent_triggers_wait_lock_heavy():
    bd = breakdown(_rows({"CPU_TIME": "100000", "DATA_IO_TIME": "10000",
                          "NET_SEND_TIME": "10000"}))
    waits = [{"wait_class": "LOCK_EVENT", "event": "tuple",
              "waits": "12", "wait_us": "150000"}]  # 15%
    findings = judge_dbtime(bd, waits)
    assert _codes(findings) == ["WAIT_LOCK_HEAVY"]
    assert findings[0].severity == Severity.WARN


def test_lwlock_event_at_15_percent_triggers_wait_lwlock_heavy():
    bd = breakdown(_rows({"CPU_TIME": "100000", "DATA_IO_TIME": "10000",
                          "NET_SEND_TIME": "10000"}))
    waits = [{"wait_class": "LWLOCK_EVENT", "event": "buffer content",
              "waits": "40", "wait_us": "150000"}]  # 15%
    findings = judge_dbtime(bd, waits)
    assert _codes(findings) == ["WAIT_LWLOCK_HEAVY"]
    assert findings[0].severity == Severity.WARN


def test_other_wait_classes_do_not_count_toward_lock_or_lwlock():
    """IO_EVENT / STATUS 等其它 wait_class 不该被计进 LOCK/LWLOCK 的耗时——
    判定表里没有给 IO_EVENT 定阈值，混进来会把不相关的等待算成锁等待。"""
    bd = breakdown(_rows({"CPU_TIME": "100000", "DATA_IO_TIME": "10000",
                          "NET_SEND_TIME": "10000"}))
    waits = [{"wait_class": "IO_EVENT", "event": "data file read",
              "waits": "1000", "wait_us": "900000"}]
    findings = judge_dbtime(bd, waits)
    assert findings == []


def test_all_normal_returns_empty_findings_list_not_none():
    bd = breakdown(_rows())  # CPU 50%/IO 10%/NET 5%，全部低于阈值
    findings = judge_dbtime(bd, [])
    assert findings == []
    assert findings is not None


def test_zero_db_time_judge_returns_empty_list_without_dividing_by_zero():
    bd = breakdown(_rows({"DB_TIME": "0"}))
    waits = [{"wait_class": "LOCK_EVENT", "wait_us": "999"}]
    assert judge_dbtime(bd, waits) == []


# --- judge_dbtime(): 跨实例重启 → 只报 DBTIME_RESTART -------------------------

def test_restarted_window_produces_only_dbtime_restart():
    bd = breakdown(_rows({"CPU_TIME": "-1"}))
    findings = judge_dbtime(bd, [])
    assert _codes(findings) == ["DBTIME_RESTART"]
    assert findings[0].severity == Severity.NOTICE
    assert findings[0].dimension == "DB Time"


def test_restarted_window_suppresses_findings_even_if_waits_look_heavy():
    """陷阱 3：waitevent.events 继承了 wdr.waits 的
    `HAVING SUM(e.wt-b.wt) > 0`，重启窗口里这个谓词会把负增量行悄悄滤掉，
    于是 waits 只是"行变少了"，看不出重启——不能反过来拿 waits 的内容去猜
    有没有重启。这里即使 waits 摆出一份看起来严重超标的锁等待，重启窗口
    也必须只报 DBTIME_RESTART，其余判定一律不做。"""
    bd = breakdown(_rows({"NET_SEND_TIME": "-1"}))
    assert bd.restarted is True
    waits = [{"wait_class": "LOCK_EVENT", "event": "tuple",
              "waits": "999", "wait_us": "999999999"}]
    findings = judge_dbtime(bd, waits)
    assert _codes(findings) == ["DBTIME_RESTART"]


# --- judge_dbtime(): waits 侧的取值校验 ---------------------------------------

def test_null_wait_us_on_a_matched_class_is_not_silently_treated_as_zero():
    bd = breakdown(_rows())
    waits = [{"wait_class": "LOCK_EVENT", "wait_us": ""}]
    with pytest.raises(ValueError):
        judge_dbtime(bd, waits)


def test_empty_waits_on_a_live_window_is_not_an_error():
    """空的 waits 列表在**没有重启**的窗口里是合法结果（真的没有锁/轻量锁
    等待），不是取数出了问题——这一点与陷阱 3 的重启场景相反，必须分开看。"""
    bd = breakdown(_rows())
    assert judge_dbtime(bd, []) == []


# --- gate(): 空闲窗口不判比例,自相矛盾的比例不判 ---------------------------------
# 2026-10-18 那轮测试:空闲库上每小时 DB time 0.16–0.22 秒,LWLOCK 等待却「占 DB time 171.8%」,
# 5 个小时窗口各报一条 🟠,健康检查据此给了总体告警,模型还替这个数编了解释。

def _hourly_idle():
    """og5 空闲小时窗口的形状:DB time 0.2 秒,CPU 0.08 秒,LWLOCK 0.34 秒(后台 WAL 写入)。"""
    return breakdown(_rows({"DB_TIME": "200000", "EXECUTION_TIME": "150000", "CPU_TIME": "80000",
                            "DATA_IO_TIME": "0", "NET_SEND_TIME": "1000", "PARSE_TIME": "0",
                            "PLAN_TIME": "0", "REWRITE_TIME": "0", "PL_EXECUTION_TIME": "0",
                            "PL_COMPILATION_TIME": "0"}))


_IDLE_WAITS = [{"wait_class": "LWLOCK_EVENT", "event": "WALWriteLock", "waits": "20", "wait_us": "343600"}]


def test_an_idle_hour_judges_no_ratio_at_all():
    from dbtime import gate
    bd = _hourly_idle()
    # 不知道窗口多长时不做空闲判断(不猜),但第二道闸照样拦住:CPU 40% + LWLOCK 171.8% 超过 100%
    assert judge_dbtime(bd, _IDLE_WAITS) == []
    assert set(gate(bd, _IDLE_WAITS).contradictory) == {"LWLOCK_EVENT"}
    assert judge_dbtime(bd, _IDLE_WAITS, window_s=3600) == []
    g = gate(bd, _IDLE_WAITS, 3600)
    assert g.idle is True and g.aas == pytest.approx(0.2 / 3600)
    assert "不做比例判定" in g.notes[0] and "不是查过了没问题" in g.notes[0]


def test_a_wait_class_that_with_cpu_exceeds_db_time_is_not_judged():
    """不空闲,但 LWLOCK + CPU 超过 DB time:等待里混着后台线程,这个占比不可信。"""
    from dbtime import gate
    bd = breakdown(_rows({"DB_TIME": "600000000", "CPU_TIME": "400000000", "EXECUTION_TIME": "500000000"}))
    waits = [{"wait_class": "LWLOCK_EVENT", "event": "WALWriteLock", "waits": "9", "wait_us": "360000000"}]
    assert judge_dbtime(bd, waits, window_s=600) == []       # 平均活跃会话 1.0,不空闲
    g = gate(bd, waits, 600)
    assert set(g.contradictory) == {"LWLOCK_EVENT"}
    assert "自相矛盾、不可信" in g.notes[0] and "不要替它编解释" in g.notes[0]
    assert "126.7%" in g.notes[0]                             # 60% + 66.7%


def test_the_og5_fault_window_still_reports_its_lock_waits():
    """故障窗口:DB time 23510 秒、CPU 14.9%、LOCK 65.4% —— 合计 80%,照判,这才是真凶。"""
    bd = breakdown(_rows({"DB_TIME": "23510330000", "CPU_TIME": "3501100000", "EXECUTION_TIME": "20000000000",
                          "DATA_IO_TIME": "1000000000", "NET_SEND_TIME": "10000000"}))
    waits = [{"wait_class": "LOCK_EVENT", "event": "transactionid", "waits": "20171", "wait_us": "15369740000"},
             {"wait_class": "LWLOCK_EVENT", "event": "WALFlushWait", "waits": "785155", "wait_us": "623140000"}]
    found = judge_dbtime(bd, waits, window_s=1226)
    assert _codes(found) == ["WAIT_LOCK_HEAVY"]
    assert found[0].value == "65.4%"
