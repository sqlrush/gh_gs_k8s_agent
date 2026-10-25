"""gaussdb-login 的句柄:登录发句柄并醒目打印;第二次登录不覆盖第一次;--status 列全;
--logout 按句柄退,分不清时拒绝。"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-login" / "scripts"
sys.path.insert(0, str(_ROOT))

from common import session  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("login_session_under_test", _SCRIPTS / "login.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


login = _load()


@pytest.fixture()
def api_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "test-token")
    (tmp_path / "config.yaml").write_text(
        "connection_mode: api\napi_connection:\n  - host: 127.0.0.1\n    port: 8769\n"
        "    token_env: GRMP_AUTH_TOKEN\n", encoding="utf-8")
    monkeypatch.delenv(session.ENV_HANDLE, raising=False)
    session.use(None)
    monkeypatch.setattr(login, "_verify", lambda conn: (True, "ok"))
    monkeypatch.setattr(login, "_probe_role", lambda conn: "主库（in_recovery=false）")
    monkeypatch.setattr(login, "whitelist", lambda conn: ["health.overview"])
    monkeypatch.setattr(login, "_check_database", lambda conn: login.DbCheck("ok", conn.database))
    yield tmp_path
    import os
    os.environ.pop(session.ENV_HANDLE, None)
    session.use(None)


def _login(ip, db):
    assert login.main(["--ip", ip, "--database", db]) == 0


def test_login_prints_handle_and_how_to_pass_it(api_home, capsys):
    _login("10.0.0.9", "core")
    out = capsys.readouterr().out
    (info,) = session.list_sessions()
    assert "会话句柄" in out and info.handle in out
    assert "--session " + info.handle in out


def test_second_login_keeps_the_first_and_says_nothing_about_it(api_home, capsys):
    """串库的根源是第二次登录覆盖第一次——各存各的;越权的根源是横幅把别人的会话列出来——一个字不提。"""
    _login("10.0.0.9", "core")
    capsys.readouterr()
    h_core = session.list_sessions()[0].handle
    _login("10.0.0.20", "report")
    out = capsys.readouterr().out
    assert {s.conn.database for s in session.list_sessions()} == {"core", "report"}
    assert "其他会话" not in out and "core" not in out and h_core not in out


def test_status_without_handle_reveals_nothing(api_home, capsys):
    """用户 C 新对话第一步就是 --status:不能把 A、B 的句柄、IP、库名摆出来。"""
    _login("10.0.0.9", "core")
    _login("10.0.0.20", "report")
    capsys.readouterr()
    login.main(["--status"])
    out = capsys.readouterr().out
    assert "本对话未登录" in out
    for secret in [s.handle for s in session.list_sessions()] + ["core", "report", "10.0.0.9", "10.0.0.20"]:
        assert secret not in out, secret


def test_status_with_handle_shows_only_that_session(api_home, capsys):
    _login("10.0.0.9", "core")
    _login("10.0.0.20", "report")
    capsys.readouterr()
    h_core = next(s.handle for s in session.list_sessions() if s.conn.database == "core")
    h_rep = next(s.handle for s in session.list_sessions() if s.conn.database == "report")
    assert login.main(["--status", "--session", h_core]) == 0
    out = capsys.readouterr().out
    assert h_core in out and "core" in out
    assert h_rep not in out and "report" not in out


def test_logout_by_handle_removes_only_that_session(api_home, capsys):
    _login("10.0.0.9", "core")
    _login("10.0.0.20", "report")
    h_core = next(s.handle for s in session.list_sessions() if s.conn.database == "core")
    assert login.main(["--logout", "--session", h_core]) == 0
    assert [s.conn.database for s in session.list_sessions()] == ["report"]


def test_logout_without_handle_refuses_even_with_one_session_and_all_clears(api_home, capsys):
    _login("10.0.0.9", "core")
    capsys.readouterr()
    assert login.main(["--logout"]) != 0
    assert "--session" in capsys.readouterr().err
    assert len(session.list_sessions()) == 1
    _login("10.0.0.20", "report")
    capsys.readouterr()
    assert login.main(["--logout", "--all"]) == 0
    assert session.list_sessions() == []
