"""接口二（执行白屏化诊断命令）。

这里有本项目最大的一处空白：**接口二失败时的响应结构，文档零样例**。
接口二的成功响应里根本没有 code/msg，SQL 报错时错误信息无处可放。

规范说明 §9 把它标为风险最高的一项，理由是：若实现选择
`status:"failed"` + result 为空，而调用方只解析 result.data，
就会把「执行失败」读成「查询结果为空」—— 对诊断场景这是最坏的一类错误，
「慢 SQL 返回 0 条」会被读成「当前没有慢 SQL」，结论与事实相反。

本实现的约定（**是发明，不是复刻**，横幅里会声明）：
  失败时 **不产出 result 键**，并把错误放进 msg。
  这样调用方按 §9 的建议校验「status==finished 且 result 存在」是有效的，
  而盲目取 resp["result"]["data"] 的调用方会当场 KeyError —— 吵闹地失败，
  而不是安静地拿到空列表。
"""
import sys
import pathlib
import json

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.backends.base import DBError  # noqa: E402
from common.grmp import script as sc  # noqa: E402
from grmp_middleware.grmp_mock import instances as inst, store as st  # noqa: E402
from grmp_middleware.grmp_mock.server import App  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402
from common.grmp.placeholder import ParamDef  # noqa: E402

INVOKE_PATH = "/example/grmp/diagnostic/agent/common-operations/invoke"
TOKEN = "0123456789abcdef0123456789abcdef"
DATA_IP = "10.0.0.9"


class FakeDB:
    """记录被执行的 SQL 的假连接。真连库的验证另有 live 测试。"""

    def __init__(self, cols=("n",), rows=((1,),), error=None):
        self.cols = list(cols)
        self.rows = [tuple(r) for r in rows]
        self.error = error
        self.executed = []
        self.timeout = None
        self.closed = False

    def query(self, sql, params=None):
        self.executed.append(sql)
        if self.error:
            raise DBError(self.error)
        return self.cols, self.rows

    def set_statement_timeout(self, seconds):
        self.timeout = seconds

    def close(self):
        self.closed = True


@pytest.fixture
def ctx(tmp_path):
    """(app, store, holder) —— holder['db'] 是本次将被打开的假连接。"""
    store = st.ScriptStore(tmp_path / "sc.db")
    store.register(
        sc.ScriptRecord(
            script_name="health.db_info",
            script_content="select datname from pg_database;",
        )
    )
    store.register(
        sc.ScriptRecord(
            script_name="slowsql.slow_sql",
            script_content="select 1 where a > {{n}} limit {{lim}};",
            params=(
                ParamDef(key="n", type="INTEGER"),
                ParamDef(key="lim", type="INTEGER"),
            ),
        )
    )
    holder = {"db": FakeDB(), "opened": []}

    def open_db(name, read_only=True):
        holder["opened"].append((name, read_only))
        return holder["db"]

    app = App(
        store=store,
        instances=inst.InstanceMap({DATA_IP: "og"}),
        token=TOKEN,
        settings=Settings(),
        open_db=open_db,
    )
    return app, store, holder


def _invoke(app, body):
    return app.handle(
        "POST", INVOKE_PATH, {"auth": TOKEN}, json.dumps(body).encode()
    )


def _ok_body(script_id="1", param=None):
    body = {"dataIp": DATA_IP, "id": script_id}
    if param is not None:
        body["param"] = param
    return body


# ===========================================================================
# 1. 成功响应 —— 与客户样例同形
# ===========================================================================

def test_success_envelope_has_exactly_the_four_documented_keys(ctx):
    app, _, _ = ctx
    status, body = _invoke(app, _ok_body())
    assert status == 200
    assert set(body.keys()) == {"result", "task_id", "call_type", "status"}


def test_success_status_is_finished_and_call_type_sync(ctx):
    app, _, _ = ctx
    _, body = _invoke(app, _ok_body())
    assert body["status"] == "finished"
    assert body["call_type"] == "sync"


def test_success_task_id_has_the_grmp_uuid_form(ctx):
    app, _, _ = ctx
    _, body = _invoke(app, _ok_body())
    assert body["task_id"].startswith("grmp-")
    assert len(body["task_id"]) == len("grmp-") + 36


def test_result_set_becomes_array_with_stringified_values(ctx):
    """【实】结果集所有列值渲染成字符串，数值型也不例外。"""
    app, _, holder = ctx
    holder["db"] = FakeDB(cols=("datname", "oid"), rows=[("postgres", 16384)])
    _, body = _invoke(app, _ok_body())
    assert body["result"] == {
        "type": "array",
        "data": [{"datname": "postgres", "oid": "16384"}],
    }


def test_empty_result_set_is_array_with_empty_data(ctx):
    app, _, holder = ctx
    holder["db"] = FakeDB(cols=("n",), rows=[])
    _, body = _invoke(app, _ok_body())
    assert body["result"] == {"type": "array", "data": []}


