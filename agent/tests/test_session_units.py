"""common.session —— 会话按句柄分文件;多会话又没给句柄时拒绝,不猜。

现场缺陷(2026-09-07 客户反馈):多个用户共用一个沙箱、一个 GSDB_HOME、一个 session.yaml,
谁最后登录所有人就连谁的库,退出码 0、不报错、报告抬头写着别人的库——静默串库。
修法是把「一个沙箱一张便签」改成「一次登录一个句柄一张便签」。这里钉住的行为:

  · 每次 save 得到一个新句柄,写 sessions/<句柄>.yaml(0600),互不覆盖;
  · 只有一个会话时不用句柄(单用户沙箱行为不变);
  · 多个会话又没句柄 → ConfigError,把候选列全(句柄 / ip / 库),让模型去问用户;
  · 句柄来源:use() > 环境变量 GSDB_SESSION;形状不合法(路径穿越)一律拒;
  · 过期会话不算数且顺手删掉;读一次就更新最后使用时间;
  · 旧的单文件 session.yaml 在没有任何句柄会话时仍可用(升级过渡)。
"""
import os
import pathlib
import sys
import time

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import session  # noqa: E402
from common.config import ConfigError, Connection  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    monkeypatch.delenv(session.ENV_HANDLE, raising=False)
    session.use(None)
    yield tmp_path
    os.environ.pop(session.ENV_HANDLE, None)
    session.use(None)


def _conn(ip: str, db: str) -> Connection:
    return Connection(name="%s-%s" % (ip.replace(".", "-"), db), type="gaussdb", host="127.0.0.1",
                      port=8769, database=db, user="grmp", driver="grmp", data_ip=ip, app="api")


def test_each_login_gets_its_own_file(home):
    h1 = session.save(_conn("10.0.0.9", "core"))
    h2 = session.save(_conn("10.0.0.20", "report"))
    assert h1 != h2 and session.HANDLE_RE.match(h1) and session.HANDLE_RE.match(h2)
    files = sorted(p.name for p in (home / "sessions").glob("*.yaml"))
    assert files == sorted([h1 + ".yaml", h2 + ".yaml"])
    assert oct(session.path_for(h1).stat().st_mode & 0o777) == "0o600"


def test_no_handle_means_not_logged_in_even_with_one_session(home):
    """客户 09-11 反馈的越权:A 登录后 C 进来什么都不带就直接跑在 A 的库上。会话只属于创建它的对话,没句柄就是没登录。"""
    session.save(_conn("10.0.0.9", "core"))
    assert session.current() is None and session.current_handle() is None


def test_no_handle_never_reveals_other_sessions(home):
    """越权的根子:不带句柄时把别人的句柄、IP、库名列出来让人挑。现在一律「本对话未登录」,一个字都不透露。"""
    from common import config
    h1 = session.save(_conn("10.0.0.9", "core"))
    h2 = session.save(_conn("10.0.0.20", "report"))
    assert session.current() is None
    with pytest.raises(ConfigError) as ei:
        config.resolve("")
    msg = str(ei.value)
    assert "本对话未登录" in msg and "gaussdb-login" in msg and "--session" in msg
    for secret in (h1, h2, "10.0.0.9", "10.0.0.20", "core", "report", "2 个"):
        assert secret not in msg, secret


def test_handle_selects_the_right_session(home):
    h1 = session.save(_conn("10.0.0.9", "core"))
    h2 = session.save(_conn("10.0.0.20", "report"))
    session.use(h1)
    assert session.current().database == "core" and session.current_handle() == h1
    session.use(h2)
    assert session.current().database == "report"


def test_env_var_carries_the_handle(home, monkeypatch):
    """宿主将来能按用户注入环境变量时零改动接上;health 派生的子 skill 也靠它继承。"""
    session.save(_conn("10.0.0.9", "core"))
    h2 = session.save(_conn("10.0.0.20", "report"))
    monkeypatch.setenv(session.ENV_HANDLE, h2)
    assert session.current().database == "report"


def test_malformed_or_unknown_handle_is_refused(home):
    session.save(_conn("10.0.0.9", "core"))
    for bad in ("../etc", "a/b", "A B", "x" * 40, "ab"):     # 空串是「不选」,不在此列
        with pytest.raises(ConfigError):
            session.use(bad)
    session.use("zzzz9")                       # 形状合法但不存在
    with pytest.raises(ConfigError) as ei:
        session.current()
    assert "zzzz9" in str(ei.value) and "gaussdb-login" in str(ei.value)


