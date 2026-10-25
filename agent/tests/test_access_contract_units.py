"""统一入口的对外契约 —— 决定「换一种访问方式要改多少地方」。

设计目标：新增一种访问方式（客户换中间件、换协议、换驱动）时，
**只新增一个 Runner 实现，skill 代码一行不改**。

要做到这点，跨过门面的东西必须收敛。目前只有三样：
    access.for_conn(name)          -> runner
    runner.run(script, values)     -> list[dict[str, str]]
    access.QueryError              取数失败时的唯一异常类型

第三样是这份测试的重点。原先每个 skill 都写
    _QUERY_ERRORS = (common.DBError, GrmpError, RunError)
——把两条路径的具体异常类型抄进了 13 个 skill 里。加第三种访问方式
就要改 13 遍，门面等于白做。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common import access  # noqa: E402
from common.backends.base import DBError  # noqa: E402
from common.grmp import registry  # noqa: E402
from common.grmp.client import GrmpError  # noqa: E402
from common.grmp.columns import ColumnError  # noqa: E402
from common.grmp.placeholder import ParamError  # noqa: E402
from common.grmp.runner import DirectRunner, RunError  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402


def _registry(tmp_path):
    d = tmp_path / "registry" / "x"
    d.mkdir(parents=True)
    (d / "one.yaml").write_text(
        "name: x.one\ndescription: d\nsql: |\n  select 1 where a > {{n}};\n"
        "params:\n  - key: n\n    type: INTEGER\n    description: x\n",
        encoding="utf-8")
    return tmp_path / "registry"


class ExplodingDB:
    def __init__(self, exc):
        self.exc = exc
        self.closed = False

    def query(self, sql, params=None):
        raise self.exc

    def set_statement_timeout(self, seconds):
        pass

    def close(self):
        self.closed = True


# ===========================================================================
# 两条路径的取数失败收敛到同一个异常类型
# ===========================================================================

def test_middleware_error_is_a_query_error():
    """skill 只 catch access.QueryError 就够了，不必认识 GrmpError。"""
    assert issubclass(GrmpError, access.QueryError)


def test_runner_error_is_a_query_error():
    assert issubclass(RunError, access.QueryError)


def test_direct_path_db_failure_surfaces_as_query_error(tmp_path):
    """直连路径的 DBError 也要被门面归一 —— 否则 skill 还是得认两种。"""
    runner = DirectRunner(
        conn_name="og",
        registry=registry.Registry(_registry(tmp_path)),
        settings=Settings(),
        open_db=lambda name, read_only=True: ExplodingDB(DBError("boom")),
    )
    with pytest.raises(access.QueryError):
        runner.run("x.one", {"n": 1})


def test_original_cause_is_preserved(tmp_path):
    """归一不能把原因吃掉：底层报了什么，错误信息里要看得见。"""
    runner = DirectRunner(
        conn_name="og",
        registry=registry.Registry(_registry(tmp_path)),
        settings=Settings(),
        open_db=lambda name, read_only=True: ExplodingDB(
            DBError("relation nope does not exist")),
    )
    with pytest.raises(access.QueryError) as exc:
        runner.run("x.one", {"n": 1})
    assert "does not exist" in str(exc.value)


def test_connection_is_closed_even_when_normalising_the_error(tmp_path):
    db = ExplodingDB(DBError("boom"))
    runner = DirectRunner(
        conn_name="og",
        registry=registry.Registry(_registry(tmp_path)),
        settings=Settings(),
        open_db=lambda name, read_only=True: db,
    )
    with pytest.raises(access.QueryError):
        runner.run("x.one", {"n": 1})
    assert db.closed is True


# ===========================================================================
# 哪些错误**不该**被归一 —— 它们是缺陷，不是「这次取数失败」
# ===========================================================================

def test_column_defect_is_not_a_query_error():
    """列名重名/无名是脚本定义缺陷，被降级逻辑接住就等于永远发现不了。"""
    assert not issubclass(ColumnError, access.QueryError)


def test_param_defect_is_not_a_query_error():
    """参数类型/缺参同理：调用方写错了，必须响亮失败。"""
    assert not issubclass(ParamError, access.QueryError)


def test_session_unavailable_is_not_a_query_error():
    """「这条路径不提供持久会话」是能力缺口，不是一次取数失败 ——
    降级掉它会让 hypopg 验证悄悄给出错误结论。"""
    assert not issubclass(access.SessionUnavailable, access.QueryError)


# ===========================================================================
# 门面的对外面：换访问方式时要实现什么
# ===========================================================================

def test_runner_contract_is_two_members():
    """一个 Runner 只需提供 run() 与 provides_session。

    这份断言就是「新增访问方式要做什么」的清单本身 ——
    它变长了，说明门面在漏。
    """
    for attr in ("run", "provides_session"):
        assert hasattr(DirectRunner, attr), attr
