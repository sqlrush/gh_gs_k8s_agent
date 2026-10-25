"""slowsql 走统一入口后的取数环节。

改造只动「怎么把行取回来」这一段：CSV 导出、StmtRow 构造、表格渲染
全部不动。两条路径返回的都是全字符串化的行字典，这里把它按脚本的列序
摊成位置元组，下游代码一行不用改。
"""
import sys
import pathlib
import importlib.util

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

_SCRIPT = _ROOT / "skills" / "gaussdb-slowsql" / "scripts" / "slowsql.py"


def _load():
    """按文件路径加载 skill 脚本。

    exec_module 之前必须先登记到 sys.modules：Python 3.9 的 dataclasses
    在处理 @dataclass 时会 sys.modules.get(cls.__module__).__dict__ 反查
    模块命名空间，没登记就拿到 None。
    """
    spec = importlib.util.spec_from_file_location("slowsql_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


slowsql = _load()


class FakeRunner:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def run(self, script_name, values=None):
        self.calls.append((script_name, values))
        return self.rows


ROW = {
    "unique_sql_id": "550595269",
    "query": "select 1",
    "calls": "3",
    "avg_ms": "25.34",
    "total_sec": "0.08",
    "cpu_sec": "0.07",
    "rows": "1",
}


def test_fetch_rows_calls_the_registered_logical_name():
    """按逻辑名调用，不硬编码脚本 ID —— ID 是环境相关数据。"""
    runner = FakeRunner([ROW])
    slowsql.fetch_rows(runner, 100, 20, "2020-01-01 00:00:00")
    assert runner.calls[0][0] == "slowsql.slow_sql"


def test_fetch_rows_passes_the_declared_parameter_names():
    runner = FakeRunner([ROW])
    slowsql.fetch_rows(runner, 100, 20, "2020-01-01 00:00:00")
    assert runner.calls[0][1] == {
        "threshold_ms": 100,
        "limit": 20,
        "begin_time": "2020-01-01 00:00:00",
    }


def test_fetch_rows_flattens_dict_rows_into_column_order():
    """下游按 r[0]..r[6] 取值，列序必须与脚本 SELECT 的列序一致。"""
    rows = slowsql.fetch_rows(FakeRunner([ROW]), 100, 20, "2020-01-01 00:00:00")
    assert rows == [("550595269", "select 1", "3", "25.34", "0.08", "0.07", "1")]


def test_missing_column_is_reported_rather_than_silently_dropped():
    """脚本 SELECT 列变了而这里没跟上时，必须报错。

    用 .get() 兜底会让缺失的列静默变成 None，之后 int(None) 的报错
    指向的是格式化环节，排查方向被带偏。
    """
    incomplete = {k: v for k, v in ROW.items() if k != "cpu_sec"}
    with pytest.raises(KeyError):
        slowsql.fetch_rows(FakeRunner([incomplete]), 100, 20, "2020-01-01 00:00:00")


def test_stringified_values_still_build_a_stmt_row():
    """两条路径的取值都是字符串，StmtRow 的 int()/float() 要能直接吃。"""
    rows = slowsql.fetch_rows(FakeRunner([ROW]), 100, 20, "2020-01-01 00:00:00")
    r = rows[0]
    row = slowsql.StmtRow(r[0], r[1], int(r[2]), float(r[3]), float(r[4]),
                          float(r[5]), int(r[6]))
    assert row.calls == 3
    assert row.avg_ms == 25.34


def test_empty_result_is_passed_through(tmp_path):
    assert slowsql.fetch_rows(FakeRunner([]), 100, 20, "2020-01-01 00:00:00") == []