def test_expired_sessions_are_ignored_and_removed(home):
    """不清的话,用户走了以后沙箱里残留一堆会话,后来的人每条命令都被拦。"""
    h_old = session.save(_conn("10.0.0.9", "core"))
    stale = time.time() - (session.ttl_seconds() + 3600)
    os.utime(session.path_for(h_old), (stale, stale))
    h_new = session.save(_conn("10.0.0.20", "report"))
    session.use(h_new)
    assert session.current().database == "report"
    assert not session.path_for(h_old).exists()


def test_reading_touches_last_used(home):
    h = session.save(_conn("10.0.0.9", "core"))
    old = time.time() - 3600
    os.utime(session.path_for(h), (old, old))
    session.use(h)
    session.current()
    assert session.path_for(h).stat().st_mtime > old + 1800


def test_legacy_single_file_is_ignored_and_removed_on_next_login(home):
    """旧版单文件 session.yaml 也是「谁都能用」的口子:不再认,登录时顺手删。"""
    legacy = _conn("10.0.0.9", "legacy")
    (home / "session.yaml").write_text(yaml.safe_dump({
        "name": legacy.name, "type": legacy.type, "host": legacy.host, "port": legacy.port,
        "database": legacy.database, "user": legacy.user, "sslmode": "", "driver": "grmp",
        "data_ip": legacy.data_ip, "app": "api"}), encoding="utf-8")
    assert session.current() is None
    h = session.save(_conn("10.0.0.20", "core"))
    assert not (home / "session.yaml").exists()
    session.use(h)
    assert session.current().database == "core"


def test_list_and_clear_by_handle(home):
    h1 = session.save(_conn("10.0.0.9", "core"))
    h2 = session.save(_conn("10.0.0.20", "report"))
    listed = session.list_sessions()
    assert {s.handle for s in listed} == {h1, h2}
    assert {s.conn.database for s in listed} == {"core", "report"}
    assert session.clear(h1) is True
    assert [s.handle for s in session.list_sessions()] == [h2]
    assert session.clear("nope1") is False
    assert session.clear(h2) is True and session.list_sessions() == []


def test_clear_without_handle_always_refuses(home):
    """退出也要指名:不带句柄退掉「唯一那个」,退的可能是别人的。"""
    h1 = session.save(_conn("10.0.0.9", "core"))
    with pytest.raises(ConfigError):
        session.clear()
    assert session.clear(h1) is True
    session.save(_conn("10.0.0.20", "report"))
    assert session.clear_all() == 1


def test_handles_are_twelve_random_chars(home):
    hs = {session.new_handle() for _ in range(50)}
    assert len(hs) == 50 and all(len(h) == 12 and session.HANDLE_RE.match(h) for h in hs)


def test_unknown_keys_in_a_session_file_are_still_refused(home):
    h = session.save(_conn("10.0.0.9", "core"))
    path = session.path_for(h)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["dataip"] = raw.pop("data_ip")
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        session.current()
    assert "dataip" in str(ei.value)


def _api_config(home_dir: pathlib.Path, host: str, port: int = 8080) -> None:
    (home_dir / "config.yaml").write_text(
        "connection_mode: api\napi_connection:\n  - host: %s\n    port: %d\n" % (host, port),
        encoding="utf-8")


def test_grmp_session_takes_middleware_address_from_current_config(home, monkeypatch):
    """会话文件是登录那一刻的快照;中间件换了地址,老对话的句柄要打到新地址(09-14 现场问题)。"""
    _api_config(home, "old-grmp.internal")
    conn = Connection(name="a", type="gaussdb", host="old-grmp.internal", port=8080,
                      database="db1", user="grmp", driver="grmp", data_ip="10.0.0.5", app="api")
    handle = session.save(conn)
    _api_config(home, "new-grmp.internal", 9090)
    monkeypatch.delenv("GRMP_API_HOST", raising=False)
    session.use(handle)
    got = session.current()
    assert got.host == "new-grmp.internal" and got.port == 9090
    assert got.data_ip == "10.0.0.5" and got.database == "db1"


def test_grmp_session_prefers_env_host_over_config(home, monkeypatch):
    _api_config(home, "cfg-grmp.internal")
    conn = Connection(name="a", type="gaussdb", host="x", port=8080,
                      database="db1", user="grmp", driver="grmp", data_ip="10.0.0.5", app="api")
    handle = session.save(conn)
    monkeypatch.setenv("GRMP_API_HOST", "env-grmp.internal")
    session.use(handle)
    assert session.current().host == "env-grmp.internal"


def test_gsql_session_keeps_its_own_host(home, monkeypatch):
    conn = Connection(name="og", type="opengauss", host="db-host", port=5432,
                      database="db1", user="u", driver="gsql")
    handle = session.save(conn)
    session.use(handle)
    assert session.current().host == "db-host" and session.current().port == 5432
