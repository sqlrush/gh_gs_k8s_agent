"""统一入口的会话守卫。

`access.run()` **两条路径都不提供跨调用的持久会话**：

  中间件路径  接口二每次调用是独立连接，实测 set work_mem 后下次调用读回默认值
  直连路径    DirectRunner 每次 run() 开连接、执行完就 close

所以 hypopg 虚拟索引验证这类「建完虚拟索引再 EXPLAIN」的流程，
**用 access.run() 一定拿不到正确结果**——而且不报错：虚拟索引在第二次调用时
已经没了，EXPLAIN 看到的是原计划，于是得出「加索引没用」的错误结论。

仓库早就为 gsql 加过同款守卫（tests/test_hypopg_session_guard.py），
理由一模一样。这里把守卫补到统一入口上。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common import access  # noqa: E402
from common.config import Connection  # noqa: E402
from common.grmp import registry  # noqa: E402
from common.grmp.client import GrmpClient, GrmpRunner  # noqa: E402
from common.grmp.runner import DirectRunner  # noqa: E402


def _registry(tmp_path):
    d = tmp_path / "registry" / "x"
    d.mkdir(parents=True)
    (d / "one.yaml").write_text(
        "name: x.one\ndescription: d\nsql: |\n  select 1;\n", encoding="utf-8")
    return tmp_path / "registry"


def _conn(**kw):
    base = dict(name="og", type="opengauss", host="h", port=5432,
                database="d", user="u")
    base.update(kw)
    return Connection(**base)


# ===========================================================================
# 两条路径都不提供持久会话
# ===========================================================================

def test_direct_runner_does_not_provide_a_session(tmp_path):
    """即便底层是 psycopg2（本身支持持久会话），DirectRunner 也每次开关连接。

    能力属于「这个入口」，不属于「这个驱动」——按驱动判断会得出错误结论。
    """
    runner = DirectRunner(conn_name="og", registry=registry.Registry(_registry(tmp_path)))
    assert runner.provides_session is False


def test_middleware_runner_does_not_provide_a_session():
    client = GrmpClient("http://127.0.0.1:1", "x" * 32, "10.0.0.9")
    assert GrmpRunner(client).provides_session is False


# ===========================================================================
# 需要会话的流程必须显式索取，拿不到就报错
# ===========================================================================

def test_session_for_refuses_the_middleware_path(tmp_path, monkeypatch):
    """中间件路径没有会话概念，直接拒绝，不必连库再发现。"""
    monkeypatch.setenv("GRMP_REGISTRY", str(_registry(tmp_path)))
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "x" * 32)
    with pytest.raises(access.SessionUnavailable) as exc:
        access.session_for_conn(_conn(driver="grmp", data_ip="10.0.0.9"))
    assert "grmp" in str(exc.value)


def test_session_error_tells_the_caller_what_to_do(tmp_path, monkeypatch):
    """报错要能直接指导操作：说明哪条路径不支持、以及该怎么办。"""
    monkeypatch.setenv("GRMP_REGISTRY", str(_registry(tmp_path)))
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "x" * 32)
    with pytest.raises(access.SessionUnavailable) as exc:
        access.session_for_conn(_conn(driver="grmp", data_ip="10.0.0.9"))
    msg = str(exc.value)
    assert "会话" in msg
    assert "driver" in msg


def test_session_for_refuses_a_backend_without_persistent_session(monkeypatch):
    """gsql 每条语句起独立子进程，同样没有持久会话 —— 打开后立刻拒绝并关闭。"""
    closed = {"yes": False}

    class FakeDB:
        provides_session = False

        def close(self):
            closed["yes"] = True

    monkeypatch.setattr(access, "_open_database", lambda conn, read_only=True: FakeDB())
    with pytest.raises(access.SessionUnavailable):
        access.session_for_conn(_conn(driver="gsql"))
    assert closed["yes"] is True, "拒绝时必须把已建立的连接关掉"


def test_session_for_returns_the_handle_when_supported(monkeypatch):
    class FakeDB:
        provides_session = True

        def close(self):
            pass

    fake = FakeDB()
    monkeypatch.setattr(access, "_open_database", lambda conn, read_only=True: fake)
    assert access.session_for_conn(_conn(driver="psycopg2")) is fake


def test_session_for_defaults_to_read_only(monkeypatch):
    """写会话必须显式索取 —— 默认放开的话，--analyze 之外的路径也会拿到写权限。"""
    seen = {}

    class FakeDB:
        provides_session = True

    def _fake_open(conn, read_only=True):
        seen["read_only"] = read_only
        return FakeDB()

    monkeypatch.setattr(access, "_open_database", _fake_open)
    access.session_for_conn(_conn(driver="psycopg2"))
    assert seen["read_only"] is True
    access.session_for_conn(_conn(driver="psycopg2"), read_only=False)
    assert seen["read_only"] is False
