"""切到业务库执行的脚本不许用 now() / current_timestamp 算时间差(v1.4.14,2026-11-16 现场反馈)。

业务库可能是 M 兼容模式(dbcompatibility='M'):那里 now()、current_timestamp 返回 M 兼容的 datetime,
与 pg_stat_* 里的 timestamptz 列相减后,EXTRACT(EPOCH …) 的结果类型 GaussDB JDBC 驱动不认识,整条脚本报 un support type
(vacuum.dead_tuples 的 last_autovacuum_age_s 列)。业务库脚本统一写 date_part('epoch', clock_timestamp() - 列):
clock_timestamp() 两种兼容模式下都返回 timestamptz(已在 M 兼容业务库上实测通过)。

在 postgres 库执行的脚本不受影响(postgres 库为 A 兼容),不在本守卫范围内 —— 那些脚本照旧用 now()。
"""
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import routing  # noqa: E402
from common.grmp.registry import Registry  # noqa: E402

# M 兼容库里返回 datetime 的「当前时间」写法
M_DATETIME_NOW = re.compile(r"\bnow\s*\(\s*\)|\bcurrent_timestamp\b|\blocaltimestamp\b|\bsysdate\b", re.I)

_REG = Registry()
_BUSINESS = [_REG.find(n) for n in sorted(_REG.names()) if _REG.find(n).database_type == routing.APPBUSINESS]


def _code(sql: str) -> str:
    """去掉 -- 注释,只看真正执行的 SQL。"""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


@pytest.mark.parametrize("rec", _BUSINESS, ids=lambda r: r.script_name)
def test_business_db_scripts_do_not_use_m_datetime_now(rec):
    hit = M_DATETIME_NOW.search(_code(rec.script_content))
    assert not hit, (
        "%s 在业务库执行,出现了 %r:M 兼容库里它返回 datetime,与 timestamptz 列相减后驱动报 un support type。"
        "改成 date_part('epoch', clock_timestamp() - 列)" % (rec.script_name, hit.group(0)))


def test_the_two_fixed_scripts_use_clock_timestamp():
    for name, cols in (("vacuum.dead_tuples", ("last_autovacuum_age_s", "last_vacuum_age_s")),
                       ("lockwait.pairs", ("waiter_wait_s", "holder_xact_age_s"))):
        sql = _REG.find(name).script_content
        for col in cols:
            line = next(ln for ln in sql.splitlines() if re.search(r"\bAS\s+%s\b" % col, ln))
            assert "date_part('epoch', clock_timestamp() - " in line, (name, col, line)
            assert "COALESCE" not in line, "%s.%s 故意不 COALESCE 成 0(见 YAML 注释)" % (name, col)


def test_the_guard_itself_can_fail():
    """守卫要能红:原写法必须被认出来,修好的写法不能误报,注释里的 now() 不算。"""
    assert M_DATETIME_NOW.search("EXTRACT(EPOCH FROM (now()-t.last_autovacuum))")
    assert M_DATETIME_NOW.search("x - CURRENT_TIMESTAMP")
    assert not M_DATETIME_NOW.search(_code("date_part('epoch', clock_timestamp() - t.last_vacuum)"))
    assert not M_DATETIME_NOW.search(_code("SELECT 1 -- 以前写 now()"))
    assert len(_BUSINESS) >= 40, "业务库脚本一条都没读到,守卫形同虚设"


# ---- 拼接(2026-11-17 现场):M 兼容模式下 || 是逻辑或,不是字符串拼接 ------------------------------------------
# lockwait.pairs 的 n.nspname || '.' || c.relname 在业务库上整条报「argument … not type name」(锁对象那一列),
# 同类的 health.stale_stats / health.unused_index / perf.table_stat / explain.kernel_funcs 失败时只写「这一项无法查询」,
# 不容易发现。业务库脚本一律用 concat(…)。在 postgres 库执行的脚本(A 兼容)不在本守卫范围内。

M_PIPES = re.compile(r"\|\|")


@pytest.mark.parametrize("rec", _BUSINESS, ids=lambda r: r.script_name)
def test_business_db_scripts_do_not_concatenate_with_pipes(rec):
    hit = M_PIPES.search(_code(rec.script_content))
    assert not hit, ("%s 在业务库执行,出现了 ||:M 兼容库里它是逻辑或,不是拼接,整条报错。改成 concat(…);"
                     "可能为空的列先判空(concat 遇 NULL 在两种模式下结果不同)" % rec.script_name)