def test_duplicate_column_names_fail_loudly(ctx):
    """协议按列名做键，重名列会被后一个覆盖 —— 必须报错而不是少返回几列。

    `select round(a,2), round(b,2)` 两列都叫 round，位置访问下没问题，
    一走中间件就只剩一列，且不报错。
    """
    app, _, holder = ctx
    holder["db"] = FakeDB(cols=("round", "round"), rows=[(1, 2)])
    _, body = _invoke(app, _ok_body())
    assert body["status"] != "finished"
    assert "round" in body["msg"]


def test_anonymous_column_fails_loudly(ctx):
    """`select a||b` 的列名是 ?column?，拿不到有意义的键。"""
    app, _, holder = ctx
    holder["db"] = FakeDB(cols=("?column?",), rows=[(1,)])
    _, body = _invoke(app, _ok_body())
    assert body["status"] != "finished"


def test_statement_without_result_set_becomes_text(ctx):
    """【规】type 取 Text 或 array。无结果集走 Text 分支。"""
    app, _, holder = ctx
    holder["db"] = FakeDB(cols=(), rows=[])
    _, body = _invoke(app, _ok_body())
    assert body["result"]["type"] == "Text"


def test_connection_is_closed_after_execution(ctx):
    app, _, holder = ctx
    _invoke(app, _ok_body())
    assert holder["db"].closed is True


def test_instance_mapping_selects_the_connection(ctx):
    app, _, holder = ctx
    _invoke(app, _ok_body())
    assert [name for name, _ro in holder["opened"]] == ["og"]


def test_read_only_script_opens_a_read_only_session(ctx):
    app, _, holder = ctx
    _invoke(app, _ok_body())
    assert holder["opened"][0][1] is True


def test_writable_script_opens_a_writable_session(ctx):
    """只有脚本自己声明了 readonly: false，执行器才开可写会话。"""
    app, store, holder = ctx
    rec = store.register(sc.ScriptRecord(
        script_name="ddl.one", script_content="create index i on t(a);",
        readonly=False))
    _invoke(app, _ok_body(rec.id))
    assert holder["opened"][0][1] is False


def test_caller_cannot_ask_for_a_writable_session(ctx):
    """写权限只能由已注册脚本携带，请求里指定一律拒绝。

    否则任何调用方都能给自己开写权限，白名单与只读会话同时失效。
    """
    app, _, _ = ctx
    body = _ok_body()
    body["readonly"] = False
    _, resp = _invoke(app, body)
    assert resp["status"] != "finished"
    assert "readonly" in resp["msg"]


# ===========================================================================
# 2. 参数：文本替换，且请求里的元素是 OperationValue
# ===========================================================================

def test_parameters_are_substituted_textually_into_the_sql(ctx):
    """【实】客户样例 param 元素为 {param_name, param_value}，取值是字符串。"""
    app, _, holder = ctx
    _invoke(
        app,
        _ok_body(
            "2",
            [
                {"param_name": "n", "param_value": "10"},
                {"param_name": "lim", "param_value": "5"},
            ],
        ),
    )
    assert holder["db"].executed == ["select 1 where a > 10 limit 5;"]


def test_old_operation_param_shape_is_rejected(ctx):
    """接口文档 3.2 的示例把参数「定义」当成「取值」发了出去。

    那个形状要明确拒绝：接受它就得猜「值到底在 description 还是别处」，
    猜错就是静默取错值。
    """
    app, _, _ = ctx
    _, body = _invoke(
        app,
        _ok_body(
            "2",
            [{"param_name": "n", "data_type": "Integer",
              "required": True, "description": "10"}],
        ),
    )
    assert body["status"] != "finished"


def test_non_string_param_value_is_rejected(ctx):
    """【规】param_value 一律以字符串承载，包括整数。"""
    app, _, _ = ctx
    _, body = _invoke(
        app, _ok_body("2", [{"param_name": "n", "param_value": 10},
                            {"param_name": "lim", "param_value": "5"}])
    )
    assert body["status"] != "finished"


def test_duplicate_param_name_is_rejected(ctx):
    app, _, _ = ctx
    _, body = _invoke(
        app,
        _ok_body("2", [{"param_name": "n", "param_value": "1"},
                       {"param_name": "n", "param_value": "2"},
                       {"param_name": "lim", "param_value": "5"}]),
    )
    assert body["status"] != "finished"


def test_missing_required_param_is_rejected_not_defaulted(ctx):
    app, _, holder = ctx
    _, body = _invoke(app, _ok_body("2", [{"param_name": "n", "param_value": "1"}]))
    assert body["status"] != "finished"
    assert holder["db"].executed == []      # 参数不全时根本不该连库


