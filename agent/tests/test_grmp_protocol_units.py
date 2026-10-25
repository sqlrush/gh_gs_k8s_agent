"""GRMP 协议契约测试 —— 断言全部引自接口规范说明的【实】【规】【例】三档证据。

每个测试的 docstring 标注证据等级与出处小节：
  【实】客户实际调用证实（权重最高，与文档冲突时以此为准）
  【规】接口文档明确规定
  【例】仅在文档示例中体现

不在此文件中断言的：【推】（我方推断）、【缺】（文档未涉及）、【矛】（文档自相矛盾）——
那三类不构成「文档已知行为」，它们的处理策略见 settings.py 与启动横幅。
"""
import sys
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.grmp import serialize  # noqa: E402
from grmp_middleware.grmp_mock import envelope, pagination  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402


# ---------------------------------------------------------------------------
# golden 报文：逐字取自规范说明 §4.4 / §5.7，客户测试环境标识已剔除
# ---------------------------------------------------------------------------

GOLDEN_LIST_RESULT = {
    "total": 1,
    "list": [
        {
            "id": "56",
            "cmd": "select pg_encoding_to_char(encoding) as encoding_name, * "
                   "from pg_database where datname not in "
                   "('template1','postgres','template0');",
            "cmd_name": "查看数据库相关信息 1",
            "description": "查看数据库相关信息 1",
            "cmd_type": "SQL",
            "param": [],
        }
    ],
    "pageNum": 1,
    "pageSize": 10,
    "size": 1,
    "startRow": 1,
    "endRow": 1,
    "pages": 1,
    "prePage": 0,
    "nextPage": 0,
    "isFirstPage": True,
    "isLastPage": True,
    "hasPreviousPage": False,
    "hasNextPage": False,
    "navigatePages": 8,
    "navigatepageNums": [1],
    "navigateFirstPage": 1,
    "navigateLastPage": 1,
}

GOLDEN_INVOKE_ROW = {
    "datacl": "{=c/rdsAdmin,rdsAdmin=CTc/rdsAdmin}",
    "dattablespace": "1663",
    "datname": "templatea",
    "datconnlimit": "-1",
    "datctype": "C",
    "encoding": "7",
    "datistemplate": "true",
    "encoding_name": "UTF8",
    "datcollate": "C",
    "datcompatibility": "A",
    "datlastsysoid": "12888",
    "datfrozenxid64": "122490",
    "datallowconn": "false",
    "dattimezone": "PRC",
    "datdba": "10",
    "datminmxid": "2",
    "datfrozenxid": "0",
    "dattype": "D",
}

# 上面 golden 行的原生值（驱动返回什么，中间件就字符串化什么）
NATIVE_COLS = list(GOLDEN_INVOKE_ROW.keys())
NATIVE_ROW = (
    "{=c/rdsAdmin,rdsAdmin=CTc/rdsAdmin}",
    1663,
    "templatea",
    -1,
    "C",
    7,
    True,
    "UTF8",
    "C",
    "A",
    12888,
    122490,
    False,
    "PRC",
    10,
    2,
    0,
    "D",
)


# ===========================================================================
# 1. 响应信封（规范说明 §2.3）
# ===========================================================================

def test_list_envelope_wraps_result_with_string_code():
    """【实】接口一成功信封为 {code,msg,result}，且 code 是字符串 "0" 不是数字 0。"""
    env = envelope.ok_list({"total": 0})
    assert env == {"code": "0", "msg": "success", "result": {"total": 0}}
    assert isinstance(env["code"], str)


def test_list_error_envelope_has_no_result_key():
    """【实】+【推】接口一错误信封只有 code/msg；示例未显示 result，本实现不产出该键。"""
    env = envelope.error("随便什么错")
    assert env == {"code": "1", "msg": "随便什么错"}
    assert "result" not in env


def test_instance_not_found_message_is_reproduced_verbatim():
    """【实】dataIp 查不到实例时的 msg 必须逐字一致（规范说明 §9 唯一已知错误样例）。"""
    assert envelope.ERR_INSTANCE_NOT_FOUND == "通过dataIp查询不到对应高斯实例信息"
    assert envelope.error(envelope.ERR_INSTANCE_NOT_FOUND) == {
        "code": "1",
        "msg": "通过dataIp查询不到对应高斯实例信息",
    }


def test_invoke_envelope_has_exactly_four_keys_and_no_code_msg():
    """【规】+【例】接口二信封与接口一结构不同：无 code/msg，任务信息平铺在顶层。"""
    env = envelope.ok_invoke({"type": "Text", "data": ""}, task_id="grmp-x")
    assert set(env.keys()) == {"result", "task_id", "call_type", "status"}
    assert "code" not in env
    assert "msg" not in env


