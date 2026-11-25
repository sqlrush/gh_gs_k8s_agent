"""在业务库执行的脚本,每一列的输出类型都要在 M 兼容库上能原样取回来(v1.5.2,2026-11-25 现场反馈)。

现场:健康检查 Schema 维度在 8 个库里 6 个报「无法识别的布尔取值 '[B@276fe535'」。业务库是 M 兼容模式时,
现场中间件(Java)按 JDBC 类型映射取值再转成文字,布尔列取出来是字节数组,转成文字就成了对象地址,原值丢失。
1.5.1 的 M 兼容修复只管 SQL 能不能执行,1.5.1 的模拟测试用 gsql 看文本输出,都碰不到这一层。

规则:业务库脚本的输出列只能是《GaussDB M-Compatibility 开发指南》JDBC 映射表里取出来就是文字 / 整数 / 小数的
类型(``SAFE``)。布尔写成 ``CASE WHEN … THEN 1 ELSE 0 END``,名字类(name)与 ``"char"`` 写 ``::text``。

类型要连库才知道:由 ``tools/registry_output_types.py`` 连 A / B 兼容测试库实测后写进
``tests/fixtures/registry_output_types.json``(含每条脚本正文的 sha256)。本守卫不连库,核对清单完整、未过期、
类型都安全;另有一道不连库的静态检查:输出列里的表达式都写了别名(M 兼容库给表达式起的列名与 A 库不一定相同)。
"""
import hashlib
import json
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import routing  # noqa: E402
from common.grmp.registry import Registry  # noqa: E402
from common.grmp.values import as_bool, is_java_object_text  # noqa: E402

FIXTURE = _ROOT / "tests" / "fixtures" / "registry_output_types.json"
REGEN = ("在有 A / B 兼容测试库的机器上重跑:python3 tools/registry_output_types.py --port <端口> --user <账号> "
         "--password-file <口令文件> --write(见该工具开头的说明)")

# 取出来就是 String / Integer / Long / BigDecimal / Float / Double 的类型
SAFE = frozenset({"text", "varchar", "bpchar", "int2", "int4", "int8", "numeric", "float4", "float8"})

# 例外:每一条都写明为什么可以不改
EXCEPTIONS = {
    # 登录探针:name 类型不在映射表里,但现场已证实能正常取到 —— 1.5.1 在现场 M 兼容库上按这一列核对切库成功,
    # 之后的业务库脚本才在 M 库上执行、才撞上布尔列。登录是一切的入口,不为统一写法去动一条现场已验证的脚本。
    ("login.current_db", "db"): "name",
    ("login.current_db", "usr"): "name",
    # EXPLAIN 的输出列类型改不了(json)。M 兼容库上取回来是 [B@… 时,sqltune 改用 sqltune.plan_yaml(text 类型)。
    ("sqltune.plan_json", "QUERY PLAN"): "json",
}
# 读 GaussDB 专有视图 ADM_SOURCE,测试库(openGauss)上跑不了;M 兼容库上调用方直接跳过(selfcheck.M_NOT_APPLICABLE)
UNRUNNABLE = {"procinfo.source_adm", "proctune.source_adm"}

_REG = Registry()
_BUSINESS = sorted(routing.BUSINESS_DB_SCRIPTS)


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _digest(name: str) -> str:
    return hashlib.sha256(_REG.find(name).script_content.encode("utf-8")).hexdigest()


def test_fixture_covers_exactly_the_business_db_scripts():
    got = set(_fixture())
    assert got == set(_BUSINESS), (
        "类型清单与业务库脚本对不上:缺 %s,多 %s。%s" % (sorted(set(_BUSINESS) - got), sorted(got - set(_BUSINESS)), REGEN))


@pytest.mark.parametrize("name", _BUSINESS)
def test_fixture_is_not_stale(name):
    entry = _fixture()[name]
    assert entry["sql_sha256"] == _digest(name), (
        "%s 的 SQL 改过了,类型清单是按旧正文测的,不能再信。%s" % (name, REGEN))


