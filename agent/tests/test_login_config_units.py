"""新配置格式、会话、内联密文的单测。

这一层的坏法几乎全是「连到了另一个库，而输出看起来完全正常」——
跨应用重名、会话文件键名写错、密文被挪到别的连接名下。所以测试重点在
「该拒的拒了」，而不是「能读出来」。
"""
import base64
import importlib
import os
import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """把 GSDB_HOME 指到临时目录。

    **不要 importlib.reload。** state_dir() 是在**调用时**读 os.environ 的，
    monkeypatch.setenv 就够了；reload 不但多余,还会造出一个新的 ConfigError
    类 —— 别的测试文件在模块加载时 import 的是旧那个,于是
    `pytest.raises(ConfigError)` 抓不住 validate() 抛出的新类。

    实测踩到过:单独跑绿、全量跑红，而红的是 test_grmp_access_units 里一条
    与本文件毫无关系的测试。这类污染极难定位,因为失败的地方看起来完全无辜。
    """
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    monkeypatch.delenv("GSDB_PASSWORD", raising=False)
    monkeypatch.delenv("GDAA_PASSWORD", raising=False)
    return tmp_path


def write_config(home, payload):
    (home / "config.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


_CONN = {"type": "opengauss", "host": "127.0.0.1", "port": 5432,
         "database": "db", "user": "gaussdb"}


# --- 模式 --------------------------------------------------------------------

def test_mode_defaults_to_gsql(home):
    """老配置没有 connection_mode，而它们全是直连。

    默认成 api 会让老配置在第一次取数时才失败，且错误表现成「中间件连不上」。
    """
    from common import config
    write_config(home, {"connections": [dict(_CONN, name="legacy")]})
    assert config.mode() == "gsql"


def test_unknown_mode_refuses(home):
    from common import config
    write_config(home, {"connection_mode": "jdbc"})
    with pytest.raises(config.ConfigError) as ei:
        config.mode()
    assert "jdbc" in str(ei.value)


# --- 应用分组 ----------------------------------------------------------------

def test_db_connections_are_grouped_by_app(home):
    from common import config
    write_config(home, {"connection_mode": "gsql", "db_connections": {
        "app1": [dict(_CONN, name="a1c1")],
        "app2": [dict(_CONN, name="a2c1")]}})
    by_app = {c.name: c.app for c in config.load()}
    assert by_app == {"a1c1": "app1", "a2c1": "app2"}


def test_qualified_name_includes_app(home):
    from common import config
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    assert config.find("c1").qualified == "app1/c1"


def test_find_accepts_app_qualified_name(home):
    from common import config
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    assert config.find("app1/c1").name == "c1"


def test_duplicate_name_across_apps_refuses(home):
    """**最重要的一条。**

    `-c conn1` 无法判断该用哪个应用的库。取第一个会连到另一个应用上，
    执行成功、结果无关、不报错 —— 正是本项目一路在防的那类失败。
    """
    from common import config
    write_config(home, {"db_connections": {
        "app1": [dict(_CONN, name="conn1")],
        "app2": [dict(_CONN, name="conn1", port=5433)]}})
    with pytest.raises(config.ConfigError) as ei:
        config.load()
    assert "重复" in str(ei.value)
    assert "app1" in str(ei.value) and "app2" in str(ei.value)


def test_old_flat_format_still_loads(home):
    """老配置不动也要能跑 —— 否则升级会让所有现存部署当场失效。"""
    from common import config
    write_config(home, {"connections": [dict(_CONN, name="legacy")]})
    conns = config.load()
    assert [c.name for c in conns] == ["legacy"]
    assert conns[0].app == ""


def test_both_formats_merge(home):
    from common import config
    write_config(home, {
        "connections": [dict(_CONN, name="legacy")],
        "db_connections": {"app1": [dict(_CONN, name="new")]}})
    assert {c.name for c in config.load()} == {"legacy", "new"}


# --- api 端点 ----------------------------------------------------------------

def test_api_endpoint_parsed(home):
    from common import config
    write_config(home, {"connection_mode": "api", "api_connection": [
        {"host": "grmp-app.example.internal", "port": 8080, "token": "t0"}]})
    ep = config.api_endpoint()
    assert ep.host == "grmp-app.example.internal" and ep.port == 8080


def test_env_token_wins_over_inline(home, monkeypatch):
    """内联令牌会进版本库、进备份、被随手 cat；环境变量少一份落盘副本。"""
    from common import config
    write_config(home, {"connection_mode": "api", "api_connection": [
        {"host": "h", "port": 8080, "token": "inline"}]})
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "from-env")
    assert config.api_endpoint().resolve_token() == "from-env"


