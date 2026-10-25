import sys, pathlib
_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from common.config import Connection, validate, ConfigError, load, state_dir  # noqa: E402

def _conn(**kw):
    base = dict(name="a", type="opengauss", host="h", port=5432, database="d", user="u")
    base.update(kw)
    return Connection(**base)

def test_driver_defaults_to_gsql():
    assert _conn().driver == "gsql"

def test_validate_accepts_psycopg2():
    validate(_conn(driver="psycopg2"))  # 不抛即通过


def test_retired_pg8000_driver_is_rejected_with_the_fix(tmp_path):
    """pg8000 已整体换成 psycopg2,不保留别名。老配置必须**当场报错并说清怎么改**——
    留个别名或给一句泛泛的 must be one of,都会让客户不知道该动哪一行。"""
    with pytest.raises(ConfigError) as ei:
        validate(_conn(driver="pg8000"))
    msg = str(ei.value)
    assert "pg8000" in msg and "psycopg2" in msg and "config.yaml" in msg

def test_validate_rejects_unknown_driver():
    with pytest.raises(ConfigError):
        validate(_conn(driver="mysqlcli"))

def test_load_fills_default_driver(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "connections:\n"
        "  - name: a\n    type: opengauss\n    host: h\n"
        "    port: 5432\n    database: d\n    user: u\n"
    )
    # GSDB_HOME 优先级高于 GDAA_HOME，不清掉就会读到真实的 ~/.gdaa/config.yaml，
    # 断言变成对本机配置的偶然依赖（本机按文档就是带 GSDB_HOME 运行的）
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.setenv("GDAA_HOME", str(tmp_path))
    conns = load()
    assert conns[0].driver == "gsql"

def test_load_reads_explicit_driver(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "connections:\n"
        "  - name: a\n    type: opengauss\n    host: h\n"
        "    port: 5432\n    database: d\n    user: u\n    driver: psycopg2\n"
    )
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.setenv("GDAA_HOME", str(tmp_path))
    conns = load()
    assert conns[0].driver == "psycopg2"


def test_state_dir_honors_gsdb_home(tmp_path, monkeypatch):
    monkeypatch.delenv("GDAA_HOME", raising=False)
    monkeypatch.setenv("GSDB_HOME", str(tmp_path / "x"))
    assert state_dir() == tmp_path / "x"

def test_state_dir_gsdb_home_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("GDAA_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("GSDB_HOME", str(tmp_path / "new"))
    assert state_dir() == tmp_path / "new"

def test_state_dir_falls_back_to_legacy_gdaa_home(tmp_path, monkeypatch):
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.setenv("GDAA_HOME", str(tmp_path / "legacy"))
    assert state_dir() == tmp_path / "legacy"

def test_state_dir_defaults_to_the_container_path(monkeypatch):
    """两个环境变量都没有时，落到客户容器里的绝对路径。

    这不是笔误：客户改造版把默认值从 ~/.gdaa 改成了容器内的固定路径，
    OpenCode 容器里 /workspace 一定存在。而测试原本还断言 ~/.gdaa，
    所以它自客户版导入起就一直是红的。

    **在容器外这是个坑**：开发机上 /workspace 不存在、也建不出来（根目录
    无写权限），不设 GSDB_HOME 直接跑任何 skill 都会在写状态时失败。
    本机开发/测试请显式 export GSDB_HOME=$HOME/.gdaa。
    """
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.delenv("GDAA_HOME", raising=False)
    assert state_dir() == pathlib.Path("/workspace/.opencode/skills/common")


def test_ensure_dir_explains_when_default_dir_is_not_writable(tmp_path, monkeypatch):
    """镜像只读、GSDB_HOME 又没设时,要的是一句话,不是 PermissionError 堆栈。"""
    import os
    if os.geteuid() == 0:
        pytest.skip("root 不受目录权限约束")
    from common import config
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.delenv("GDAA_HOME", raising=False)
    monkeypatch.setattr(config, "_DEFAULT_STATE_DIR", ro / "state")
    try:
        with pytest.raises(ConfigError) as exc:
            config.ensure_dir()
    finally:
        ro.chmod(0o700)
    msg = str(exc.value)
    assert "GSDB_HOME" in msg and str(ro / "state") in msg


def test_ensure_dir_uses_gsdb_home_when_set(tmp_path, monkeypatch):
    from common import config
    monkeypatch.setenv("GSDB_HOME", str(tmp_path / "home"))
    assert config.ensure_dir() == tmp_path / "home"
    assert (tmp_path / "home").is_dir()
