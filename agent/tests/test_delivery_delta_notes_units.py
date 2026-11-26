"""白名单增量的说明文件(docs/delivery/delta-notes.yaml)不许有重复的脚本名(2026-11-19)。

YAML 里同一个键写两次不报错,后一个静默覆盖前一个。这份文件新的说明写在前面、旧的留在后面,
于是 v1.4.14 的增量 scripts-delta-20261116.sql 里 vacuum.dead_tuples 的变更说明被一条更早的旧说明顶替了
(SQL 正文是对的,说明写错了)。工具读这份文件时遇到重复键直接报错;仓库里这份文件也要能被它读进来。
"""
import importlib.util
import pathlib
import sys

import pytest

_AGENT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("delivery_delta_under_test", _AGENT / "tools" / "delivery_delta.py")
dd = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dd
_spec.loader.exec_module(dd)


def test_duplicate_script_names_in_the_notes_are_refused():
    with pytest.raises(ValueError, match="vacuum.dead_tuples"):
        dd.load_notes("changed:\n  vacuum.dead_tuples: 新说明\n  lockwait.pairs: x\n  vacuum.dead_tuples: 旧说明\n")


def test_the_notes_in_the_repo_have_no_duplicates_and_cover_this_round():
    notes = dd.load_notes(dd.NOTES.read_text(encoding="utf-8"))
    for name in ("lockwait.pairs", "health.stale_stats", "perf.table_stat", "explain.kernel_funcs"):
        assert "concat" in notes["changed"][name], name
    # v1.5.3 这一轮:布尔列改 1/0、名字类改 ::text(M 兼容库上取值形态),新增 YAML 计划
    for name in ("health.unused_index", "sqltune.indexes", "proctune.indexes", "vacuum.dead_tuples"):
        assert "1/0" in notes["changed"][name] and "::text" in notes["changed"][name], name
    for name in ("sqlreview.tables", "sqlreview.indexes", "health.table_writes"):
        assert "1/0" in notes["changed"][name], name
    for name in ("explain.relation_schemas", "sqltune.column_stats", "topproc.top_procs"):
        assert "::text" in notes["changed"][name], name
    assert "YAML" in notes["added"]["sqltune.plan_yaml"][0]


# ---- 文件头按实际改了哪些字段写(2026-11-19):本版 4 条只改脚本正文,模板却写「UPDATE 会同时写 database_type 与
# parameter_config」—— 中间件团队据此会去核对执行库与参数,白费功夫,也会误以为执行库被重写了。

def _head(changed, pchanged=frozenset(), tchanged=frozenset()):
    text = dd.render([], changed, {"changed": {n: "说明" for n in changed}}, 132, 132, "2026-11-19", set(pchanged), set(tchanged))
    return text.split("一、新增")[0]


def test_header_says_only_the_body_changes_when_no_routing_or_params_changed():
    head = _head(["lockwait.pairs", "health.stale_stats"])
    assert "只改脚本正文(script_content)" in head and "不动执行库" in head
    assert "会同时写 database_type 与 parameter_config" not in head


def test_header_names_the_updates_that_rewrite_routing_or_params():
    head = _head(["lockwait.pairs", "health.stale_stats"], tchanged={"lockwait.pairs"})
    assert "lockwait.pairs" in head and "同时写 database_type 与 parameter_config" in head
    assert "其余 1 条只改脚本正文" in head
