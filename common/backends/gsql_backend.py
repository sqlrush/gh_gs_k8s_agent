"""gsql 后端：本机 TCP 直连 openGauss/GaussDB 的命令行客户端。

每次查询起一个 gsql -c 子进程（无状态）；会话级设置（只读钉、
statement_timeout）作为前缀拼进每次调用。行列结构靠 json_agg 包裹
（见 gsql_protocol）——但**类型保真是有边界的**：绝对值小于 1 的数会被
openGauss 写成带引号的 `".5"`，与文本值无法区分。边界与实测见
gsql_protocol.parse_json_result 的注释，别在别处重复声称「类型保真」。

口令走 `-2/--pipeline`（从 stdin 读），**绝不进 argv**。
openGauss 的 gsql 不认 PGPASSWORD —— 实测（openGauss-lite 5.0.3）：

    只给 PGPASSWORD   rc=2  'Password for user gaussdb: ... Invalid username/password'
    用 -2 管道传口令   rc=0  '1'

此前这里只设 PGPASSWORD，于是 gsql 会去等一个没人喂的交互口令提示，
一直卡到超时，报出来是「gsql timed out」—— 看起来像网络慢或库卡住，
而实际是根本没通过认证。PGPASSWORD 一并保留：对认它的客户端无害，
少一个环境差异。argv 里传口令（-W）不用，那样 `ps` 能看见。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Optional

from .base import Backend, DBError
from . import gsql_protocol as gp

CONNECT_TIMEOUT = 15  # 秒，对齐 psycopg2


class GsqlBackend(Backend):
    name = "gsql"
    provides_session = False  # 每查询一个子进程:会话级状态/hypopg 虚拟索引不跨语句留存

    def __init__(self, conn: Any, password: str, binary: str,
                 read_only: bool = True):
        self.conn = conn
        self._password = password
        self._binary = binary
        self._read_only = read_only
        self._timeout_ms: Optional[int] = None

    @classmethod
    def open(cls, conn: Any, password: str, read_only: bool = True) -> "GsqlBackend":
        binary = shutil.which(os.environ.get("GDAA_GSQL", "gsql"))
        if not binary:
            raise DBError(
                "gsql binary not found (set GDAA_GSQL or add gsql to PATH)"
            )
        b = cls(conn, password, binary, read_only=read_only)
        b._run("SELECT 1", {})  # 验活；失败抛 DBError 供门面兜底
        return b

    # ---- 内部 ----

    def _env(self) -> dict:
        env = dict(os.environ)
        env["PGPASSWORD"] = self._password
        sslmode = self.conn.sslmode or "disable"
        env["PGSSLMODE"] = sslmode
        return env

    def _prefix(self, *, read_only: bool) -> str:
        parts = []
        if read_only:
            parts.append("SET default_transaction_read_only = on;")
        if self._timeout_ms is not None:
            parts.append(f"SET statement_timeout = {self._timeout_ms};")
        return " ".join(parts)

    def _run(self, full_sql: str, vars_: dict, *, tuples_only: bool = True) -> str:
        argv = [
            self._binary,
            "-h", str(self.conn.host),
            "-p", str(self.conn.port),
            "-U", self.conn.user,
            "-d", self.conn.database,
            "-A", "-q",
            "-2",                      # 口令从 stdin 读，不进 argv
            "-v", "ON_ERROR_STOP=1",
            "-v", "VERBOSITY=verbose",
        ]
        if tuples_only:
            argv.append("-t")          # 去表头；带表头的场景见 query()
        for name, value in vars_.items():
            argv += ["-v", f"{name}={value}"]
        argv += ["-c", full_sql]
        try:
            cp = subprocess.run(
                argv, capture_output=True, text=True,
                input=self._password + "\n",   # -2 从这里取口令
                env=self._env(), timeout=CONNECT_TIMEOUT + (
                    (self._timeout_ms or 0) // 1000),
            )
        except FileNotFoundError as exc:
            raise DBError(f"gsql not executable: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DBError(f"gsql timed out: {exc}") from exc
        if cp.returncode != 0:
            raise DBError(gp.parse_gsql_error(cp.stderr))
        return cp.stdout

    # ---- Backend 接口 ----

    def _columns_of(self, body: str, vars_: dict) -> list:
        """零行时单独取一次真实列名。

        `WHERE false` 保证取不到数据行，而 -A 不带 -t 时 gsql 仍会打印表头 ——
        于是拿到的是真实列名，不是猜的。只在结果为空时才多跑这一次。
        """
        inner = body.rstrip().rstrip(";")
        probe = f"SELECT * FROM ({inner}) AS __cols WHERE false"
        full = f"{self._prefix(read_only=self._read_only)} {probe}".strip()
        cols, _ = gp.parse_text_result_with_header(
            self._run(full, vars_, tuples_only=False))
        return cols

    def query(self, sql, params=None):
        body, vars_ = gp.rewrite_params(sql, params or ())
        if gp.is_wrappable_select(body):
            stmt = gp.wrap_select_json(body)
            full = f"{self._prefix(read_only=self._read_only)} {stmt}".strip()
            cols, rows = gp.parse_json_result(self._run(full, vars_))
            if not cols:
                # 零行时 json_agg 返回 NULL，列名无从得知。但「查到 0 行」和
                # 「这条语句没有结果集」是两件事 —— runner 正是用 cols 区分
                # 它们的。不补列名的话，driver: gsql 下任何空结果都会被报成
                # 「未返回结果集」：slowsql 阈值调高查不到慢 SQL 会失败，
                # memanalyze 探测不存在的视图会直接抛 Traceback。
                cols = self._columns_of(body, vars_)
            return cols, rows
        # 非 SELECT（EXPLAIN / SHOW …）走文本旁路。**带表头跑**：runner 用
        # `if not cols` 判断有没有结果集，空列名会被判成「未返回结果集」，
        # 而 psycopg2 那条路会给出 QUERY PLAN —— 同一条脚本两个驱动结果不同。
        full = f"{self._prefix(read_only=self._read_only)} {body}".strip()
        return gp.parse_text_result_with_header(
            self._run(full, vars_, tuples_only=False))

    def execute(self, sql, params=None) -> None:
        body, vars_ = gp.rewrite_params(sql, params or ())
        full = f"{self._prefix(read_only=self._read_only)} {body}".strip()
        self._run(full, vars_)

    def query_in_rollback(self, sql, params=None):
        # 始终走文本旁路，返回形式为 ([], [(line,), ...])。
        # 当前第一方消费者仅为 EXPLAIN ANALYZE 纯文本输出，不涉及行返回语句。
        # 若将来有行返回语句走此路径，须注意其行形与 psycopg2 的 (cols, rows) 不同。
        body, vars_ = gp.rewrite_params(sql, params or ())
        parts = ["BEGIN;"]
        prefix = self._prefix(read_only=False)  # 回滚路径不注入只读钉
        if prefix:
            parts.append(prefix)
        parts.append(f"{body};")
        parts.append("ROLLBACK;")
        full = " ".join(parts)
        return gp.parse_text_result(self._run(full, vars_))

    def set_statement_timeout(self, seconds: int) -> None:
        self._timeout_ms = int(seconds) * 1000

    def close(self) -> None:
        pass  # 无常驻连接
