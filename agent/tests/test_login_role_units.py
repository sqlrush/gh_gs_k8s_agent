"""gaussdb-login 登录时探测主备:备机要说出来(unlogged 表读不到),探测不了要如实写「未探测」,永不阻断。"""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_LOGIN = _ROOT / "skills" / "gaussdb-login" / "scripts" / "login.py"

spec = importlib.util.spec_from_file_location("login_role_test", _LOGIN)
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


class _Endpoint:
    port = 8769

    def resolve_host(self):
        return "127.0.0.1"


class _Client:
    def list_operations(self):
        return [{"cmd_name": "health.overview", "id": "x"}, {"cmd_name": "sqlfetch.from_history", "id": "y"}]


class _Runner:
    client = _Client()

    def __init__(self, rows=None, exc=None):
        self.rows, self.exc = rows, exc

    def run(self, script, values=None):
        if self.exc:
            raise self.exc
        return self.rows


def _conn():
    return login._api_connection("10.0.0.9", "postgres", _Endpoint())


def test_probe_role_reads_in_recovery(monkeypatch):
    monkeypatch.setattr(login.access, "runner_for", lambda conn: _Runner(rows=[{"in_recovery": "t", "cache_hit_pct": "99"}]))
    assert login._probe_role(_conn()).startswith("备机")
    monkeypatch.setattr(login.access, "runner_for", lambda conn: _Runner(rows=[{"in_recovery": "false"}]))
    assert login._probe_role(_conn()).startswith("主库")


def test_probe_role_never_raises(monkeypatch):
    monkeypatch.setattr(login.access, "runner_for", lambda conn: _Runner(exc=RuntimeError("脚本 health.overview 不存在")))
    role = login._probe_role(_conn())
    assert role.startswith("未探测") and "health.overview 不存在" in role
    monkeypatch.setattr(login.access, "runner_for", lambda conn: _Runner(rows=[{"cache_hit_pct": "99"}]))
    assert "没有 in_recovery 列" in login._probe_role(_conn())


def test_describe_shows_role_row_and_standby_note(monkeypatch):
    monkeypatch.setattr(login.access, "runner_for", lambda conn: _Runner(rows=[]))
    out = login._describe(_conn(), "ok", "/tmp/s.json", "备机（in_recovery=true）")
    assert "主备" in out and "备机（in_recovery=true）" in out
    assert "unlogged" in out and "主库 IP" in out
    out2 = login._describe(_conn(), "ok", "/tmp/s.json", "主库（in_recovery=false）")
    assert "unlogged" not in out2 and "主库（in_recovery=false）" in out2
