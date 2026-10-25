"""把协议返回的字符串值还原成 Python 类型。

协议把**所有**列值渲染成字符串（数值型也不例外），于是 Python 的真值
判断在这里是陷阱：

    bool("f")     -> True
    bool("false") -> True
    bool("0")     -> True

布尔列取回来是 'f'，bool() 一律得 True ——「这张表有主键吗」永远答有，
「实例在恢复态吗」永远答是。**结论正好相反，而且不报错。**

放在共用层而不是各 skill 自己写：这类判断错一次就是静默出错误结论，
不该有 13 份各自演进的实现。
"""
from __future__ import annotations

from typing import Any

# 布尔的渲染形式不止一种：接口文档 §3.1 给 true/false、§3.2 给 t/f，
# 同一中间件同一张系统表两种写法（§7.2 判断中间件不做归一化，表现随
# 内核版本/驱动而变）。所以两套都得接，另外把常见的 y/n、1/0、on/off 一并认下。
_TRUE = frozenset({"t", "true", "y", "yes", "1", "on"})
_FALSE = frozenset({"f", "false", "n", "no", "0", "off", ""})


def is_null(value: Any) -> bool:
    """这个取值是不是「无值」。

    协议把 NULL 渲染成**空串**（规范说明 §7.3 的推断），不是 None。
    所以迁移前的 `if x is None` 在协议下全部失效 —— 条件永不成立，
    后面那句 float(x) 直接抛 ValueError。

    不能写 `if not x`：那会把 0 当成 NULL，而「autovacuum 距今 0 秒」
    与「从未 autovacuum」是两回事。

    副作用：NULL 与真正的空串分不开。这是客户中间件自带的信息损失，
    不是我们能修的，只能一并当作「无值」。
    """
    return value is None or value == ""


def as_bool(value: Any) -> bool:
    """把协议返回的布尔列还原成 bool。

    认不出来的写法**报错而不是当成 False** —— 静默当假会让「未知」
    伪装成「否」，正是本函数要防的那类错。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(
        "无法识别的布尔取值 %r。已知形式：%s / %s。"
        "中间件对布尔的渲染形式文档自相矛盾（§3.1 true/false、§3.2 t/f），"
        "遇到新形式请补进 common/grmp/values.py 而不是就地绕过。"
        % (value, "/".join(sorted(_TRUE)), "/".join(sorted(_FALSE - {""})))
    )


def as_int(value: Any, default: int = 0) -> int:
    """把字符串数值还原成 int。空串/None 取默认值（NULL 渲染成空串）。

    **接受小数形态并截断。** openGauss 的 pg_class.relpages / reltuples 是
    double precision，经协议变成 "3704.0"，而 int("3704.0") 直接抛
    ValueError —— 实测让每一次 sqltune 运行都挂掉。

    迁移前这里是 int(3704.0)，即截断。协议只是把同一个值换了个形态传过来，
    语义不该跟着变。小数形态是数据库对这些列的真实类型，不是错误信号；
    真正的错误信号（列取错了）由 columns.py 的列名检查负责。

    非数字仍然**报错而不是取默认值** —— 那会把「取数出错」伪装成「值就是 0」。
    """
    if value is None or value == "":
        return default
    if isinstance(value, str):
        text = value.strip()
        # 先按整数试；带小数点的走 float 再截断，与迁移前的 int(float) 等价
        try:
            return int(text)
        except ValueError:
            return int(float(text))
    return int(value)


def as_float(value: Any, default: float = 0.0) -> float:
    """把字符串数值还原成 float。空串/None 取默认值。"""
    if value is None or value == "":
        return default
    return float(value)