def test_invoke_envelope_defaults_are_sync_and_finished():
    """【例】两个执行示例的 call_type 为 "sync"，status 为 "finished"。"""
    env = envelope.ok_invoke({"type": "Text", "data": ""}, task_id="grmp-x")
    assert env["call_type"] == "sync"
    assert env["status"] == "finished"


def test_task_id_is_grmp_prefix_plus_uuid4():
    """【例】task_id 格式为 "grmp-" + 标准 UUID v4（两个示例一致）。"""
    tid = envelope.new_task_id()
    assert re.fullmatch(
        r"grmp-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        tid,
    ), tid


def test_task_ids_are_unique_per_call():
    """【例】task_id 是任务标识，每次调用必须不同。"""
    assert envelope.new_task_id() != envelope.new_task_id()


# ===========================================================================
# 2. 分页（规范说明 §6）—— PageHelper PageInfo 全 18 字段
# ===========================================================================

def test_page_info_reproduces_customer_golden_response():
    """【实】客户真实响应的 result 必须被逐字段复现（单页单条的场景）。"""
    got = pagination.paginate(GOLDEN_LIST_RESULT["list"], page_num=1, page_size=10)
    assert got == GOLDEN_LIST_RESULT


def test_page_info_has_exactly_eighteen_fields():
    """【实】result 是完整 PageInfo：参数表只定义 total/list，实际多出 16 个字段。"""
    got = pagination.paginate([], page_num=1, page_size=10)
    assert len(got) == 18, sorted(got.keys())


def test_navigatepagenums_second_p_is_lowercase():
    """【实】PageHelper 原样输出 navigatepageNums，第二个 p 小写，不符合驼峰规范。

    写成 navigatePageNums 会让客户端取不到导航页码且不报错。
    """
    got = pagination.paginate([1], page_num=1, page_size=10)
    assert "navigatepageNums" in got
    assert "navigatePageNums" not in got


def test_navigate_pages_is_fixed_eight():
    """【实】navigatePages 为 PageHelper 默认值 8。"""
    got = pagination.paginate(list(range(100)), page_num=1, page_size=10)
    assert got["navigatePages"] == 8


def test_offset_is_page_number_not_row_offset():
    """【规】offset 语义是「第几页」1-based，不是行偏移量。

    按行偏移量实现，翻到第二页会取到错误的数据段且报文完全正常。
    """
    items = list(range(1, 31))  # 1..30
    page2 = pagination.paginate(items, page_num=2, page_size=10)
    assert page2["pageNum"] == 2
    assert page2["list"] == list(range(11, 21))  # 第 11..20 条，不是第 3..12 条
    assert page2["startRow"] == 11
    assert page2["endRow"] == 20


def test_pre_page_and_next_page_are_zero_at_boundaries_not_null():
    """【实】prePage/nextPage 在边界处是 0 而不是 null。

    客户端不能用「非空」判断有无相邻页，要用 hasPreviousPage/hasNextPage。
    """
    only = pagination.paginate([1], page_num=1, page_size=10)
    assert only["prePage"] == 0
    assert only["nextPage"] == 0
    assert only["prePage"] is not None
    assert only["nextPage"] is not None


def test_pre_page_and_next_page_carry_neighbour_page_numbers():
    """【规】中间页的 prePage/nextPage 为相邻页码。"""
    mid = pagination.paginate(list(range(30)), page_num=2, page_size=10)
    assert mid["prePage"] == 1
    assert mid["nextPage"] == 3


def test_boolean_page_flags_are_real_booleans():
    """【实】isFirstPage/isLastPage/hasPreviousPage/hasNextPage 是真布尔值，不是字符串。

    与结果集里被字符串化的布尔列不同——分页对象是 GRMP 自己序列化的。
    """
    mid = pagination.paginate(list(range(30)), page_num=2, page_size=10)
    for key in ("isFirstPage", "isLastPage", "hasPreviousPage", "hasNextPage"):
        assert isinstance(mid[key], bool), key
    assert mid["isFirstPage"] is False
    assert mid["isLastPage"] is False
    assert mid["hasPreviousPage"] is True
    assert mid["hasNextPage"] is True


def test_size_is_actual_row_count_of_last_page():
    """【规】size 是当前页实际条数，末页可小于 pageSize；pageSize 保持请求值。"""
    last = pagination.paginate(list(range(25)), page_num=3, page_size=10)
    assert last["pageSize"] == 10
    assert last["size"] == 5
    assert last["total"] == 25
    assert last["pages"] == 3


def test_empty_page_list_is_empty_array_not_null():
    """【规】空页的 list 为 [] 而非 null。"""
    empty = pagination.paginate([], page_num=1, page_size=10)
    assert empty["list"] == []
    assert empty["total"] == 0


