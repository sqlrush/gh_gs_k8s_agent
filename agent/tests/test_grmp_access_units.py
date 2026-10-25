"""skill 侧统一入口：按连接的 driver 选路，两条路径对外形状一致。

    runner = access.for_conn("og")
    rows = runner.run("slowsql.slow_sql", {"threshold_ms": 200, ...})

skill 不感知自己走的是中间件还是直连。这一点是本阶段的全部价值：
skill 代码在本地与客户环境完全相同，不需要为两边各留一套。
"""
import re
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common import access  # noqa: E402
from common.config import Connection, ConfigError, validate  # noqa: E402
from common.grmp import registry, script as sc  # noqa: E402
from common.grmp.params import ParamValueError, to_param_value  # noqa: E402
from common.grmp.placeholder import ParamError, ParamDef  # noqa: E402
from common.grmp.runner import DirectRunner, RunError  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402


class FakeDB:
    def __init__(self, cols=("n",), rows=((1,),)):
        self.cols = list(cols)
        self.rows = [tuple(r) for r in rows]
        self.executed = []
        self.closed = False

    def query(self, sql, params=None):
        self.executed.append(sql)
        return self.cols, self.rows

    def set_statement_timeout(self, seconds):
        pass

    def close(self):
        self.closed = True


def _registry(tmp_path, name="slowsql.slow_sql", sql=None, params=None):
    """写一个临时脚本仓库目录。"""
    body = ["name: %s" % name, "description: d"]
    body.append("sql: |")
    body.append("  " + (sql or "select 1 where a > {{n}};"))
    if params is None:
        params = [("n", "INTEGER")]
    if params:
        body.append("params:")
        for key, typ in params:
            body += ["  - key: %s" % key, "    type: %s" % typ,
                     "    description: x"]
    d = tmp_path / "registry" / name.split(".")[0]
    d.mkdir(parents=True, exist_ok=True)
    (d / (name.split(".")[1] + ".yaml")).write_text(
        "\n".join(body) + "\n", encoding="utf-8"
    )
    return tmp_path / "registry"


# ===========================================================================
# 1. 脚本仓库：按逻辑名查，不按 ID
# ===========================================================================

def test_registry_finds_script_by_logical_name(tmp_path):
    reg = registry.Registry(_registry(tmp_path))
    assert reg.find("slowsql.slow_sql").script_name == "slowsql.slow_sql"


def test_unknown_logical_name_error_lists_available_scripts(tmp_path):
    """报错要能直接用：列出有哪些脚本，而不是只说「找不到」。"""
    reg = registry.Registry(_registry(tmp_path))
    with pytest.raises(registry.RegistryError) as exc:
        reg.find("nope.nope")
    assert "slowsql.slow_sql" in str(exc.value)


def test_registry_rejects_duplicate_logical_names(tmp_path):
    """逻辑名是跨环境的匹配键，重名会让「按名解析 ID」变得不确定。"""
    root = _registry(tmp_path)
    dup = root / "other"
    dup.mkdir()
    (dup / "dup.yaml").write_text(
        "name: slowsql.slow_sql\ndescription: d\nsql: |\n  select 2 where a > {{n}};\n"
        "params:\n  - key: n\n    type: INTEGER\n    description: x\n",
        encoding="utf-8",
    )
    with pytest.raises(registry.RegistryError):
        registry.Registry(root).names()


# ===========================================================================
# 2. 取值转换：skill 传原生类型，协议要字符串
# ===========================================================================

def test_int_becomes_decimal_string():
    assert to_param_value(200) == "200"
    assert to_param_value(-2) == "-2"


def test_bool_becomes_lowercase_literal():
    """必须是 "true"/"false"。str(True) 得到 "True"，协议不认。"""
    assert to_param_value(True) == "true"
    assert to_param_value(False) == "false"


