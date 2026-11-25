"""M 兼容业务库上技能的行为(v1.5.1,2026-11-20 现场反馈)。

  · procinfo / proctune.source_adm:M 库没有 ADM_SOURCE(只继承了部分系统视图)→ 不发这条脚本,原因写清;
  · memanalyze.wlm_*:函数权限不足(Permission denied for function …)不是兼容问题 → 原因说成「执行账号没授权」;
  · 部署自检:「业务库兼容模式」一项;--m-trial 把 41 条业务库脚本各试跑一次,M 库上 source_adm 标「不适用」。
"""
import importlib.util
import pathlib
import sys
from dataclasses import replace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import compat, procname  # noqa: E402
from common import proc_source as ps  # noqa: E402
from common.access import QueryError  # noqa: E402
from common.config import Connection  # noqa: E402
from common.grmp import routing  # noqa: E402
from common.grmp.registry import Registry  # noqa: E402

CONN = Connection(name="10-0-0-9-appdb", type="gaussdb", host="127.0.0.1", port=8769, database="appdb",
                  user="grmp", driver="grmp", data_ip="10.0.0.9", app="api", route_db="appdb")


class _Runner:
    """记下发了哪些脚本;procinfo.proc_def 查不到,其余按 rows 回。"""

    def __init__(self, rows=None, broken=()):
        self.calls, self.rows, self.broken = [], rows or {}, dict(broken)

    def run(self, name, values=None):
        self.calls.append(name)
        if name in self.broken:
            raise QueryError(self.broken[name])
        return self.rows.get(name, [])


def test_adm_source_not_sent_on_m_library():
    runner = compat.tag(_Runner(), replace(CONN, compat="M"))
    with pytest.raises(procname.NotFound) as ei:
        ps.resolve(runner, "procinfo.proc_def", "procinfo.source_adm", "app.calc_total")
    assert "procinfo.source_adm" not in runner.calls, "M 库不发 ADM_SOURCE 那条脚本"
    assert "M（MySQL 兼容）" in ei.value.adm and "ADM_SOURCE" in ei.value.adm and not ei.value.adm_denied


def test_adm_source_still_used_on_a_library():
    runner = compat.tag(_Runner(), replace(CONN, compat="A"))
    with pytest.raises(procname.NotFound):
        ps.resolve(runner, "procinfo.proc_def", "procinfo.source_adm", "app.calc_total")
    assert "procinfo.source_adm" in runner.calls, "A 库(与未知)照旧走 ADM_SOURCE 兜底"


# ---------------- memanalyze:WLM 函数权限 ----------------
_MEM = _ROOT / "skills" / "gaussdb-memanalyze" / "scripts"


def _is_skill_module(m) -> bool:
    f = getattr(m, "__file__", "") or ""
    return "/skills/" in f and "/scripts/" in f


@pytest.fixture()
def wlm(request):
    saved = {n: m for n, m in sys.modules.items() if _is_skill_module(m)}
    saved_path = list(sys.path)
    for n in saved:
        sys.modules.pop(n, None)
    sys.path.insert(0, str(_MEM))
    import wlm as mod  # noqa: E402

    def restore():
        for n in [n for n, m in sys.modules.items() if _is_skill_module(m)]:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
        sys.path[:] = saved_path
    request.addfinalizer(restore)
    return mod


def test_wlm_permission_denied_is_explained(wlm):
    why = wlm._wlm_why(QueryError("ERROR: Permission denied for function pg_stat_get_wlm_realtime_session_info."))
    assert why.startswith("执行账号没有 WLM 资源跟踪函数的执行权限") and "需 DBA 授权" in why
    other = wlm._wlm_why(QueryError("ERROR: relation \"gs_wlm_session_statistics\" does not exist"))
    assert "执行权限" not in other


# ---------------- 部署自检 ----------------
_LOGIN = _ROOT / "skills" / "gaussdb-login" / "scripts"


