"""切到业务库(dbName)的划分守卫(2026-11-11,见 common/grmp/routing.py)。

① 运行时用的 BUSINESS_DB_SCRIPTS 与脚本仓库 YAML 的 database_type 一致;
② 每条脚本按「读的是什么」划分:读 SQL 执行历史 / WDR 快照 / 活跃会话落表的必须 postgres(GaussDB 官方文档:
   必须在 postgres 库内查询,其它库中不存数据),读按库各一份的对象目录或 EXPLAIN 业务 SQL 的必须 appbusiness,
   只读实例级视图的留 postgres。11-11 核对时已交付的 52 条 appbusiness 里有 6 条读历史 / 快照 —— 切过去就查不到;
③ dbName 在 parameter_config 里的写法与位置与行方逐字一致,接口一的定义 required 为 false。
"""
import json
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import routing  # noqa: E402
from common.grmp.registry import Registry  # noqa: E402
from common.grmp.script import ScriptError, load_script  # noqa: E402

PG_ONLY = [r"\bstatement_history\b", r"\bsnapshot\.", r"\bgs_asp\b", r"\bgenerate_wdr_report\b"]
DB_LOCAL = [r"\bpg_class\b", r"\bpg_namespace\b", r"\bpg_attribute\b", r"\bpg_index\b", r"\bpg_indexes\b", r"\bpg_proc\b",
            r"\bpg_stats\b", r"\bpg_statistic\b", r"\bpg_constraint\b", r"\bpg_partition\b", r"\bgs_package\b",
            r"\bpg_description\b", r"\bpg_inherits\b", r"\bpg_tables\b", r"\bpg_views\b", r"\bpg_trigger\b", r"\bpg_depend\b",
            r"\bpg_type\b", r"\bpg_stat_(user|all|sys)_(tables|indexes)\b", r"\bpg_statio_\w+", r"\bpg_stat_user_functions\b",
            r"\binformation_schema\.", r"\badm_\w+", r"\bdb_(tables|objects|indexes|source|tab_\w+)\b", r"\bmy_\w+",
            r"::regclass\b", r"\bpg_(total_)?relation_size\b", r"\bpg_table_size\b", r"\bpg_indexes_size\b",
            r"\bpg_get_(functiondef|indexdef|viewdef|constraintdef)\b", r"\bto_regclass\b", r"^\s*explain\b", r"\bexplain\s*\("]
# 按规则判不出、有意为之的
EXEMPT = {routing.PROBE: routing.APPBUSINESS}      # 探针只读 current_database(),但就是要切过去看


def _hits(pats, sql):
    return [p for p in pats if re.search(p, sql, re.I | re.M)]


def _expected(rec):
    if rec.script_name in EXEMPT:
        return EXEMPT[rec.script_name]
    if _hits(PG_ONLY, rec.script_content):
        return routing.POSTGRES
    return routing.APPBUSINESS if _hits(DB_LOCAL, rec.script_content) else routing.POSTGRES


_REG = Registry()
_RECS = [_REG.find(n) for n in sorted(_REG.names())]


def test_runtime_table_matches_the_registry():
    in_yaml = {r.script_name for r in _RECS if r.database_type == routing.APPBUSINESS}
    assert routing.BUSINESS_DB_SCRIPTS == in_yaml, (
        "common/grmp/routing.py 的 BUSINESS_DB_SCRIPTS 与 YAML 的 database_type 不一致:多 %s,少 %s"
        % (sorted(routing.BUSINESS_DB_SCRIPTS - in_yaml), sorted(in_yaml - routing.BUSINESS_DB_SCRIPTS)))


@pytest.mark.parametrize("rec", _RECS, ids=lambda r: r.script_name)
def test_each_script_runs_where_its_data_lives(rec):
    want = _expected(rec)
    assert rec.database_type == want, (
        "%s 应为 %s:%s" % (rec.script_name, want,
                          "读只在 postgres 库的数据(SQL 历史 / WDR 快照 / 活跃会话落表),切到业务库会查不到" if want == routing.POSTGRES
                          and _hits(PG_ONLY, rec.script_content) else
                          "读按库各一份的对象目录或 EXPLAIN 业务 SQL,要在业务库执行" if want == routing.APPBUSINESS else
                          "只读实例级视图,在哪个库执行结果都一样,留在 postgres"))


def test_the_rule_itself_can_fail():
    """守卫本身要能红:历史 / 快照类不能被判成业务库,对象目录类不能被判成 postgres。"""
    class R:
        def __init__(self, sql):
            self.script_name, self.script_content = "x.y", sql
    assert _expected(R("SELECT * FROM dbe_perf.statement_history h JOIN pg_class c ON c.oid = h.x")) == routing.POSTGRES
    assert _expected(R("SELECT * FROM snapshot.snap_summary_statement")) == routing.POSTGRES
    assert _expected(R("SELECT relname FROM pg_class")) == routing.APPBUSINESS
    assert _expected(R("EXPLAIN (FORMAT TEXT) {{sql}}")) == routing.APPBUSINESS
    assert _expected(R("SELECT count(*) FROM pg_stat_activity")) == routing.POSTGRES


def test_db_name_entry_matches_the_bank_verbatim():
    """行方测试环境已改好的记录:parameter_config 最前面一项,type 写 String(不是 STRING)。"""
    rec = _REG.find("sqltune.plan_text")
    items = json.loads(rec.parameter_config)
    assert rec.parameter_config.startswith('[{"key":"dbName","value":"","type":"String","autoAcquire":false},')
    assert items[0] == routing.CONFIG_ENTRY and [i["key"] for i in items[1:]] == [p.key for p in rec.params]
    plain = _REG.find("topsql.snap_statement")
    assert "dbName" not in plain.parameter_config, "postgres 的脚本不带 dbName"


def test_interface_one_declares_db_name_as_optional():
    detail = _REG.find("sqltune.plan_text").to_api_detail()
    assert detail["param"][0]["param_name"] == "dbName" and detail["param"][0]["required"] is False
    assert all(p["param_name"] != "dbName" for p in _REG.find("config.settings").to_api_detail()["param"])


def test_probe_is_registered_as_business_db():
    rec = _REG.find(routing.PROBE)
    assert rec.database_type == routing.APPBUSINESS and "current_database()" in rec.script_content


def test_yaml_rejects_unknown_database_type_and_hand_written_db_name(tmp_path):
    bad = tmp_path / "x.yaml"
    bad.write_text("name: a.b\ndatabase_type: business\nsql: SELECT 1\n", encoding="utf-8")
    with pytest.raises(ScriptError, match="database_type"):
        load_script(bad)
    bad.write_text("name: a.b\nsql: SELECT '{{dbName}}'\nparams:\n  - {key: dbName, type: String}\n", encoding="utf-8")
    with pytest.raises(ScriptError, match="路由参数"):
        load_script(bad)