def test_navigate_window_caps_at_eight_pages():
    """【实】navigatepageNums 最多 8 个页码，navigateFirst/LastPage 为其首尾。"""
    got = pagination.paginate(list(range(200)), page_num=1, page_size=10)
    assert got["pages"] == 20
    assert got["navigatepageNums"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert got["navigateFirstPage"] == 1
    assert got["navigateLastPage"] == 8


# ===========================================================================
# 3. 结果序列化（规范说明 §7）
# ===========================================================================

def test_all_column_values_are_rendered_as_json_strings():
    """【实】结果集所有列值都被渲染成 JSON 字符串，数值型也不例外。"""
    st = Settings(bool_style="true_false")
    result = serialize.result_array(NATIVE_COLS, [NATIVE_ROW], st)
    row = result["data"][0]
    for key, value in row.items():
        assert isinstance(value, str), "%s -> %r" % (key, value)


def test_invoke_result_reproduces_customer_golden_row():
    """【实】§5.7 客户真实响应的结果行必须被逐字段复现。"""
    st = Settings(bool_style="true_false")
    result = serialize.result_array(NATIVE_COLS, [NATIVE_ROW], st)
    assert result["type"] == "array"
    assert result["data"] == [GOLDEN_INVOKE_ROW]


def test_row_keys_are_column_names_in_query_order():
    """【规】type=array 时 data 是对象数组，每个对象一行，键为列名。"""
    st = Settings()
    result = serialize.result_array(["b", "a"], [(1, 2)], st)
    assert list(result["data"][0].keys()) == ["b", "a"]


def test_result_type_is_text_when_no_result_set():
    """【规】type 取 Text 或 array；无结果集走 Text，data 是字符串。"""
    result = serialize.result_text("ALTER TABLE")
    assert result == {"type": "Text", "data": "ALTER TABLE"}


def test_result_array_keeps_row_order():
    """【规】data 数组顺序即结果集行顺序（脚本自带 ORDER BY 时语义相关）。"""
    st = Settings()
    result = serialize.result_array(["n"], [(1,), (2,), (3,)], st)
    assert [r["n"] for r in result["data"]] == ["1", "2", "3"]


def test_empty_result_set_is_array_with_empty_data():
    """【规】有结果集结构但零行，仍是 array，data 为 []。"""
    st = Settings()
    result = serialize.result_array(["n"], [], st)
    assert result == {"type": "array", "data": []}


# --- 布尔与 NULL：文档【矛】【推】，本实现做成显式配置，两种渲染都必须可用 ---

@pytest.mark.parametrize(
    "style,expect_true,expect_false",
    [("true_false", "true", "false"), ("t_f", "t", "f")],
)
def test_boolean_rendering_follows_configured_style(style, expect_true, expect_false):
    """【矛】文档 §3.1 给 true/false，§3.2 给 t/f。两种都必须能产出，由配置选定。

    §7.2 的判断是中间件不做归一化、随内核版本而变——所以本实现不能写死一种。
    """
    st = Settings(bool_style=style)
    result = serialize.result_array(["a", "b"], [(True, False)], st)
    assert result["data"][0] == {"a": expect_true, "b": expect_false}


def test_unknown_bool_style_is_rejected_at_construction():
    """配置项是边界输入，非法值必须 fail fast，不能在渲染时静默退回默认值。"""
    with pytest.raises(ValueError):
        Settings(bool_style="yes_no")


# --- 启动横幅：把「当前假设了什么」摆出来 ---

def test_assumptions_report_every_unconfirmed_choice():
    """未证实的选择必须在启动时可见。

    这些项猜错时大多不报错、只出错值（布尔渲染尤甚），运行中没有任何征兆。
    唯一的防线是启动时让人看见「本进程当前按哪一套假设在跑」。
    """
    text = "\n".join(Settings().assumption_lines())
    for keyword in ("布尔", "NULL", "String", "文本替换"):
        assert keyword in text


def test_assumptions_reflect_the_actual_configured_values():
    """横幅必须反映实际取值，不能是一段写死的说明文字。"""
    t_f = "\n".join(Settings(bool_style="t_f").assumption_lines())
    true_false = "\n".join(Settings(bool_style="true_false").assumption_lines())
    assert "t/f" in t_f
    assert "true/false" in true_false
    assert t_f != true_false


def test_null_renders_as_empty_string_by_default():
    """【推】§7.3：openGauss 中 datacl 为 NULL 的库在响应里是 ""，据此推断 NULL→空串。"""
    st = Settings()
    result = serialize.result_array(["datacl"], [(None,)], st)
    assert result["data"][0]["datacl"] == ""


def test_null_and_empty_string_are_indistinguishable_by_design():
    """【推】NULL 与空串渲染结果相同——这是客户中间件的信息损失，必须一并复刻。

    做得比客户更好（比如渲染成 null）会让本地测不出这个损失。
    """
    st = Settings()
    both = serialize.result_array(["x"], [(None,), ("",)], st)
    assert both["data"][0]["x"] == both["data"][1]["x"] == ""
