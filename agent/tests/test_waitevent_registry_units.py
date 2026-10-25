"""注册脚本的形态检查 —— 白名单模式下这两条是 waitevent 的全部取数来源。

waitevent 复用 wdr 的 `wdr.snapshots`（列快照）、`wdr.window`（窗口起止），
不重复写；这里只覆盖 wdr 没有的两样：

  instance_time —— 窗口内 DB time 十项时间模型的增量（wdr 的 registry 里
                    没有任何 instance_time 查询）
  events        —— 等待事件下钻到具体 event（wdr.waits 只聚合到 wait_class，
                    答不了「哪个具体等待事件最费时间」）

STATUS/NONE 排除是本模块测试的重点：STATUS（等客户端发命令的空闲时间）
实测单项累计 681262104468 us，比其余全部等待事件加起来还高三个数量级。
不排除，聚合结果会变成「99.9% 花在 STATUS」——技术上不算错，但没用且
误导。wdr.waits 已经排除，events.yaml 必须用完全相同的谓词，两边才不会
对同一个窗口给出不同的「等待事件花了多少时间」。
"""
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_REG = _ROOT / "scripts" / "registry"

from common.grmp.script import load_script  # noqa: E402


@pytest.mark.parametrize("rel,name", [
    ("waitevent/instance_time.yaml", "waitevent.instance_time"),
    ("waitevent/events.yaml", "waitevent.events"),
])
def test_script_loads_and_is_readonly(rel, name):
    rec = load_script(_REG / rel)
    assert rec.script_name == name
    assert rec.readonly is True, "%s 不是只读 —— 诊断脚本不该能写" % rel


def test_instance_time_reads_snap_global_instance_time_with_two_snapshot_ids():
    """时间模型的数据源只有一个视图，窗口两端各取一次快照。"""
    sql = load_script(_REG / "waitevent/instance_time.yaml").script_content
    assert "snapshot.snap_global_instance_time" in sql
    assert "{{b}}" in sql
    assert "{{e}}" in sql


def test_instance_time_subtracts_later_minus_earlier():
    """snap_value 是累计计数器，窗口值必须是「后一快照 − 前一快照」的减法，
    不能直接读某一个快照的值当窗口成本（那是从实例启动到那一刻的全部累计，
    不是这个窗口的成本）。两个 CTE（或一次自连接）各自锚定一个快照 id，
    是能做减法的前提 —— 没有这个结构，SQL 里出现的 "-" 也可能只是别的算式。

    方向也要钉死，不能只查"有没有减法"：这条脚本里 b/e 两个 CTE 分别锚定
    起始/结束快照、结果列叫 e.v/b.v，方向必须是 e.v - b.v（后减前）。
    反过来写成 b.v - e.v（前减后）不会报错、也不会必然出现负数触发"跨了
    实例重启"的告警（e.v > b.v 时 b.v-e.v 只是变成负数，恰好会被误判成
    重启），而是安安静静把全部十项的正负号倒过来 —— 光查字符串里有没有
    "-" 号截不出这种回归。"""
    sql = load_script(_REG / "waitevent/instance_time.yaml").script_content
    assert "-" in sql, "instance_time.yaml 里看不到减法，snap_value 是累计量，" \
                        "直接返回单个快照的值会把「全量累计」当成「窗口成本」"
    cte_count = sql.upper().count(" AS (SELECT") + sql.upper().count(") AS (\nSELECT")
    self_join = sql.count("JOIN") >= 1 and sql.lower().count("snap_global_instance_time") >= 2
    assert cte_count >= 2 or self_join, \
        "instance_time.yaml 既没有两个 CTE 也没有自连接 —— 减法两边必须" \
        "各自先按 {{b}}/{{e}} 锚定一个快照，否则减出来的数字没有意义"
    assert re.search(r"e\.v\s*-\s*b\.v", sql), \
        "delta_us 的减法方向不对（或者列/别名变了）—— 必须是 e.v - b.v，" \
        "e 是结束快照、b 是起始快照，反过来会把十项的正负号全部倒转"
    assert not re.search(r"b\.v\s*-\s*e\.v", sql), \
        "出现了 b.v - e.v（前减后）—— 会静默倒转全部十项的正负号"


def test_instance_time_returns_the_columns_the_report_needs():
    """列名是契约：少一列，后续报告会静默漏掉一格，而不是报错。"""
    sql = load_script(_REG / "waitevent/instance_time.yaml").script_content
    for col in ("stat_name", "delta_us"):
        assert col in sql, "instance_time.yaml 少了列 %s" % col


def test_events_excludes_status_and_none_with_the_same_predicate_as_wdr_waits():
    """STATUS/wait cmd 是等客户端发命令的空闲时间，不是工作耗时；实测单项
    累计 681262104468 us，三个数量级压过其余全部等待事件之和。不排除，
    「DB time 花在哪个等待事件上」这个问题的答案会被淹没成「几乎全在 STATUS」，
    没用且误导。谓词必须与 wdr.waits 完全一致的写法（upper(...) NOT IN
    ('STATUS','NONE')），否则两个 skill 对同一个窗口能算出不同答案。"""
    sql = load_script(_REG / "waitevent/events.yaml").script_content
    assert "upper(e.wait_class) NOT IN ('STATUS','NONE')" in sql, \
        "events.yaml 排除 STATUS/NONE 的谓词必须与 wdr.waits 逐字一致"


def test_events_returns_event_column_for_drill_down():
    """wdr.waits 只 GROUP BY 到 wait_class；waitevent.events 存在的意义就是
    多下钻一层到具体 event，所以 event 必须出现在 SELECT 列表和「下钻聚合」
    那一层 GROUP BY 里，不能只在 WHERE/JOIN 条件里出现。

    这条 SQL 里 GROUP BY 出现三次：两个 CTE 各按 snapshot_id 聚合一次
    （GROUP BY snap_type, snap_event），外层下钻聚合一次
    （GROUP BY e.wait_class, e.event）。必须精确定位到**最后**那一次 ——
    用 split(..., 1) 取第一次出现的话，拿到的是 CTE 内部那句
    "GROUP BY snap_type, snap_event"，而 "SNAP_EVENT" 这个列名字面上就
    含有子串 "EVENT"，会让检查在真正回归时（外层 GROUP BY 被人改回只剩
    e.wait_class，这条脚本退化成 wdr.waits 的重复实现）仍然"碰巧"通过。"""
    sql = load_script(_REG / "waitevent/events.yaml").script_content
    for col in ("wait_class", "event", "waits", "wait_us"):
        assert col in sql, "events.yaml 少了列 %s" % col
    upper = sql.upper()
    assert upper.count("GROUP BY") >= 3, \
        "events.yaml 结构变了：两个 CTE + 一次外层下钻聚合应各有一次 GROUP BY"
    outer_group_by = upper.rsplit("GROUP BY", 1)[-1].splitlines()[0]
    assert re.search(r"\bEVENT\b", outer_group_by), \
        "外层（下钻）聚合子句里没有 event —— 这条脚本存在的唯一理由就是比" \
        "wdr.waits 多下钻一层到具体 event，退化成只按 wait_class 聚合会让" \
        "它变成 wdr.waits 的重复实现"
