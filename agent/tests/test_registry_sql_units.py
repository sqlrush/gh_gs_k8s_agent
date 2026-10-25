"""白名单 SQL 正文的静态检查。

这里只放**看一眼就知道错、但跑起来不报错**的那类问题 —— 它们不会让任何
命令失败，只会让报告里的内容悄悄变成错的。双路径一致性检查抓不到：
两条链路跑的是同一条 SQL，一起错，输出照样一致。
"""
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp.script import load_script  # noqa: E402

_REGISTRY = _ROOT / "scripts" / "registry"
_SCRIPTS = sorted(_REGISTRY.glob("*/*.yaml"))


def _rel(path):
    return path.relative_to(_REGISTRY).as_posix()


# openGauss/PostgreSQL 的 E'' 是**转义字符串**：反斜杠后面跟着未定义的转义
# 字符时，反斜杠被丢掉、字符按字面留下。于是 E'\s+' 到正则引擎手里是 's+'
# —— 匹配字母 s，不是空白。
#
#     REGEXP_REPLACE('select stats', E'\s+', ' ', 'g')  ->  ' elect  tat '
#     REGEXP_REPLACE('select stats',  '\s+', ' ', 'g')  ->  'select stats'
#
# 实测抓到过：慢 SQL 报告里 dbe_perf.statement_history 显示成
# 「dbe_perf. tatement_hi tory」，DBA 照着这个名字去查表，查不到。
_BAD_ESCAPES = re.compile(r"E'[^']*\\[sSwWdDbB][^']*'")


@pytest.mark.parametrize("path", _SCRIPTS, ids=_rel)
def test_no_regex_class_inside_escape_string(path):
    """正则字符类不能写在 E'' 里。"""
    sql = load_script(path).script_content
    hit = _BAD_ESCAPES.search(sql)
    assert not hit, (
        "%s 用了 %s：E'' 是转义字符串，\\s 到正则引擎手里会变成字面量 s，"
        "于是「压缩空白」变成「删掉字母 s」——不报错，只是把表名悄悄改了。"
        "去掉 E 前缀即可（普通字符串里反斜杠原样传给正则引擎）。"
        % (_rel(path), hit.group(0) if hit else "")
    )
