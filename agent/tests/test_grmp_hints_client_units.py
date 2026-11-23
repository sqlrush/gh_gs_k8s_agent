"""报错翻译(hints)与中间件客户端对 4xx 的处理。

钉住的纪律:HTTPError 先于 URLError 接住,响应体里的原因必须带出来(现场为「HTTP Error 400: 」后面
一片空白追了两天参数名);已知报错模式追加中文提示但不改原文;认不出的原样返回。
"""
import io
import json
import pathlib
import sys
import urllib.error

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import client as client_mod  # noqa: E402
from common.grmp import hints  # noqa: E402
from common.grmp.client import GrmpClient, GrmpError  # noqa: E402

STANDBY = ("SQL execution failed via JDBC on instance 1d95b59d: "
           "ERROR: Temporary or unlogged table cannot be accessed on the standby.")


def test_explain_matches_known_patterns_and_keeps_silent_otherwise():
    assert "备机" in hints.explain(STANDBY) and "statement_history" in hints.explain(STANDBY)
    assert "语句跟踪" in hints.explain("ERROR: enable_stmt_track is off")
    assert "权限" in hints.explain("ERROR: permission denied for relation statement_history")
    assert hints.explain("something completely unknown") == ""
    assert hints.explain("") == ""


# --- 2026-09-07 现场四类报错:每类都要翻成一句能定位到动作的中文 -----------------------

RECOVERY = ("SQL execution failed via JDBC on instance 5021a3af: ERROR: Recovery is in progress. "
            "建议: WAL control functions cannot be executed during recovery. "
            "在位置: referenced column: diff_xlog_size")
PERM = "ERROR: Permission denied for relation pg_user_status."
FUNC_MISSING = ("ERROR: function gs_get_explain(bigint) does not exist "
                "建议: No function matches the given name and argument types. "
                "You might need to add explicit type casts. (SQLSTATE 42883)")


def test_recovery_in_progress_is_explained_as_standby():
    """「Recovery is in progress」是被派到备机的第二种形态(第一种是 unlogged 表),
    提示要点名 WAL 控制函数、要给出「换主库 IP」这个动作。"""
    hint = hints.explain(RECOVERY)
    assert "备机" in hint and "WAL" in hint and "主库" in hint
    # 只命中「WAL control functions … during recovery」这一句也要认得
    assert "备机" in hints.explain("WAL control functions cannot be executed during recovery")


def test_permission_denied_names_the_object_and_the_action():
    hint = hints.explain(PERM)
    assert "权限" in hint and "pg_user_status" in hint and "这一项无法查询" in hint and "不影响其余部分" in hint
    assert "请 DBA" not in hint, "生产环境权限固定(11-12 行方):先说这一项查不了、不影响其余,不把授权当前提"
    # 大小写不同的 GaussDB 写法(Permission)与 PG 写法(permission)都要认
    assert "权限" in hints.explain("ERROR: permission denied for relation statement_history")


def test_function_missing_hint_separates_the_three_causes():
    """函数级 does not exist 与视图/列的 does not exist 不能是同一句泛话:
    报错里的实参类型是调用时传的,函数在但类型不符也报这句——三种原因都得点到。"""
    hint = hints.explain(FUNC_MISSING)
    assert "gs_get_explain" in hint                              # 点名函数
    assert "参数类型" in hint or "实参" in hint                     # ① 类型不符
    assert "函数定义逐 database 独立" in hint and "参数签名" in hint   # ② 逐库 catalog
    assert "SELECT" not in hint and "pg_proc" not in hint, "排查提示写要核对什么,不写查数据字典的 SQL"
    assert "升级" in hint                                         # ③ catalog 未升级
    # 通用的对象不存在仍走老提示,不被函数提示抢走
    generic = hints.explain("ERROR: relation \"dbe_perf.foo\" does not exist")
    assert "内核版本或兼容模式不同" in generic and "pg_proc" not in generic


def test_with_hint_appends_without_altering_original():
    out = hints.with_hint("请求 /x 失败：" + STANDBY)
    assert out.startswith("请求 /x 失败：" + STANDBY) and "\n提示:" in out and "主库 IP" in out
    assert hints.with_hint("plain") == "plain"


def _client():
    return GrmpClient(base_url="http://127.0.0.1:1", token="t0ken", data_ip="10.0.0.9")


