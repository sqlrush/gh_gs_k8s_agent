"""注册脚本的形态检查 —— 白名单模式下这四条是 vacuum 的全部取数来源。"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_REG = _ROOT / "scripts" / "registry"

from common.grmp.script import load_script  # noqa: E402

_SCRIPTS = [
    ("vacuum/dead_tuples.yaml", "vacuum.dead_tuples"),
    ("vacuum/autovac_settings.yaml", "vacuum.autovac_settings"),
    ("vacuum/autovac_workers.yaml", "vacuum.autovac_workers"),
    ("vacuum/oldest_xmin.yaml", "vacuum.oldest_xmin"),
]


@pytest.mark.parametrize("rel,name", _SCRIPTS)
def test_script_loads_and_is_readonly(rel, name):
    rec = load_script(_REG / rel)
    assert rec.script_name == name
    assert rec.readonly is True, "%s 不是只读 —— 诊断脚本不该能写" % rel


def test_dead_tuples_returns_what_the_rules_need():
    """列名是**契约**。少一列，对应的规则会静默失效。"""
    sql = load_script(_REG / "vacuum/dead_tuples.yaml").script_content
    for col in ("n_live_tup", "n_dead_tup", "reltuples", "table_bytes",
                "last_autovacuum_age_s", "vacuum_count", "autovacuum_count",
                "autovac_enabled", "reloptions"):
        assert col in sql, "dead_tuples.yaml 少了列 %s" % col


def test_dead_tuples_excludes_system_schemas():
    """系统表的死元组不是用户该管的事，混进来会淹没真正的风险表。"""
    sql = load_script(_REG / "vacuum/dead_tuples.yaml").script_content
    for s in ("pg_catalog", "information_schema", "snapshot", "dbe_perf"):
        assert s in sql, "没排除 %s" % s


def test_settings_covers_the_trigger_formula_inputs():
    """触发线 = threshold + scale_factor × reltuples，两个参数都得取得到。"""
    sql = load_script(_REG / "vacuum/autovac_settings.yaml").script_content
    assert "autovacuum" in sql


def test_oldest_xmin_covers_all_three_sources():
    """**R4 的数据来源。** 少一个来源就会漏掉一类卡住回收的原因，
    而漏掉的表现是「建议手工 VACUUM」—— 一条跑了也没用的建议。"""
    sql = load_script(_REG / "vacuum/oldest_xmin.yaml").script_content
    for src in ("pg_stat_activity", "pg_prepared_xacts", "pg_replication_slots"):
        assert src in sql, "oldest_xmin.yaml 少了来源 %s" % src