def test_api_mode_without_endpoint_refuses(home):
    from common import config
    write_config(home, {"connection_mode": "api"})
    with pytest.raises(config.ConfigError) as ei:
        config.api_endpoint()
    assert "api_connection" in str(ei.value)


def test_multiple_endpoints_refuse(home):
    """两个端点时无从判断用哪个 —— 猜一个就是往错误的环境发请求。"""
    from common import config
    write_config(home, {"connection_mode": "api", "api_connection": [
        {"host": "a", "port": 8080}, {"host": "b", "port": 8080}]})
    with pytest.raises(config.ConfigError):
        config.api_endpoint()


# --- 会话 --------------------------------------------------------------------

def test_session_roundtrip(home):
    from common import config, session
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    conn = config.find("c1")
    h = session.save(conn)
    assert session.current() is None                  # 没句柄就是没登录(2026-09-11 越权修法)
    session.use(h)
    live = session.current()
    assert live.name == "c1" and live.app == "app1"
    session.use(None)


def test_resolve_prefers_explicit_name(home):
    from common import config, session
    write_config(home, {"db_connections": {
        "app1": [dict(_CONN, name="c1"), dict(_CONN, name="c2", port=5433)]}})
    session.save(config.find("c1"))
    assert config.resolve("c2").name == "c2"


def test_resolve_falls_back_to_session(home):
    from common import config, session
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    h = session.save(config.find("c1"))
    session.use(h)
    try:
        assert config.resolve().name == "c1"
    finally:
        session.use(None)


def test_resolve_without_session_says_what_to_do(home):
    from common import config
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    with pytest.raises(config.ConfigError) as ei:
        config.resolve()
    assert "gaussdb-login" in str(ei.value)


def test_session_connection_not_in_config_still_resolves(home):
    """api 模式的连接是登录时现场造的，配置文件里根本没有它。

    不回落到会话的话，下一个 skill 会报「没有这个连接」。
    """
    from common import config, session
    write_config(home, {"connection_mode": "api", "api_connection": [
        {"host": "h", "port": 8769}]})
    live = config.Connection(name="10-0-0-9", type="gaussdb", host="h",
                             port=8769, database="10.0.0.9", user="grmp",
                             driver="grmp", data_ip="10.0.0.9", app="api")
    h = session.save(live)
    session.use(h)
    try:
        assert config.resolve("10-0-0-9").data_ip == "10.0.0.9"
        assert config.resolve().data_ip == "10.0.0.9"
    finally:
        session.use(None)


def test_session_with_unknown_key_refuses(home):
    """手工改会话文件时写错键名（data_ip 写成 dataip），静默忽略会连错实例。"""
    from common import config, session
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    h = session.save(config.find("c1"))
    session.path_for(h).write_text(
        yaml.safe_dump({"name": "c1", "type": "opengauss", "host": "h",
                        "port": 5432, "database": "db", "user": "u",
                        "dataip": "10.0.0.9"}), encoding="utf-8")
    session.use(h)
    try:
        with pytest.raises(config.ConfigError) as ei:
            session.current()
        assert "dataip" in str(ei.value)
    finally:
        session.use(None)


def test_clear_session(home):
    from common import config, session
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    h = session.save(config.find("c1"))
    with pytest.raises(config.ConfigError):            # 退出也要指名
        session.clear()
    assert session.clear(h) is True
    assert session.current() is None
    assert session.clear(h) is False


# --- 内联密文 ----------------------------------------------------------------

def test_inline_encrypted_password_roundtrip(home):
    from common import config, credential
    blob = credential.seal_secret("c1", "s3cret")
    write_config(home, {"db_connections": {"app1": [
        dict(_CONN, name="c1", password=blob, encrypted=True)]}})
    assert credential.secret_for(config.find("c1")) == "s3cret"


def test_ciphertext_cannot_be_moved_to_another_connection(home):
    """AAD 绑定连接名 —— 密文泄露了也不能挪到别的连接上复用。"""
    from common import config, credential
    blob = credential.seal_secret("c1", "s3cret")
    write_config(home, {"db_connections": {"app1": [
        dict(_CONN, name="c2", password=blob, encrypted=True)]}})
    with pytest.raises(credential.CredentialError) as ei:
        credential.secret_for(config.find("c2"))
    assert "AAD" in str(ei.value) or "解开" in str(ei.value)


