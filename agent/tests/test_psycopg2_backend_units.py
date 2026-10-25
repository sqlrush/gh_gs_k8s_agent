# tests/test_psycopg2_backend_units.py
"""psycopg2 后端(取代原 pg8000 后端)。

与被取代的 pg8000 相比有三处行为差异,每一处都有测试钉住:
  · 连接参数走 libpq 原生的 connect_timeout / sslmode,不再自己造 SSLContext;
  · psycopg2 不暴露 socket,查询超时**只由服务端 statement_timeout 管**——
    pg8000 那套 `_usock` 客户端兜底连同它的坑一起消失;
  · 无参数时必须给 execute 传 None 而不是空元组:psycopg2 收到参数容器就会做
    %-插值,registry 里 `LIKE '/* missing SQL statement%'` 这种字面量百分号会当场炸。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.backends import psycopg2_backend as pgb  # noqa: E402
from common.backends.base import DBError  # noqa: E402
from common.config import Connection  # noqa: E402


class FakeCursor:
    def __init__(self, desc, rows):
        self._rows, self.description = rows, desc
        self.calls = []

    def execute(self, sql, params=None):
        self.last = (sql, params)
        self.calls.append((sql, params))

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, desc=None, rows=None):
        self.autocommit = False
        self._cur = FakeCursor(desc, rows or [])
        self.executed = []

    def cursor(self):
        self.executed.append("cursor")
        return self._cur

    def commit(self):
        self.executed.append("commit")

    def rollback(self):
        self.executed.append("rollback")

    def close(self):
        self.executed.append("close")


def _conn(sslmode=""):
    return Connection(name="a", type="opengauss", host="h", port=5432, database="d",
                      user="u", sslmode=sslmode)


def _patch(monkeypatch, fake, capture=None):
    def connect(**kw):
        if capture is not None:
            capture.update(kw)
        return fake
    monkeypatch.setattr(pgb.psycopg2, "connect", connect)


# --- 打开连接 ---------------------------------------------------------------

def test_open_pins_read_only(monkeypatch):
    fake = FakeConn()
    _patch(monkeypatch, fake)
    pgb.Psycopg2Backend.open(_conn(), "pw", read_only=True)
    assert fake.autocommit is True
    assert fake._cur.last[0] == "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"


def test_open_skips_pin_when_not_read_only(monkeypatch):
    fake = FakeConn()
    _patch(monkeypatch, fake)
    pgb.Psycopg2Backend.open(_conn(), "pw", read_only=False)
    assert "cursor" not in fake.executed
    assert not hasattr(fake._cur, "last")


def test_connect_uses_libpq_timeout_and_sslmode(monkeypatch):
    """握手必须有界;sslmode 直接交给 libpq,不再自己造 SSLContext。"""
    seen = {}
    _patch(monkeypatch, FakeConn(), seen)
    pgb.Psycopg2Backend.open(_conn(), "pw")
    assert seen["connect_timeout"] == pgb.CONNECT_TIMEOUT
    assert seen["dbname"] == "d" and seen["user"] == "u" and seen["port"] == 5432
    assert seen["sslmode"] == "disable"          # 未配 sslmode 时与 pg8000 时代一致:不加密


def test_configured_sslmode_is_passed_through(monkeypatch):
    seen = {}
    _patch(monkeypatch, FakeConn(), seen)
    pgb.Psycopg2Backend.open(_conn(sslmode="require"), "pw")
    assert seen["sslmode"] == "require"


def test_unknown_sslmode_is_rejected_not_silently_downgraded(monkeypatch):
    """写错的 sslmode 不能被悄悄当成不加密——那是把加密要求静默丢掉。"""
    _patch(monkeypatch, FakeConn())
    with pytest.raises(DBError) as ei:
        pgb.Psycopg2Backend.open(_conn(sslmode="verify-none"), "pw")
    assert "sslmode" in str(ei.value) and "verify-none" in str(ei.value)


def test_open_connect_failure_raises_dberror(monkeypatch):
    def boom(**kw):
        raise RuntimeError("refused")
    monkeypatch.setattr(pgb.psycopg2, "connect", boom)
    with pytest.raises(DBError):
        pgb.Psycopg2Backend.open(_conn(), "pw")


# --- 查询 -------------------------------------------------------------------

def test_query_returns_cols_and_rows(monkeypatch):
    fake = FakeConn(desc=[("a",), ("b",)], rows=[(1, "x")])
    _patch(monkeypatch, fake)
    b = pgb.Psycopg2Backend.open(_conn(), "pw", read_only=False)
    cols, rows = b.query("select 1 a, 'x' b")
    assert cols == ["a", "b"] and rows == [(1, "x")]


def test_no_params_means_none_so_literal_percent_survives(monkeypatch):
    """registry SQL 里有 `LIKE '/* missing SQL statement%'`。psycopg2 只要收到
    参数容器(哪怕空元组)就会做 %-插值并在这个百分号上报错;必须传 None。"""
    fake = FakeConn(desc=[("a",)], rows=[])
    _patch(monkeypatch, fake)
    b = pgb.Psycopg2Backend.open(_conn(), "pw", read_only=False)
    sql = "select 1 where query not like '/* missing SQL statement%'"
    b.query(sql)
    b.execute(sql)
    assert [c[1] for c in fake._cur.calls] == [None, None]


def test_params_are_passed_when_present(monkeypatch):
    fake = FakeConn(desc=[("a",)], rows=[])
    _patch(monkeypatch, fake)
    b = pgb.Psycopg2Backend.open(_conn(), "pw", read_only=False)
    b.query("select %s", (7,))
    assert fake._cur.last == ("select %s", (7,))


def test_query_in_rollback_rolls_back_and_restores_autocommit(monkeypatch):
    fake = FakeConn(desc=[("QUERY PLAN",)], rows=[("Seq Scan",)])
    _patch(monkeypatch, fake)
    b = pgb.Psycopg2Backend.open(_conn(), "pw", read_only=False)
    cols, rows = b.query_in_rollback("explain analyze update t set a = 1")
    assert cols == ["QUERY PLAN"] and rows == [("Seq Scan",)]
    assert "rollback" in fake.executed
    assert fake.autocommit is True


# --- 错误信息 ---------------------------------------------------------------

class FakeDiag:
    message_primary = "relation \"nope\" does not exist"


class FakePgError(Exception):
    pgcode = "42P01"
    diag = FakeDiag()


def test_error_keeps_the_sqlstate_shape_skills_match_on(monkeypatch):
    """报错形态与 pg8000 时代逐字一致:`ERROR: <msg> (SQLSTATE <code>)`。"""
    fake = FakeConn()
    _patch(monkeypatch, fake)
    b = pgb.Psycopg2Backend.open(_conn(), "pw", read_only=False)

    def boom(sql, params=None):
        raise FakePgError("relation \"nope\" does not exist")
    fake._cur.execute = boom
    with pytest.raises(DBError) as ei:
        b.query("select * from nope")
    assert str(ei.value) == 'ERROR: relation "nope" does not exist (SQLSTATE 42P01)'


def test_error_without_sqlstate_still_readable(monkeypatch):
    fake = FakeConn()
    _patch(monkeypatch, fake)
    b = pgb.Psycopg2Backend.open(_conn(), "pw", read_only=False)

    def boom(sql, params=None):
        raise RuntimeError("server closed the connection unexpectedly")
    fake._cur.execute = boom
    with pytest.raises(DBError) as ei:
        b.query("select 1")
    assert "server closed the connection" in str(ei.value)


# --- 超时 -------------------------------------------------------------------

def test_statement_timeout_is_server_side_only(monkeypatch):
    """psycopg2 不暴露 socket:超时只由服务端管,不再有客户端兜底那套 hack。"""
    fake = FakeConn()
    _patch(monkeypatch, fake)
    b = pgb.Psycopg2Backend.open(_conn(), "pw", read_only=False)
    b.set_statement_timeout(60)
    assert fake._cur.last[0] == "SET statement_timeout = 60000"
    b.set_statement_timeout(0)
    assert fake._cur.last[0] == "SET statement_timeout = 0"


def test_provides_session_stays_true(monkeypatch):
    """hypopg 虚拟索引验证依赖跨语句持久会话——换驱动不能把这个能力弄丢。"""
    assert pgb.Psycopg2Backend.provides_session is True
    assert pgb.Psycopg2Backend.name == "psycopg2"
