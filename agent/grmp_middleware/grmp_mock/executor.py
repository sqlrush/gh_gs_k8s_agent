"""执行器：渲染 → 连库 → 查询 → 序列化。

只做同步 SQL。异步与 PYTHON 在上层被显式拒绝，不在这里兜底 ——
兜底就意味着调用方以为自己拿到了异步语义或 Python 执行能力。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Sequence

from common.grmp import serialize
from common.grmp.columns import check_columns
from common.grmp.placeholder import ParamDef, render
from common.grmp.script import ScriptRecord
from common.grmp.settings import Settings

# 文档未定义结果集上限（【缺】）。本实现取一个上限并在超限时**报错**，
# 不截断 —— 截断等于静默丢数据，诊断场景下会得出「就这么多」的错误结论。
DEFAULT_MAX_RESULT_ROWS = 10000

# 文档未定义超时（【缺】）。给一个显式值，避免一条坏脚本把连接挂死。
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 60


class ExecError(Exception):
    """执行阶段的可预期失败，由上层转成接口二的失败响应。"""


def execute(
    record: ScriptRecord,
    values: Mapping[str, str],
    conn_name: str,
    settings: Settings,
    open_db: Callable[[str], Any],
    max_rows: int = DEFAULT_MAX_RESULT_ROWS,
    timeout: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """渲染并执行，返回 result 对象。

    渲染（含类型校验）在连库**之前**完成：参数不合法时根本不该建立连接，
    更不该把可疑取值送到数据库面前。
    """
    sql = render(record.script_content, record.params, dict(values), settings)

    # 会话模式只由**已注册脚本**的声明决定，请求里无从指定 ——
    # 否则任何调用方都能给自己开写权限，白名单与只读会话同时失效。
    db = open_db(conn_name, record.readonly)
    try:
        db.set_statement_timeout(timeout)
        cols, rows = db.query(sql)
    finally:
        db.close()

    if not cols:
        # 无结果集（DDL/DML/无返回的语句）走 Text 分支。
        # 【缺】文档没说 Text 分支的 data 放什么，这里给空串。
        return serialize.result_text("")

    # 结果按列名做键返回，重名列会被后一个覆盖 —— 报错，不能少返回几列
    check_columns(cols, record.script_name)

    if len(rows) > max_rows:
        raise ExecError(
            "结果集 %d 行，超过上限 %d 行。本实现拒绝执行而不截断——"
            "截断会让调用方把「只取到前 %d 行」当成「一共就这么多」。"
            % (len(rows), max_rows, max_rows)
        )

    return serialize.result_array(cols, rows, settings)