def test_credentials_dir_is_the_default_source(home):
    """推荐形态：配置里没有 password，口令在凭据目录，由脚本自动解密。

    （这条原先测的是「明文 password 能用」—— 那个行为已被禁止，见
    test_plaintext_password_is_refused。改成测现在该走的路。）
    """
    from common import config, credential
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    credential.save_secret("c1", "from-store")
    assert credential.secret_for(config.find("c1")) == "from-store"


def test_bad_base64_says_what_to_fix(home):
    from common import config, credential
    write_config(home, {"db_connections": {"app1": [
        dict(_CONN, name="c1", password="不是base64", encrypted=True)]}})
    with pytest.raises(credential.CredentialError) as ei:
        credential.secret_for(config.find("c1"))
    assert "encrypted" in str(ei.value)


def test_env_password_overrides_everything(home, monkeypatch):
    """环境变量优先级最高 —— 调试时临时换口令不必改配置或重存凭据。"""
    from common import config, credential
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    credential.save_secret("c1", "from-store")
    monkeypatch.setenv("GSDB_PASSWORD", "from-env")
    assert credential.secret_for(config.find("c1")) == "from-env"


# --- 报告里必须写明「针对哪个库」 --------------------------------------------

def test_resolved_name_falls_back_to_the_session(home):
    """省略 -c 时报告不能记成空串 —— 而「省略 -c」正是推荐用法。

    一份不写明针对哪个库的健康报告，事后没法分辨它是生产还是测试的；
    两份放在一起更是完全一样。
    """
    from common import config, session
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    h = session.save(config.find("c1"))
    session.use(h)
    try:
        assert config.resolved_name() == "app1/c1"
        assert config.resolved_name("") == "app1/c1"
    finally:
        session.use(None)


def test_resolved_name_is_app_qualified(home):
    """只记 `og` 在多应用环境里仍然是二义的。"""
    from common import config
    write_config(home, {"db_connections": {
        "app1": [dict(_CONN, name="c1")],
        "app2": [dict(_CONN, name="c2")]}})
    assert config.resolved_name("c2") == "app2/c2"


def test_resolved_name_does_not_raise_when_unresolvable(home):
    """它只负责「报告怎么写」—— 连接取不到该由真正取数时报错，不该它抢先抛。"""
    from common import config
    write_config(home, {})
    assert config.resolved_name("nope") == "nope"
    assert config.resolved_name() == ""


# --- api 模式：登录要回答两个问题 --------------------------------------------

def test_api_login_answers_both_questions(monkeypatch):
    """客户环境的流程：对话框里给库名 → 走 api_connection 确认

      1) 这个库在不在（中间件按 dataIp 查实例，查不到就拒）
      2) 这个库能跑哪些脚本（白名单）

    两问都靠接口一 —— 它同时回答实例存在性与脚本清单，所以登录只需一次调用。
    """
    import importlib.util as _u
    spec = _u.spec_from_file_location(
        "login_for_test",
        _ROOT / "skills" / "gaussdb-login" / "scripts" / "login.py")
    mod = _u.module_from_spec(spec)
    sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-login" / "scripts"))
    spec.loader.exec_module(mod)

    # 白名单按 skill 前缀分组呈现：91 条平铺没人看得下去，
    # 而「哪个能力域有几条」正好回答「这个库支持哪些诊断」
    grouped = mod._by_skill(["health.overview", "health.bloat", "topsql.top_sql"])
    assert "health" in grouped and "2" in grouped
    assert "topsql" in grouped


def test_api_target_derives_a_safe_connection_name():
    """dataIp 常带点（10.0.0.9），而连接名规则不许有点 —— 派生而不是放松规则。

    那个名字还用来拼 credentials/<name>.enc 的路径，放开点和斜杠等于给路径
    穿越开口子。
    """
    import importlib.util as _u
    spec = _u.spec_from_file_location(
        "login_for_test2",
        _ROOT / "skills" / "gaussdb-login" / "scripts" / "login.py")
    mod = _u.module_from_spec(spec)
    sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-login" / "scripts"))
    spec.loader.exec_module(mod)

    from common.config import ConfigError

    assert mod._safe_name("10.0.0.9") == "10-0-0-9"
    assert mod._safe_name("PROD_DB01") == "prod_db01"
    # 全是分隔符时**拒绝**，不凑一个名字出来 —— 派生出空名或垃圾名之后，
    # 后面拼 credentials/<name>.enc 会指向一个谁也没料到的路径
    with pytest.raises(ConfigError):
        mod._safe_name("....")


