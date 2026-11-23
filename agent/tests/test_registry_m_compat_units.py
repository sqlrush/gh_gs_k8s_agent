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
