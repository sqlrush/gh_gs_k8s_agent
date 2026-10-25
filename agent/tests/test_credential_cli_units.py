"""口令加密存取 CLI 的单测。

这是处理口令的代码，测试重点有两块：

  1. **口令不能被打印**。诊断脚本的输出会流转到日志、报告、聊天窗口 ——
     口令被打印一次就收不回来了。
  2. **口令不能被悄悄改动**。存进去和取出来必须逐字节一致；strip 掉一个
     空格，表现是「连库失败」，而错在哪要查很久。
"""
import io
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """state_dir() 调用时才读 os.environ，所以 setenv 足够 —— **不要 reload**。

    reload 会造出新的 ConfigError/CredentialError 类，别处 `pytest.raises`
    抓的是旧类，于是全量跑时在一个毫无关系的文件里红。
    """
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    monkeypatch.delenv("GSDB_PASSWORD", raising=False)
    monkeypatch.delenv("GDAA_PASSWORD", raising=False)
    return tmp_path


def _set(monkeypatch, name, secret):
    from common import credential_cli
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret))
    return credential_cli.main(["set", name, "--stdin"])


# --- 存取往返 ----------------------------------------------------------------

def test_set_then_load_roundtrip(home, monkeypatch):
    from common import credential
    assert _set(monkeypatch, "c1", "s3cret") == 0
    assert credential.load_secret("c1") == "s3cret"


@pytest.mark.parametrize("secret", [
    " leading",          # 前导空格是口令的一部分
    "trailing ",         # 尾随空格同上
    "with spaces here",
    "p@ss:w0rd!#$%^&*()",
    "中文口令",
    "a" * 200,
])
def test_secret_survives_verbatim(home, monkeypatch, secret):
    """**存进去和取出来必须逐字节一致。**

    --stdin 只剥末尾换行（管道带来的），不 strip —— 口令**可以**以空格开头
    或结尾，strip 掉会存下一个错的口令，而表现只是「连库失败」，
    错在哪要查很久。
    """
    from common import credential
    assert _set(monkeypatch, "c1", secret) == 0
    assert credential.load_secret("c1") == secret


def test_trailing_newline_from_pipe_is_stripped(home, monkeypatch):
    """管道的末尾换行不是口令的一部分。"""
    from common import credential
    _set(monkeypatch, "c1", "s3cret\n")
    assert credential.load_secret("c1") == "s3cret"


def test_stored_file_permissions(home, monkeypatch):
    _set(monkeypatch, "c1", "s3cret")
    path = home / "credentials" / "c1.enc"
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_ciphertext_on_disk_is_not_the_plaintext(home, monkeypatch):
    """落盘的必须是密文 —— 这条看着显然，但值得钉住。"""
    _set(monkeypatch, "c1", "s3cret")
    blob = (home / "credentials" / "c1.enc").read_bytes()
    assert b"s3cret" not in blob


# --- 口令不能被打印 ----------------------------------------------------------

def test_check_never_prints_the_secret(home, monkeypatch, capsys):
    """**check 只回答能不能解开。**

    口令被打印一次就收不回来了 —— 输出会进日志、报告、聊天窗口。
    """
    from common import credential_cli
    _set(monkeypatch, "c1", "s3cret")
    capsys.readouterr()
    assert credential_cli.main(["check", "c1"]) == 0
    out = capsys.readouterr()
    assert "s3cret" not in (out.out + out.err)
    assert "长度 6" in out.out       # 只给长度，不给内容


def test_set_never_echoes_the_secret(home, monkeypatch, capsys):
    from common import credential_cli
    _set(monkeypatch, "c1", "s3cret")
    out = capsys.readouterr()
    assert "s3cret" not in (out.out + out.err)


def test_list_never_prints_secrets(home, monkeypatch, capsys):
    from common import credential_cli
    _set(monkeypatch, "c1", "s3cret")
    capsys.readouterr()
    credential_cli.main(["list"])
    out = capsys.readouterr()
    assert "c1" in out.out and "s3cret" not in out.out


def test_check_on_missing_credential_fails(home, monkeypatch, capsys):
    from common import credential_cli
    assert credential_cli.main(["check", "nope"]) == 1


# --- seal（内联密文） ---------------------------------------------------------

def test_seal_outputs_ciphertext_usable_inline(home, monkeypatch, capsys):
    from common import config, credential, credential_cli
    monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret"))
    assert credential_cli.main(["seal", "c1", "--stdin"]) == 0
    blob = capsys.readouterr().out.strip()
    assert "s3cret" not in blob

    conn = config.Connection(name="c1", type="opengauss", host="h", port=5432,
                             database="d", user="u", password=blob,
                             encrypted=True)
    assert credential.secret_for(conn) == "s3cret"


# --- 拒绝路径 ----------------------------------------------------------------

def test_empty_secret_is_refused(home, monkeypatch, capsys):
    """空口令存进去，表现会是「连库失败」而不是「你没设口令」。"""
    from common import credential_cli
    monkeypatch.setattr("getpass.getpass", lambda *_: "")
    assert credential_cli.main(["set", "c1"]) == 1
    assert not (home / "credentials" / "c1.enc").exists()


def test_mismatched_confirmation_is_refused(home, monkeypatch):
    """两次输入不一致 —— 存下去的会是打错的那个，而且不会有人发现。"""
    from common import credential_cli
    answers = iter(["first", "second"])
    monkeypatch.setattr("getpass.getpass", lambda *_: next(answers))
    assert credential_cli.main(["set", "c1"]) == 1
    assert not (home / "credentials" / "c1.enc").exists()


@pytest.mark.parametrize("bad", ["_lead", "-lead", "UPPER", "with space",
                                 "../escape", "a/b"])
def test_illegal_names_never_store_anything(home, monkeypatch, bad):
    """名字同时用来拼 credentials/<name>.enc 的路径 —— 放开等于开路径穿越。

    断言的是**安全性质**（什么都没存），不是某个退出码：`-lead` 会被 argparse
    先当成选项拦掉（SystemExit 2），别的走到我们自己的校验（返回 1）。
    两条路都安全；不安全的只有第三种 —— 真把文件写出去。
    """
    from common import credential_cli
    try:
        _set(monkeypatch, bad, "s3cret")
    except SystemExit as exc:
        assert exc.code != 0
    cred_dir = home / "credentials"
    assert not list(cred_dir.glob("*")) if cred_dir.exists() else True
    # 路径穿越尤其要确认没往上层写
    assert not (home.parent / "escape.enc").exists()


def test_ciphertext_cannot_be_reused_under_another_name(home, monkeypatch,
                                                        capsys):
    """AAD 绑定连接名 —— 密文泄露了也不能挪到别的连接上复用。"""
    from common import credential, credential_cli
    monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret"))
    credential_cli.main(["seal", "c1", "--stdin"])
    blob = capsys.readouterr().out.strip()
    with pytest.raises(credential.CredentialError):
        credential.open_secret("c2", blob)


def test_non_interactive_gives_actionable_advice(home, monkeypatch, capsys):
    """**非交互环境下 getpass 抛 EOFError，不能让它变成 Python 栈。**

    实测在 deploy.sh 被管道喂参数时踩到：用户看到的是一段 traceback，
    而真正该说的是「这里不是交互终端，用 --stdin」。
    """
    from common import credential_cli

    def _boom(*_):
        raise EOFError

    monkeypatch.setattr("getpass.getpass", _boom)
    assert credential_cli.main(["set", "c1"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "--stdin" in err, "要告诉用户在脚本里该怎么传"
    assert not (home / "credentials" / "c1.enc").exists()