@pytest.mark.parametrize("name", _BUSINESS)
def test_every_output_column_is_m_safe(name):
    entry = _fixture()[name]
    if name in UNRUNNABLE:
        assert entry.get("error_a") and entry.get("error_b"), "%s 在测试库上能跑了:从 UNRUNNABLE 里删掉,按常规核对" % name
        return
    assert not entry.get("error_a") and not entry.get("error_b"), (
        "%s 在测试库上报错(A:%s / B:%s),类型无从核对" % (name, entry.get("error_a"), entry.get("error_b")))
    cols_a, cols_b = entry["columns_a"], entry["columns_b"]
    assert [c for c, _ in cols_a] == [c for c, _ in cols_b], "%s 在 A / B 两种库上的列名不一样:%s / %s" % (
        name, cols_a, cols_b)
    bad = []
    for (col, ta), (_c, tb) in zip(cols_a, cols_b):
        allowed = {EXCEPTIONS[(name, col)]} if (name, col) in EXCEPTIONS else set()
        for typ in (ta, tb):
            if typ not in SAFE and typ not in allowed:
                bad.append("%s=%s" % (col, typ))
    assert not bad, (
        "%s 的输出列 %s 不在 M 兼容库能原样取回的类型里:布尔改成 CASE WHEN … THEN 1 ELSE 0 END,名字类 / \"char\" 加 ::text,"
        "改完 %s" % (name, "、".join(sorted(set(bad))), REGEN))


def test_exceptions_are_still_needed():
    """例外清单里的列如果已经改成安全类型,就该从例外里删掉 —— 例外只减不增。"""
    fx = _fixture()
    for (name, col), typ in EXCEPTIONS.items():
        types = {t for c, t in fx[name]["columns_a"] + fx[name]["columns_b"] if c == col}
        assert types == {typ}, "%s.%s 现在是 %s,不再需要例外" % (name, col, types)


# ---------------------------------------------------------------- 不连库的静态检查:表达式列都要有别名

def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).strip().rstrip(";")


def select_items(sql: str):
    """最外层 SELECT 与 FROM 之间的输出列,按顶层逗号切开(跳过括号与引号内)。EXPLAIN 脚本返回 None。"""
    s = _code(sql)
    if re.match(r"\s*explain\b", s, re.I):
        return None
    m = re.search(r"\bselect\b", s, re.I)
    if not m:
        return None
    i, depth, start, items, quote = m.end(), 0, m.end(), [], None
    while i < len(s):
        ch = s[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch == ",":
            items.append(s[start:i])
            start = i + 1
        elif depth == 0 and s[i:i + 4].lower() == "from" and not re.match(r"\w", s[i - 1]) and not re.match(r"\w", s[i + 4:i + 5]):
            items.append(s[start:i])
            return [x.strip() for x in items]
        i += 1
    items.append(s[start:])
    return [x.strip() for x in items]


_BARE = re.compile(r'^(?:\w+\.)?"?\w+"?$')
_ALIASED = re.compile(r'\bAS\s+"?\w+"?$', re.I)


def unaliased(sql: str):
    items = select_items(sql) or []
    return [x for x in items if not _BARE.match(x) and not _ALIASED.search(x)]


@pytest.mark.parametrize("name", _BUSINESS)
def test_expression_columns_have_an_alias(name):
    bad = unaliased(_REG.find(name).script_content)
    assert not bad, "%s 的输出列里有没写别名的表达式:%s(M 兼容库给表达式起的列名与 A 库不一定相同,技能按列名取值)" % (
        name, bad)


# ---------------------------------------------------------------- 守卫自己要能红

def test_the_guards_themselves_can_fail():
    assert unaliased("SELECT l.pid::text, l.mode FROM pg_locks l") == ["l.pid::text"]
    assert unaliased("SELECT l.pid::text AS pid, count(*) AS n, x.y FROM t x") == []
    assert unaliased("EXPLAIN (FORMAT TEXT) {{sql}}") == []
    assert select_items("SELECT a, (SELECT b FROM t2) AS c, 'from x' AS d FROM t") == [
        "a", "(SELECT b FROM t2) AS c", "'from x' AS d"]
    assert "bool" not in SAFE and "name" not in SAFE and "char" not in SAFE and "json" not in SAFE
    assert len(_BUSINESS) >= 42, "业务库脚本没读全,守卫形同虚设"


def test_java_object_text_is_recognized_and_rejected_by_as_bool():
    assert is_java_object_text("[B@276fe535") and is_java_object_text("[B@6d06d69c")
    assert not is_java_object_text("[1,2]") and not is_java_object_text("t") and not is_java_object_text("")
    with pytest.raises(ValueError) as ei:
        as_bool("[B@276fe535")
    assert "字节数组" in str(ei.value) and "CASE WHEN" in str(ei.value)
    assert as_bool("1") is True and as_bool("0") is False and as_bool("true") is True and as_bool("f") is False
