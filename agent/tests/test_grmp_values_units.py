"""结果值的类型还原。

协议把所有列值渲染成字符串，于是 Python 的真值判断在这里是陷阱：

    bool("f")     -> True
    bool("false") -> True
    bool("0")     -> True

布尔列取回来是 'f'，bool() 一律得 True —— 「这张表有主键吗」永远答有，
「实例在恢复态吗」永远答是。**结论正好相反，而且不报错。**

而且布尔的渲染形式还不止一种：接口文档 §3.1 给 true/false、§3.2 给 t/f，
同一中间件同一张系统表两种写法（规范说明 §7.2 判断中间件不做归一化，
表现随内核版本/驱动而变）。所以解析必须两套都接。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.grmp.values import as_bool, as_int, as_float, is_null  # noqa: E402


# ===========================================================================
# NULL 判定 —— `x is None` 在协议下永远为假
# ===========================================================================

def test_null_renders_as_empty_string_not_none():
    """协议把 NULL 渲染成空串（规范说明 §7.3），不是 None。

    所以迁移前的 `if x is None` 全部失效：条件永不成立，后面那句
    float("") 直接抛 ValueError。实测在 health 的 bloat 维度里就有一处
    ——只因那条 SQL 本身坏着、维度一直降级才没暴露出来。
    """
    assert is_null("") is True
    assert is_null(None) is True


def test_real_values_are_not_null():
    assert is_null("0") is False
    assert is_null("f") is False
    assert is_null(0) is False


def test_zero_is_not_null():
    """0 和 NULL 必须分得开：`if not x` 会把 0 当成 NULL。

    「autovacuum 距今 0 秒」与「从未 autovacuum」是两回事。
    """
    assert is_null(0) is False
    assert is_null("0") is False
    assert is_null(0.0) is False


def test_null_and_empty_string_are_indistinguishable_by_design():
    """这是客户中间件自带的信息损失，不是我们能修的 ——
    NULL 与真正的空串渲染结果相同，只能一并当作「无值」。"""
    assert is_null("") == is_null(None)


# ===========================================================================
# 布尔
# ===========================================================================

@pytest.mark.parametrize("raw", ["t", "true", "TRUE", "True", "y", "yes", "1", "on"])
def test_truthy_forms(raw):
    assert as_bool(raw) is True


@pytest.mark.parametrize("raw", ["f", "false", "FALSE", "False", "n", "no", "0", "off"])
def test_falsy_forms(raw):
    """这些用 bool() 判全是 True —— 本函数存在的全部理由。"""
    assert as_bool(raw) is False
    assert bool(raw) is True, "如果这条挂了，说明 Python 改了真值语义"


def test_empty_and_none_are_false():
    """NULL 渲染成空串（规范说明 §7.3 的推断），按假处理。"""
    assert as_bool("") is False
    assert as_bool(None) is False


def test_real_booleans_pass_through():
    """直连路径若拿到真布尔值也要正确 —— 两条路径共用同一个解析。"""
    assert as_bool(True) is True
    assert as_bool(False) is False


def test_unknown_form_is_rejected_rather_than_guessed():
    """认不出来的写法必须报错。

    静默当成 False 会让「未知」伪装成「否」—— 正是这个函数要防的那类错。
    """
    with pytest.raises(ValueError):
        as_bool("maybe")


# ===========================================================================
# 数值
# ===========================================================================

def test_int_parses_string_form():
    assert as_int("42") == 42
    assert as_int("-1") == -1


def test_int_treats_empty_as_default():
    """NULL 渲染成空串，取默认值而不是抛异常 —— 统计类字段常有 NULL。"""
    assert as_int("") == 0
    assert as_int(None) == 0
    assert as_int("", default=-1) == -1


def test_float_parses_string_form():
    assert as_float("25.34") == pytest.approx(25.34)


def test_float_treats_empty_as_default():
    assert as_float("") == 0.0
    assert as_float(None, default=1.5) == pytest.approx(1.5)


def test_numeric_garbage_is_rejected():
    """非数字不能静默变成 0 —— 那会把「取数出错」伪装成「值就是 0」。"""
    with pytest.raises(ValueError):
        as_int("abc")
    with pytest.raises(ValueError):
        as_float("abc")


# ===========================================================================
# 小数形态的整数列 —— 迁移中真炸过
# ===========================================================================

@pytest.mark.parametrize("raw,want", [
    ("3704.0", 3704),
    ("-2.0", -2),
    ("1.9", 1),        # 截断，不是四舍五入
    ("-1.9", -1),
])
def test_int_accepts_decimal_form_and_truncates(raw, want):
    """openGauss 的 pg_class.relpages / reltuples 是 double precision，
    经协议变成 "3704.0"。而 int("3704.0") 直接抛 ValueError ——
    实测让每一次 sqltune 运行都挂掉。

    迁移前这里是 int(3704.0)，即**截断**。协议只是把同一个值换了个形态
    传过来，语义不该跟着变，所以照旧截断。

    这不是「宽容解析」：小数形态是数据库对这些列的真实类型，
    不是错误信号。真正的错误信号（列取错了）由列名检查负责。
    """
    assert as_int(raw) == want


def test_int_still_rejects_non_numeric():
    """放宽只针对小数形态，非数字仍然报错。"""
    with pytest.raises(ValueError):
        as_int("3704 rows")


def test_int_matches_pre_migration_semantics():
    """与迁移前的 int(float) 逐值一致 —— 这才是「行为没变」的判据。"""
    for native in (3704.0, -2.0, 1.9, -1.9, 0.0):
        assert as_int(str(native)) == int(native)