def test_string_passes_through_unchanged():
    assert to_param_value("2024-10-19 11:10:25") == "2024-10-19 11:10:25"


@pytest.mark.parametrize("bad", [1.5, None, [1], {"a": 1}])
def test_unsupported_type_is_rejected_rather_than_stringified(bad):
    """str() 兜底会把 None 变成 "None"、把 1.5 变成 "1.5" 送进 INTEGER 校验。

    前者会被当成合法字符串塞进 SQL，后者虽然会被拒但错误信息指向错的地方。
    在入口处就拒绝，错误信息才指得准。
    """
    with pytest.raises(ParamValueError):
        to_param_value(bad)


# ===========================================================================
# 3. 直连路径
# ===========================================================================

def test_direct_runner_renders_and_returns_stringified_rows(tmp_path):
    """直连路径同样把结果字符串化 —— 与中间件路径形状一致。

    保留原生类型会让本地写出的解析代码到客户环境全部失效：
    客户那边拿到的一律是字符串。
    """
    db = FakeDB(cols=("datname", "oid"), rows=[("postgres", 16384)])
    runner = DirectRunner(
        conn_name="og",
        registry=registry.Registry(_registry(tmp_path)),
        settings=Settings(),
        open_db=lambda name, read_only=True: db,
    )
    rows = runner.run("slowsql.slow_sql", {"n": 10})
    assert rows == [{"datname": "postgres", "oid": "16384"}]
    # YAML 块标量自带尾换行，比对时不计首尾空白
    assert [s.strip() for s in db.executed] == ["select 1 where a > 10;"]
    assert db.closed is True


def test_direct_runner_rejects_missing_param(tmp_path):
    runner = DirectRunner(
        conn_name="og",
        registry=registry.Registry(_registry(tmp_path)),
        settings=Settings(),
        open_db=lambda name, read_only=True: FakeDB(),
    )
    with pytest.raises(ParamError):
        runner.run("slowsql.slow_sql", {})


def test_direct_runner_rejects_injection_before_connecting(tmp_path):
    opened = []

    def open_db(name):
        opened.append(name)
        return FakeDB()

    runner = DirectRunner(
        conn_name="og",
        registry=registry.Registry(_registry(tmp_path)),
        settings=Settings(),
        open_db=open_db,
    )
    with pytest.raises(ParamError):
        runner.run("slowsql.slow_sql", {"n": "1 OR 1=1"})
    assert opened == []


def test_direct_runner_also_rejects_duplicate_columns(tmp_path):
    """两条路径都要挡：否则同一条坏脚本在直连下能跑、走中间件才炸。"""
    from common.grmp.columns import ColumnError
    runner = DirectRunner(
        conn_name="og",
        registry=registry.Registry(_registry(tmp_path)),
        settings=Settings(),
        open_db=lambda name, read_only=True: FakeDB(
            cols=("round", "round"), rows=[(1, 2)]),
    )
    with pytest.raises(ColumnError):
        runner.run("slowsql.slow_sql", {"n": 1})


def test_statement_without_result_set_raises_instead_of_returning_empty(tmp_path):
    """无结果集时报错，不返回 []。

    返回 [] 会让调用方把「这条语句根本没有结果集」读成「查到 0 行」——
    两条路径都必须这样，否则一致性比对会掩盖差异。
    """
    runner = DirectRunner(
        conn_name="og",
        registry=registry.Registry(_registry(tmp_path)),
        settings=Settings(),
        open_db=lambda name, read_only=True: FakeDB(cols=(), rows=[]),
    )
    with pytest.raises(RunError):
        runner.run("slowsql.slow_sql", {"n": 1})


# ===========================================================================
# 4. 选路与配置
# ===========================================================================

def _conn(**kw):
    base = dict(
        name="og", type="opengauss", host="h", port=5432,
        database="d", user="u",
    )
    base.update(kw)
    return Connection(**base)


