"""`--session` 参数与句柄透传。

每个取数 skill 都收 `--session <句柄>`;apply 之后句柄写进环境变量 GSDB_SESSION,
health 派生的子 skill(lockwait / vacuum / waitevent)靠继承环境自然拿到同一个句柄,
不必逐个改 aggregate 的命令行拼装。
"""
import argparse
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import cli, session  # noqa: E402
from common.config import ConfigError  # noqa: E402


@pytest.fixture()
def clean(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))     # 别读到开发机上真实的会话
    monkeypatch.delenv(session.ENV_HANDLE, raising=False)
    session.use(None)
    yield
    # apply_session_arg 直接写 os.environ;delenv 在变量本不存在时不记录,teardown 不会替我们删——
    # 不手工 pop 的话句柄会泄漏到后面的测试文件里(实测:test_login_config 六条一起红)。
    os.environ.pop(session.ENV_HANDLE, None)
    session.use(None)


def test_add_and_apply_session_arg(clean):
    ap = argparse.ArgumentParser()
    cli.add_session_arg(ap)
    args = ap.parse_args(["--session", "ab12c"])
    cli.apply_session_arg(args)
    assert session.current_handle() == "ab12c"
    assert os.environ[session.ENV_HANDLE] == "ab12c"        # 子进程继承


def test_apply_without_session_keeps_env_untouched(clean, monkeypatch):
    monkeypatch.setenv(session.ENV_HANDLE, "fromenv")
    ap = argparse.ArgumentParser()
    cli.add_session_arg(ap)
    cli.apply_session_arg(ap.parse_args([]))
    assert os.environ[session.ENV_HANDLE] == "fromenv"


def test_malformed_session_arg_exits_cleanly_with_code_2(clean, capsys):
    """这一行在各 skill 的 try/except 之外:不能抛 ConfigError 变成 Traceback,要像 argparse 一样干净退出。"""
    ap = argparse.ArgumentParser()
    cli.add_session_arg(ap)
    with pytest.raises(SystemExit) as ei:
        cli.apply_session_arg(ap.parse_args(["--session", "../x"]))
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err and "句柄" in err and "Traceback" not in err
    assert session.current_handle() is None and session.ENV_HANDLE not in os.environ
    assert ConfigError  # 仍是同一个异常族;这里只是不让它裸露出去


SKILL_SCRIPTS = sorted(
    p for p in (_ROOT / "skills").glob("gaussdb-*/scripts/*.py")
    if 'add_argument("-c"' in p.read_text(encoding="utf-8")
)


@pytest.mark.parametrize("path", SKILL_SCRIPTS, ids=lambda p: p.parent.parent.name + "/" + p.name)
def test_every_script_that_takes_a_connection_also_takes_a_session(path):
    """漏掉一个 skill,那个 skill 就还是「猜最后登录的库」。"""
    src = path.read_text(encoding="utf-8")
    assert "add_session_arg(" in src and "apply_session_arg(" in src
