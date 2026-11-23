"""执行器：渲染 → 连库 → 查询 → 序列化。

只做同步 SQL。异步与 PYTHON 在上层被显式拒绝，不在这里兜底 ——
兜底就意味着调用方以为自己拿到了异步语义或 Python 执行能力。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Sequence

from common.grmp import routing, serialize
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


class DbMissingError(ExecError):
    """按 dbName 连库时库不存在(含库名为空)。行方的形态:HTTP 400 {"error_msg": "数据库[…]在目标高斯实例中不存在, instanceId: …",
    "error_code": 400}(2026-11-11 现场截图),由上层按这个形态回。"""


BANK_DB_MISSING = "数据库[%s]在目标高斯实例中不存在"

# 切库(2026-11-11 行方《切库问题说明》):appbusiness 的脚本带 dbName 时连到该库执行。行方没说清的两种情况做成开关:
#   dbname_missing:     appbusiness 的脚本没带 dbName —— "reject"(默认)按行方实测报「数据库[]在目标高斯实例中不存在」/
#                       "postgres" 照常连实例默认库(11-11 之前的假设,保留做对照)
#   dbname_on_postgres: postgres 的脚本带了 dbName —— "ignore" 忽略照常执行 / "reject" 报错(行方未答复)
DBNAME_MISSING = ("reject", "postgres")
DBNAME_ON_POSTGRES = ("ignore", "reject")


def route(record: ScriptRecord, values: Mapping[str, str], dbname_missing: str = "reject",
          dbname_on_postgres: str = "ignore"):
    """(要连的库或 None, 去掉路由参数后的取值)。None = 连实例默认库(postgres)。"""
    rest = {k: v for k, v in values.items() if k != routing.PARAM}
    db_name = values.get(routing.PARAM)
    if record.database_type == routing.APPBUSINESS:
        if db_name:
            return db_name, rest
        if dbname_missing == "reject":
            raise DbMissingError(BANK_DB_MISSING % "")
        return None, rest
    if db_name is not None and dbname_on_postgres == "reject":
        raise ExecError("脚本 %s 在 postgres 执行,不接受 %s" % (record.script_name, routing.PARAM))
    return None, rest


def execute(
    record: ScriptRecord,
    values: Mapping[str, str],
    conn_name: str,
    settings: Settings,
    open_db: Callable[[str], Any],
    max_rows: int = DEFAULT_MAX_RESULT_ROWS,
    timeout: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
    dbname_missing: str = "reject",
    dbname_on_postgres: str = "ignore",
) -> Dict[str, Any]:
    """渲染并执行，返回 result 对象。

    渲染（含类型校验）在连库**之前**完成：参数不合法时根本不该建立连接，
    更不该把可疑取值送到数据库面前。
    """
    database, values = route(record, values, dbname_missing, dbname_on_postgres)
    sql = render(record.script_content, record.params, dict(values), settings)

    # 会话模式只由**已注册脚本**的声明决定，请求里无从指定 ——
    # 否则任何调用方都能给自己开写权限，白名单与只读会话同时失效。
    if database:
        try:
            db = open_db(conn_name, record.readonly, database=database)
        except Exception as exc:  # noqa: BLE001 —— 库不存在按行方的形态回,其余原样上抛
            if "does not exist" in str(exc):
                raise DbMissingError(BANK_DB_MISSING % database) from exc
            raise
    else:
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