def test_grmp_is_a_valid_driver():
    validate(_conn(driver="grmp", data_ip="10.0.0.9"))


def test_grmp_driver_requires_data_ip():
    """dataIp 是中间件路由到实例的唯一依据，缺了必须启动即报错。"""
    with pytest.raises(ConfigError):
        validate(_conn(driver="grmp"))


def test_data_ip_is_ignored_for_direct_drivers():
    validate(_conn(driver="psycopg2"))


def test_existing_drivers_still_validate():
    validate(_conn(driver="psycopg2"))
    validate(_conn(driver="gsql"))


@pytest.mark.parametrize("driver", ["psycopg2", "gsql"])
def test_direct_drivers_select_the_direct_runner(driver, tmp_path, monkeypatch):
    monkeypatch.setenv("GRMP_REGISTRY", str(_registry(tmp_path)))
    runner = access.runner_for(_conn(driver=driver))
    assert isinstance(runner, DirectRunner)


def test_grmp_driver_selects_the_middleware_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("GRMP_REGISTRY", str(_registry(tmp_path)))
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "x" * 32)
    runner = access.runner_for(_conn(driver="grmp", data_ip="10.0.0.9"))
    assert not isinstance(runner, DirectRunner)
    assert runner.data_ip == "10.0.0.9"


def test_middleware_runner_requires_the_path_prefix_at_construction(tmp_path, monkeypatch):
    """中间件接口路径前缀是部署配置(GRMP_PATH_PREFIX):没配就在造 runner 时报清楚,经凭据代理也一样。"""
    monkeypatch.setenv("GRMP_REGISTRY", str(_registry(tmp_path)))
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "x" * 32)
    monkeypatch.delenv("GRMP_PATH_PREFIX", raising=False)
    with pytest.raises(access.AccessError) as exc:
        access.runner_for(_conn(driver="grmp", data_ip="10.0.0.9"))
    assert "GRMP_PATH_PREFIX" in str(exc.value)
    monkeypatch.setenv("GRMP_PROXY_SOCKET", "/tmp/nope.sock")
    with pytest.raises(access.AccessError) as exc:
        access.runner_for(_conn(driver="grmp", data_ip="10.0.0.9"))
    assert "GRMP_PATH_PREFIX" in str(exc.value)


def test_middleware_runner_requires_the_token_at_construction(tmp_path, monkeypatch):
    """令牌只从环境变量读；缺失即报错，不等到第一次请求才失败。"""
    monkeypatch.setenv("GRMP_REGISTRY", str(_registry(tmp_path)))
    monkeypatch.delenv("GRMP_AUTH_TOKEN", raising=False)
    with pytest.raises(access.AccessError):
        access.runner_for(_conn(driver="grmp", data_ip="10.0.0.9"))


# ===========================================================================
# 语句超时 —— 迁移曾把它整个丢掉
# ===========================================================================

def _timeout_conn(name="c", driver="psycopg2"):
    return Connection(name=name, type="opengauss", host="127.0.0.1", port=5433,
                      database="d", user="u", driver=driver,
                      data_ip="127.0.0.1")


def test_explicit_timeout_reaches_the_direct_runner():
    """--timeout 5 必须真的变成 statement_timeout。

    迁移后这里断了：skill 收下参数、写进 --help，然后谁也不传给取数层。
    用户设 --timeout 5，查询照样能跑到天荒地老，而且不报错。
    """
    r = access.runner_for(_timeout_conn(), timeout=5)
    assert r._timeout == 5


def test_unspecified_timeout_keeps_the_pre_migration_default():
    """没提要求时用 30 秒 —— 与迁移前各 skill 的 argparse 默认值一致。

    迁移只该换取数通道，不该顺手改超时行为。
    """
    r = access.runner_for(_timeout_conn(), timeout=None)
    assert r._timeout == access.DEFAULT_SKILL_TIMEOUT_SECONDS == 30


