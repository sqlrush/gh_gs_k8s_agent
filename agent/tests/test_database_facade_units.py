"""Database 门面：**driver 严格生效，不做跨驱动兜底**。

原先的行为是「首选驱动失败就换另一个」。那会让同一份配置在不同机器上
跑出不同的后端，而两个后端的能力并不相同：

    gsql    provides_session = False   每条语句独立子进程
    psycopg2  provides_session = True    单条持久连接

于是 hypopg 虚拟索引验证这类依赖会话的功能，在「配了 gsql、实际兜底到
psycopg2」的机器上能跑，在真用 gsql 的客户环境跑不了 —— 而且不报错。

改成严格生效后，配了什么就是什么。本机没有 gsql，就在 config.yaml 里
配一条 driver: psycopg2 的连接，而不是靠兜底蒙混过去。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from common.config import Connection  # noqa: E402
from common.backends.base import DBError  # noqa: E402
import common.db as dbmod  # noqa: E402


def _conn(driver="gsql"):
    return Connection(name="a", type="opengauss", host="h", port=5432,
                      database="d", user="u", driver=driver)


class FakeBackend:
    def __init__(self, tag):
        self.tag = tag

    @classmethod
    def make(cls, tag, fail):
        def _open(conn, password, read_only=True):
            if fail:
                raise DBError("%s cannot connect" % tag)
            return cls(tag)
        return _open


def _loader(seen, failing=()):
    def fake_load(driver):
        seen.append(driver)
        return type("B", (), {
            "open": staticmethod(FakeBackend.make(driver, driver in failing))
        })
    return fake_load


@pytest.mark.parametrize("driver", ["gsql", "psycopg2"])
def test_uses_the_configured_driver(monkeypatch, driver):
    seen = []
    monkeypatch.setattr(dbmod, "_load_backend", _loader(seen))
    db = dbmod.Database.open(_conn(driver=driver), "pw")
    assert seen == [driver]
    assert db._backend.tag == driver


def test_does_not_fall_back_to_another_driver(monkeypatch):
    """首选失败时不再改用别的驱动 —— 只试配置里写的那一个。"""
    seen = []
    monkeypatch.setattr(dbmod, "_load_backend", _loader(seen, failing=("gsql",)))
    with pytest.raises(DBError):
        dbmod.Database.open(_conn(driver="gsql"), "pw")
    assert seen == ["gsql"], "不应再尝试 psycopg2"


def test_failure_message_names_the_configured_driver_and_how_to_change_it(monkeypatch):
    """错误信息要能直接指导操作，而不是只说「连不上」。"""
    monkeypatch.setattr(dbmod, "_load_backend", _loader([], failing=("gsql",)))
    with pytest.raises(DBError) as ei:
        dbmod.Database.open(_conn(driver="gsql"), "pw")
    msg = str(ei.value)
    assert "gsql" in msg
    assert "driver" in msg
    assert "cannot connect" in msg          # 保留底层原因


def test_unknown_driver_is_rejected(monkeypatch):
    with pytest.raises(DBError):
        dbmod.Database.open(_conn(driver="mysqlcli"), "pw")


def test_missing_driver_module_is_a_clean_dberror_not_a_traceback(monkeypatch):
    """客户机器的 python3 没装 psycopg2 时,不能把 ModuleNotFoundError 的整条栈甩给用户
    (模型级验收里 kimi 看到栈后自己去 pip install 了)。要的是一句中文:装哪个包、或改走 grmp。"""
    monkeypatch.setitem(sys.modules, "common.backends.psycopg2_backend", None)  # None ⇒ import 立刻 ImportError
    with pytest.raises(DBError) as ei:
        dbmod._load_backend("psycopg2")
    msg = str(ei.value)
    assert "psycopg2" in msg and "pip install psycopg2-binary" in msg and "grmp" in msg
    assert "Traceback" not in msg
