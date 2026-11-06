"""统一入口的异常契约。

**换一种数据库访问方式时，只应改「访问模块」本身，skill 一行不改。**
要做到这点，跨过门面的东西必须收敛。异常类型是最容易漏的一样：
如果 skill 里写着

    _QUERY_ERRORS = (common.DBError, GrmpError, RunError)

那么每加一种访问方式，这一行就要在每个 skill 里改一遍 —— 门面白做。

所以取数失败一律归一到 QueryError：skill 只认这一个类型。
新增访问方式时，新 Runner 抛 QueryError（或其子类）即可。

**哪些错误不归一**（同样重要）：

    ColumnError        列名重名/无名 —— 脚本定义缺陷
    ParamError         参数类型不对/缺参 —— 调用方写错了
    SessionUnavailable 该路径不提供持久会话 —— 能力缺口

这三类被降级逻辑接住就等于永远发现不了：列悄悄少几个、参数悄悄不生效、
hypopg 悄悄给出错误结论。它们必须穿透降级、直接炸出来。
"""
from __future__ import annotations


class QueryError(Exception):
    """一次取数失败 —— 网络不通、SQL 执行报错、目标实例查不到等。

    这类失败是「可预期的运行时状况」，skill 的降级逻辑应当接住它，
    把对应维度标为不可用，而不是让整个命令崩掉。
    """