def test_whitelist_path_says_out_loud_that_it_cannot_enforce_timeout(monkeypatch, capsys):
    """中间件设不了超时（协议没这个旋钮），显式指定时必须说一声。

    收下参数然后当没看见才是真正危险的：用户以为查询 5 秒会被掐断，
    实际它能在客户生产库上一直跑。
    """
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "x" * 32)
    access.runner_for(_timeout_conn(driver="grmp"), timeout=5)
    assert "无法设置语句超时" in capsys.readouterr().err


def test_whitelist_path_stays_quiet_when_timeout_was_not_asked_for(monkeypatch, capsys):
    """没显式指定就不吭声 —— 每次调用都刷一行，提示很快就没人看了。"""
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "x" * 32)
    access.runner_for(_timeout_conn(driver="grmp"), timeout=None)
    assert capsys.readouterr().err == ""


_DECLARES_TIMEOUT_OPTION = re.compile(
    r'\.add_argument\((?:(?!\)).)*?["\']--timeout["\']', re.DOTALL)


def test_declares_timeout_option_matches_regardless_of_flag_order():
    """--timeout 可以不是 add_argument() 的第一个位置参数。

    这条曾经真的错过：最初的正则要求 --timeout 紧跟在左括号后面，
    而这个仓库里到处都是 add_argument("-c", "--conn", ...) 这种
    短选项在前的写法（health.py:67、lockwait.py:634、proctune.py:383）。
    照那种写法声明 add_argument("-t", "--timeout", ...) 会让最初那版
    正则直接看不见——而这恰恰是这条守卫本该抓的那类回归。
    """
    assert _DECLARES_TIMEOUT_OPTION.search(
        'ap.add_argument("--timeout", type=int, default=None)')
    assert _DECLARES_TIMEOUT_OPTION.search(
        'ap.add_argument("-t", "--timeout", type=int, default=None)')
    assert not _DECLARES_TIMEOUT_OPTION.search(
        'ap.add_argument("--limit", type=int, default=20)')


def test_every_skill_that_offers_timeout_actually_passes_it_down():
    """任何**声明了** --timeout 的 skill，都得把它交给取数层。

    这条是防复发的：以后新增 skill 或改写入口，忘了传就红。

    只认 argparse 里显式的 `add_argument(..., "--timeout", ...)`——
    `--timeout` 在调用里出现在哪个位置不重要（`add_argument("-t",
    "--timeout", ...)` 这种短选项在前的写法在这个仓库里很常见，必须也
    抓得到），但**不认**「文本里出现过 --timeout 这个词」。声明和提及是
    两回事：转发者——比如 gaussdb-health/scripts/aggregate.py 为子进程
    拼 subprocess argv 时会写出字面量 "--timeout"——并没有把 --timeout
    定义成*自己*的命令行选项，也没有义务接住 args.timeout 或调用
    set_statement_timeout，它只是把收到的 timeout 值转发给别的进程，这
    本身就是「交给取数层」的一种方式，只是通过这条守卫最初没预料到的
    机制。按字面量扫描会把转发者也计入「收下却没用」，误伤跟这条守卫
    想防的回归（真正声明了 --timeout、却在解析后直接把 args.timeout
    扔掉）是两回事。以后别把这条改回「只认第一个参数」或纯字符串扫描
    ——前者会漏掉短选项在前的声明，后者会把转发代码一并抓进来。
    """
    skills = _ROOT / "skills"
    offenders = []
    for entry in sorted(skills.glob("gaussdb-*/scripts/*.py")):
        text = entry.read_text(encoding="utf-8")
        if not _DECLARES_TIMEOUT_OPTION.search(text):
            continue
        if "timeout=args.timeout" in text or "set_statement_timeout" in text:
            continue
        offenders.append(entry.relative_to(skills).as_posix())
    assert not offenders, "这些 skill 收下 --timeout 却没往下传：%s" % offenders
