"""psycopg2 后端：openGauss/GaussDB 的 PostgreSQL wire 协议直连。

会话默认钉 READ ONLY。与被取代的 pg8000 后端相比有三处实质差异：

1. 连接参数交给 libpq 原生的 `connect_timeout` / `sslmode`，不再自己造 SSLContext；
2. **查询超时只由服务端 `statement_timeout` 负责**。pg8000 那边要额外摆弄
   `Connection._usock`，是因为它把 connect 的 timeout 当成整条连接的 socket 超时，
   跑得久的查询会被客户端掐断；psycopg2 不暴露 socket，也没有这个毛病，
   于是那段私有属性 hack 连同它的坑一并删掉；
3. 没有绑定参数时给 `execute` 传 **None** 而不是空元组 —— psycopg2 只要收到参数
   容器就会做 %-插值，registry 里 `LIKE '/* missing SQL statement%'` 这种字面量
   百分号会当场报 unsupported format character。
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

import psycopg2

from .base import Backend, DBError

CONNECT_TIMEOUT = 15  # 秒，对齐 gdaa pingTimeout

# libpq 的取值全集。`disable` 也在内：不配 sslmode 时沿用 pg8000 时代的行为（不加密）。
_SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})


def _sslmode(raw: str) -> str:
    mode = (raw or "disable").strip()
    if mode not in _SSL_MODES:
        raise DBError(
            f"sslmode {mode!r} 不是 libpq 认识的取值，只能是 "
            f"{'/'.join(sorted(_SSL_MODES))}；写错的值不会被当成不加密放行。"
        )
    return mode


def _format_pg_error(exc: Exception) -> str:
    """`ERROR: <消息> (SQLSTATE <码>)` —— 与 pg8000 时代逐字一致的形态。

    按属性取值而不是 isinstance：psycopg2 的异常层级深，且测试替身不必是真异常类。
    """
    diag = getattr(exc, "diag", None)
    msg = getattr(diag, "message_primary", None) or str(exc)
    code = getattr(exc, "pgcode", None)
    if code:
        return f"ERROR: {msg} (SQLSTATE {code})"
    return str(exc) if msg == str(exc) else f"ERROR: {msg}"


def _args(params: Optional[Sequence[Any]]):
    """空参数一律 None：见模块头第 3 条。"""
    return params if params else None


class Psycopg2Backend(Backend):
    name = "psycopg2"
    provides_session = True  # 单条持久连接:会话级 GUC / hypopg 虚拟索引跨语句留存

    def __init__(self, raw: Any, conn: Any):
        self._raw = raw
        self.conn = conn

    @classmethod
    def open(cls, conn: Any, password: str, read_only: bool = True) -> "Psycopg2Backend":
        sslmode = _sslmode(getattr(conn, "sslmode", ""))     # 值不合法时在连库之前就报
        try:
            raw = psycopg2.connect(
                host=conn.host,
                port=int(conn.port),
                dbname=conn.database,
                user=conn.user,
                password=password,
                connect_timeout=CONNECT_TIMEOUT,
                sslmode=sslmode,
            )
        except Exception as exc:
            raise DBError(
                f"connect to {conn.name} "
                f"({conn.user}@{conn.host}:{conn.port}/{conn.database}): {_format_pg_error(exc)}"
            ) from exc

        raw.autocommit = True
        b = cls(raw, conn)
        if read_only:
            try:
                b.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            except DBError:
                b.execute("SET default_transaction_read_only = on")
        return b

    def query(self, sql, params=None):
        cur = self._raw.cursor()
        try:
            cur.execute(sql, _args(params))
            cols = [d[0] for d in (cur.description or [])]
            rows = [tuple(r) for r in cur.fetchall()] if cur.description else []
            return cols, rows
        except Exception as exc:
            raise DBError(_format_pg_error(exc)) from exc
        finally:
            cur.close()

    def query_in_rollback(self, sql, params=None):
        prev = self._raw.autocommit
        self._raw.autocommit = False
        cur = self._raw.cursor()
        try:
            cur.execute(sql, _args(params))
            cols = [d[0] for d in (cur.description or [])]
            rows = [tuple(r) for r in cur.fetchall()] if cur.description else []
            return cols, rows
        except Exception as exc:
            raise DBError(_format_pg_error(exc)) from exc
        finally:
            try:
                self._raw.rollback()
            finally:
                cur.close()
                self._raw.autocommit = prev

    def set_statement_timeout(self, seconds: int) -> None:
        """服务端超时是唯一的查询期限（0 = 不限）。客户端不再另设 socket 超时。"""
        self.execute(f"SET statement_timeout = {int(seconds) * 1000}")

    def execute(self, sql, params=None) -> None:
        cur = self._raw.cursor()
        try:
            cur.execute(sql, _args(params))
        except Exception as exc:
            raise DBError(_format_pg_error(exc)) from exc
        finally:
            cur.close()

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass
