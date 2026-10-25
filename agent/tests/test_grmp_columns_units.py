"""结果列名的唯一性检查。

协议的结果集是**按列名做键的对象数组**：

    {"type":"array","data":[{"datname":"omm","datdba":"10"}]}

JSON 对象的键唯一，所以两个同名列只会剩一个 —— 后面的把前面的覆盖掉，
不报错、不告警，只是少了几列数据。

`select round(a,2), round(b,2) from t` 在 PostgreSQL/openGauss 下两列都叫
round；`select a||b from t` 的列名是 ?column?。这类 SQL 在原来的位置访问
（r[3]、r[4]）下毫无问题，一迁到中间件就开始丢数据。

实测仓库里 49 条模板有 13 条中招，wdr_003 更是 6 列全叫 coalesce ——
迁过去只会剩 1 列。所以这道检查放在注册期，硬拦。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.grmp.columns import (  # noqa: E402
    ColumnError, anonymous_columns, check_columns, duplicate_columns,
)


# ===========================================================================
# 重名列
# ===========================================================================

def test_duplicate_names_are_detected():
    assert duplicate_columns(["a", "b", "a"]) == ["a"]


def test_all_duplicates_are_listed():
    assert duplicate_columns(["round", "round", "x", "x"]) == ["round", "x"]


def test_distinct_names_are_clean():
    assert duplicate_columns(["a", "b", "c"]) == []


def test_case_differing_names_are_not_duplicates():
    """列名大小写不同就是不同的键，不该误报。"""
    assert duplicate_columns(["a", "A"]) == []


# ===========================================================================
# 无名列
# ===========================================================================

def test_anonymous_column_is_detected():
    assert anonymous_columns(["a", "?column?"]) == ["?column?"]


def test_empty_name_is_anonymous():
    assert anonymous_columns([""]) == [""]


# ===========================================================================
# 入口：check_columns
# ===========================================================================

def test_clean_columns_pass():
    check_columns(["a", "b"], "x.one")


def test_duplicates_raise_and_name_the_script_and_columns():
    with pytest.raises(ColumnError) as exc:
        check_columns(["round", "round"], "topproc.top_proc")
    msg = str(exc.value)
    assert "topproc.top_proc" in msg
    assert "round" in msg
    assert "AS" in msg, "错误信息要告诉作者怎么修（加别名）"


def test_anonymous_raises():
    with pytest.raises(ColumnError) as exc:
        check_columns(["?column?"], "health.idx")
    assert "?column?" in str(exc.value)


def test_error_explains_the_consequence():
    """错误信息要说清后果，否则作者会以为只是风格问题而绕过它。"""
    with pytest.raises(ColumnError) as exc:
        check_columns(["c", "c"], "x.one")
    assert "覆盖" in str(exc.value) or "丢" in str(exc.value)
