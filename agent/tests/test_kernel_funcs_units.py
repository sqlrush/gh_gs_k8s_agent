"""common.kernel_funcs —— GaussDB 私有函数(gs_get_explain / gs_get_kernel_info)的探测与使用。

纪律:
  · 探测永不抛:探测脚本本身跑不了(没注册、连不上)也只是「未知」,由调用方决定退不退回;
  · 「该有而没有」只对 GaussDB 成立:openGauss 从来没有这两个函数(og5 / og7 实测),不能对它报「缺失」;
  · 运行态计划按探测到的签名二选一:505.2.1.SPC0600 实测是 gs_get_explain(bigint)(2026-09-08 客户 pg_catalog 截图),
    文档写的 (integer) 已过时;pid 是 64 位线程号,只有 integer 签名才需要范围检查。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import kernel_funcs as kf  # noqa: E402
from common.grmp.errors import QueryError  # noqa: E402

GAUSS = "(GaussDB Kernel V500R002C10 build 1234) compiled at 2025-01-01 openGauss compat"
OG = "(openGauss-lite 5.0.3 build 89d144c2) compiled at 2024-07-31 21:39:16 commit 0 last mr  release"


class FakeRunner:
    def __init__(self, probe_rows=None, plan_rows=None, session_rows=None, kernel_rows=None, fail=None):
        self.probe_rows, self.plan_rows = probe_rows or [], plan_rows or []
        self.session_rows, self.kernel_rows = session_rows or [], kernel_rows or []
        self.fail = fail or {}
        self.calls = []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if script in self.fail:
            raise QueryError(self.fail[script])
        return {kf.PROBE_SCRIPT: self.probe_rows, kf.RUNTIME_PLAN_SCRIPT: self.plan_rows,
                kf.RUNTIME_PLAN_INT4_SCRIPT: self.plan_rows,
                kf.ACTIVE_PID_SCRIPT: self.session_rows, kf.SESSION_BY_PID_SCRIPT: self.session_rows,
                kf.KERNEL_INFO_SCRIPT: self.kernel_rows}[script]


def _probe_rows(version, explain_args=None, kernel_info=False):
    rows = [{"item": "version", "detail": version}]
    if explain_args is not None:
        rows.append({"item": "func:gs_get_explain", "detail": explain_args})
    if kernel_info:
        rows.append({"item": "func:gs_get_kernel_info", "detail": ""})
    return rows


# ---------------------------------------------------------------- probe

def test_probe_reads_engine_and_both_functions():
    r = FakeRunner(_probe_rows(GAUSS, "integer", True))
    p = kf.probe(r)
    assert p.engine == "gaussdb" and p.has_explain and p.explain_args == "integer" and p.kernel_info
    assert p.expected_but_missing == [] and p.probe_error == ""
    assert r.calls == [(kf.PROBE_SCRIPT, {})]


def test_probe_on_gaussdb_flags_missing_functions_as_expected_but_missing():
    p = kf.probe(FakeRunner(_probe_rows(GAUSS)))
    assert p.engine == "gaussdb" and not p.has_explain and not p.kernel_info
    assert p.expected_but_missing == ["gs_get_explain", "gs_get_kernel_info"]
    note = kf.missing_note(p)
    assert "GaussDB" in note and "gs_get_explain" in note and "gs_get_kernel_info" in note
    assert "pg_proc" in note and "database" in note and "升级" in note and "EXPLAIN" in note


def test_probe_on_opengauss_expects_nothing():
    """openGauss 本来就没有这两个函数——不能对它说「该有而没有」。"""
    p = kf.probe(FakeRunner(_probe_rows(OG)))
    assert p.engine == "opengauss" and not p.has_explain
    assert p.expected_but_missing == [] and kf.missing_note(p) == ""


def test_probe_never_raises_when_the_probe_script_itself_fails():
    r = FakeRunner(fail={kf.PROBE_SCRIPT: "中间件未注册脚本 explain.kernel_funcs"})
    p = kf.probe(r)
    assert p.engine == "unknown" and not p.has_explain and not p.kernel_info
    assert "未注册" in p.probe_error and p.expected_but_missing == []
    assert "探测" in kf.missing_note(p) and "explain.kernel_funcs" in kf.missing_note(p)


def test_probe_tolerates_odd_rows():
    p = kf.probe(FakeRunner([{"item": "version", "detail": None}, {"weird": 1}, {"item": "func:other", "detail": "x"}]))
    assert p.engine == "unknown" and not p.has_explain


# ---------------------------------------------------------------- runtime plan

def test_runtime_plan_bigint_signature_takes_any_pid_and_joins_rows():
    """505.2.1.SPC0600 实测签名 gs_get_explain(bigint):64 位线程号直接传,走 explain.runtime_plan。
    pid 以 int 进 INTEGER 参数位(中间件有数字校验,String 位是注入面),SQL 里显式 ::bigint。"""
    r = FakeRunner(plan_rows=[{"plan": "Seq Scan on t"}, {"plan": "  Filter: a = 1"}])
    text = kf.runtime_plan(r, "281440978523808", "bigint")
    assert text == "Seq Scan on t\n  Filter: a = 1"
    assert r.calls == [(kf.RUNTIME_PLAN_SCRIPT, {"pid": 281440978523808})]      # 传的是 int,不是字符串
    assert kf.runtime_plan(FakeRunner(plan_rows=[{"plan": "ok"}]), "12345") == "ok"   # 默认按 bigint


def test_runtime_plan_integer_signature_uses_the_int4_script_when_pid_fits():
    r = FakeRunner(plan_rows=[{"plan": "ok"}])
    assert kf.runtime_plan(r, 4321, "integer") == "ok"
    assert r.calls == [(kf.RUNTIME_PLAN_INT4_SCRIPT, {"pid": 4321})]


def test_runtime_plan_refuses_pid_beyond_int4_range_only_for_the_integer_signature():
    """文档签名 (integer) 的老内核:pg_stat_activity.pid 是 64 位线程号(实测 281440978523808),
    硬转 ::integer 会报 integer out of range,直接传 bigint 就是那句 does not exist。
    超范围时**不调用**,把这层关系讲清楚再退回;bigint 签名的内核不受此限。"""
    r = FakeRunner(plan_rows=[{"plan": "x"}])
    with pytest.raises(kf.NoRuntimePlan) as ei:
        kf.runtime_plan(r, 281440978523808, "integer")
    msg = str(ei.value)
    assert "integer" in msg and "线程" in msg and "gs_get_dn_explain" in msg and "bigint" in msg
    assert r.calls == []                                            # 没有打到数据库


def test_runtime_plan_accepts_named_parameter_signatures():
    """pg_get_function_arguments 返回的是 "p integer" / "p bigint" 这种带参数名的写法(og5 桩函数实测),
    比对时只看类型。"""
    r = FakeRunner(plan_rows=[{"plan": "ok"}])
    assert kf.runtime_plan(r, 42, "p integer") == "ok"
    assert kf.runtime_plan(r, 42, "IN p integer") == "ok"
    assert kf.runtime_plan(r, 281440978523808, "p bigint") == "ok"
    assert [c[0] for c in r.calls] == [kf.RUNTIME_PLAN_INT4_SCRIPT, kf.RUNTIME_PLAN_INT4_SCRIPT, kf.RUNTIME_PLAN_SCRIPT]
    assert kf._arg_types("a text, b bigint") == "text,bigint" and kf._arg_types("") == ""


def test_runtime_plan_refuses_a_signature_neither_script_was_written_for():
    r = FakeRunner(plan_rows=[{"plan": "x"}])
    with pytest.raises(kf.NoRuntimePlan) as ei:
        kf.runtime_plan(r, 42, "text")
    assert "text" in str(ei.value) and "bigint" in str(ei.value) and "integer" in str(ei.value) and r.calls == []


def test_runtime_plan_rejects_non_integer_pid():
    with pytest.raises(ValueError) as ei:
        kf.runtime_plan(FakeRunner(), "abc")
    assert "pid" in str(ei.value)


def test_runtime_plan_empty_result_is_reported_not_swallowed():
    """文档:plan_collect_thresh=-1 时永远返回空——空计划要说清原因,不能当成功。"""
    with pytest.raises(kf.NoRuntimePlan) as ei:
        kf.runtime_plan(FakeRunner(plan_rows=[{"plan": ""}]), 7)
    assert "plan_collect_thresh" in str(ei.value) and "track_activities" in str(ei.value)


def test_runtime_plan_propagates_query_error_with_hint():
    r = FakeRunner(fail={kf.RUNTIME_PLAN_SCRIPT: "ERROR: function gs_get_explain(bigint) does not exist"})
    with pytest.raises(QueryError):
        kf.runtime_plan(r, 7)


# ---------------------------------------------------------------- session lookup

def test_active_session_for_sql_returns_pid_and_query():
    r = FakeRunner(session_rows=[{"pid": "4321", "query": "select 1", "query_start": "2026-09-07 10:00:00"}])
    s = kf.active_session_for_sql(r, "825712617")
    assert s is not None and s.pid == 4321 and s.query == "select 1"
    assert r.calls == [(kf.ACTIVE_PID_SCRIPT, {"sql_id": 825712617})]


def test_active_session_for_sql_returns_none_when_idle():
    assert kf.active_session_for_sql(FakeRunner(session_rows=[]), "1") is None


def test_session_by_pid_returns_query_text():
    r = FakeRunner(session_rows=[{"pid": "77", "query": "select count(*) from t", "query_start": ""}])
    s = kf.session_by_pid(r, 77)
    assert s is not None and s.query.startswith("select count") and r.calls[0][1] == {"pid": 77}


# ---------------------------------------------------------------- kernel info

def test_kernel_info_returns_rows_as_is():
    rows = [{"node_name": "dn1", "module": "XACT", "name": "recent_global_xmin", "value": "15805"}]
    assert kf.kernel_info(FakeRunner(kernel_rows=rows)) == rows