def test_the_five_fixed_scripts_keep_their_columns_and_use_concat():
    for name, col in (("lockwait.pairs", "lock_object"), ("health.stale_stats", "tbl_name"),
                      ("health.unused_index", "idx_name"), ("perf.table_stat", "tbl"), ("explain.kernel_funcs", "item")):
        sql = _code(_REG.find(name).script_content)
        line = next(ln for ln in sql.splitlines() if re.search(r"\bAS\s+%s\b" % col, ln, re.I) and "concat(" in ln)
        assert "concat(" in line, (name, col)
    lock = next(ln for ln in _REG.find("lockwait.pairs").script_content.splitlines() if "AS lock_object" in ln)
    assert "CASE WHEN c.relname IS NULL THEN ''" in lock, "锁对象不是表时两列都是 NULL:先判空,两种模式都回 ''"


def test_the_pipe_guard_itself_can_fail():
    assert M_PIPES.search("n.nspname || '.' || c.relname") and M_PIPES.search("a||b")
    assert not M_PIPES.search(_code("concat(a, '.', b) -- 以前写 a || b"))


# ---- v1.5.1(2026-11-20 现场反馈 15 条):M 兼容业务库上的其余写法 ---------------------------------------------
# 依据 GaussDB 25.1.32《M-Compatibility 开发者指南》:ILIKE 不支持;TABLE / INDEX / KEY … 是保留字;双引号默认是字符串;
# CAST 目标类型只有 SIGNED / UNSIGNED / DATE / TIME / DATETIME / BINARY / CHAR / DECIMAL / FLOAT / DOUBLE / JSON;
# 系统视图只继承一部分(没有 ADM_*);name 类型进 COALESCE 会按 M 的类型推导矩阵走(health.table_writes 报 any <> unknown)。

M_ILIKE = re.compile(r"\bilike\b|~~\*", re.I)
M_RESERVED_ALIAS = re.compile(r"\bAS\s+(table|index|key|array|limit|offset|order|group|select|from|where|desc|asc)\b(?!\")", re.I)
M_ARRAY_LITERAL = re.compile(r"\bARRAY\s*\[|::\s*\w+\s*\[\]", re.I)
M_CAST_TARGETS = {"signed", "unsigned", "date", "time", "datetime", "binary", "char", "decimal", "float", "double", "json"}
NAME_COLS = ("nspname", "relname", "proname", "pkgname", "usename", "attname", "conname", "schemaname",
             "indexrelname", "datname", "tablename", "rolname")
_NAME_REF = re.compile(r"(?<![\w.'])(?:\w+\.)?(?:%s)\b(?!\s*::\s*text)" % "|".join(NAME_COLS), re.I)
_TEXT_FUNCS = re.compile(r"\b(coalesce|concat)\s*\(", re.I)


def _no_strings(sql: str) -> str:
    """单引号字面量抹成 '' —— 字面量里的词不算。"""
    return re.sub(r"'(?:[^']|'')*'", "''", _code(sql))


def _paren_body(sql: str, open_at: int) -> str:
    depth = 0
    for j in range(open_at, len(sql)):
        if sql[j] == "(":
            depth += 1
        elif sql[j] == ")":
            depth -= 1
            if depth == 0:
                return sql[open_at + 1:j]
    return sql[open_at + 1:]


