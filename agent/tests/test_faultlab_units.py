"""faultlab 的纯函数单测:排期预算、慢 SQL 规模校验、残留登记簿、报告渲染、分区 DDL。

**不连库。** 连库的部分(造故障、观测)靠 `faultlab.py run` 自己的自证(probe)与
`expected.yaml` 的人工核对,不在单测里 —— 单测要能在任何机器上随全量测试一起跑。

这里钉的都是「改坏了不会立刻被发现」的地方:
  · 预期发现矩阵 expected.yaml 读得通、每项都有 actual、编号与排期表一一对应(它是人手填的);
  · 排期表改完之后 CPU 预算是不是还在上限内(手写的排期很容易不小心排满);
  · 规模 cap 直接拼进 SQL,校验必须挡住非整数;
  · 残留登记簿是清理的依据,加了又撤的抵消逻辑错了会漏清;
  · 库对象名拼进 DDL,必须挡住奇怪的名字。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LAB = _ROOT / "tools" / "faultlab"
sys.path.insert(0, str(_LAB))

from lab import config as C                       # noqa: E402
from lab import expected, prepare, report, state  # noqa: E402
from lab.cleanup import _safe                     # noqa: E402
from lab.scenarios import ALL as REGISTERED       # noqa: E402
from lab.scenarios import slow                    # noqa: E402


# ---------------------------------------------------------------- 排期表

def test_every_scheduled_scenario_has_an_implementation():
    """排期表里的每个编号都要有实现 —— 少一个就是「这个场景从此没跑过」,
    而终端上什么都看不出来。"""
    missing = [s.code for s in C.ALL_SCENARIOS if s.code not in REGISTERED]
    assert not missing, "排期表里有 %s,但 scenarios/ 下没有实现" % missing


def test_cpu_budget_stays_within_the_limit():
    budget = C.cpu_budget(C.ALL_SCENARIOS)
    assert budget["peak"] <= budget["limit"], (
        "同时吃 CPU 的会话峰值 %d 超过上限 %d,og5 只有 4 个核" % (budget["peak"], budget["limit"]))


def test_cpu_budget_counts_only_overlapping_scenarios():
    """预算算的是**同时**在跑的,不是总和 —— 依次上的两个场景不该被算成两份。"""
    a = C.Scenario("A", "slow", "", 1, 1, 0, 100)
    b = C.Scenario("B", "slow", "", 1, 1, 100, 200)     # 紧接着,不重叠
    assert C.cpu_budget((a, b))["peak"] == 1
    c = C.Scenario("C", "slow", "", 1, 1, 50, 200)      # 与 a 重叠
    assert C.cpu_budget((a, c))["peak"] == 2


def test_each_lwlock_slot_sits_inside_one_snapshot_window():
    """要钉的是**意图**:每个快照窗口里只有一种轻量锁争用,WDR 的多窗口分解才看得出
    「这 4 分钟是谁在争、下 4 分钟换成了谁」。

    所以判的是「这一段不跨快照边界」,不是「起点正好等于边界」—— L1 故意比窗口起点
    晚 30 秒开始(让基线那个快照是干净的),它仍然完整落在第一个窗口里。
    """
    seen = {}
    for s in C.LWLOCK:
        end = s.stop_s or C.WINDOW_S
        first = s.start_s // C.SNAPSHOT_EVERY_S
        last = (end - 1) // C.SNAPSHOT_EVERY_S
        assert first == last, "%s(%d~%d 秒)跨了快照边界" % (s.code, s.start_s, end)
        assert first not in seen, "%s 与 %s 落在同一个快照窗口里" % (s.code, seen[first])
        seen[first] = s.code


def test_lwlock_slots_do_not_overlap_each_other():
    slots = sorted(((s.start_s, s.stop_s or C.WINDOW_S, s.code) for s in C.LWLOCK))
    for (a0, a1, ca), (b0, b1, cb) in zip(slots, slots[1:]):
        assert a1 <= b0, "%s 与 %s 的时间段重叠了" % (ca, cb)


def test_scenario_codes_are_unique():
    codes = [s.code for s in C.ALL_SCENARIOS]
    assert len(codes) == len(set(codes)), "排期表里有重复编号:%s" % codes


def test_by_group_filters_and_rejects_unknown_names():
    assert {s.group for s in C.by_group(("lock",))} == {"lock"}
    assert C.by_group(()) == C.ALL_SCENARIOS
    with pytest.raises(ValueError):
        C.by_group(("nosuchgroup",))


def test_every_group_has_at_least_one_scenario():
    for g in C.GROUPS:
        assert C.by_group((g,)), "分类 %s 下一个场景都没有" % g


# ---------------------------------------------------------------- 慢 SQL 规模

@pytest.mark.parametrize("bad", [0, -1, 1.5, "3000", None, 10_000_001])
def test_sql_for_rejects_a_bad_cap(bad):
    """cap 直接拼进 SQL,这是唯一一处把外部数值拼进 SQL 的地方,必须挡住。"""
    from lab.db import LabError
    with pytest.raises(LabError):
        slow.sql_for("S3", bad)


def test_sql_for_substitutes_the_cap_and_leaves_no_placeholder():
    sql = slow.sql_for("S3", 1234)
    assert "1234" in sql
    assert "{cap}" not in sql and "{code}" not in sql


@pytest.mark.parametrize("code", ["S1", "S2", "S3", "S4", "S5", "S6", "S7"])
def test_the_sql_sent_to_the_database_carries_no_root_cause_comment(code):
    """注释写的是成因,发过去就进了语句统计 —— 模型会照抄它当结论(2026-10-18 测试)。"""
    sql = slow.sql_for(code, 1234)
    assert "--" not in sql and "成因" not in sql
    assert sql.lstrip().split()[0].upper() in ("SELECT", "WITH", "DO", "INSERT", "UPDATE", "DELETE")


def test_strip_comments_keeps_code_lines_intact():
    assert slow.strip_comments("-- why\n  -- more\nSELECT 1\n  FROM t -- not a full-line comment\n") == \
        "SELECT 1\n  FROM t -- not a full-line comment\n"


def test_sql_for_rejects_an_unknown_scenario():
    from lab.db import LabError
    with pytest.raises(LabError):
        slow.sql_for("S99", 100)


def test_every_slow_scenario_has_a_sql_file_and_a_spec():
    for s in C.SLOW:
        assert s.code in slow.SPECS, "%s 没有 spec" % s.code
        path = C.SQL_DIR / "slow" / slow.SPECS[s.code].sql
        assert path.exists(), "%s 的 SQL 文件不在:%s" % (s.code, path)


def test_slow_sql_files_join_at_least_five_tables():
    """「5 张表以上多层嵌套」是这批慢 SQL 的设计要求,别改着改着就简化没了。"""
    for code, spec in slow.SPECS.items():
        if code == "S7":                       # S7 是高频点查,只有两张表,设计如此
            continue
        text = (C.SQL_DIR / "slow" / spec.sql).read_text(encoding="utf-8")
        tables = {t for t in ("orders", "order_items", "customers", "payments", "shipments",
                              "regions", "channels", "promos", "warehouses", "couriers",
                              "products", "import_stage") if "fl." + t in text}
        assert len(tables) >= 5, "%s 只用到 %d 张表:%s" % (code, len(tables), sorted(tables))


def test_load_caps_survives_a_broken_file(tmp_path, monkeypatch):
    """校准文件坏了不该让运行失败 —— 退回默认规模,但要说一声。"""
    bad = tmp_path / "caps.json"
    bad.write_text("{这不是 json", encoding="utf-8")
    monkeypatch.setattr(slow, "CAPS_FILE", bad)
    caps = slow.load_caps()
    assert caps["S3"] == slow.SPECS["S3"].cap


def test_load_caps_ignores_nonsense_values(tmp_path, monkeypatch):
    f = tmp_path / "caps.json"
    f.write_text(json.dumps({"S3": -5, "S4": "big", "S5": 999, "NOPE": 1}), encoding="utf-8")
    monkeypatch.setattr(slow, "CAPS_FILE", f)
    caps = slow.load_caps()
    assert caps["S3"] == slow.SPECS["S3"].cap    # 负数被忽略
    assert caps["S4"] == slow.SPECS["S4"].cap    # 字符串被忽略
    assert caps["S5"] == 999                     # 正整数采纳
    assert "NOPE" not in caps


# ---------------------------------------------------------------- 残留登记簿

@pytest.fixture()
def lab_state(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    monkeypatch.setattr(C, "STATE_FILE", tmp_path / "state.json")
    state.clear()
    return tmp_path


def test_residues_round_trip(lab_state):
    state.add_residue("slot", "fl_slot_v4")
    state.add_residue("prepared_xact", "fl_orphan_k10")
    assert ("slot", "fl_slot_v4") in state.residues()
    state.drop_residue("slot", "fl_slot_v4")
    assert ("slot", "fl_slot_v4") not in state.residues()
    assert ("prepared_xact", "fl_orphan_k10") in state.residues()


def test_adding_the_same_residue_twice_is_one_item(lab_state):
    """K5 每一轮都会重建索引,登记两次不该变成要删两次。"""
    state.add_residue("index", "fl.idx_x")
    state.add_residue("index", "fl.idx_x")
    assert state.residues().count(("index", "fl.idx_x")) == 1


def test_unknown_residue_kind_is_refused(lab_state):
    with pytest.raises(ValueError):
        state.add_residue("什么东西", "x")


def test_a_broken_state_file_is_not_read_as_empty(lab_state):
    """登记簿坏了**不能**当成「没有残留」—— 那会让真实残留被永久忽略。"""
    (lab_state / "state.json").write_text("{坏掉的", encoding="utf-8")
    assert ("_broken", str(lab_state / "state.json")) in state.residues()


# ---------------------------------------------------------------- 库对象名

@pytest.mark.parametrize("ident", ["fl.payments", "fl.idx_fl_k5_items_note", "acct"])
def test_safe_accepts_plain_identifiers(ident):
    assert _safe(ident) == ident


@pytest.mark.parametrize("ident", ["", "fl.t; DROP TABLE x", 'fl."t"', "fl.t--", "fl t"])
def test_safe_rejects_anything_else(ident):
    from lab.db import LabError
    with pytest.raises(LabError):
        _safe(ident)


# ---------------------------------------------------------------- 分区 DDL

def test_partition_ddl_buckets_fit_inside_the_partitions():
    """第 i 个分区收 bucket < i,所以写入的 bucket 必须是 0 … parts-1。
    取 1 … parts 的话最大那个值落在所有分区之外,插入时报「没有匹配的分区」
    (2026-10-17 第一次 prepare 就是这么失败的)。"""
    stmts = prepare.partition_ddl(parts=4, rows=10)
    insert = next(s for s in stmts if s.startswith("INSERT"))
    assert "g % 4" in insert
    create = next(s for s in stmts if s.startswith("CREATE TABLE"))
    assert "VALUES LESS THAN (4)" in create
    assert "VALUES LESS THAN (5)" not in create


@pytest.mark.parametrize("parts", [0, 1, -3, 10_001, "1000", 2.5])
def test_partition_ddl_rejects_a_bad_partition_count(parts):
    from lab.db import LabError
    with pytest.raises(LabError):
        prepare.partition_ddl(parts=parts)


# ---------------------------------------------------------------- 报告

def test_report_renders_scenario_windows(tmp_path):
    rows = [
        {"offset_s": 0, "wall": "10:00:00", "db_time": "2026-10-17 10:00:00",
         "event": "开始", "detail": "窗口 1200 秒"},
        {"offset_s": 1, "wall": "10:00:01", "db_time": "", "event": "WDR 快照",
         "detail": "基线前 snap_id=2960"},
        {"offset_s": 20, "wall": "10:00:20", "db_time": "2026-10-17 10:00:20",
         "event": "起 K1", "detail": "行锁阻塞链"},
        {"offset_s": 300, "wall": "10:05:00", "db_time": "2026-10-17 10:05:00",
         "event": "停 K1", "detail": "按排期结束"},
        {"offset_s": 301, "wall": "10:05:01", "db_time": "", "event": "清理",
         "detail": '{"残留": "无"}'},
    ]
    f = tmp_path / "timeline.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    out = report.render(str(f))
    assert "K1" in out and "2960" in out
    assert "2026-10-17 10:00:20" in out and "2026-10-17 10:05:00" in out


def test_report_skips_a_truncated_last_line(tmp_path):
    """时间线是边跑边写的,进程被 kill 时最后一行可能只写了一半 ——
    报告不该因此整个读不出来。"""
    f = tmp_path / "timeline.jsonl"
    good = json.dumps({"offset_s": 0, "wall": "10:00:00", "event": "开始", "detail": ""},
                      ensure_ascii=False)
    f.write_text(good + "\n{\"offset_s\": 5, \"ev", encoding="utf-8")
    assert "faultlab 运行报告" in report.render(str(f))


def test_report_without_a_timeline_says_so(tmp_path):
    from lab.db import LabError
    with pytest.raises(LabError):
        report.render(str(tmp_path / "nope.jsonl"))


# ---------------------------------------------------------------- 预期发现矩阵(expected.yaml)

def test_expected_matrix_parses_and_every_item_has_an_actual_slot():
    """矩阵是人手填的:读得通、每项都有 actual(待填 = 空串)、编号与排期表一一对应。

    第一版里 S7 那一行的值以 ** 开头,整份文件解析不了 —— 一直没人发现,因为没有任何代码读它。
    """
    data = expected.load()
    assert expected.problems(data, [s.code for s in C.ALL_SCENARIOS]) == []
    assert all("actual" in s for s in data["scenarios"]) and all("actual" in c for c in data["cross_cutting"])
    assert {s["code"] for s in data["scenarios"]} == {s.code for s in C.ALL_SCENARIOS}


def test_expected_problems_catch_missing_or_malformed_actual_and_unregistered_codes():
    data = {"scenarios": [{"code": "S1", "title": "t", "signals": "s", "expect": ["x"]},
                          {"code": "S2", "title": "t", "signals": "s", "expect": ["x"], "actual": ["列表"]},
                          {"code": "S2", "title": "t", "signals": "s", "expect": ["x"], "actual": ""}],
            "cross_cutting": [{"项": "两张榜"}]}
    msgs = expected.problems(data, ["S1", "S2", "S9"])
    assert any("S1 缺 actual" in m for m in msgs)
    assert any("S2 的 actual 要写成一段文字" in m for m in msgs)
    assert any("S2 重复登记" in m for m in msgs)
    assert any("跨场景项 #1 缺 actual" in m for m in msgs)
    assert any("S9" in m and "没登记" in m for m in msgs)


def test_expected_progress_counts_filled_and_pending():
    data = {"scenarios": [{"code": "S1", "actual": "health 报了 ACTIVE_SQL_HOT"}, {"code": "S2", "actual": ""},
                          {"code": "S3", "actual": None}],
            "cross_cutting": [{"项": "两张榜", "actual": "  "}]}
    assert expected.progress(data) == (1, ["S2", "S3", "跨场景:两张榜"])


def test_expected_load_explains_an_unquoted_star(tmp_path):
    from lab.db import LabError
    bad = tmp_path / "expected.yaml"
    bad.write_text("scenarios:\n  - code: S7\n    expect:\n      - topsql: **总耗时**榜首\n    actual: \"\"\n",
                   encoding="utf-8")
    with pytest.raises(LabError, match="加引号"):
        expected.load(bad)


def test_expected_render_reports_progress_and_problems(tmp_path):
    ok_text, ok = expected.render()                     # 仓库里的矩阵:格式没问题(填了几项随测试进度变,不钉)
    assert ok and "格式问题" not in ok_text and "actual 已填" in ok_text
    f = tmp_path / "expected.yaml"
    f.write_text("scenarios:\n  - code: S1\n    title: t\n    signals: s\n    expect: [x]\n    actual: 报了\n",
                 encoding="utf-8")
    text, ok = expected.render(f)
    assert not ok and "已填 1、待填 0" in text and "排期表里有、矩阵里没登记" in text


# ---------------------------------------------------------------- 数据规模

def test_total_rows_stay_under_ten_million():
    """user 2026-10-17 的要求:og5 放不下太多测试数据,总行数控制在千万以内。"""
    total = sum(t.rows for t in C.TABLES) + C.IMPORT_ROWS
    assert total < 10_000_000, "造的数据总行数 %d 超过千万" % total


def test_analyze_list_excludes_the_stale_stats_table():
    """import_stage 永远不能被 ANALYZE —— 它的统计信息停在空表就是 S5 的成因。"""
    assert "import_stage" not in prepare.ANALYZE_TABLES


# ---------------------------------------------------------------- WDR 快照号(2026-10-21:时间线上的编号差 1)

class _FakeTl:
    def __init__(self):
        self.rows = []

    def add(self, event, detail="", db_time=""):
        self.rows.append((event, detail, db_time))


class _FakeConn:
    def one(self, sql, params=None):
        return "2026-10-21 10:00:05"


def _fake_omm(latest_rows, create_out="WDR snapshot request has been submitted\n"):
    """替身 omm_sql:按顺序回答「查最新快照」;create_wdr_snapshot 单独回答。记下收到的每段 SQL。"""
    calls, answers = [], list(latest_rows)

    def omm_sql(sql, timeout_s=60):
        calls.append(sql)
        if "create_wdr_snapshot" in sql:
            return create_out
        return answers.pop(0) if len(answers) > 1 else answers[0]
    return omm_sql, calls


@pytest.fixture
def snap_runner(monkeypatch):
    from lab import runner
    monkeypatch.setattr(runner, "SNAPSHOT_POLL_S", 0)
    return runner


def test_snapshot_waits_for_the_new_id_instead_of_taking_the_previous_one(snap_runner, monkeypatch):
    """快照是异步落表的:调用后第一次查还是旧号 2960,第二次才出现 2961——记的必须是 2961,并带上它的 start_ts。"""
    omm, calls = _fake_omm(["2960|2026-10-21 09:00:00\n",          # 调用前的最大号
                            "2960|2026-10-21 09:00:00\n",          # 调用后第一次:还没落表
                            "2961|2026-10-21 10:00:03\n"])         # 第二次:新快照出现
    monkeypatch.setattr(snap_runner, "omm_sql", omm)
    tl = _FakeTl()
    assert snap_runner._snapshot(_FakeConn(), tl, "基线前") == 2961
    assert tl.rows == [("WDR 快照", "基线前 start_ts=2026-10-21 10:00:03 snap_id=2961", "2026-10-21 10:00:05")]
    assert "create_wdr_snapshot" in calls[1] and len(calls) == 4      # 先查 max、再建、再轮询两次


def test_snapshot_timeout_records_unconfirmed_not_a_wrong_id(snap_runner, monkeypatch):
    omm, _ = _fake_omm(["2960|2026-10-21 09:00:00\n"])               # 一直是旧号
    monkeypatch.setattr(snap_runner, "omm_sql", omm)
    monkeypatch.setattr(snap_runner, "SNAPSHOT_WAIT_S", 0)
    tl = _FakeTl()
    assert snap_runner._snapshot(_FakeConn(), tl, "故障结束") is None
    (event, detail, _), = tl.rows
    assert event == "WDR 快照" and "编号未确认" in detail and detail.endswith("snap_id=未确认")
    assert "2960" in detail and "snap_id=2960" not in detail


def test_snapshot_error_is_reported_as_a_failure_not_waited_on(snap_runner, monkeypatch):
    omm, calls = _fake_omm(["2960|2026-10-21 09:00:00\n"],
                           create_out='ERROR:  WDR snapshot request can not be accepted, please enable "enable_wdr_snapshot".\n')
    monkeypatch.setattr(snap_runner, "omm_sql", omm)
    tl = _FakeTl()
    assert snap_runner._snapshot(_FakeConn(), tl, "基线前") is None
    assert tl.rows[0][0] == "WDR 快照失败" and "enable_wdr_snapshot" in tl.rows[0][1]
    assert len(calls) == 2                                            # 建快照报错就不再轮询


def test_snapshot_on_an_empty_snapshot_table_starts_from_zero(snap_runner, monkeypatch):
    omm, _ = _fake_omm(["\n", "1|2026-10-21 10:00:03\n"])
    monkeypatch.setattr(snap_runner, "omm_sql", omm)
    assert snap_runner._snapshot(_FakeConn(), _FakeTl(), "基线前") == 1


def test_report_lists_the_confirmed_id_and_flags_the_unconfirmed(tmp_path):
    rows = [
        {"offset_s": 0, "wall": "10:00:00", "db_time": "", "event": "开始", "detail": ""},
        {"offset_s": 1, "wall": "10:00:01", "db_time": "", "event": "WDR 快照",
         "detail": "基线前 start_ts=2026-10-21 10:00:01 snap_id=2961"},
        {"offset_s": 9, "wall": "10:00:09", "db_time": "", "event": "WDR 快照",
         "detail": "故障结束 编号未确认:调用后 15 秒内没出现大于 2961 的快照号 snap_id=未确认"},
    ]
    f = tmp_path / "timeline.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    assert "- WDR 快照:2961, 未确认" in report.render(str(f))


# ---------------------------------------------------------------- 2026-10-24 全量跑后的修复
#
# 那一轮暴露的工具问题:K3 停下后 ALTER 还挂在锁队列里,把 S5、status、explain、health 全堵住;
# status 自己没设等锁超时;L3 的自证把客户端空闲算成在等;清理对没加上的列也报「已删」;
# 以及 S2 / S5 / V4 / V6 / K2 / K5 / L2–L5 没按设计造出来。下面每条钉住一处修复。

from lab import cleanup, db, session, status            # noqa: E402
from lab.scenarios import lock as lockmod               # noqa: E402
from lab.scenarios import lwlock                        # noqa: E402
from lab.scenarios import vacuum as vacmod              # noqa: E402


def _meta(code):
    return next(s for s in C.ALL_SCENARIOS if s.code == code)


def _ctx(window_s=C.WINDOW_S):
    return session.Ctx(log=lambda m: None, window_s=window_s)


def test_lock_guard_follows_the_scenario_slot_not_the_window():
    """K3 的 ALTER 等锁最多等到它自己的时段结束后一分钟,而不是「窗口 + 10 分钟」。"""
    k3, k1 = _meta("K3"), _meta("K1")
    assert C.lock_guard_s(k3) == (k3.stop_s - k3.start_s) + C.LOCK_GUARD_MARGIN_S
    assert C.lock_guard_s(k1) == C.WINDOW_S - k1.start_s + C.LOCK_GUARD_MARGIN_S   # 跟窗口一起结束
    assert C.lock_guard_s(k3, window_s=600) == (600 - k3.start_s) + C.LOCK_GUARD_MARGIN_S
    assert C.lock_guard_s(None) == C.STATEMENT_TIMEOUT_S


def test_guard_sql_and_the_scenario_observer_maintenance_presets():
    sqls = db.guard_sql(db.Guards(30, 0, 3))
    assert "SET statement_timeout = '30s'" in sqls and "SET lockwait_timeout = '3s'" in sqls
    assert not any("idle_in_transaction" in s for s in sqls)
    assert not any("ALTER SYSTEM" in s.upper() for s in sqls)            # 全是会话级
    with pytest.raises(db.LabError):
        db.guard_sql(db.Guards(-1, 0, 0))
    g = db.resolve_guards(True, lockwait_s=190)
    assert g.lockwait_s == 190 and g.statement_s == C.STATEMENT_TIMEOUT_S
    assert db.resolve_guards(False) is None
    assert db.resolve_guards(db.OBSERVER_GUARDS) == db.OBSERVER_GUARDS
    assert 0 < db.OBSERVER_GUARDS.lockwait_s <= 5 and 0 < db.OBSERVER_GUARDS.statement_s <= 60
    assert db.MAINT_GUARDS.statement_s == 0 and db.MAINT_GUARDS.lockwait_s > 0  # VACUUM FULL 不限时长


def test_base_worker_hands_the_slot_based_lock_guard_to_the_session(monkeypatch):
    from lab.scenarios import base
    seen = {}

    class FakeWorker:
        def __init__(self, ctx, app, fn, **kw):
            seen.update(kw, app=app)

        def start(self):
            return self
    monkeypatch.setattr(base, "Worker", FakeWorker)
    base.Base(_ctx(), _meta("K3")).worker("ddl", lambda c, s: None)
    assert seen["lock_guard_s"] == C.lock_guard_s(_meta("K3")) and seen["app"] == "K3/ddl"


class _StopWorker:
    """替身 Worker:被「置位 / 取消 / 终止」里的哪一种叫醒,由 dies_on 决定。"""

    def __init__(self, app, drain=False, dies_on=("signal",), terminate=False):
        self.app, self.drain, self.dies_on, self.terminate = app, drain, dies_on, terminate
        self.alive, self.signalled = True, False

    def signal(self):
        self.signalled = True
        if "signal" in self.dies_on:
            self.alive = False

    def join(self, timeout_s):
        pass


def _killer(workers):
    calls = []

    def kill(app, action):
        calls.append((app, action))
        for w in workers:
            if w.app == app and action in w.dies_on:
                w.alive = False
        return 1
    return kill, calls


def _stop(workers, ctx, kill):
    return session.stop_workers(workers, ctx, killer=kill, grace_s=0, cancel_grace_s=0, join_s=0)


def test_stop_cancels_a_statement_stuck_in_a_lock_wait_and_leaves_idle_holders_alone():
    """K3:置停止位叫不醒排在锁队列里的 ALTER,要取消那条语句(2026-10-24 全量跑)。"""
    holder, ddl = _StopWorker("K3/longread"), _StopWorker("K3/ddl", dies_on=("cancel",))
    kill, calls = _killer([holder, ddl])
    assert _stop([holder, ddl], _ctx(), kill) == {"cancelled": 1, "terminated": 0, "left_running": 0}
    assert calls == [("K3/ddl", "cancel")]


def test_stop_escalates_to_terminate_when_cancel_does_not_help():
    w = _StopWorker("K5/ddl", dies_on=("terminate",))
    q = _StopWorker("S1/q", dies_on=("terminate",), terminate=True)    # 慢 SQL:被取消的语句也会进执行历史
    kill, calls = _killer([w, q])
    assert _stop([w, q], _ctx(), kill)["terminated"] == 2
    assert calls == [("K5/ddl", "cancel"), ("K5/ddl", "terminate"), ("S1/q", "terminate")]


def test_a_draining_slow_query_finishes_its_round_until_the_window_ends():
    """S3 的最后一轮不能在排期停时被打断 —— 打断了就进不了执行历史;窗口结束时照样取消。"""
    w = _StopWorker("S3/q", drain=True, dies_on=("cancel",))
    kill, calls = _killer([w])
    ctx = _ctx()
    assert _stop([w], ctx, kill)["left_running"] == 1 and w.signalled and calls == []
    ctx.stop.set()
    _stop([w], ctx, kill)
    assert calls == [("S3/q", "cancel")]


@pytest.mark.parametrize("text", ["canceling statement due to user request",
                                  "terminating connection due to administrator command",
                                  "server closed the connection unexpectedly"])
def test_errors_from_our_own_stop_are_not_counted_as_failures(text):
    assert session.stopped_on_purpose(text, stopping=True)
    assert not session.stopped_on_purpose(text, stopping=False)
    assert not session.stopped_on_purpose('relation "fl.nope" does not exist', stopping=True)


def test_a_failing_stop_is_logged_and_does_not_break_the_run():
    logged = []
    ctx = session.Ctx(log=logged.append)
    w = _StopWorker("K3/ddl", dies_on=())

    def broken(app, action):
        raise RuntimeError("could not connect to server")
    assert _stop([w], ctx, broken) == {"cancelled": 0, "terminated": 0, "left_running": 0}
    assert any("K3/ddl" in m and "失败" in m for m in logged)


@pytest.mark.parametrize("app", ["runner", "fl:status", "stop/x", "", "fl:", "K3/ddl'; --"])
def test_stop_targets_scenario_sessions_and_refuses_tool_connections_and_odd_names(app):
    for ok in ("K3/ddl", "fl:K3/ddl", "L5/read2", "D/x"):
        assert db.stoppable_app(ok).startswith(C.APP_PREFIX)
    with pytest.raises(db.LabError):
        db.stoppable_app(app)


def _fake(**methods):                  # 临时替身对象:_fake(query=lambda sql, args=None: …)
    return type("Fake", (), {k: staticmethod(v) for k, v in methods.items()})()


def test_stop_sessions_matches_the_exact_name_and_uses_the_session_aware_call():
    calls = []
    f = _fake(query=lambda sql, args=None: calls.append((sql, args)) or [(11, 22)],
              one=lambda sql, args=None: calls.append((sql, args)) or True)
    assert db.stop_sessions(f, "K3/ddl", "cancel") == 1
    sql, args = calls[0]
    assert args == ("fl:K3/ddl",) and "application_name = %s" in sql and "LIKE" not in sql
    assert "state = 'active'" in sql                    # 空闲会话 cancel 了也返回 true,不算「取消了一条」
    assert calls[1] == ("SELECT pg_cancel_session(%s, %s)", (11, 22))
    db.stop_sessions(f, "K3/ddl", "terminate")
    assert "state = 'active'" not in calls[2][0] and calls[3][0].startswith("SELECT pg_terminate_session")


def test_status_reports_a_blocked_item_instead_of_hanging():
    def blocked(sql, args=()):
        raise RuntimeError("Lock wait timeout: thread 1 waiting for AccessShareLock on relation 1 after 3000 ms")
    out = status._rows(_fake(query=blocked, rollback=lambda: None), "死元组最多的表", "SELECT 1")
    assert "这一项没查出来" in out and "等锁超过" in out


def test_status_connects_as_an_observer(monkeypatch):
    seen = {}

    def fake_conn(app, guards=True, **kw):
        seen["guards"] = guards
        return _fake(query=lambda sql, args=(): [], rollback=lambda: None, close=lambda: None)
    monkeypatch.setattr(status, "Conn", fake_conn)
    status.snapshot_text()
    assert seen["guards"] == db.OBSERVER_GUARDS


def test_lwlock_probe_does_not_count_an_idle_client_as_waiting():
    """L3 那轮:wait cmd(客户端空闲)×31 被算成「在等」,一段没争起来的轻量锁看着像在争。"""
    idle = lwlock.summarize({"wait cmd/wait cmd": 31, "none/none": 25, "flush data/flush data": 4}, 20)
    assert "真在等的 4 次" in idle and "轻量锁 0 次" in idle
    busy = lwlock.summarize({"acquire lwlock/WALFlushWait": 28, "none/none": 32}, 20)
    assert "真在等的 28 次" in busy and "轻量锁 28 次" in busy


class _CatalogConn:                    # 替身连接:按名字回答「在不在」;exec 当作撤成功了

    def __init__(self, present):
        self.present, self.ddl = set(present), []

    def one(self, sql, args=None):
        return 1 if args and args[-1] in self.present else 0

    def exec(self, sql, args=None):
        self.ddl.append(sql)
        self.present.clear()


@pytest.mark.parametrize("kind,name", [("column", "fl.payments.fl_k3_tmp"), ("index", "fl.idx_fl_k5_items_note"),
                                       ("slot", "fl_slot_v4"), ("prepared_xact", "fl_xmin_v3")])
def test_cleanup_reports_absent_residues_as_absent(kind, name):
    """K3 的 ALTER 被取消、列根本没加上,清理原先照样写「已删列」。"""
    c = _CatalogConn(())
    text, gone = cleanup._undo(c, kind, name)
    assert ("不在" in text or "不存在" in text) and "已删" not in text and gone and c.ddl == []


def test_cleanup_drops_a_real_column_and_says_so():
    c = _CatalogConn(("fl_k3_tmp",))
    assert cleanup._undo(c, "column", "fl.payments.fl_k3_tmp") == ("已删列 fl.payments.fl_k3_tmp", True)
    assert len(c.ddl) == 1


def test_cleanup_keeps_a_residue_on_the_books_when_it_is_still_there():
    class Stubborn(_CatalogConn):
        def exec(self, sql, args=None):
            self.ddl.append(sql)                        # 命令发了,东西还在
    text, gone = cleanup._undo(Stubborn(("fl_slot_v4",)), "slot", "fl_slot_v4")
    assert not gone and "人工" in text


def test_k2_only_touches_rows_no_other_scenario_uses_and_k5_builds_a_slow_index():
    """K2 原来从 1 号行扫起,卡在 K1 锁着的行上;K5 的普通索引 7 秒就建完。"""
    import re
    src = (C.ROOT / "lab" / "scenarios" / "deadlock.py").read_text(encoding="utf-8")
    d_ids = {int(x) for x in re.findall(r'(?:_upd\("acct", |id = |lock_parent\(|ins\()(\d+)', src)}
    others = d_ids | {1, 2, 3, 100, lockmod.ForeignKeyWait.PARENT}
    k2 = lockmod.BatchForUpdate
    assert k2.LO > max(others) and k2.HI <= 2000 and "acct_child" not in src.split("CHILD = ")[1]
    for n in range(30):
        lo, hi = k2.batch_range(n)
        assert k2.LO <= lo <= hi <= k2.HI and k2.LO <= k2.online_id(n) <= k2.HI
    sql = lockmod.IndexBuild.index_sql()
    assert sql.startswith("CREATE INDEX idx_fl_k5_items_note ON fl.order_items")
    assert "CONCURRENTLY" not in sql and "fl_slow_key(note, %d)" % C.K5_SLOW_ITERS in sql


@pytest.mark.parametrize("iters", [0, -1, 1.5, "25", 100_001])
def test_slow_key_expr_rejects_a_bad_iteration_count(iters):
    with pytest.raises(db.LabError):
        prepare.slow_key_expr("note", iters)


def test_check_does_not_demand_a_full_prepare_for_runtime_objects():
    from lab import bootstrap
    base = {t.name for t in C.TABLES if t.name not in C.RUNTIME_TABLES}
    text = bootstrap.data_state_text(base, 3000000)
    assert text.startswith("齐了") and "自动补建" in text and "prepare" not in text
    assert "要先跑 prepare" in bootstrap.data_state_text(base - {"orders"}, None)


def test_runtime_objects_use_ustore_only_when_available_and_the_v6_table_is_unlogged():
    with_u, without = prepare.runtime_ddl(True), prepare.runtime_ddl(False)
    assert "storage_type = ustore" in " ".join(with_u["hotpage"])
    assert "ustore" not in " ".join(without["hotpage"]) + " ".join(without["xact_tick"])
    maint = " ".join(with_u["maint_hist"])
    assert "UNLOGGED" in maint and "fl_slow_key(pad, %d)" % C.V6_SLOW_ITERS in maint
    assert set(C.RUNTIME_TABLES) <= set(with_u) and "maint_hist" in prepare.ANALYZE_TABLES
    assert "REFERENCES fl.acct" in " ".join(with_u["dl_child"]) and cleanup.DELETE_ABOVE["dl_child"] == C.DL_CHILD_ROWS


def test_import_stage_keeps_autoanalyze_out_with_table_level_options_only():
    """光关 autovacuum 挡不住自动 ANALYZE(那轮导入 80 秒后就有统计了);要表级触发线,实例级一个不动。"""
    sql = prepare.import_stage_options_sql()
    assert sql.startswith("ALTER TABLE fl.import_stage SET (autovacuum_enabled = false")
    assert "autovacuum_analyze_threshold = %d" % C.IMPORT_ANALYZE_THRESHOLD in sql
    schema = (C.SQL_DIR / "schema.sql").read_text(encoding="utf-8")
    assert "autovacuum_analyze_threshold = %d" % C.IMPORT_ANALYZE_THRESHOLD in schema
    assert "ALTER SYSTEM" not in (sql + schema).upper()
    sent = []                                       # V7 导入前重建表:TRUNCATE 清不掉上一次的列统计
    prepare.import_batch(_fake(exec=lambda q, a=None: sent.append(q)), rows=10)
    assert sent[0].startswith("DROP TABLE") and "autovacuum_analyze_threshold" in sent[1] and "TRUNCATE" not in sent[2]


def test_v7_probe_only_counts_an_autoanalyze_after_this_import():
    seen = {}
    admin = _fake(query=lambda sql, args=None: seen.update(sql=sql, args=args) or [(0, None)],
                  one=lambda sql, args=None: 500000)
    out = vacmod.ImportNoAnalyze(_ctx(), _meta("V7")).probe(admin)
    assert seen["args"][0] == "infinity" and "last_autoanalyze >" in seen["sql"]
    assert out == "统计信息里 0 行、实际 500000 行"


def test_v4_slot_comes_first_and_its_probe_says_when_it_pins_nothing():
    """逻辑槽建立时要等已分配事务号的事务结束,排在孤儿两阶段事务后面就永远建不成。"""
    v4 = _meta("V4")
    assert all(v4.start_s < s.start_s for s in C.ALL_SCENARIOS if s.code != "V4")
    sc = vacmod.ReplicationSlotHold(_ctx(), v4)
    empty = _fake(query=lambda sql, args=None: [("physical", False, None, None, None, None)])
    pinned = _fake(query=lambda sql, args=None: [("logical", False, "0/5000000", None, "12345", 3 * 1048576)])
    assert "没造出来" in sc.probe(empty)
    assert "已产生 3 MB" in sc.probe(pinned) and "catalog_xmin=12345" in sc.probe(pinned)


def test_v5_probe_counts_finished_autovacuum_rounds_since_it_started():
    """V5 原来限速太狠,一轮都没跑完,last_autovacuum 一直空着,看起来和 V1 关掉了一样。"""
    sc = vacmod.BloatThrottled(_ctx(), _meta("V5"))
    sc._base = 3
    admin = _fake(query=lambda sql, args=None: [("10:00:01", 5)] if "autovacuum_count" in sql
                  else [(200000, 300000, 60.0, "79 MB")])
    assert "跑了 2 轮还追不上" in sc.probe(admin)
    assert sc.COST_LIMIT * 1000 // sc.COST_DELAY_MS >= 500       # 每秒至少 500 点配额,一轮几十秒能跑完


def test_s1_and_s2_are_the_two_that_never_finish():
    """S2 原来取编码前 8 位,只有约 2000 组,1~2 分钟就跑完(那轮跑完 13 次)。"""
    assert {code for code, spec in slow.SPECS.items() if not spec.finishes} == {"S1", "S2"}
    assert not slow.SPECS["S2"].loop and "substr(l2.cust_code, 1, 10)" in slow.sql_for("S2", 1000)


def test_l2_and_l3_sessions_each_write_rows_of_their_own():
    rows = [{lwlock.HotPage.row_for(i, n) for n in range(200)} for i in range(lwlock.SESSIONS)]
    assert all(r <= set(range(1, 101)) for r in rows)
    assert not (rows[0] & rows[1]) and not (rows[1] & rows[2]) and not (rows[0] & rows[2])
    sc = lwlock.ShortTransactions.__new__(lwlock.ShortTransactions)
    sc._batch = False
    sent = []
    rec = _fake(exec=lambda sql, args=None: sent.append((sql, args)))
    for i in range(lwlock.SESSIONS):
        sc.step(rec, i)
    assert all(s.startswith("UPDATE fl.xact_tick") for s, _ in sent)
    assert {a[0] for _, a in sent} == {1, 401, 801}


def test_l4_locks_partitions_without_scanning_and_l5_writers_commit_async():
    """og5 分组验证:L4 的 count(*) 让 CPU 占了 99%,L5 被 WALFlushWait 盖住 —— 两处都改了。"""
    sent = []
    rec = _fake(exec=lambda sql, args=None: sent.append(sql), set=lambda k, v: sent.append("SET %s=%s" % (k, v)))
    l4 = lwlock.PartitionLockMgr.__new__(lwlock.PartitionLockMgr)
    l4._batch = True
    l4.step(rec, 0)
    l5 = lwlock.SubTransactions.__new__(lwlock.SubTransactions)
    for i in range(lwlock.SESSIONS):
        l5.setup(rec, i)
    assert "LIMIT 1; COMMIT" in sent[0] and "count(*)" not in sent[0]
    assert sent[1:] == ["SET synchronous_commit=off"] * 2                 # 两条写的;读的不改
    assert lwlock.SubTransactions.LOOPS > 64 and [l5.role_of(i) for i in range(3)] == ["sub", "sub", "read"]


def test_commit_in_do_probe_falls_back_and_is_remembered(monkeypatch):
    monkeypatch.setattr(lwlock, "_COMMIT_IN_DO", {})
    tries = []

    def no_commit(sql, args=None):
        tries.append(sql)
        raise RuntimeError("cannot commit in anonymous block")
    c = _fake(exec=no_commit, rollback=lambda: None)
    assert lwlock.commit_in_do_supported(c) is False and lwlock.commit_in_do_supported(c) is False
    assert len(tries) == 1


def test_codes_pick_and_rebase_keep_durations_and_open_ends():
    picked = C.rebase(C.by_codes(("K5", "V6")))
    assert [s.code for s in picked] == ["K5", "V6"] and min(s.start_s for s in picked) == 5
    for s in picked:
        assert s.stop_s - s.start_s == _meta(s.code).stop_s - _meta(s.code).start_s
    k1, = C.rebase(C.by_codes(("K1",)))
    assert k1.start_s == 5 and k1.stop_s == 0                    # 跟窗口一起结束的保持 0
    with pytest.raises(ValueError):
        C.by_codes(("Z9",))
    import argparse
    import faultlab                                             # run 的 --only 与 --codes 二选一
    with pytest.raises(db.LabError):
        faultlab._pick(argparse.Namespace(only="lock", codes="K5"))
    assert [s.code for s in faultlab._pick(argparse.Namespace(only="", codes="V6"))] == ["V6"]
