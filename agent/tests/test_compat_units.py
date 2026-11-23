"""业务库兼容模式(common/compat.py,v1.5.1;2026-11-20 现场 M 兼容业务库上 15 条脚本报错)。

钉住:探针取回的值怎么归一;会话旁挂文件 <句柄>.compat 能存能读、旧会话没有它照常可用;取数器打标;
登录切库时把兼容模式带进会话、登录输出写「兼容模式」一行;报告那一行只在 M / B 库出(A 库报告与 1.5.0 一字不差)。
"""
import importlib.util
import pathlib
import sys
from dataclasses import replace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import compat, session  # noqa: E402
from common.config import Connection  # noqa: E402
from common.grmp import routing  # noqa: E402

CONN = Connection(name="10-0-0-9-appdb", type="gaussdb", host="127.0.0.1", port=8769, database="appdb",
                  user="grmp", driver="grmp", data_ip="10.0.0.9", app="api", route_db="appdb")


def test_normalize_accepts_known_modes_only():
    assert compat.normalize(" m ") == "M" and compat.normalize("A") == "A" and compat.normalize("pg") == "PG"
    for bad in (None, "", "X", "mysql", "NULL"):
        assert compat.normalize(bad) == ""
    assert compat.normalize_lctn("1") == "1" and compat.normalize_lctn(None) == "" and compat.normalize_lctn("9") == ""


def test_mode_from_connection_and_tagged_runner():
    m = replace(CONN, compat="M", lctn="0")
    assert compat.is_m(m) and compat.is_mysql_like(m) and compat.mode_of(m) == "M"
    runner = compat.tag(type("R", (), {})(), m)
    assert compat.is_m(runner) and compat.lctn_of(runner) == "0"
    assert not compat.is_mysql_like(CONN) and compat.mode_of(object()) == "" and compat.mode_of(None) == ""
    assert compat.is_mysql_like(replace(CONN, compat="B"))


def test_label_and_report_line():
    assert compat.label(CONN) == compat.UNKNOWN_LABEL
    assert compat.label(replace(CONN, compat="A")) == "A（Oracle 兼容）"
    assert "区分大小写" in compat.label(replace(CONN, compat="M", lctn="0"))
    assert "不区分大小写" in compat.label(replace(CONN, compat="M", lctn="1"))
    # 报告那一行:A 库 / 未知不出,M 库出且点名写法要点
    assert compat.report_line(CONN) == "" and compat.report_line(replace(CONN, compat="A")) == ""
    line = compat.report_line(replace(CONN, compat="M"))
    assert line.startswith("> 业务库兼容模式：M") and "反引号" in line and "concat()" in line and "ILIKE" in line


def test_case_folding_and_identifier_quoting():
    assert compat.folds_unquoted(CONN) and compat.folds_unquoted(replace(CONN, compat="A"))
    assert compat.folds_unquoted(replace(CONN, compat="M", lctn="1"))
    assert not compat.folds_unquoted(replace(CONN, compat="M", lctn="0"))
    assert compat.quote_ident("Or`d", replace(CONN, compat="M")) == "`Or``d`"
    assert compat.quote_ident('Or"d', CONN) == '"Or""d"'


def test_encode_decode_roundtrip_and_garbage():
    assert compat.decode(compat.encode("M", "1")) == ("M", "1")
    assert compat.decode(compat.encode("A", "")) == ("A", "")
    assert compat.encode("", "1") == "" and compat.decode("") == ("", "") and compat.decode("zz 1") == ("", "")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "test-token")
    monkeypatch.delenv(session.ENV_HANDLE, raising=False)
    return tmp_path


def test_session_keeps_compat_in_a_sidecar_not_in_yaml(home):
    h = session.save(replace(CONN, compat="M", lctn="1"))
    path = session.path_for(h)
    assert path.with_suffix(".compat").read_text(encoding="utf-8").strip() == "M 1"
    assert "compat" not in path.read_text(encoding="utf-8"), "兼容模式不进会话 yaml:回滚到旧版时旧代码会拒认新键"
    got = session._read(path)
    assert (got.compat, got.lctn) == ("M", "1")
    # 再登录成未知:旁挂文件删掉
    session.save(replace(CONN, compat=""), handle=h)
    assert not path.with_suffix(".compat").exists() and session._read(path).compat == ""


def test_old_sessions_without_sidecar_read_as_unknown(home):
    h = session.save(CONN)
    assert session._read(session.path_for(h)).compat == ""
    session.path_for(h).with_suffix(".compat").write_text("乱码\n", encoding="utf-8")
    assert session._read(session.path_for(h)).compat == "", "认不出的内容当未知,不报错"


# ---------------- 登录:切库探针把兼容模式带回来 ----------------
_SCRIPTS = _ROOT / "skills" / "gaussdb-login" / "scripts"


def _load_login():
    sys.modules.pop("render", None)
    sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("login_compat_under_test", _SCRIPTS / "login.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    sys.path.remove(str(_SCRIPTS))
    return mod


class _Client:
    def registered(self, n):
        return True

    def declares(self, n, p):
        return True


class _ProbeRunner:
    def __init__(self, row):
        self.client, self.row = _Client(), row

    def run(self, name, values=None):
        assert name == routing.PROBE
        return [self.row]


@pytest.mark.parametrize("row,want", [
    ({"db": "appdb", "usr": "u", "compat": "M", "lctn": "1"}, ("M", "1")),
    ({"db": "appdb", "usr": "u", "compat": "A", "lctn": ""}, ("A", "")),
    ({"db": "appdb", "usr": "u"}, ("", "")),                       # 旧探针没有这两列
])
def test_try_route_returns_compat_from_probe(monkeypatch, row, want):
    login = _load_login()
    monkeypatch.setattr(login.access, "runner_for", lambda conn: _ProbeRunner(row))
    rc = login._try_route(CONN, "appdb")
    assert rc.ok and (rc.compat, rc.lctn) == want


def test_login_output_has_compat_row_when_routed():
    login = _load_login()
    out = login._describe(replace(CONN, compat="M", lctn="0"), "ok", "/tmp/x.yaml")
    assert "| 兼容模式 | M（MySQL 兼容）；表名区分大小写" in out
    out = login._describe(CONN, "ok", "/tmp/x.yaml")
    assert "| 兼容模式 | 未知（本版白名单增量未导入" in out
    out = login._describe(replace(CONN, route_db="", exec_database="postgres"), "ok", "/tmp/x.yaml")
    assert "兼容模式" not in out, "没切到业务库:取数在 postgres 上,不写业务库的兼容模式"
