"""Backend 抽象与共享 DBError（后端无关）。"""
from __future__ import annotations

import abc
from typing import Any, Optional, Sequence


class DBError(Exception):
    """连接或查询失败时抛出（与具体后端无关）。"""


class Backend(abc.ABC):
    """各驱动后端的统一接口。Database 门面通过它转发。"""

    name: str
    # 是否提供跨语句持久的会话(会话级 GUC / hypopg 虚拟索引在多次调用间留存)。
    # 持久连接(psycopg2)为 True;无状态子进程后端(gsql)覆盖为 False。
    provides_session: bool = True

    @classmethod
    @abc.abstractmethod
    def open(
        cls, conn: "Any", password: str, read_only: bool = True
    ) -> "Backend":
        ...

    @abc.abstractmethod
    def query(
        self, sql: str, params: Optional[Sequence[Any]] = None
    ) -> tuple[list[str], list[tuple]]:
        ...

    @abc.abstractmethod
    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        ...

    @abc.abstractmethod
    def query_in_rollback(
        self, sql: str, params: Optional[Sequence[Any]] = None
    ) -> tuple[list[str], list[tuple]]:
        ...

    @abc.abstractmethod
    def set_statement_timeout(self, seconds: int) -> None:
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...
