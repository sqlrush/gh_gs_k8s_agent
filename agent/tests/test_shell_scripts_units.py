"""shell 脚本的静态闸 —— 中文注释/提示语里最容易踩的两个坑。

这两条都是**真踩过**的，而且都是同一个形态：脚本语法合法、`bash -n` 通过、
dry-run 也跑得下去，只在某条分支真正执行到时才炸。
"""
import pathlib
import re
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = sorted(_ROOT.glob("*.sh"))

# `$VAR` 后面直接跟一个多字节字符（全角括号、中文…）。
# bash 解析变量名时会把那个字节当成名字的一部分，于是：
#     ok "已写入 $CFG（api 模式）"   →  变量名成了 CFG（  →  set -u 下 unbound
# 排除 `${VAR}`（已用花括号界定）和 `\$VAR`（转义的字面量）。
_BARE_VAR_THEN_MULTIBYTE = re.compile(
    r"(?<!\\)\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]")


@pytest.mark.parametrize("path", _SCRIPTS, ids=lambda p: p.name)
def test_syntax_is_valid(path):
    proc = subprocess.run(["bash", "-n", str(path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("path", _SCRIPTS, ids=lambda p: p.name)
def test_no_bare_variable_before_multibyte_char(path):
    """**踩过两次。**

    `$CFG（...` 里那个全角括号会被 bash 吃进变量名，`set -u` 下报
    「CFG（: unbound variable」。第一次修完，写新代码时又犯了一次
    （`$TOK_ENV）`）—— 所以钉在这里，别有第三次。

    修法：用 `${CFG}` 把名字界定住。
    """
    text = path.read_text(encoding="utf-8")
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        m = _BARE_VAR_THEN_MULTIBYTE.search(line)
        if m:
            hits.append("  %s:%d  %s" % (path.name, i, m.group(0)))
    assert not hits, (
        "裸变量后面紧跟多字节字符，bash 会把它当成变量名的一部分：\n%s\n"
        "改用 ${变量名} 把名字界定住。" % "\n".join(hits))


@pytest.mark.parametrize("path", _SCRIPTS, ids=lambda p: p.name)
def test_set_u_scripts_guard_optional_vars(path):
    """开了 `set -u` 的脚本，引用可能未定义的变量时要带默认值。

    只查我们自己会在分支里跳过赋值的那几个 —— 全量静态分析做不到，
    但这几个是真出过问题的：MODE_SEL / SKIP_CFG / CRED_MISSING / TOK_ENV
    都只在某条分支里赋值，另一条分支引用它们时必须写 ${VAR:-默认}。
    """
    text = path.read_text(encoding="utf-8")
    if "set -u" not in text:
        pytest.skip("%s 没开 set -u" % path.name)

    branch_vars = ("MODE_SEL", "SKIP_CFG", "CRED_MISSING")
    bad = []
    for var in branch_vars:
        # 出现在条件判断里却没带 :- 默认值
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r'\[\s*"\$%s"' % var, line):
                bad.append("  %s:%d  $%s 未带默认值" % (path.name, i, var))
    assert not bad, (
        "这些变量只在某条分支里赋值，另一条分支引用时会触发 set -u：\n%s\n"
        "改用 \"${VAR:-默认}\"。" % "\n".join(bad))
