"""M 兼容业务库的词法(v1.5.1):语句守卫、取表名、补 schema、sqlreview 切分。

M 兼容库(MySQL 词法):# 到行尾是注释、-- 后跟空白才是注释、双引号是字符串、字符串里反斜杠转义、反引号是标识符。
语句守卫是 EXPLAIN {{sql}} 模板的第一道防线 —— 只按 A 规则切,M 库里藏在 # 注释或转义引号后面的第二条语句就漏过去了。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import schema_infer, sqlqualify, sqlrefs  # noqa: E402
from common.grmp import statement as st  # noqa: E402
from common.grmp.statement import ExplainNotAllowed  # noqa: E402

A, BOTH = st.DIALECT_A, st.DIALECT_BOTH

# 只按 A 规则看是一条只读语句、M 库实际是两条(第二条是写)的载荷
SMUGGLED = [
    "SELECT 1 # '\n; DELETE FROM t; -- '",                       # # 注释里的引号让 A 规则以为后面全是字符串
    "SELECT 'a\\' ; DELETE FROM t; -- '",                        # sql_mode 关了反斜杠转义时 'a\' 在这里就结束
    "SELECT \"x\\\"; DELETE FROM t; -- \"",                       # 双引号字符串里的反斜杠转义
]


@pytest.fixture(autouse=True)
def _reset_dialect():
    saved = st._dialect
    yield
    st._dialect = saved


@pytest.mark.parametrize("sql", SMUGGLED)
def test_m_library_rejects_statements_hidden_from_a_rules(sql):
    with pytest.raises(ExplainNotAllowed):
        st.ensure_explainable(sql, dialect=BOTH)
    st.set_dialect("M")
    with pytest.raises(ExplainNotAllowed):
        st.ensure_explainable(sql)                                # 不传 dialect:按 set_dialect 设的本进程方言


def test_a_library_behaviour_is_unchanged():
    """A 库只按 A 规则:1.5.0 能过的照样能过(-- 后不跟空白也是注释)。"""
    st.set_dialect("A")
    for sql in ("SELECT 1 --c;d", "SELECT 1 -- a;b;c", "SELECT 'x;y' AS a", 'SELECT "a;b" FROM t'):
        st.ensure_explainable(sql)
    assert st._resolve(None) == A


def test_unknown_mode_scans_both_ways():
    st.set_dialect("")
    assert st._resolve(None) == BOTH
    with pytest.raises(ExplainNotAllowed):
        st.ensure_explainable(SMUGGLED[0])


def test_not_set_means_a_like_before():
    st._dialect = None
    assert st._resolve(None) == A


def test_m_valid_statements_still_pass():
    for sql in ("SELECT `order` FROM `t_order` WHERE note = 'it\\'s'",
                "# 取最近一天\nSELECT * FROM t_order",
                "SELECT * FROM t WHERE a = \"x;y\" -- 尾注释;\n"):
        st.ensure_explainable(sql, dialect=BOTH)


def test_dml_detected_in_either_lexing():
    assert st.is_dml("SELECT 1 # x\n; UPDATE t SET a = 1", dialect=BOTH)
    assert not st.is_dml("SELECT 'update' AS x", dialect=BOTH)
    assert not st.is_read_only("/*!50000 DELETE FROM t */ SELECT 1", dialect=BOTH), "/*! */ 在 MySQL 里会执行,不当注释"


def test_dialect_mapping():
    assert st.dialect_for("A") == A and st.dialect_for("pg") == A and st.dialect_for("C") == A
    assert st.dialect_for("M") == BOTH and st.dialect_for("B") == BOTH and st.dialect_for("") == BOTH


# ---------------- 取表名 / 补 schema ----------------

def test_table_refs_accept_backticks_and_case_rule():
    sql = "SELECT * FROM `App`.`Orders` o, `t_x` x JOIN Customers c ON o.c = c.id"
    assert sqlrefs.extract_table_refs(sql) == ["app.orders", "t_x", "customers"]
    assert sqlrefs.extract_table_refs(sql, fold=False) == ["App.Orders", "t_x", "Customers"]
    assert sqlrefs.extract_tables(sql, fold=False) == ["Orders", "t_x", "Customers"]
    assert sqlrefs.extract_table_refs("SELECT * FROM orders o, items i") == ["orders", "items"], "A 库照旧"


def test_qualify_on_m_uses_backticks_and_respects_m_strings():
    sql = "SELECT * FROM orders WHERE note = 'it\\'s from t2' # from t3\n"
    out, changed = sqlqualify.qualify_tables(sql, "App", mysql=True)
    assert out == "SELECT * FROM `App`.orders WHERE note = 'it\\'s from t2' # from t3\n" and changed == ["orders"]
    out, _ = sqlqualify.qualify_tables('SELECT * FROM orders WHERE a = "from x"', "app", mysql=True)
    assert out == 'SELECT * FROM app.orders WHERE a = "from x"', "M 库双引号是字符串,里面的 from 不是表引导词"
    assert sqlqualify.qualify_tables("SELECT * FROM `t_o` x", "app", mysql=True)[0] == "SELECT * FROM app.`t_o` x"


def test_qualify_on_a_is_unchanged():
    assert sqlqualify.qualify_tables("SELECT * FROM orders", "App")[0] == 'SELECT * FROM "App".orders'
    assert sqlqualify.unqualified_tables('SELECT * FROM "Orders" o JOIN app.x ON 1=1') == ['"Orders"']


def test_schema_infer_lookup_key_on_m():
    assert schema_infer._lookup_key("`Orders`") == "orders"
    assert schema_infer._lookup_key("`Orders`", fold=False) == "Orders"
    assert schema_infer._lookup_key("Orders", fold=False) == "Orders"
    assert schema_infer._lookup_key('"Orders"') == "Orders" and schema_infer._lookup_key("Orders") == "orders"


# ---------------- sqlreview 切分 ----------------
_SR = _ROOT / "skills" / "gaussdb-sqlreview" / "scripts"


@pytest.fixture()
def lexer():
    saved = {n: sys.modules.pop(n) for n in ("lexer", "model") if n in sys.modules}
    sys.path.insert(0, str(_SR))
    spec = importlib.util.spec_from_file_location("lexer_m_under_test", _SR / "lexer.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    mod.set_mysql(False)
    sys.modules.pop(spec.name, None)
    sys.path.remove(str(_SR))
    for n in ("lexer", "model"):
        sys.modules.pop(n, None)
    sys.modules.update(saved)


def test_sqlreview_split_on_m(lexer):
    sql = ("SELECT * FROM `t_order` WHERE note = \"a;b\" # c;d\n AND x = 'it\\'s;';"
           " CREATE TABLE `OrderItems` (id int)")
    lexer.set_mysql(False)
    assert len(lexer.split(sql)) == 3, "A 规则:双引号当标识符、# 不是注释 —— 与 1.5.0 相同"
    lexer.set_mysql(True)
    stmts = lexer.split(sql)
    assert [s.verb for s in stmts] == ["select", "create_table"]
    assert lexer._written("`OrderItems`") == "OrderItems"
    assert "a;b" in lexer.normalize('SELECT "a;b"')[1].values()
