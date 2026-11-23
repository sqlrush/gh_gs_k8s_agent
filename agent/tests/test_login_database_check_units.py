"""api 模式登录时核对库名(v1.1.6 待修第 8 项)。

**静默失效(2026-10-14 抓到):** api 模式下库名根本不发给中间件 —— 报文里只有 dataIp / id / param,
GRMP 一个 dataIp 只对一个库(2026-09-08)。用户把库名写错(那次模型写成了 og5)不会报错,
只会变成报告抬头与大盘上的一个错误标签,还把同一个库的历史劈成两份。

修法:登录时跑白名单里**已有**的 locate_context(返回 current_database() 与实例上的库清单),
不一致就不建会话、告诉用户实际库名。只差大小写时按实际库名登录。
脚本没登记(现场没导入那批白名单)时不拦,但输出里要明写「库名未核实」—— 不能假装核过。

2026-11-06 行方:中间件对各实例都固定连 postgres,用户填业务库名一律对不上,「不一致就拦」让 api 模式谁都登不上。
改为:库名在实例库清单里就放行(routed)—— 会话按用户要诊断的库登记,取数库另记 exec_database。
v1.4.9 行方要求库名限制全部放开:不在清单里(unlisted)也登录,输出里摆出库清单请用户确认名字。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_LOGIN = _ROOT / "skills" / "gaussdb-login" / "scripts" / "login.py"

from common import session  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("login_dbcheck_under_test", _LOGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


login = _load()


class _Endpoint:
    port = 8769

    def resolve_host(self):
        return "127.0.0.1"


class _Runner:
    def __init__(self, rows=None, missing=()):
        self.rows, self.missing, self.ran = rows, set(missing), []

    def run(self, script, values=None):
        self.ran.append(script)
        if script in self.missing:
            raise RuntimeError("脚本 %s 不存在" % script)
        return self.rows


def _conn(db="postgres"):
    return login._api_connection("10.0.0.9", db, _Endpoint())


ROW = [{"db": "postgres", "usr": "grmp_ro", "databases": "appdb, postgres, reportdb"}]


def test_matching_name_is_confirmed(monkeypatch):
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(ROW))
    chk = login._check_database(_conn())
    assert chk.status == "ok" and chk.actual == "postgres"


def test_wrong_name_is_unlisted_with_the_real_name_and_the_alternatives(monkeypatch):
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(ROW))
    chk = login._check_database(_conn("og5"))
    assert chk.status == "unlisted" and chk.actual == "postgres"
    assert chk.databases == ("appdb", "postgres", "reportdb")


def test_another_database_on_the_instance_is_routed_not_refused(monkeypatch):
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(ROW))
    chk = login._check_database(_conn("appdb"))
    assert chk.status == "routed" and chk.target == "appdb" and chk.actual == "postgres"
    chk = login._check_database(_conn("AppDB"))                       # 大小写以实例库清单为准
    assert chk.status == "routed" and chk.target == "appdb"


def test_without_the_database_list_a_different_name_is_unlisted(monkeypatch):
    monkeypatch.setattr(login.access, "runner_for",
                        lambda c: _Runner([{"db": "postgres", "usr": "grmp_ro", "databases": ""}]))
    assert login._check_database(_conn("appdb")).status == "unlisted"


def test_case_only_difference_logs_in_under_the_real_name(monkeypatch):
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(ROW))
    assert login._check_database(_conn("Postgres")).status == "case"


def test_falls_back_to_the_proctune_copy_then_to_unknown(monkeypatch):
    r = _Runner(ROW, missing={"procinfo.locate_context"})
    monkeypatch.setattr(login.access, "runner_for", lambda c: r)
    assert login._check_database(_conn()).status == "ok"
    assert r.ran == ["procinfo.locate_context", "proctune.locate_context"]
    r2 = _Runner(ROW, missing={"procinfo.locate_context", "proctune.locate_context"})
    monkeypatch.setattr(login.access, "runner_for", lambda c: r2)
    chk = login._check_database(_conn())
    assert chk.status == "unknown" and "不存在" in chk.note


# --- 整条登录 -----------------------------------------------------------------

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
    yield tmp_path
    import os
    os.environ.pop(session.ENV_HANDLE, None)
    session.use(None)


def test_login_with_a_name_not_on_the_instance_still_logs_in_and_asks_to_confirm(api_home, monkeypatch, capsys):
    """v1.4.9 行方要求库名限制全部放开:清单里没有也不拦,把库清单摆出来请用户确认名字。"""
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(ROW))
    assert login.main(["--ip", "10.0.0.9", "--database", "og5"]) == 0
    out = capsys.readouterr().out
    assert "实例库清单里没有「og5」" in out and "appdb、postgres、reportdb" in out and "确认库名" in out
    assert "未建立会话" not in out
    (info,) = session.list_sessions()
    conn = session._read(session.path_for(info.handle))
    assert conn.database == "og5" and conn.exec_database == "postgres"


def test_login_to_another_database_on_the_instance_records_both(api_home, monkeypatch, capsys):
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(ROW))
    assert login.main(["--ip", "10.0.0.9", "--database", "appdb"]) == 0
    out = capsys.readouterr().out
    assert "要诊断的库 | appdb（未直连）" in out and "实例 10.0.0.9 的 postgres 库" in out
    assert "要诊断的库（appdb）与取数的库（postgres）不同" in out
    assert "不要让用户去改 search_path 或授权" in out
    (info,) = session.list_sessions()
    conn = session._read(session.path_for(info.handle))
    assert conn.database == "appdb" and conn.exec_database == "postgres"     # 报告抬头按要诊断的库


def test_same_database_writes_no_exec_database(api_home, monkeypatch, capsys):
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(ROW))
    assert login.main(["--ip", "10.0.0.9", "--database", "postgres"]) == 0
    assert "未直连" not in capsys.readouterr().out
    (info,) = session.list_sessions()
    assert "exec_database" not in session.path_for(info.handle).read_text(encoding="utf-8")


def test_login_with_a_case_difference_uses_the_real_name(api_home, monkeypatch, capsys):
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(ROW))
    assert login.main(["--ip", "10.0.0.9", "--database", "POSTGRES"]) == 0
    (info,) = session.list_sessions()
    assert "postgres" in capsys.readouterr().out
    assert session._read(session.path_for(info.handle)).database == "postgres"


def test_unverifiable_name_still_logs_in_but_says_so(api_home, monkeypatch, capsys):
    monkeypatch.setattr(login.access, "runner_for",
                        lambda c: _Runner(ROW, missing={"procinfo.locate_context", "proctune.locate_context"}))
    assert login.main(["--ip", "10.0.0.9", "--database", "core"]) == 0
    out = capsys.readouterr().out
    assert "库名未核实" in out and "core" in out


def test_no_verify_skips_the_check(api_home, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(login, "_check_database", lambda c: calls.append(c) or None)
    assert login.main(["--ip", "10.0.0.9", "--database", "core", "--no-verify"]) == 0
    assert calls == []