# --- 配置文件不允许明文口令 --------------------------------------------------

def test_plaintext_password_is_refused(home):
    """**config.yaml 会被 cat、进备份、贴进工单** —— 明文口令不该在这条路径上。

    拒绝而不是「警告后继续」：警告在一堆输出里没人看，而配置一旦带着明文
    跑起来，它就会一直那样跑下去。
    """
    from common import config
    write_config(home, {"db_connections": {"app1": [
        dict(_CONN, name="c1", password="hunter2")]}})
    with pytest.raises(config.ConfigError) as ei:
        config.load()
    assert "明文" in str(ei.value)
    assert "credential_cli" in str(ei.value), "要告诉用户下一步跑什么"


def test_plaintext_with_encrypted_false_is_also_refused(home):
    """显式写 encrypted: false 也不行 —— 那只是把明文说得更明白。"""
    from common import config
    write_config(home, {"db_connections": {"app1": [
        dict(_CONN, name="c1", password="hunter2", encrypted=False)]}})
    with pytest.raises(config.ConfigError):
        config.load()


def test_encrypted_password_is_accepted(home):
    from common import config, credential
    blob = credential.seal_secret("c1", "hunter2")
    write_config(home, {"db_connections": {"app1": [
        dict(_CONN, name="c1", password=blob, encrypted=True)]}})
    assert credential.secret_for(config.find("c1")) == "hunter2"


def test_no_password_at_all_is_the_recommended_shape(home):
    """推荐形态：配置里根本没有 password，口令在凭据目录。"""
    from common import config
    write_config(home, {"db_connections": {"app1": [dict(_CONN, name="c1")]}})
    conn = config.find("c1")
    assert conn.password == "" and conn.encrypted is False


# --- api 模式：实例 IP + 数据库名 --------------------------------------------

def _login_mod():
    import importlib.util as _u
    spec = _u.spec_from_file_location(
        "login_api_test",
        _ROOT / "skills" / "gaussdb-login" / "scripts" / "login.py")
    mod = _u.module_from_spec(spec)
    sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-login" / "scripts"))
    spec.loader.exec_module(mod)
    return mod


def _Endpoint():
    """用真的 ApiEndpoint 当替身,别手写。

    手写替身会随生产类的接口漂移:a57f37f 给 ApiEndpoint 加了 resolve_host()
    之后,原来那个只有 host/port 两个属性的假类就让四个用例一起红了,
    而生产代码其实是好的。
    """
    from common.config import ApiEndpoint
    return ApiEndpoint(host="grmp.example", port=8080)


def test_api_connection_maps_ip_to_dataip_and_keeps_database():
    """**IP 进 dataIp，库名进 database。**

    接口一的报文里只有 dataIp 一个定位字段（文档写明是单 IP），没有库名的
    位置 —— 所以中间件按 IP 找实例。库名仍必须收：它决定取数落在哪个库，
    也要写进报告抬头，否则同一实例上的多个库，报告事后分不清。
    """
    conn = _login_mod()._api_connection("10.0.0.9", "appdb", _Endpoint())
    assert conn.data_ip == "10.0.0.9"      # 发给中间件的定位键
    assert conn.database == "appdb"        # 记录 + 报告抬头
    assert conn.driver == "grmp"
    assert conn.host == "grmp.example" and conn.port == 8080


def test_api_connection_name_carries_both():
    """连接名同时带上 IP 与库名 —— 同一实例上的多个库要能分辨。"""
    conn = _login_mod()._api_connection("10.0.0.9", "appdb", _Endpoint())
    assert conn.name == "10-0-0-9-appdb"


def test_same_ip_different_database_are_distinct_connections():
    """否则第二次登录会覆盖第一次的会话，而用户以为换了库。"""
    mod = _login_mod()
    a = mod._api_connection("10.0.0.9", "db_a", _Endpoint())
    b = mod._api_connection("10.0.0.9", "db_b", _Endpoint())
    assert a.name != b.name


def test_api_connection_passes_validation():
    """现场构造的连接也要过 validate —— 它会被写进会话文件。"""
    from common import config
    config.validate(_login_mod()._api_connection("10.0.0.9", "appdb",
                                                 _Endpoint()))