def m_problems(sql: str):
    """一段 SQL 在 M 兼容业务库上的已知问题(守卫本体,下面的「守卫能红」也用它)。"""
    code = _no_strings(sql)
    out = []
    if M_ILIKE.search(code):
        out.append("ILIKE(M 库不支持,改 lower(…) LIKE lower(…))")
    m = M_RESERVED_ALIAS.search(code)
    if m:
        out.append("AS %s(M 库保留字,改 AS \"%s\")" % (m.group(1), m.group(1)))
    for q in re.finditer(r'"[^"]*"', code):
        if not re.search(r"\bAS\s*$", code[:q.start()], re.I):
            out.append("表达式里引用 %s(M 库双引号是字符串,只许作最外层别名)" % q.group(0))
    if M_ARRAY_LITERAL.search(code):
        out.append("ARRAY[…] / ::类型[] 字面量")
    for c in re.finditer(r"\bCAST\s*\(", code, re.I):
        body = _paren_body(code, code.index("(", c.start()))
        tgt = re.findall(r"\bAS\s+([A-Za-z][A-Za-z0-9_]*)", body, re.I)
        if tgt and tgt[-1].lower() not in M_CAST_TARGETS:
            out.append("CAST(… AS %s)(M 库 CAST 不认这个类型)" % tgt[-1])
    for f in _TEXT_FUNCS.finditer(code):
        body = _paren_body(code, code.index("(", f.start()))
        n = _NAME_REF.search(body)
        if n:
            out.append("%s(…%s…) 里名字列没有 ::text" % (f.group(1), n.group(0)))
    if re.search(r"\badm_\w+", code, re.I):
        out.append("ADM_* 视图(M 库没有)")
    return out


_ADM_ONLY = {"procinfo.source_adm", "proctune.source_adm"}


@pytest.mark.parametrize("rec", _BUSINESS, ids=lambda r: r.script_name)
def test_business_db_scripts_run_on_m_compat(rec):
    probs = m_problems(rec.script_content)
    if rec.script_name in _ADM_ONLY:
        probs = [p for p in probs if not p.startswith("ADM_*")]
    assert not probs, "%s 在业务库执行,M 兼容库上有问题:%s" % (rec.script_name, ";".join(probs))


def test_adm_source_is_skipped_on_m_by_the_caller():
    """source_adm 两条没有 M 库的对等写法:调用方(common/proc_source.py)在 M 库不发这条脚本。"""
    src = (_ROOT / "common" / "proc_source.py").read_text(encoding="utf-8")
    body = src[src.index("def _query(runner"):]
    body = body[:body.index("\ndef ", 10)]
    assert "compat.is_mysql_like(runner)" in body and "raise Unavailable" in body


def test_the_photo_scripts_keep_their_columns():
    """现场照片里的脚本改写后输出列名不变(调用方按列名取值)。"""
    want = {
        "health.partitions": ("schema", "table", "parts", "strategy"),
        "health.table_writes": ("schema", "table", "is_toast", "n_tup_ins", "n_tup_upd", "n_tup_del", "toast_bytes"),
        "sqlreview.tables": ("schema", "table", "has_pk", "fks", "columns"),
        "sqlreview.indexes": ("schema", "table", "name", "columns", "is_unique", "is_primary", "scans"),
        "procinfo.locate_search": ("kind", "nspname", "pkgname", "proname"),
        "proctune.locate_search": ("kind", "nspname", "pkgname", "proname"),
        "login.current_db": ("db", "usr", "compat", "lctn"),
    }
    for name, cols in want.items():
        sql = _code(_REG.find(name).script_content)
        for col in cols:
            assert re.search(r"\bAS\s+\"?%s\"?\s*(,|;|$|\n)" % col, sql, re.I | re.M) or \
                re.search(r"\b%s\b" % col, sql.split("FROM")[0]), (name, col)


def test_the_m_guard_itself_can_fail():
    """守卫要能红:现场照片里 1.4.14 / 1.5.0 的写法都得认出来,修好的写法不误报。"""
    assert m_problems("SELECT 1 FROM pg_proc p WHERE p.proname ILIKE '%x%'")
    assert m_problems("SELECT c.relname::text AS table FROM pg_class c")
    assert m_problems('SELECT schema, "table" FROM (SELECT 1 AS "table") t')
    assert m_problems("SELECT array_to_string(COALESCE(x, ARRAY[]::text[]), ',')")
    assert m_problems("SELECT CAST(abs(a - b) AS float8) / b")
    assert m_problems("SELECT COALESCE(pn.nspname, s.schemaname) AS schema")
    assert m_problems("SELECT concat(n.nspname, '.', c.relname)")
    assert m_problems("SELECT * FROM pg_catalog.adm_source")
    assert not m_problems('SELECT c.relname AS "table", concat(n.nspname::text, \'.\', c.relname::text) AS x')
    assert not m_problems("SELECT lower(p.proname::text) LIKE lower('%a ilike b%') -- ILIKE 注释里不算")
    assert not m_problems("SELECT COALESCE(t.n_live_tup, 0), CAST(x AS DECIMAL)")
