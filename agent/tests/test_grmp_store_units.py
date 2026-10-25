"""script_config 存储（SQLite）。

不建在 og 上：og 有 200 万行 demo 数据，不希望被测试元数据污染；
中间件的元数据本来就属于中间件自己，客户那边 GRMP 也有独立的库。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.grmp import script as sc  # noqa: E402
from grmp_middleware.grmp_mock import store as st  # noqa: E402
from common.grmp.placeholder import ParamDef  # noqa: E402


def _rec(name="slowsql.slow_sql", sql="select 1 where a > {{n}}"):
    return sc.ScriptRecord(
        script_name=name,
        script_content=sql,
        description="人看的描述",
        params=(ParamDef(key="n", type="INTEGER", description="阈值"),),
    )


@pytest.fixture
def store(tmp_path):
    return st.ScriptStore(tmp_path / "script_config.db")


# ===========================================================================
# 建表与写入
# ===========================================================================

def test_table_has_all_twenty_one_customer_columns(store):
    """导出的 DML 要能直接用于客户环境，本地表就得保留 21 列全集。"""
    assert set(store.columns()) == set(sc.SCRIPT_CONFIG_COLUMNS)


def test_register_assigns_string_id(store):
    """【规】id 是 String。用整数会让客户端的 id 比较静默失配。"""
    rec = store.register(_rec())
    assert isinstance(rec.id, str)
    assert rec.id


def test_ids_increase_across_registrations(store):
    first = store.register(_rec("a.one"))
    second = store.register(_rec("b.two"))
    assert int(second.id) > int(first.id)


def test_registered_script_is_found_by_logical_name(store):
    store.register(_rec())
    found = store.find_by_name("slowsql.slow_sql")
    assert found is not None
    assert found.script_content == "select 1 where a > {{n}}"


def test_registered_script_is_found_by_id(store):
    rec = store.register(_rec())
    assert store.find_by_id(rec.id).script_name == "slowsql.slow_sql"


def test_missing_lookups_return_none_not_raise(store):
    """查不到是正常分支，由调用方转成协议错误码；这里不抛异常。"""
    assert store.find_by_name("nope.nope") is None
    assert store.find_by_id("99999") is None


def test_list_all_returns_scripts_in_id_order(store):
    store.register(_rec("a.one"))
    store.register(_rec("b.two"))
    store.register(_rec("c.three"))
    assert [r.script_name for r in store.list_all()] == ["a.one", "b.two", "c.three"]


def test_invalid_scripts_are_excluded_from_list(store):
    """【实】is_valid=1 才有效。失效脚本不该出现在命令清单里。"""
    store.register(_rec("a.one"))
    store.register(sc.ScriptRecord(
        script_name="b.two", script_content="select 1", is_valid=0
    ))
    assert [r.script_name for r in store.list_all()] == ["a.one"]


# ===========================================================================
# 重名：默认拒绝，显式替换时保持 ID 不变
# ===========================================================================

def test_duplicate_logical_name_is_rejected_by_default(store):
    store.register(_rec())
    with pytest.raises(st.StoreError) as exc:
        store.register(_rec())
    assert "slowsql.slow_sql" in str(exc.value)


def test_replace_keeps_the_same_id(store):
    """ID 稳定性是硬要求：调用方会缓存「逻辑名 → id」的解析结果。

    替换脚本时换掉 id，缓存里的旧 id 要么查不到（好），要么指向了
    别的脚本（灾难 —— 执行成功、结果无关、不报错）。
    """
    first = store.register(_rec())
    second = store.register(
        _rec(sql="select 2 where a > {{n}}"), replace=True
    )
    assert second.id == first.id
    assert store.find_by_id(first.id).script_content == "select 2 where a > {{n}}"


def test_replace_of_absent_script_still_registers(store):
    rec = store.register(_rec(), replace=True)
    assert rec.id


# ===========================================================================
# 往返：经过 script_config 之后还剩下什么
# ===========================================================================

def test_round_trip_preserves_params_and_types(store):
    store.register(_rec())
    found = store.find_by_name("slowsql.slow_sql")
    assert [(p.key, p.type) for p in found.params] == [("n", "INTEGER")]


def test_round_trip_loses_param_description(store):
    """parameter_config 的四个键里没有描述位 —— 客户的数据模型就是这样。

    这不是 bug，是必须复刻的信息损失：响应中 OperationParam.description
    在客户环境同样无处可取。若我们额外存一份描述，本地 API 会返回
    客户 API 给不出的信息，调用方就会写出到客户环境失效的代码。
    """
    store.register(_rec())
    found = store.find_by_name("slowsql.slow_sql")
    assert found.params[0].description == ""


def test_round_trip_preserves_scope_columns(store):
    rec = sc.ScriptRecord(
        script_name="a.one",
        script_content="select 1",
        kernel_version="5.0",
        region="hz",
        cluster_deployment_mode="distribution",
    )
    store.register(rec)
    found = store.find_by_name("a.one")
    assert found.kernel_version == "5.0"
    assert found.region == "hz"
    assert found.cluster_deployment_mode == "distribution"


def test_data_survives_reopening_the_store(tmp_path):
    path = tmp_path / "script_config.db"
    st.ScriptStore(path).register(_rec())
    assert st.ScriptStore(path).find_by_name("slowsql.slow_sql") is not None


# ===========================================================================
# 只读标记：存在本地，不占 script_config 的 21 列
# ===========================================================================

def test_readonly_defaults_to_true_after_round_trip(store):
    store.register(_rec())
    assert store.find_by_name("slowsql.slow_sql").readonly is True


def test_writable_script_round_trips(store):
    store.register(sc.ScriptRecord(
        script_name="ddl.one", script_content="create index i on t(a);",
        readonly=False))
    assert store.find_by_name("ddl.one").readonly is False


def test_readonly_flag_does_not_add_a_column_to_script_config(store):
    """21 列是交付契约。只读标记是我们自己加的概念，客户数据模型里没有，
    塞进 script_config 会让导出的 DML 到客户环境用不了。"""
    store.register(sc.ScriptRecord(
        script_name="ddl.one", script_content="create index i on t(a);",
        readonly=False))
    assert set(store.columns()) == set(sc.SCRIPT_CONFIG_COLUMNS)
    assert len(sc.SCRIPT_CONFIG_COLUMNS) == 21


def test_readonly_flag_survives_replace(store):
    store.register(sc.ScriptRecord(
        script_name="ddl.one", script_content="create index i on t(a);",
        readonly=False))
    store.register(sc.ScriptRecord(
        script_name="ddl.one", script_content="create index j on t(b);",
        readonly=False), replace=True)
    assert store.find_by_name("ddl.one").readonly is False


def test_list_all_carries_the_readonly_flag(store):
    store.register(_rec("a.one"))
    store.register(sc.ScriptRecord(
        script_name="b.two", script_content="create index i on t(a);",
        readonly=False))
    flags = {r.script_name: r.readonly for r in store.list_all()}
    assert flags == {"a.one": True, "b.two": False}


def test_register_does_not_mutate_the_input_record(store):
    rec = _rec()
    store.register(rec)
    assert rec.id is None
