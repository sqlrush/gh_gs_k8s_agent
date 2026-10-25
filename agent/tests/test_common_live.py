"""Live smoke test for the common/ connection layer against og-pri.

Skipped automatically when the connection isn't configured. Run with:
    python3 -m pytest tests/test_common_live.py -v
or standalone:
    python3 tests/test_common_live.py

All tests in this file are marked ``live`` so CI can exclude them with::

    python3 -m pytest -m "not live" -q
"""
import sys
import pathlib
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
import common  # noqa: E402

pytestmark = pytest.mark.live

CONN = "og-pri"


def _available() -> bool:
    try:
        common.find(CONN)
        return True
    except common.ConfigError:
        return False


def _connect_or_skip():
    """连不上就 skip，不要 fail。

    这几条是 live 测试（pytestmark = live）：跑不跑得起来取决于**本机装没装
    对应的驱动**，而不是代码对不对。本机没有 gsql 二进制时让它们红，等于把
    「环境缺件」伪装成「代码有问题」，久了就没人认真看红灯了。

    原先只有参数化那条带守卫，另外两条直接 connect() —— 同一份判断没用全。
    抽成一个函数，省得下次再漏一处。
    """
    if not _available():
        pytest.skip(f"connection {CONN!r} not configured")
    try:
        return common.Database.connect(CONN)
    except common.DBError as exc:
        pytest.skip(f"{CONN} 连不上（多半是驱动没装）：{exc}")


def test_config_loads():
    conns = {c.name: c for c in common.load()}
    assert CONN in conns
    assert conns[CONN].type == "opengauss"


def test_credential_decrypts():
    pw = common.load_secret(CONN)
    assert isinstance(pw, str) and pw


def test_connect_and_read():
    db = _connect_or_skip()
    try:
        ver = db.scalar("select version()")
        assert "openGauss" in ver or "GaussDB" in ver
        cols, rows = db.query("select 1 as a, 'x' as b")
        assert cols == ["a", "b"]
        assert rows == [(1, "x")]
    finally:
        db.close()


@pytest.mark.parametrize("driver", ["gsql", "psycopg2"])
def test_connect_and_read_each_driver(driver):
    if not _available():
        pytest.skip(f"connection {CONN!r} not configured")
    conn = replace(common.find(CONN), driver=driver)
    try:
        db = common.Database.open(conn, common.load_secret(CONN))
    except common.DBError:
        pytest.skip(f"driver {driver} unavailable on this host")
    try:
        ver = db.scalar("select version()")
        assert "openGauss" in ver or "GaussDB" in ver
        cols, rows = db.query("select 1 as a, 'x' as b")
        assert cols == ["a", "b"]
        assert rows == [(1, "x")]
    finally:
        db.close()


def test_read_only_blocks_write():
    db = _connect_or_skip()
    try:
        try:
            db.execute("create temp table _rw_probe (x int)")
        except common.DBError:
            return  # expected: read-only session rejected the write
        raise AssertionError("read-only session unexpectedly allowed DDL")
    finally:
        db.close()


if __name__ == "__main__":
    if not _available():
        print(f"SKIP: connection {CONN!r} not configured in ~/.gdaa")
        sys.exit(0)
    test_config_loads()
    print("test_config_loads: OK")
    test_credential_decrypts()
    print("test_credential_decrypts: OK")
    test_connect_and_read()
    print("test_connect_and_read: OK")
    test_read_only_blocks_write()
    print("test_read_only_blocks_write: OK")
    print("\nALL common/ live tests passed.")
