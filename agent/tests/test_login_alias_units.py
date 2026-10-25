"""api 模式的实例别名:部署时登记「别名 → 实例 IP / 库名」,login --list 列出、--alias 登录。

为什么要有:api 模式没有预置的连接清单,用户说「og5」模型只能反问是哪个库(2026-10-16)。
隔离不变:别名是部署配置,不是谁的登录会话 —— 用别名登录照样只属于本对话、每次发新句柄;
--list 不列实例 IP(红线只放行用户自己给的 / 已连上的实例 IP),只列别名、库名与说明。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_LOGIN = _ROOT / "skills" / "gaussdb-login" / "scripts" / "login.py"

from common import config, session  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("login_alias_under_test", _LOGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


login = _load()

CFG = ("connection_mode: api\napi_connection:\n  - host: 127.0.0.1\n    port: 8769\n    token_env: GRMP_AUTH_TOKEN\n"
       "instance_aliases:\n"
       "  - {name: og5, ip: 10.0.0.9, database: postgres, note: 测试库}\n"
       "  - {name: Core, ip: 10.1.2.3, database: coredb, note: ''}\n")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "t")
    (tmp_path / "config.yaml").write_text(CFG, encoding="utf-8")
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


def test_config_reads_aliases(home):
    got = config.instance_aliases()
    assert [(a.name, a.ip, a.database, a.note) for a in got] == [("og5", "10.0.0.9", "postgres", "测试库"),
                                                                 ("Core", "10.1.2.3", "coredb", "")]


def test_list_shows_aliases_without_instance_ips(home, capsys):
    assert login.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "og5" in out and "postgres" in out and "测试库" in out and "--alias" in out
    assert "10.0.0.9" not in out and "10.1.2.3" not in out, "别名清单里的实例 IP 不是用户给的,不列"


def test_login_by_alias_is_the_same_connection_as_by_ip(home, capsys):
    assert login.main(["--alias", "OG5"]) == 0            # 别名大小写不敏感
    (info,) = session.list_sessions()
    conn = session._read(session.path_for(info.handle))
    assert (conn.data_ip, conn.database) == ("10.0.0.9", "postgres")
    assert conn.name == login._api_connection("10.0.0.9", "postgres", config.api_endpoint()).name, \
        "和 --ip/--database 同一条连接:否则大盘上同一个库的报告劈成两份"


def test_each_alias_login_gets_its_own_handle(home):
    assert login.main(["--alias", "og5"]) == 0
    assert login.main(["--alias", "og5"]) == 0
    assert len(session.list_sessions()) == 2, "别名不是共享会话:每个对话各登各的"


def test_unknown_alias_lists_the_known_names_only(home, capsys):
    assert login.main(["--alias", "og7"]) == 2
    err = capsys.readouterr().err
    assert "og7" in err and "og5" in err and "Core" in err and "10.0.0.9" not in err


def test_alias_and_ip_together_is_refused(home, capsys):
    assert login.main(["--alias", "og5", "--ip", "10.0.0.9", "--database", "postgres"]) == 2
    assert "二选一" in capsys.readouterr().err


def test_alias_errors_from_the_deployment_are_shown_on_list(home, capsys):
    (home / "config.yaml").write_text(CFG + "instance_aliases_errors:\n  - '第 2 项「broken line」:要写成 别名=实例IP/库名'\n",
                                      encoding="utf-8")
    login.main(["--list"])
    out = capsys.readouterr().out
    assert "broken line" in out and "运维" in out
    # 这份脚本两条线共用:要同时指明容器版与非容器版各自改哪里,不能只说容器版的环境变量
    assert "GRMP_INSTANCE_ALIASES" in out and "config.yaml 的 instance_aliases" in out