def test_injection_payload_is_rejected_before_reaching_the_database(ctx):
    """文本替换方案下类型校验是唯一防线，必须在连库之前就拦住。"""
    app, _, holder = ctx
    _, body = _invoke(
        app,
        _ok_body("2", [{"param_name": "n", "param_value": "1; drop table t"},
                       {"param_name": "lim", "param_value": "5"}]),
    )
    assert body["status"] != "finished"
    assert holder["db"].executed == []


def test_param_is_optional_for_parameterless_script(ctx):
    app, _, _ = ctx
    _, body = _invoke(app, _ok_body("1"))
    assert body["status"] == "finished"


# ===========================================================================
# 3. 失败 —— 本实现约定：不产出 result 键
# ===========================================================================

def test_failure_omits_the_result_key_entirely(ctx):
    """核心约定：失败时没有 result。

    盲目取 resp["result"]["data"] 的调用方会当场 KeyError，
    而不是安静地拿到 [] 然后把「执行失败」读成「没有慢 SQL」。
    """
    app, _, holder = ctx
    holder["db"] = FakeDB(error="syntax error at or near \"seelct\"")
    _, body = _invoke(app, _ok_body())
    assert "result" not in body
    assert body["status"] == "failed"


def test_failure_carries_the_database_error_text(ctx):
    app, _, holder = ctx
    holder["db"] = FakeDB(error="relation \"nope\" does not exist")
    _, body = _invoke(app, _ok_body())
    assert "does not exist" in body["msg"]


def test_failure_still_carries_a_task_id(ctx):
    """失败也是一次任务，task_id 要有，否则日志无从关联。"""
    app, _, holder = ctx
    holder["db"] = FakeDB(error="boom")
    _, body = _invoke(app, _ok_body())
    assert body["task_id"].startswith("grmp-")


def test_failure_never_reports_finished(ctx):
    app, _, holder = ctx
    holder["db"] = FakeDB(error="boom")
    _, body = _invoke(app, _ok_body())
    assert body["status"] != "finished"


def test_connection_is_closed_even_when_the_query_fails(ctx):
    app, _, holder = ctx
    holder["db"] = FakeDB(error="boom")
    _invoke(app, _ok_body())
    assert holder["db"].closed is True


# ===========================================================================
# 4. 请求校验与显式不支持的能力
# ===========================================================================

def test_missing_id_is_rejected(ctx):
    app, _, _ = ctx
    _, body = _invoke(app, {"dataIp": DATA_IP})
    assert body["status"] != "finished"


def test_unknown_id_is_rejected(ctx):
    app, _, _ = ctx
    _, body = _invoke(app, _ok_body("9999"))
    assert body["status"] != "finished"
    assert "9999" in body["msg"]


def test_unknown_data_ip_uses_the_customer_error_message(ctx):
    """接口二的实例查不到，沿用接口一那句已证实的文案。"""
    app, _, _ = ctx
    _, body = _invoke(app, {"dataIp": "1.2.3.4", "id": "1"})
    assert "通过dataIp查询不到对应高斯实例信息" in body["msg"]


def test_python_command_is_refused_explicitly(ctx):
    """【缺】文档对 PYTHON 只有声明没有规范：运行时、执行位置、沙箱全空白。

    在拿到规范之前不启用，且必须显式报错 —— 静默当成 SQL 跑是灾难。
    """
    app, store, _ = ctx
    rec = store.register(
        sc.ScriptRecord(
            script_name="x.py_one", script_content="print(1)", script_type="PYTHON"
        )
    )
    _, body = _invoke(app, _ok_body(rec.id))
    assert body["status"] != "finished"
    assert "PYTHON" in body["msg"].upper()


def test_async_script_is_refused_instead_of_silently_running_sync(ctx):
    """不做「异步脚本偷偷同步执行后返回 call_type:sync」—— 那会让调用方
    误以为拿到了异步语义。文档本身也没有异步的状态查询与结果拉取接口。"""
    app, store, _ = ctx
    rec = store.register(
        sc.ScriptRecord(
            script_name="x.async_one", script_content="select 1;", is_asyn=1
        )
    )
    _, body = _invoke(app, _ok_body(rec.id))
    assert body["status"] != "finished"
    assert "异步" in body["msg"]


def test_oversized_result_set_is_refused_not_truncated(ctx):
    """决策表第 9 项：超过行数上限报错，不截断。截断 = 静默丢数据。"""
    app, _, holder = ctx
    holder["db"] = FakeDB(cols=("n",), rows=[(i,) for i in range(11)])
    app_small = App(
        store=app._store,
        instances=app._instances,
        token=TOKEN,
        settings=Settings(),
        open_db=lambda name, read_only=True: holder["db"],
        max_result_rows=10,
    )
    _, body = _invoke(app_small, _ok_body())
    assert body["status"] != "finished"
    assert "10" in body["msg"]
