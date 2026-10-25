"""注册期风险标注。

关键区分：**标注不等于拦截**。
凡是客户环境也会失败的（占位符未声明、渲染后语法错误），硬拦；
凡是客户环境能跑通的（占位符落在表名位、ORDER BY 位），**放行并标注** ——
拦了就偏离了「复现客户行为」这个目标，本地会拒绝一条客户那边正常运行的脚本。

这份标注清单本身是有价值的交付物：它精确回答「客户现有脚本库里，
哪些脚本的参数可能被用来注入」。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.grmp import placeholder as ph  # noqa: E402

from grmp_middleware.grmp_mock import risk  # noqa: E402


def _codes(sql):
    return sorted(r.code for r in risk.assess(sql))


# ===========================================================================
# IDENT_POSITION：占位符落在标识符位，类型校验对它无效
# ===========================================================================

@pytest.mark.parametrize(
    "sql",
    [
        "select * from t order by {{col}} desc",
        "select * from t group by {{col}}",
        "select * from {{tbl}} where a = 1",
        "select * from t join {{tbl2}} on 1=1",
        "select {{col}} from t",
    ],
)
def test_identifier_position_is_flagged(sql):
    """标识符位无法用绑定变量表达，也无法靠类型校验约束 —— 注入面在此。"""
    assert "IDENT_POSITION" in _codes(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "select 1 where runtime > {{threshold_seconds}}",
        "select * from t where a = {{a}} limit {{n}}",
        "select * from t offset {{n}}",
        "select count(*) from t where ts > {{since}}",
    ],
)
def test_value_position_is_not_flagged(sql):
    """取值位由类型校验兜住，不应误报 —— 误报会淹没真正需要人看的条目。"""
    assert "IDENT_POSITION" not in _codes(sql)


def test_select_list_placeholder_after_from_is_not_confused_with_column_position():
    """WHERE 里的占位符不能因为出现在 SELECT 之后就被当成列名位。"""
    assert _codes("select count(*) from t where x > {{n}}") == []


def test_sort_clause_does_not_leak_past_its_closing_paren():
    """CTE 里的 GROUP BY 不能把范围拖到括号外面去。

    实测踩到：
        WITH b AS (SELECT ... WHERE id={{b}} GROUP BY x),
             e AS (SELECT ... WHERE id={{e}} GROUP BY x)
    GROUP BY 的列表一直扫到下一个 group by，把第二个 CTE 的 WHERE 值位
    占位符 {{e}} 误判成排序位。30 条脚本里误报了 3 条。

    误报比漏报更伤：标注一旦不可信，人就学会整体忽略它，
    真正需要人工判断的那几条也跟着被淹掉。
    """
    sql = ("WITH b AS (SELECT a FROM t WHERE id={{b}} GROUP BY a), "
           "e AS (SELECT a FROM t WHERE id={{e}} GROUP BY a) "
           "SELECT * FROM e JOIN b USING (a)")
    assert "IDENT_POSITION" not in _codes(sql)


def test_sort_clause_inside_parens_still_detected():
    """括号内的排序表达式仍要能识别，别为了消误报把真信号也丢了。"""
    assert "IDENT_POSITION" in _codes(
        "select * from (select a from t order by {{col}}) x")


# ===========================================================================
# MULTI_VALUE：IN (...) 内的占位符，多值展开语义客户中间件未定义
# ===========================================================================

def test_in_list_placeholder_is_flagged():
    assert "MULTI_VALUE" in _codes("select * from t where id in ({{ids}})")


def test_plain_equality_is_not_flagged_as_multi_value():
    assert "MULTI_VALUE" not in _codes("select * from t where id = {{id}}")


# ===========================================================================
# NON_READONLY：非只读语句需单独审批
# ===========================================================================

@pytest.mark.parametrize(
    "sql",
    [
        "delete from t where id = {{id}}",
        "update t set a = 1",
        "insert into t values (1)",
        "drop table t",
        "truncate table t",
    ],
)
def test_write_statements_are_flagged(sql):
    assert "NON_READONLY" in _codes(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "select 1",
        "  select 1",
        "-- 注释\nselect 1",
        "/* 块注释 */ select 1",
        "with x as (select 1) select * from x",
        "explain select 1",
        "show all",
    ],
)
def test_read_statements_are_not_flagged(sql):
    assert "NON_READONLY" not in _codes(sql)


def test_case_is_ignored():
    assert "NON_READONLY" in _codes("DELETE FROM t")
    assert "IDENT_POSITION" in _codes("SELECT * FROM T ORDER BY {{c}}")


# ===========================================================================
# 风险条目本身
# ===========================================================================

def test_risk_carries_actionable_detail():
    """标注要能被人读懂并据此判断，只给一个代码没有意义。"""
    risks = risk.assess("select * from t order by {{col}}")
    ident = [r for r in risks if r.code == "IDENT_POSITION"][0]
    assert "col" in ident.detail


def test_clean_script_has_no_risks():
    assert risk.assess("select 1 where a > {{n}}") == ()


# ===========================================================================
# STRING_PARAM：String 参数在文本替换下就是注入向量
# ===========================================================================

def _defs(*pairs):
    return [ph.ParamDef(key=k, type=t) for k, t in pairs]


def test_string_typed_param_is_flagged():
    """实测：给 where usename = '{{username}}' 传 x' or '1'='1
    会渲染成 where usename = 'x' or '1'='1'，返回全表。

    这不是实现 bug，是文本替换的固有注入面 —— 客户中间件若同样用
    String.replace 实现，客户环境存在同一个洞。所以要标注，不要静默。
    String 类型没有格式约束，类型校验对它无效。
    """
    risks = risk.assess(
        "select 1 where usename = '{{username}}'",
        _defs(("username", "String")),
    )
    codes = [r.code for r in risks]
    assert "STRING_PARAM" in codes
    assert "username" in [r for r in risks if r.code == "STRING_PARAM"][0].detail


@pytest.mark.parametrize("typ", ["INTEGER", "Boolean", "DateTime", "Timestamp"])
def test_constrained_types_are_not_flagged(typ):
    """其余四种类型都有格式约束，校验能挡住注入载荷，不该误报。"""
    risks = risk.assess("select 1 where a = {{x}}", _defs(("x", typ)))
    assert "STRING_PARAM" not in [r.code for r in risks]


def test_assess_without_param_defs_still_works():
    """只有 SQL、没有参数声明时（如仅做语句形态检查）不应报错。"""
    assert isinstance(risk.assess("select 1 where a > {{n}}"), tuple)


def test_multiple_risks_are_all_reported():
    risks = _codes("delete from t where id in ({{ids}})")
    assert "NON_READONLY" in risks
    assert "MULTI_VALUE" in risks
