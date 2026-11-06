"""要诊断的库与取数的库不同时(api 模式)的统一说法。

2026-11-06 行方:中间件按实例 IP 固定连 postgres,不按库名切换;用户要诊断的是同一实例上的业务库。
gaussdb-login 校验目标库在实例上存在后放行,会话里 database 记目标库、exec_database 记取数库。
这时对目标库的 SQL 现取执行计划,在取数库里找不到对象 —— 原因是库不对,不是 search_path 或权限,
各 skill 原先的「让 DBA 给执行账号设 search_path」提示在这里是误导(而且要改数据库)。
"""
from __future__ import annotations

from typing import Optional

from .config import ConfigError, Connection, resolve

_MISSING = "does not exist"


def routed(conn_name: Optional[str]) -> Optional[Connection]:
    """会话的目标库与取数库不同 → 返回这条连接;相同或取不到 → None。"""
    try:
        conn = resolve(conn_name or None)
    except ConfigError:
        return None
    return conn if conn.exec_database and conn.exec_database != conn.database else None


def object_missing(message: str) -> bool:
    return _MISSING in (message or "")


def first_line(message: str, limit: int = 160) -> str:
    lines = (message or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def misplaced(conn: Connection, message: str) -> str:
    """「对象不存在」在这种会话里的真实原因。"""
    return ("这条 SQL 用到的对象在目标库「%s」里，而中间件按实例 IP 固定连「%s」取数，"
            "在「%s」里现取执行计划找不到它们（原始报错：%s）。"
            "这不是权限或 search_path 的问题，改 search_path、授权都解决不了，也不需要改数据库。"
            % (conn.database, conn.exec_database, conn.exec_database, first_line(message)))