@pytest.fixture()
def sc():
    sys.modules.pop("render", None)
    sys.path.insert(0, str(_LOGIN))
    spec = importlib.util.spec_from_file_location("selfcheck_m_under_test", _LOGIN / "selfcheck.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    sys.path.remove(str(_LOGIN))
    yield mod
    sys.modules.pop(spec.name, None)


def test_selfcheck_compat_item(sc):
    assert sc.check_compat(replace(CONN, route_db="")) == []
    ok = sc.check_compat(replace(CONN, compat="M", lctn="1"))
    assert ok[0].mark == sc.OK and ok[0].detail.startswith("M（MySQL 兼容）")
    unk = sc.check_compat(CONN)
    assert unk[0].mark == sc.WARN and routing.PROBE in unk[0].detail and unk[0].owner == sc.MIDDLEWARE


class _Client:
    def registered(self, n):
        return True


class _TrialRunner(_Runner):
    client = _Client()


def test_m_trial_runs_all_business_scripts_readonly(sc):
    runner = _TrialRunner(broken={"health.partitions": "ERROR: un support type"})
    rows = sc.trial_business(replace(CONN, compat="M"), runner, Registry())
    names = [r[0] for r in rows]
    assert names == sorted(routing.BUSINESS_DB_SCRIPTS) and len(names) == 42   # v1.5.2 加 sqltune.plan_yaml
    by = {r[0]: r for r in rows}
    assert by["procinfo.source_adm"][1] == sc.SKIP and "不适用" in by["procinfo.source_adm"][2]
    assert "procinfo.source_adm" not in runner.calls and "proctune.source_adm" not in runner.calls
    assert by["health.partitions"][1] == sc.FAIL and "un support type" in by["health.partitions"][2]
    assert by["health.invalid_index"][1] == sc.OK
    summary = sc.trial_summary(rows, replace(CONN, compat="M"))
    assert summary[0].mark == sc.FAIL and "health.partitions" in summary[0].detail
    report = sc.render_report(summary, rows)
    assert "## 业务库脚本试跑(42 条)" in report and sc.TRIAL_OBJECT in report


def test_m_trial_flags_values_that_came_back_as_java_objects(sc):
    """v1.5.2(2026-11-25 现场):M 兼容库上布尔列经中间件取回来是 [B@…,脚本照样「成功」—— 试跑要按值认出来。"""
    runner = _TrialRunner(rows={
        "health.unused_index": [{"idx_name": "app.ix_a", "indisprimary": "[B@276fe535", "indisunique": "0"}],
        "health.partitions": [{"schema": "app", "table": "t", "parts": "3", "strategy": "r"}],
    })
    by = {r[0]: r for r in sc.trial_business(replace(CONN, compat="M"), runner, Registry())}
    assert by["health.unused_index"][1] == sc.FAIL
    assert "indisprimary" in by["health.unused_index"][2] and "[B@276fe535" in by["health.unused_index"][2]
    assert "indisunique" not in by["health.unused_index"][2]
    assert by["health.partitions"][1] == sc.OK and by["health.partitions"][2] == "1 行"
    assert by["health.invalid_index"][1] == sc.OK and "无从核对" in by["health.invalid_index"][2]


def test_m_trial_json_plan_is_not_applicable_on_m_because_sqltune_falls_back_to_yaml(sc):
    runner = _TrialRunner(rows={"sqltune.plan_json": [{"QUERY PLAN": "[B@3be7717a"}],
                                "sqltune.plan_yaml": [{"QUERY PLAN": "- Plan: \n    Node Type: \"Result\""}]})
    by = {r[0]: r for r in sc.trial_business(replace(CONN, compat="M"), runner, Registry())}
    assert by["sqltune.plan_json"][1] == sc.SKIP and "sqltune.plan_yaml" in by["sqltune.plan_json"][2]
    assert by["sqltune.plan_yaml"][1] == sc.OK
    # A 兼容库上 JSON 计划取回来是 [B@… 就是真问题,照报
    by_a = {r[0]: r for r in sc.trial_business(replace(CONN, compat="A"), runner, Registry())}
    assert by_a["sqltune.plan_json"][1] == sc.FAIL


def test_m_trial_params_are_safe(sc):
    """试跑参数:EXPLAIN 只给 SELECT 1;对象名都是不存在的名字;每条脚本的参数都配得上。"""
    reg = Registry()
    for name in routing.BUSINESS_DB_SCRIPTS:
        vals = sc._trial_params(reg, name)
        assert vals is not None, name
        if "sql" in vals:
            assert vals["sql"] == "SELECT 1"
    for name in ("explain.plan_text_analyze", "sqltune.plan_text_analyze", "proctune.plan_text_analyze"):
        assert "ANALYZE false" in reg.find(name).script_content, "试跑不能真执行语句"


def test_m_trial_on_a_library_treats_adm_denied_as_warning(sc):
    runner = _TrialRunner(broken={"procinfo.source_adm": "ERROR: permission denied for relation adm_source"})
    rows = {r[0]: r for r in sc.trial_business(replace(CONN, compat="A"), runner, Registry())}
    assert rows["procinfo.source_adm"][1] == sc.WARN and "DBA 授权" in rows["procinfo.source_adm"][2]
