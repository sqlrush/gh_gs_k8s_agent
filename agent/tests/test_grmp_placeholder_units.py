"""{{占位符}} 的类型校验与文本替换。

两条执行路径（中间件 / 直连）共用本模块。共用是硬要求：否则同一模板在两侧
渲染出不同的 SQL，双路径一致性测试比对的就是两条不同的 SQL，而不是两条
不同的执行链路，测试本身失去意义。

替换方式为**文本替换**而非绑定变量 —— 这是刻意的，见开发方案 §2.4：
中间件的职责是复现客户行为，改用绑定变量会让标识符位的占位符静默失效。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.grmp import placeholder as ph  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402


def _defs(*pairs):
    return [ph.ParamDef(key=k, type=t) for k, t in pairs]


# ===========================================================================
# 1. 类型名：parameter_config 用全大写，API 用驼峰，两套必须能互转
# ===========================================================================

def test_canonical_type_accepts_both_document_casings():
    """【实】parameter_config.type 是 "INTEGER"，API data_type 是 "Integer"。"""
    assert ph.canonical_type("INTEGER") == "INTEGER"
    assert ph.canonical_type("Integer") == "INTEGER"


def test_api_type_name_restores_document_camel_case():
    """【规】API 侧枚举为 String|Integer|Boolean|DateTime|Timestamp。

    DateTime/Timestamp 不是简单首字母大写，写成 .capitalize() 会得到
    "Datetime"，与文档枚举不符。
    """
    assert ph.api_type_name("INTEGER") == "Integer"
    assert ph.api_type_name("STRING") == "String"
    assert ph.api_type_name("BOOLEAN") == "Boolean"
    assert ph.api_type_name("DATETIME") == "DateTime"
    assert ph.api_type_name("TIMESTAMP") == "Timestamp"


def test_unknown_type_is_rejected():
    """类型是脚本注册时的边界输入，五种之外一律拒绝。"""
    with pytest.raises(ph.ParamError):
        ph.canonical_type("NUMERIC")


# ===========================================================================
# 2. 取值校验（规范说明 §3.5）—— 所有取值都以字符串承载
# ===========================================================================

@pytest.mark.parametrize("raw", ["1", "-2", "100", "0"])
def test_integer_accepts_decimal_strings_with_optional_sign(raw):
    """【规】Integer 的 param_value 为十进制字符串，可带负号。"""
    ph.validate_value("INTEGER", raw, Settings())


@pytest.mark.parametrize("raw", ["1.5", "", " 1", "1 ", "abc", "0x10", "１"])
def test_integer_rejects_non_decimal(raw):
    """非十进制串一律拒绝。放宽会让类型校验这道防线形同虚设。"""
    with pytest.raises(ph.ParamError):
        ph.validate_value("INTEGER", raw, Settings())


def test_integer_rejects_sql_injection_payload():
    """类型校验是文本替换方案下的第一道防线，注入载荷必须在这里被挡住。"""
    with pytest.raises(ph.ParamError):
        ph.validate_value("INTEGER", "1 OR 1=1", Settings())


@pytest.mark.parametrize("raw", ["true", "false"])
def test_boolean_accepts_only_lowercase_literals(raw):
    """【规】Boolean 的取值为字符串化的布尔字面量 "true"/"false"。"""
    ph.validate_value("BOOLEAN", raw, Settings())


@pytest.mark.parametrize("raw", ["True", "FALSE", "1", "0", "t", "f", "yes"])
def test_boolean_rejects_other_spellings(raw):
    """文档只给了 true/false 两种写法，其余一律拒绝而不做宽容解析。

    宽容解析会让「客户端传了 t、客户中间件拒绝、本地却接受」这类落差
    到交付现场才暴露。
    """
    with pytest.raises(ph.ParamError):
        ph.validate_value("BOOLEAN", raw, Settings())


def test_datetime_accepts_documented_format():
    """【规】DateTime 格式为 yyyy-MM-dd HH:mm:ss。"""
    ph.validate_value("DATETIME", "2024-10-19 11:10:25", Settings())


@pytest.mark.parametrize(
    "raw", ["2024-10-19", "2024/10/19 11:10:25", "2024-10-19T11:10:25", "2024-13-01 00:00:00"]
)
def test_datetime_rejects_other_formats_and_impossible_dates(raw):
    with pytest.raises(ph.ParamError):
        ph.validate_value("DATETIME", raw, Settings())


@pytest.mark.parametrize("raw", ["1695026670878", "1577836800"])
def test_timestamp_accepts_both_documented_lengths(raw):
    """【矛】文档同时给出 13 位（毫秒）与 10 位（秒）两个例子。

    但在文本替换方案下中间件并不解释单位 —— 它只把数字串原样贴进 SQL，
    秒/毫秒的判断责任在脚本作者（写 to_timestamp({{ts}}) 还是 /1000）。
    所以这里两种长度都必须接受，不能替客户选一种。
    """
    ph.validate_value("TIMESTAMP", raw, Settings())


@pytest.mark.parametrize("raw", ["-1", "1.5", "abc", ""])
def test_timestamp_rejects_non_digits(raw):
    with pytest.raises(ph.ParamError):
        ph.validate_value("TIMESTAMP", raw, Settings())


# --- String：引号责任未经客户环境证实，默认拒绝而不是猜一种 ---

def test_string_param_is_substituted_as_is_by_default():
    """【推】接口文档「案例 2」把字符串与布尔型合并为同一个案例：

        取值例如 "value"、"zhangsan"、"true"、"false"

    若中间件按类型补引号，String 要补、Boolean 不能补，两类不可能合并。
    合并说明替换时不按类型分支 —— 纯文本替换，引号责任在脚本作者。

    更直接的一条：**客户中间件不会拒绝 String 参数**。默认拒绝是我们
    单方面加的行为，客户没有，属于「改良」，与保真原则相悖。
    """
    ph.validate_value("STRING", "zhangsan", Settings())


def test_string_param_can_be_refused_when_policy_tightened():
    """拿到反证（客户确实按类型补引号）时可切回拒绝，策略是显式配置。"""
    st = Settings(string_param_policy="refuse")
    with pytest.raises(ph.ParamError) as exc:
        ph.validate_value("STRING", "zhangsan", st)
    assert "未经客户环境证实" in str(exc.value)


def test_string_param_value_is_not_quoted_by_the_middleware():
    """引号责任在脚本作者：中间件贴进去的是裸值，不补引号。

    这是决策表第 5 项。脚本要写成 = '{{name}}'，不能写成 = {{name}}。
    """
    sql = ph.render(
        "select 1 where usename = '{{name}}'",
        _defs(("name", "STRING")),
        {"name": "zhangsan"},
        Settings(),
    )
    assert sql == "select 1 where usename = 'zhangsan'"


def test_unknown_string_policy_is_rejected_at_construction():
    with pytest.raises(ValueError):
        Settings(string_param_policy="maybe")


# ===========================================================================
# 3. 占位符抽取
# ===========================================================================

def test_extract_returns_placeholders_in_first_appearance_order():
    names = ph.extract_placeholders(
        "select * from t where a > {{hi}} and b < {{lo}} limit {{n}}"
    )
    assert names == ["hi", "lo", "n"]


def test_extract_deduplicates_repeated_placeholder():
    names = ph.extract_placeholders("where a = {{x}} or b = {{x}}")
    assert names == ["x"]


def test_extract_returns_empty_list_for_parameterless_sql():
    assert ph.extract_placeholders("select 1") == []


# ===========================================================================
# 4. 渲染 —— 文本替换，不是绑定变量
# ===========================================================================

def test_render_substitutes_value_textually():
    """【实】客户样例 where runtime > {{threshold_seconds}}，INTEGER 裸露无引号。"""
    sql = ph.render(
        "select 1 where runtime > {{threshold_seconds}}",
        _defs(("threshold_seconds", "INTEGER")),
        {"threshold_seconds": "10"},
        Settings(),
    )
    assert sql == "select 1 where runtime > 10"


def test_render_substitutes_in_limit_position():
    """LIMIT 位的占位符能正常工作，这正是绑定变量做不到而文本替换能做到的。"""
    sql = ph.render(
        "select * from t limit {{n}}",
        _defs(("n", "INTEGER")),
        {"n": "20"},
        Settings(),
    )
    assert sql == "select * from t limit 20"


def test_render_substitutes_in_identifier_position():
    """标识符位（ORDER BY 列名）用绑定变量会静默按常量排序，文本替换则正常。

    注册期会对这类脚本出具 IDENT_POSITION 风险标注，但不拦截 ——
    拦了就偏离客户行为（客户环境这条脚本是能跑的）。
    """
    sql = ph.render(
        "select * from t order by {{col}} desc",
        _defs(("col", "STRING")),
        {"col": "runtime"},
        Settings(),
    )
    assert sql == "select * from t order by runtime desc"


def test_render_replaces_every_occurrence_of_same_placeholder():
    sql = ph.render(
        "select {{x}} where a = {{x}}",
        _defs(("x", "INTEGER")),
        {"x": "7"},
        Settings(),
    )
    assert sql == "select 7 where a = 7"


def test_render_leaves_no_placeholder_behind():
    sql = ph.render(
        "select {{a}}, {{b}}",
        _defs(("a", "INTEGER"), ("b", "INTEGER")),
        {"a": "1", "b": "2"},
        Settings(),
    )
    assert "{{" not in sql and "}}" not in sql


def test_render_rejects_missing_required_param_naming_it():
    """决策表第 7 项：可选参数未传时报错，不做「删掉条件」的猜测。

    猜测会静默改变 SQL 语义 —— 「没有慢 SQL」和「没传阈值」会变得无法区分。
    """
    with pytest.raises(ph.ParamError) as exc:
        ph.render(
            "select 1 where a > {{threshold_ms}}",
            _defs(("threshold_ms", "INTEGER")),
            {},
            Settings(),
        )
    assert "threshold_ms" in str(exc.value)


def test_render_rejects_undeclared_param_naming_it():
    """传了脚本没声明的参数，说明调用方与脚本版本不一致，必须报错而非忽略。"""
    with pytest.raises(ph.ParamError) as exc:
        ph.render(
            "select 1",
            _defs(),
            {"stray": "1"},
            Settings(),
        )
    assert "stray" in str(exc.value)


def test_render_rejects_placeholder_absent_from_declarations():
    """SQL 里有未声明的 {{}}，渲染后会残留占位符并导致语法错误 —— 提前挡住。"""
    with pytest.raises(ph.ParamError) as exc:
        ph.render(
            "select 1 where a > {{undeclared}}",
            _defs(),
            {},
            Settings(),
        )
    assert "undeclared" in str(exc.value)


def test_render_applies_type_validation_before_substitution():
    """注入载荷不能因为「反正是文本替换」就放行 —— 类型校验先于替换执行。"""
    with pytest.raises(ph.ParamError):
        ph.render(
            "select 1 where a > {{n}}",
            _defs(("n", "INTEGER")),
            {"n": "1; drop table t"},
            Settings(),
        )


def test_render_does_not_mutate_inputs():
    defs = _defs(("n", "INTEGER"))
    values = {"n": "5"}
    template = "select {{n}}"
    ph.render(template, defs, values, Settings())
    assert values == {"n": "5"}
    assert template == "select {{n}}"
    assert defs[0].key == "n"


def test_render_does_not_rescan_substituted_text():
    """替换出的值里若含 {{...}} 形态，不能被当成占位符再替换一轮。"""
    sql = ph.render(
        "select '{{a}}', '{{b}}'",
        _defs(("a", "STRING"), ("b", "STRING")),
        {"a": "{{b}}", "b": "X"},
        Settings(),
    )
    assert sql == "select '{{b}}', 'X'"
