"""本仓库自己的 shell 脚本的语法与陷阱闸。

agent/tests/test_shell_scripts_units.py 守的是技能包里的脚本(`agent/*.sh`),
**扫不到本仓库的 scripts/ 与 docker/** —— 2026-09-20 写 local-testbed.sh 时就在
「$VAR 后面紧跟多字节字符」上又栽了一次:守则早就有,只是覆盖不到出事的地方。
"""
import pathlib
import re
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = sorted(p for p in _ROOT.rglob("*.sh")
                  if ".git" not in p.parts and "dist" not in p.parts and "agent" not in p.parts)

# `$VAR` 后面直接跟一个多字节字符(全角括号、中文…)。bash 解析变量名时会把那个字节
# 当成名字的一部分,`set -u` 下就是 "VAR�: unbound variable",而且只在真跑到那一行才炸。
_BARE_VAR_THEN_MULTIBYTE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])")


def test_there_are_scripts_to_check():
    assert _SCRIPTS, "一个 .sh 都没扫到,glob 写错了"


@pytest.mark.parametrize("path", _SCRIPTS, ids=lambda p: str(p.relative_to(_ROOT)))
def test_syntax_is_valid(path):
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("path", _SCRIPTS, ids=lambda p: str(p.relative_to(_ROOT)))
def test_no_bare_variable_before_multibyte_char(path):
    """**踩过三次。** 写成 `${VAR}` 就没事。"""
    bad = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for m in _BARE_VAR_THEN_MULTIBYTE.finditer(line):
            bad.append("%s:%d  %s…" % (path.name, n, line[max(0, m.start() - 12):m.end() + 4].strip()))
    assert not bad, "变量名紧跟多字节字符,bash 会把它吃进名字里(改成 ${VAR}):\n  " + "\n  ".join(bad)
