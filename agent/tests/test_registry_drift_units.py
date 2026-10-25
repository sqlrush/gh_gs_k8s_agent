"""跨 skill 重复脚本的防漂移闸。

7 组脚本在不同 skill 的命名空间下各注册了一份**逐字相同**的 SQL：

    procinfo.key_gucs / proctune.key_gucs / sqltune.key_gucs
    sqlfetch/sqlreview/proctune/sqltune 各自的 from_history
    sqlfetch/sqlreview/proctune/sqltune 各自的 from_statement
    procinfo.proc_def / proctune.proc_def
    proctune.tables / sqltune.tables
    proctune.column_stats / sqltune.column_stats
    proctune.db_version / sqltune.version

**不合并**是有意的：客户只能通过发布 DML 往 script_config 里灌脚本，
没有脚本管理接口。各 skill 自带全套脚本，客户才能单独上线某一个 skill；
合并成公共命名空间后，装 sqlfetch 就得连带把 proctune 的脚本一起灌进去。

但重复带来一个真实风险：在一份里修了 bug，另外两份还坏着，而两条路径
一致性检查发现不了 —— 它比的是「同一条脚本走两条链路一不一样」，
不是「三条同源脚本彼此一不一样」。所以在这里钉死：任一组内出现分叉就红。

改动其中一份时，要么同步改全组，要么就是有意分叉 —— 把它从 _GROUPS 里
移出去，并在旁边写清为什么分叉。别直接改这里的期望值。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp.script import load_script  # noqa: E402

_REGISTRY = _ROOT / "scripts" / "registry"

# 每组内的脚本 SQL 必须逐字相同。组名只是给失败信息用的。
_GROUPS = {
    "过程找不到时的排查:当前连接": [
        "procinfo/locate_context.yaml",
        "proctune/locate_context.yaml",
    ],
    "过程找不到时的排查:schema": [
        "procinfo/locate_schema.yaml",
        "proctune/locate_schema.yaml",
    ],
    "过程找不到时的排查:近似名搜索": [
        "procinfo/locate_search.yaml",
        "proctune/locate_search.yaml",
    ],
    "关键 GUC": [
        "procinfo/key_gucs.yaml",
        "proctune/key_gucs.yaml",
        "sqltune/key_gucs.yaml",
    ],
    "从历史视图取 SQL 原文": [
        "sqlfetch/from_history.yaml",
        "sqlreview/from_history.yaml",
        "proctune/sql_from_history.yaml",
        "sqltune/from_history.yaml",
        "explain/from_history.yaml",
    ],
    "从 statement 视图取 SQL 原文": [
        "sqlfetch/from_statement.yaml",
        "sqlreview/from_statement.yaml",
        "proctune/sql_from_statement.yaml",
        "sqltune/from_statement.yaml",
        "explain/from_statement.yaml",
    ],
    "存储过程定义": [
        "procinfo/proc_def.yaml",
        "proctune/proc_def.yaml",
    ],
    # 先切 search_path 再 EXPLAIN 的两语句模板(现场 400 的修法);analyze 名下的那份 SQL 相同,
    # 因为现场按客户只读要求 ANALYZE 固定关闭
    "带 search_path 的 EXPLAIN TEXT 模板": [
        "explain/plan_text_schema.yaml",
        "explain/plan_text_analyze_schema.yaml",
        "sqltune/plan_text_schema.yaml",
        "sqltune/plan_text_analyze_schema.yaml",
        "proctune/plan_text_schema.yaml",
        "proctune/plan_text_analyze_schema.yaml",
    ],
    "表基本信息": [
        "proctune/tables.yaml",
        "sqltune/tables.yaml",
    ],
    "列统计信息": [
        "proctune/column_stats.yaml",
        "sqltune/column_stats.yaml",
    ],
    "内核版本": [
        "proctune/db_version.yaml",
        "sqltune/version.yaml",
    ],
    # EXPLAIN 模板 —— 三个 skill 各一份。这几条是注入面，分叉的后果比别的
    # 严重得多：在一份里补了防护、另外两份还敞着，而防护本身看起来是有的。
    "执行计划 TEXT": [
        "explain/plan_text.yaml",
        "sqltune/plan_text.yaml",
        "proctune/plan_text.yaml",
    ],
    "执行计划 TEXT + ANALYZE": [
        "explain/plan_text_analyze.yaml",
        "sqltune/plan_text_analyze.yaml",
        "proctune/plan_text_analyze.yaml",
    ],
}


@pytest.mark.parametrize("group,rels", sorted(_GROUPS.items()))
def test_duplicated_scripts_stay_identical(group, rels):
    """同组脚本的 SQL 逐字相同。分叉了就是有人只修了其中一份。"""
    sqls = {}
    for rel in rels:
        path = _REGISTRY / rel
        assert path.exists(), "%s 不在了 —— 若是有意删除，请一并更新 _GROUPS" % rel
        sqls[rel] = load_script(path).script_content

    first_rel = rels[0]
    for rel in rels[1:]:
        assert sqls[rel] == sqls[first_rel], (
            "「%s」组内出现分叉：%s 与 %s 的 SQL 不一致。\n"
            "多半是在一份里修了 bug，另外几份还坏着。要么同步改全组，"
            "要么把它从 tests/test_registry_drift_units.py 的 _GROUPS 里移出去"
            "并写清为什么有意分叉。" % (group, rel, first_rel)
        )


def test_group_membership_is_complete():
    """全仓扫一遍：SQL 相同却不在同一组里的脚本，说明 _GROUPS 漏登记了。

    没有这条，新增一份重复脚本时上面的检查根本不会覆盖它 —— 名单是手写的，
    手写名单最常见的坏法就是没跟上。
    """
    listed = {rel for rels in _GROUPS.values() for rel in rels}

    by_sql = {}
    for path in sorted(_REGISTRY.glob("*/*.yaml")):
        rel = path.relative_to(_REGISTRY).as_posix()
        by_sql.setdefault(load_script(path).script_content, []).append(rel)

    unlisted = [
        rels for rels in by_sql.values()
        if len(rels) > 1 and not set(rels) <= listed
    ]
    assert not unlisted, (
        "这些脚本 SQL 相同但没登记进 _GROUPS，防漂移闸盖不到它们：%s" % unlisted
    )