def _http_error(code: int, payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return urllib.error.HTTPError("http://127.0.0.1:1/x", code, "Bad Request", None, io.BytesIO(body))


def test_post_reads_4xx_body_and_appends_hint(monkeypatch):
    def boom(request, timeout=None):
        raise _http_error(400, {"code": "1", "msg": STANDBY, "status": "failed"})
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", boom)
    with pytest.raises(GrmpError) as ei:
        _client()._post("/x", {"dataIp": "10.0.0.9"})
    text = str(ei.value)
    assert "HTTP 400" in text and "cannot be accessed on the standby" in text      # 原文带出来
    assert "提示:" in text and "备机" in text                                       # 翻译追加在后


def test_post_4xx_with_non_json_body_still_surfaces_it(monkeypatch):
    def boom(request, timeout=None):
        raise _http_error(500, b"<html>Internal Server Error</html>")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", boom)
    with pytest.raises(GrmpError) as ei:
        _client()._post("/x", {})
    assert "HTTP 500" in str(ei.value) and "Internal Server Error" in str(ei.value)


def test_post_plain_urlerror_unchanged(monkeypatch):
    def boom(request, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", boom)
    with pytest.raises(GrmpError) as ei:
        _client()._post("/x", {})
    assert "调用中间件失败" in str(ei.value) and "connection refused" in str(ei.value)


def test_invoke_failure_message_gets_hint(monkeypatch):
    c = _client()
    # 换掉命令清单(接口一):invoke 要靠它取 id、看行方有没有声明 dbName
    monkeypatch.setattr(c, "_id_map", lambda: {"sqlfetch.from_history": "id-1"})
    monkeypatch.setattr(c, "_post", lambda path, payload: {"status": "failed", "task_id": "t", "msg": STANDBY})
    with pytest.raises(GrmpError) as ei:
        c.invoke("sqlfetch.from_history", {"sid": 300316117})
    assert "status='failed'" in str(ei.value) and "提示:" in str(ei.value) and "备机" in str(ei.value)


def test_permission_denied_on_a_schema_asks_for_usage_not_select():
    """schema 级的 permission denied(dbe_perf / snapshot)不是 GRANT SELECT 能解决的:
    要的是 schema 的 USAGE,而监控 schema 实际要 MONADMIN——照搬「授 SELECT」会让 DBA 白授一次。"""
    hint = hints.explain("ERROR: permission denied for schema dbe_perf (SQLSTATE 42501)")
    assert "schema dbe_perf" in hint and "USAGE" in hint and "MONADMIN" in hint
    assert "SELECT 权限" not in hint
    fn = hints.explain("ERROR: permission denied for function gs_get_kernel_info")
    assert "gs_get_kernel_info" in fn and "EXECUTE" in fn


def test_relation_missing_hint_points_at_search_path_for_unqualified_names():
    """sqltune 的 400:业务 SQL 的表名不带 schema,靠应用账号的 search_path 解析;
    中间件执行账号的 search_path 是 "$user", public,解析不到就 relation does not exist。
    原先的提示只说「版本差异 / 注册错库」,把人往错的方向带。"""
    hint = hints.explain('ERROR: relation "orders" does not exist (SQLSTATE 42P01)')
    assert "orders" in hint and "search_path" in hint and "schema" in hint
    assert "版本差异" not in hint                       # 不带 schema 的业务表:先怀疑 search_path
    qualified = hints.explain('ERROR: relation "dbe_perf.statement_history" does not exist')
    assert "dbe_perf.statement_history" in qualified and "版本" in qualified   # 带 schema 的系统视图:版本差异


def test_ensure_hint_appends_once_and_never_twice():
    """直连原始会话的 DBError 没经过 runner:skill 收尾处补一次提示;中间件路径的 QueryError 早带了,不叠加。"""
    once = hints.ensure_hint('ERROR: relation "orders" does not exist')
    assert once.count("\n提示:") == 1 and "search_path" in once
    assert hints.ensure_hint(once) == once
    assert hints.ensure_hint("nothing to hint") == "nothing to hint"


# ---- 切库(dbName)报错与 schema 提示(2026-11-11 现场) ----------------------------------------

def test_empty_database_name_points_at_the_whitelist_delta_not_at_the_dba():
    h = hints.explain('请求 /x/invoke 失败：HTTP 400 ；中间件返回：数据库[]在目标高斯实例中不存在, instanceId: abc')
    assert "scripts-delta-20261111.sql" in h and "执行库标注与本版不一致" in h and "不要让 DBA 改库" in h


def test_a_named_database_that_does_not_exist_asks_to_check_the_login_name():
    h = hints.explain("中间件返回：数据库[appdb]在目标高斯实例中不存在, instanceId: abc")
    assert "实例上没有库「appdb」" in h and "--database" in h


@pytest.mark.parametrize("text", ['column "foo" does not exist', 'type "bar" does not exist', "invalid input for schema x"])
def test_schema_hint_needs_both_words(text):
    """1.4.11 的缺陷:只要出现「schema 」或「 does not exist」之一就给了 schema 不存在的提示。"""
    assert not hints.explain(text).startswith("schema 不存在")


def test_schema_that_does_not_exist_still_gets_the_schema_hint():
    assert hints.explain('schema "fl" does not exist').startswith("schema 不存在")


def test_the_bank_middlewares_opaque_failure_gets_an_honest_hint():
    """2026-11-12 回归:行方中间件执行失败只回「agent诊断脚本执行失败」,数据库原因只在中间件日志里;原先认不出、
    不加提示,界面上只有一句 HTTP 400,没有「这一项无法查询,不影响其余部分」的兜底说法(行方 11-12 报告第 3 类)。"""
    h = hints.explain("调用中间件失败：HTTP 400 Bad Request；中间件返回：agent诊断脚本执行失败")
    assert "这一项无法查询" in h and "不影响其余部分" in h
    assert "中间件日志" in h and "未直连" in h and "权限" in h
    assert "改库" not in h and "授权" not in h, "生产权限与参数固定,不能把动作指向改库 / 授权"
