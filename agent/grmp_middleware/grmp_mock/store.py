"""script_config 的本地存储（SQLite）。

为什么不建在 og 上：og 有 200 万行 demo 数据，不希望被测试元数据污染；
中间件的元数据本来就属于中间件自己（客户那边 GRMP 也有独立的库）；
标准库自带 sqlite3，零依赖。

表结构保留客户 script_config 的 21 列全集，包括我们用不到的作用域列 ——
注册工具据此导出的 INSERT DML 才能直接交给客户走版本发布。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from common.grmp.placeholder import ParamDef
from common.grmp.script import SCRIPT_CONFIG_COLUMNS, ScriptRecord

# 21 列的 SQLite 类型。客户侧 id 是 varchar，这里同样用 TEXT ——
# 用 INTEGER 会让读出来的 id 变成数字，进而让协议里的字符串 id 悄悄变形。
_COLUMN_TYPES: Dict[str, str] = {
    "id": "TEXT PRIMARY KEY",
    "script_type": "TEXT NOT NULL",
    "script_name": "TEXT NOT NULL UNIQUE",
    "database_type": "TEXT",
    "refered_appbusiness": "INTEGER",
    "kernel_version": "TEXT",
    "region": "TEXT",
    "deployment_form": "TEXT",
    "execute_node_type": "TEXT",
    "cluster_deployment_mode": "TEXT",
    "script_content": "TEXT NOT NULL",
    "parameter_config": "TEXT",
    "scene": "TEXT",
    "is_valid": "INTEGER",
    "create_user": "TEXT",
    "create_time": "TEXT",
    "last_modify_user": "TEXT",
    "last_modify_time": "TEXT",
    "is_asyn": "INTEGER",
    "extend": "TEXT",
    "compliance_mode": "TEXT",
}

_FIRST_ID = 1


class StoreError(Exception):
    """存储层的可预期错误（重名等），由调用方转成协议错误码。"""


def _quote_ident(name: str) -> str:
    """SQLite 标识符引用。extend 在客户 DML 里也是带引号的。"""
    return '"%s"' % name.replace('"', '""')


class ScriptStore:
    """script_config 的读写。每次调用独立开关连接，不持有长连接。"""

    def __init__(self, path: pathlib.Path):
        self._path = pathlib.Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._create_table()

    # -- schema ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        return conn

    def _create_table(self) -> None:
        cols = ", ".join(
            "%s %s" % (_quote_ident(name), _COLUMN_TYPES[name])
            for name in SCRIPT_CONFIG_COLUMNS
        )
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS script_config (%s)" % cols)
            # 只读标记单独一张表。它是本实现自己的概念，客户 script_config
            # 的 21 列里没有对应项；塞进去会让导出的 DML 多一列，客户拿去
            # 直接执行就会失败。导出 DML 时这张表也不参与。
            conn.execute(
                "CREATE TABLE IF NOT EXISTS script_local ("
                "script_name TEXT PRIMARY KEY, readonly INTEGER NOT NULL)"
            )

    def columns(self) -> Tuple[str, ...]:
        with self._connect() as conn:
            cur = conn.execute("PRAGMA table_info(script_config)")
            return tuple(row["name"] for row in cur.fetchall())

    # -- 写 --------------------------------------------------------------

    def register(self, record: ScriptRecord, replace: bool = False) -> ScriptRecord:
        """写入一条脚本，返回带 id 的新记录。入参不被修改。

        重名默认拒绝。显式 replace 时**保持原 id 不变** —— 调用方会缓存
        「逻辑名 → id」的解析结果，换 id 会让缓存指向另一条脚本，
        表现为执行成功、结果无关、不报错。
        """
        existing = self.find_by_name(record.script_name)
        if existing is not None and not replace:
            raise StoreError(
                "脚本 %s 已注册（id=%s）。要覆盖请显式指定 replace，"
                "否则会静默冲掉已在用的定义。"
                % (record.script_name, existing.id)
            )

        script_id = existing.id if existing is not None else self._next_id()
        stored = record.with_id(script_id)
        row = stored.as_row()

        placeholders = ", ".join("?" for _ in SCRIPT_CONFIG_COLUMNS)
        col_list = ", ".join(_quote_ident(c) for c in SCRIPT_CONFIG_COLUMNS)
        values = [row[c] for c in SCRIPT_CONFIG_COLUMNS]
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO script_config (%s) VALUES (%s)"
                % (col_list, placeholders),
                values,
            )
            conn.execute(
                "INSERT OR REPLACE INTO script_local (script_name, readonly) "
                "VALUES (?, ?)",
                (stored.script_name, 1 if stored.readonly else 0),
            )
        return stored

    def _next_id(self) -> str:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT MAX(CAST(id AS INTEGER)) AS m FROM script_config"
            )
            current = cur.fetchone()["m"]
        return str(_FIRST_ID if current is None else int(current) + 1)

    # -- 读 --------------------------------------------------------------

    def find_by_name(self, script_name: str) -> Optional[ScriptRecord]:
        return self._one("script_name = ?", (script_name,))

    def find_by_id(self, script_id: str) -> Optional[ScriptRecord]:
        return self._one("id = ?", (str(script_id),))

    # 只读标记来自旁表；缺失时按只读处理（默认拒绝写，不默认放行）
    _SELECT = (
        "SELECT c.*, COALESCE(l.readonly, 1) AS readonly "
        "FROM script_config c "
        "LEFT JOIN script_local l ON l.script_name = c.script_name "
    )

    def list_all(self) -> List[ScriptRecord]:
        """有效脚本，按 id 升序。失效脚本（is_valid=0）不出现在命令清单里。"""
        with self._connect() as conn:
            cur = conn.execute(
                self._SELECT + "WHERE c.is_valid = 1 ORDER BY CAST(c.id AS INTEGER)"
            )
            return [_row_to_record(row) for row in cur.fetchall()]

    def writable_names(self) -> List[str]:
        """声明为可写的脚本名。启动横幅要把它们列出来。"""
        return sorted(r.script_name for r in self.list_all() if not r.readonly)

    def _one(self, where: str, args: Tuple[Any, ...]) -> Optional[ScriptRecord]:
        with self._connect() as conn:
            cur = conn.execute(self._SELECT + "WHERE c." + where, args)
            row = cur.fetchone()
        return _row_to_record(row) if row is not None else None


def _params_from_config(raw: Optional[str]) -> Tuple[ParamDef, ...]:
    """从 parameter_config JSON 还原参数声明。

    描述在这里丢失是必然的 —— parameter_config 的四个键
    (key/value/type/autoAcquire) 里没有描述位。这是客户数据模型自带的
    信息损失，必须一并复刻：若我们额外存一份描述，本地 API 会返回客户
    API 给不出的信息，调用方据此写出的代码到客户环境就会失效。
    """
    if not raw:
        return ()
    items = json.loads(raw)
    return tuple(
        ParamDef(key=item["key"], type=item["type"]) for item in items
    )


def _row_to_record(row: sqlite3.Row) -> ScriptRecord:
    return ScriptRecord(
        id=row["id"],
        script_type=row["script_type"],
        script_name=row["script_name"],
        database_type=row["database_type"],
        refered_appbusiness=row["refered_appbusiness"],
        kernel_version=row["kernel_version"],
        region=row["region"],
        deployment_form=row["deployment_form"],
        execute_node_type=row["execute_node_type"],
        cluster_deployment_mode=row["cluster_deployment_mode"],
        script_content=row["script_content"],
        params=_params_from_config(row["parameter_config"]),
        scene=row["scene"],
        is_valid=row["is_valid"],
        create_user=row["create_user"],
        create_time=row["create_time"],
        last_modify_user=row["last_modify_user"],
        last_modify_time=row["last_modify_time"],
        is_asyn=row["is_asyn"],
        extend=row["extend"],
        compliance_mode=row["compliance_mode"],
        readonly=bool(row["readonly"]),
    )
