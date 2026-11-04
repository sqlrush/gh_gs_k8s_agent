"""排行里排除内核系统 SQL(common/syssql.py)。

用例里的语句大多是 2026-10-22 在 og5 的 dbe_perf.statement / statement_history 里实际取到的:
内核 WDR 快照线程(WDRXdb)、我方智能体查系统视图、管理员 gsql 手工查询,以及 faultlab 的业务语句。
方向:**拿不准一律保留** —— 误排除会把行方真正的慢 SQL 藏起来。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import syssql  # noqa: E402
from common.syssql import SYSTEM, UNSURE, USER, classify  # noqa: E402


@pytest.mark.parametrize("sql", [
    # 内核 WDR 快照线程
    "select ?, ?, t.* from dbe_perf.summary_statio_all_indexes t",
    # 我方智能体 / 诊断工具
    "SELECT schema_name, to_char(start_time, ?) AS start_time, round(db_time / ?, ?) AS db_time_ms, "
    "n_returned_rows, query_plan FROM dbe_perf.statement_history WHERE unique_query_id = ?",
    "select count(*) from statement_history;",
    "SELECT generate_wdr_report(?, ?, ?, ?, ?) AS report_line;",
    "SELECT n.nspname, c.relname, pg_relation_size(c.oid) / current_setting(?)::bigint AS curpages "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace",
    # 带 WITH 的系统查询:e / b 是 WITH 的名字,不是表
    "WITH b AS (SELECT snap_unique_sql_id AS sid FROM snapshot.snap_summary_statement WHERE snapshot_id = ?), "
    "e AS (SELECT snap_unique_sql_id AS sid FROM snapshot.snap_summary_statement WHERE snapshot_id = ?) "
    "SELECT e.sid FROM e LEFT JOIN b USING (sid) ORDER BY 1 LIMIT ?;",
    # extract(epoch from now()) 里的 now 不是表
    "SELECT pid, extract(epoch from now() - xact_start) AS xact_s FROM pg_stat_activity WHERE state <> ?",
    "SELECT * FROM create_wdr_snapshot()",
    "SELECT * FROM dbe_perf.get_global_full_sql_by_timestamp(?, ?)",
    "/* 智能体 */ SELECT count(*) FROM gs_asp WHERE sample_time > ?",
    # 内核 WDR 自己的维护语句(og5 慢 SQL 榜上实测),对象不在 FROM 后面
    "analyze snapshot.tables_snap_timestamp ;",
    "VACUUM (VERBOSE) ANALYZE pg_catalog.pg_statistic",
])
def test_kernel_and_tool_queries_on_system_objects_are_system(sql):
    assert classify(sql) == SYSTEM


@pytest.mark.parametrize("sql", [
    "UPDATE fl.orders SET status = ? WHERE id = ?",
    "SELECT o.id FROM pg_class c JOIN fl.orders o ON o.relid = c.oid",       # 系统表 + 业务表
    "SELECT pg_sleep(?)",                                                    # 业务在等,不是内核在干活
    "SELECT fl.calc_bonus(id) FROM pg_class",                               # 带 schema 的业务函数
    "SELECT round(?::numeric, ?)",                                           # 只调内置函数:不是内核的
    "SELECT * FROM my_report(?)",                                            # 业务的集合返回函数
    "SELECT * FROM orders WHERE note = 'copied from pg_class'",              # 常量里的 from 不算
    "WITH t AS (SELECT * FROM pg_class) SELECT * FROM t JOIN cbst.acct a ON a.id = t.oid",
    # 2026-11-04 收紧之后仍然是业务 SQL 的:宁可多列一行,也不把行方的语句藏掉
    "SELECT nextval(?)",                                                     # 取业务序列
    "SELECT calc_bonus(?)",                                                  # 不带 schema 的业务函数
    "begin insert into fl.t values (?); end;",                               # 匿名块,里面是业务逻辑
    "SELECT o.order_id, o.customer_id, o.amount, o.status, o.created_at",    # FROM 被截掉的业务语句
    "SELECT a, b", "SELECT *",
    "SELECT * FROM dict WHERE type = ?",                                     # 业务系统常把代码表叫 dict
    "INSERT INTO fl.t (a, b) VALUES (?, ?)",
    "CREATE TABLE fl.t (a int)",                                             # 业务对象的 DDL
])
def test_anything_touching_user_objects_or_not_clearly_kernel_is_kept(sql):
    assert classify(sql) == USER


@pytest.mark.parametrize("sql,kind", [
    # 会话设置、事务控制与连接探活:连接池与驱动每次建连都发,调用上千次,会把业务 SQL 挤出榜
    ("SELECT ?", syssql.KIND_SESSION), ("select 1 as ok", syssql.KIND_SESSION),
    ("select version()", syssql.KIND_SESSION), ("select current_schema(), current_user", syssql.KIND_SESSION),
    ("select now()::text", syssql.KIND_SESSION),
    ("SET client_encoding to ?", syssql.KIND_SESSION), ("SET statement_timeout = ?", syssql.KIND_SESSION),
    ("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY", syssql.KIND_SESSION),
    ("SHOW search_path", syssql.KIND_SESSION), ("RESET ALL", syssql.KIND_SESSION),
    ("COMMIT", syssql.KIND_SESSION), ("BEGIN", syssql.KIND_SESSION), ("rollback", syssql.KIND_SESSION),
    ("START TRANSACTION ISOLATION LEVEL READ COMMITTED", syssql.KIND_SESSION),
    # 运维管理
    ("CREATE DATABASE release_wl_e2e", syssql.KIND_ADMIN), ("DROP DATABASE x", syssql.KIND_ADMIN),
    ("CREATE USER wl_reader PASSWORD ?", syssql.KIND_ADMIN), ("GRANT SELECT ON fl.t TO r", syssql.KIND_ADMIN),
    ("ANALYZE fl.orders", syssql.KIND_ADMIN), ("vacuum full cbst.acct, pg_class", syssql.KIND_ADMIN),
    ("ANALYZE;", syssql.KIND_ADMIN), ("CHECKPOINT", syssql.KIND_ADMIN),
    ("EXPLAIN SELECT * FROM fl.orders WHERE id = ?", syssql.KIND_ADMIN),        # explain / sqltune 技能发的
    # 数据字典:A 兼容视图没有 pg_ / gs_ 前缀(名单来自官方文档 common/sysdict.py)
    ("SELECT * FROM adm_tables WHERE owner = ?", syssql.KIND_KERNEL),
    ("select text from adm_source where name = ?", syssql.KIND_KERNEL),
    ("SELECT * FROM db_objects", syssql.KIND_KERNEL), ("select 1 from dual", syssql.KIND_KERNEL),
    # 中间件自身(grmp:我方交付的白名单与配置表)
    ("INSERT INTO grmp.script_config (a) VALUES (?)", syssql.KIND_TOOL),
    ("SELECT script_name, md5(script_content) FROM grmp.script_config", syssql.KIND_TOOL),
    ("CREATE TABLE grmp.script_config (a int)", syssql.KIND_TOOL),
    # 2026-11-04 演练实测上榜的两条
    ("CREATE SCHEMA grmp;", syssql.KIND_ADMIN), ("DROP SCHEMA IF EXISTS app CASCADE", syssql.KIND_ADMIN),
    ("CREATE TEMP TABLE t_probe (x int)", syssql.KIND_TOOL),      # explain.multi_stmt_probe 的探测表
    ("insert into t_probe values (?)", syssql.KIND_TOOL),
])
def test_non_business_statements_are_excluded_by_kind(sql, kind):
    assert syssql.judge(sql) == (SYSTEM, kind)


def test_dictionary_list_comes_from_the_official_docs():
    from common import sysdict
    assert len(sysdict.NAMES) > 400 and {"adm_tables", "db_objects", "my_tables", "pg_class", "gs_asp"} <= sysdict.NAMES
    assert "官方文档集" in sysdict.__doc__


def test_cut_text_never_concludes_system():
    """截断的正文里只看到系统对象 —— FROM 可能在截掉的那一半里,判不出来。"""
    head = "SELECT a.x, b.y FROM dbe_perf.statement a JOIN "
    assert classify(head, complete=False) == UNSURE
    assert classify("UPDATE fl.orders SET status = ? WHERE id IN (SELECT", complete=False) == USER


@pytest.mark.parametrize("sql,cut", [
    ("SELECT * FROM pg_class WHERE relname IN (SELECT", True),
    ("SELECT * FROM pg_class WHERE", True),
    ("SELECT * FROM pg_class WHERE relname = 'x", True),
    ("SELECT * FROM pg_class WHERE relname = ?;", False),
    ("SELECT count(*) FROM pg_class", False),
    ("SELECT * FROM pg_class ORDER BY relname DESC LIMIT ?", False),     # 归一化语句的常见结尾
    ("SELECT * FROM pg_class LIMIT $1", False),
    ("SELECT * FROM pg_class LIMIT 10", False),
    ("SELECT * FROM pg_class ORDER BY", True),
])
def test_looks_cut(sql, cut):
    assert syssql.looks_cut(sql) is cut


class _Row:
    def __init__(self, sid, q, sec=1.0):
        self.sid, self.q, self.sec = sid, q, sec


def _screen(rows, **kw):
    return syssql.screen(rows, sql_id=lambda r: r.sid, text=lambda r: r.q, seconds=lambda r: r.sec, **kw)


def test_screen_keeps_rank_order_and_fills_up_to_want():
    rows = [_Row("1", "select ?, ?, t.* from dbe_perf.summary_stat_all_tables t", 9.0),
            _Row("2", "UPDATE fl.a SET x = ?"),
            _Row("3", "select count(*) from statement_history", 2.5),
            _Row("4", "SELECT * FROM fl.b"),
            _Row("5", "SELECT * FROM fl.c")]
    s = _screen(rows, want=2)
    assert [r.sid for r in s.kept] == ["2", "4"]
    assert s.excluded_ids == ["1", "3"] and s.excluded_seconds == pytest.approx(11.5)
    assert "已排除 2 条非业务 SQL(内核与数据字典 2 条)" in s.note() and "11.5 秒" in s.note() and "sql_id 1、3" in s.note()
    assert "dbe_perf" not in s.note() and "select" not in s.note().lower(), "说明里只说条数与类别,不列正文"
    assert s.as_dict()["excluded"] == ["1", "3"]


def test_screen_does_not_report_system_rows_beyond_the_ranking_it_needed():
    rows = [_Row("1", "SELECT * FROM fl.a"), _Row("2", "SELECT * FROM fl.b"),
            _Row("3", "select count(*) from pg_class")]
    s = _screen(rows, want=2)
    assert [r.sid for r in s.kept] == ["1", "2"] and s.excluded_ids == [] and s.note() == ""


def test_cut_rows_are_resolved_with_the_full_text():
    cut = "SELECT a.x, b.y FROM dbe_perf.statement a JOIN dbe_pe"
    whole = {"7": "SELECT a.x, b.y FROM dbe_perf.statement a JOIN dbe_perf.statement_history b ON a.x = b.x",
             "8": "SELECT a.x, b.y FROM dbe_perf.statement a JOIN cbst.acct b ON a.x = b.x"}
    asked = []

    def full(sid):
        asked.append(sid)
        return whole.get(sid)

    s = _screen([_Row("7", cut), _Row("8", cut)], shown_chars=len(cut), full_text=full)
    assert s.excluded_ids == ["7"] and [r.sid for r in s.kept] == ["8"] and asked == ["7", "8"]


def test_cut_rows_without_full_text_are_dropped_when_only_system_objects_show_and_said_so():
    """补不到全文:看得到的只有系统对象 → 按非业务排除(现场要求宁可不列);一个表都看不到 → 保留。都在说明里写明。"""
    cut = "SELECT a.x FROM dbe_perf.statement a JOIN "
    blind = "SELECT o.order_id, o.customer_id, o.amount, o.status, o."
    s = _screen([_Row("9", cut), _Row("10", blind)], shown_chars=min(len(cut), len(blind)), full_text=lambda sid: None)
    assert s.excluded_ids == ["9"] and s.guessed == 1 and [r.sid for r in s.kept] == ["10"] and s.unresolved == 1
    assert "看得到的部分只有系统对象,按非业务排除" in s.note() and "按业务 SQL 保留" in s.note()


def test_lookups_are_capped():
    cut = "SELECT a.x FROM dbe_perf.statement a JOIN "
    calls = []
    s = _screen([_Row(str(i), cut) for i in range(30)], shown_chars=len(cut),
                full_text=lambda sid: calls.append(sid) or None, max_lookups=3)
    assert len(calls) == 3 and s.guessed == 30 and s.unresolved == 0


def test_short_text_under_the_cut_is_judged_without_a_lookup():
    s = _screen([_Row("1", "select count(*) from pg_class")], shown_chars=80,
                full_text=lambda sid: pytest.fail("不该补取"))
    assert s.excluded_ids == ["1"]


class _Runner:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def run(self, script, params):
        self.calls.append((script, params))
        a = self.answers.get(script)
        if isinstance(a, Exception):
            raise a
        return a or []


def test_full_text_lookup_falls_back_to_history_and_caches():
    r = _Runner({"sqlfetch.from_statement": RuntimeError("未注册脚本"),
                 "sqlfetch.from_history": [{"schema_name": "fl", "query": "select 1 from pg_class"}]})
    get = syssql.full_text_lookup(r)
    assert get("123") == "select 1 from pg_class"
    assert get("123") == "select 1 from pg_class"
    assert [c[0] for c in r.calls] == ["sqlfetch.from_statement", "sqlfetch.from_history"]
    assert r.calls[0][1] == {"sid": 123}
    assert get("not-a-number") is None


def test_all_excluded_is_told_apart_from_an_empty_ranking():
    assert _screen([_Row("1", "select count(*) from pg_class")], want=3).all_excluded()
    assert not _screen([], want=3).all_excluded()
    assert not _screen([_Row("1", "select * from fl.a")], want=3).all_excluded()


def test_all_excluded_text_says_whether_the_fetch_was_capped():
    s = _screen([_Row(str(i), "select count(*) from pg_class") for i in range(3)], want=3)
    assert s.all_excluded_text(10, "按耗时排的") == "按耗时排的 3 条全是非业务 SQL(内核、数据字典、会话设置、运维管理等),没有业务语句"
    assert "再往后的没取" in s.all_excluded_text(3, "按耗时排的")
    assert "include-system" not in s.all_excluded_text(3, "按耗时排的"), "不引导用户去看系统 SQL"


def test_keep_all_is_a_no_op():
    s = syssql.keep_all([1, 2])
    assert s.kept == [1, 2] and s.note() == ""


def test_sqltune_uses_the_same_system_object_list():
    sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))
    import systables
    assert systables._is_system_object is syssql.is_system_relation


def test_rows_without_text_are_looked_up_not_kept_blindly():
    """topsql 的老基线只存数字、正文为空 —— 空正文不能当成「SELECT ?」直接保留。"""
    s = _screen([_Row("5", "")], shown_chars=80, full_text=lambda sid: "select count(*) from pg_class")
    assert s.excluded_ids == ["5"]


def test_brief_is_short_enough_for_a_headline():
    rows = [_Row(str(i), "select count(*) from pg_class", 1.0) for i in range(6)] + [_Row("x", "SELECT * FROM fl.a")]
    b = _screen(rows, want=1).brief()
    assert b.startswith("已排除 6 条非业务 SQL(0、1、2等") and "6.0 秒" in b


def test_verdicts_from_full_text_are_cached_and_reused(tmp_path):
    """sql_id 是归一化语句的哈希:判过一次,下次不再按 sql_id 补全文(og5 上第一次要补几十次)。"""
    cut = "SELECT a.x FROM dbe_perf.statement a JOIN "
    path = tmp_path / "v.json"
    asked = []

    def full(sid):
        asked.append(sid)
        return {"1": cut + "dbe_perf.statement_history b ON a.x = b.x",
                "2": cut + "cbst.acct b ON a.x = b.x"}[sid]

    first = _screen([_Row("1", cut), _Row("2", cut)], shown_chars=len(cut), full_text=full,
                    cache=syssql.VerdictCache(path))
    again = _screen([_Row("1", cut), _Row("2", cut)], shown_chars=len(cut), full_text=full,
                    cache=syssql.VerdictCache(path))
    assert first.excluded_ids == again.excluded_ids == ["1"] and asked == ["1", "2"]


def test_a_broken_or_unwritable_cache_only_costs_lookups(tmp_path):
    bad = tmp_path / "v.json"
    bad.write_text("{not json", encoding="utf-8")
    c = syssql.VerdictCache(bad)
    assert c.get("1") is None
    c.put("1", syssql.SYSTEM)
    c.save()
    assert syssql.VerdictCache(bad).get("1") == syssql.SYSTEM
    ro = syssql.VerdictCache(tmp_path / "file-not-dir" / "v.json")
    (tmp_path / "file-not-dir").write_text("x", encoding="utf-8")
    ro.put("2", syssql.USER)
    ro.save()                                          # 写不了也不抛
    assert ro.get("2") == syssql.USER


def test_cache_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(syssql, "CACHE_MAX", 3)
    c = syssql.VerdictCache(tmp_path / "v.json")
    for i in range(5):
        c.put(str(i), syssql.USER)
    c.save()
    again = syssql.VerdictCache(tmp_path / "v.json")
    assert [again.get(str(i)) for i in range(5)] == [None, None, "user", "user", "user"]
