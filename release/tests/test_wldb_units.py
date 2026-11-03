"""白名单「只读账号直连」:只读会话、语句超时、不带参数传 None、口令不外露、报错翻译;扫描与核对步骤怎么用它(假驱动)。"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import scan, steps, whitelist, wldb  # noqa: E402
from release.engine import StepFailed  # noqa: E402
from release.store import Store  # noqa: E402
from release.tests.test_scan_units import GOOD, FakeKube  # noqa: E402

PW = "Wl#s3cret-77"
CFG = {"whitelist.source": "db", "whitelist.db.host": "10.1.2.3", "whitelist.db.port": "8000",
       "whitelist.db.database": "grmp", "whitelist.db.user": "wl_reader"}
ROWS = [("health.summary", "a" * 32, "[]", 1), ("sqltune.explain", "b" * 32, '[{"key":"sql"}]', 1)]


class FakeConn:
    def __init__(self, fail_on=""):
        self.sql, self.fail_on, self.closed, self.autocommit = [], fail_on, False, False

    def cursor(self):
        conn = self

        class Cur:
            def execute(self, sql, params):
                assert params is None, "不带参数必须传 None(psycopg2 收到容器会做 % 插值)"
                assert conn.autocommit, "先开 autocommit 再设只读,只读才对查询那句生效"
                conn.sql.append(sql)
                if conn.fail_on and conn.fail_on in sql:
                    raise RuntimeError('permission denied for relation script_config')

            def fetchall(self):
                return [list(r) for r in ROWS]
        return Cur()

    def close(self):
        self.closed = True


def _connect(conn=None, error=None, seen=None):
    def connect(**kw):
        if seen is not None:
            seen.update(kw)
        if error:
            raise RuntimeError(error)
        return conn
    return connect


def test_reads_rows_in_a_read_only_session_with_a_timeout():
    conn, seen = FakeConn(), {}
    got = wldb.rows(CFG, PW, connect=_connect(conn, seen=seen))
    assert got == [tuple(r) for r in ROWS]
    assert seen["host"] == "10.1.2.3" and seen["port"] == 8000 and seen["dbname"] == "grmp" and seen["user"] == "wl_reader"
    assert seen["password"] == PW and seen["connect_timeout"] == wldb.CONNECT_TIMEOUT_S
    assert conn.sql == ["SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY",
                        "SET statement_timeout = %d" % wldb.STATEMENT_TIMEOUT_MS, whitelist.EXPORT_SQL]
    assert conn.closed


def test_missing_settings_are_named_before_connecting():
    with pytest.raises(wldb.DbError, match="缺地址、口令"):
        wldb.rows(dict(CFG, **{"whitelist.db.host": ""}), "", connect=_connect(error="不该连"))
    with pytest.raises(wldb.DbError, match="端口不是数字"):
        wldb.rows(dict(CFG, **{"whitelist.db.port": "80x"}), PW, connect=_connect(error="不该连"))


@pytest.mark.parametrize("raw, says", [
    # 原文都是真库上取的(openGauss 5.0.3 + 镜像里的 psycopg2-binary):sha256 账号、口令错、库名错、端口没人听
    ("connection to server at \"127.0.0.1\", port 5498 failed: none of the server's SASL authentication mechanisms are supported",
     "sha256"),
    ("authentication method 10 not supported", "sha256"),
    ("connection to server at \"127.0.0.1\", port 5433 failed: FATAL:  Invalid username/password,login denied.", "账号或口令不对"),
    ("connection to server at \"127.0.0.1\", port 5499 failed: Connection refused", "连不上"),
    ("connection to server at \"10.1.2.3\", port 8000 failed: FATAL:  password authentication failed for user \"wl_reader\"",
     "账号或口令不对"),
    ("FATAL:  no pg_hba.conf entry for host \"10.9.9.9\", user \"wl_reader\", database \"grmp\"", "pg_hba"),
    ("FATAL:  database \"grmpx\" does not exist", "库名不对"),
    ("connection to server at \"10.1.2.3\", port 8000 failed: timeout expired", "连不上"),
    ("could not translate host name \"grmp-db\" to address: Name or service not known", "连不上"),
    ("something new", "查询失败"),
])
def test_connect_errors_say_what_to_do(raw, says):
    with pytest.raises(wldb.DbError) as e:
        wldb.rows(CFG, PW, connect=_connect(error=raw))
    msg = str(e.value)
    assert says in msg and "10.1.2.3:8000/grmp(账号 wl_reader)" in msg and PW not in msg
    assert raw.splitlines()[0][:40] in msg, "原文附在后面,不吞"


def test_query_errors_are_translated_and_the_connection_is_closed():
    conn = FakeConn(fail_on="script_config")
    with pytest.raises(wldb.DbError, match="没有 grmp.script_config 的查询权限"):
        wldb.rows(CFG, PW, connect=_connect(conn))
    assert conn.closed
    assert "库里没有 grmp.script_config" in wldb.explain('relation "grmp.script_config" does not exist', "x")
    assert "查询超过 30 秒" in wldb.explain("canceling statement due to statement timeout", "x")


def _scan_wl(tmp_path, db_rows):
    st = Store(tmp_path / "r.db")
    st.save_config(dict(CFG, **{"smoke.user": "u9500", "registry.mode": "preloaded"}))
    st.set_secret("whitelist_password", PW, keep=True)     # 发版台自己用的密钥存值(TOOL_SECRETS)
    (tmp_path / "rel").mkdir(exist_ok=True)
    s = scan.Scanner(store=st, kube=FakeKube(GOOD), releases=tmp_path / "rel", db_rows=db_rows)
    return s.whitelist()


def test_scan_connects_and_counts_rows(tmp_path):
    seen = {}
    it = _scan_wl(tmp_path, lambda cfg, pw: seen.update(pw=pw) or list(ROWS))
    assert (it.st, it.msg) == (scan.OK, "连通 · 读到 2 条") and seen["pw"] == PW
    assert it.addr == "只读账号 10.1.2.3:8000/grmp"
    it = _scan_wl(tmp_path / "b", lambda cfg, pw: [])
    assert it.st == scan.WARN and "一条都没有" in it.msg


def test_scan_blocks_on_a_database_error_with_the_reason(tmp_path):
    def boom(cfg, pw):
        raise wldb.DbError("10.1.2.3:8000/grmp(账号 wl_reader) 账号或口令不对")
    it = _scan_wl(tmp_path, boom)
    assert it.st == scan.BAD and "账号或口令不对" in it.msg


def test_the_whitelist_step_turns_a_database_error_into_a_plain_failure(tmp_path):
    st = Store(tmp_path / "r.db")
    st.save_config(CFG)

    def boom(cfg, pw):
        raise wldb.DbError("连不上")
    svc = steps.Services(store=st, kube=None, releases=tmp_path, state=tmp_path, db_rows=boom)
    with pytest.raises(StepFailed, match="连不上"):
        steps.registered_scripts(svc, st.config())
    svc = steps.Services(store=st, kube=None, releases=tmp_path, state=tmp_path, db_rows=lambda cfg, pw: list(ROWS))
    assert [r.name for r in steps.registered_scripts(svc, st.config())] == ["health.summary", "sqltune.explain"]
