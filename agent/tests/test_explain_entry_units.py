"""explain 入口的收尾路径 —— 走模板时 db 是 None，别拿它调方法。

**这条是回归测试。** e145e14 加了一段语句形态校验，但把它放在取到计划之后，
且无条件调用 `db.set_statement_timeout(...)` / `db.close()`。走 EXPLAIN 模板
（也就是**常规路径**）时 db 一直是 None，于是：

    AttributeError: 'NoneType' object has no attribute 'set_statement_timeout'

1111 条单测一条都没红 —— 因为没有测试覆盖 main() 的这段收尾。是通过 opencode
实跑 explain 才炸出来的。这里补上。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-explain" / "scripts"
# 本仓库里 `render` / `audit` / `baseline` 这些模块名在多个 skill 下都存在,
# 而 pytest 一个进程跑完全部测试 —— 先运行的测试文件会把自己那份留在
# sys.modules 里。不先清掉,这里导入的模块内部 `import render` 就会拿到别人的
# (2026-10-18 新增 gaussdb-config 时实际撞上过:本文件单独跑全过、
# 与 test_config_audit_units.py 一起跑全挂)。
for _m in ("render", "audit", "baseline", "model", "rules", "thresholds",
           "util", "report"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))


def _load():
    spec = importlib.util.spec_from_file_location(
        "explain_entry_for_test", _SCRIPTS / "explain.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


explain_mod = _load()


class _Runner:
    """模板路径的 runner：返回一行计划文本。"""

    def run(self, script, values=None):
        return [{"QUERY PLAN": "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)"}]


def test_template_path_does_not_touch_the_none_db(monkeypatch, capsys):
    """模板路径下 db 全程为 None —— 收尾不能调它的任何方法。

    崩的就是这条路径，而它是**最常走的那条**（能进 EXPLAIN 模板的只读单语句）。
    """
    monkeypatch.setattr(explain_mod.access, "for_conn",
                        lambda *a, **k: _Runner())

    def _boom(*a, **k):
        raise AssertionError("模板路径不该去建原始连接")

    monkeypatch.setattr(explain_mod.access, "connection_for", _boom)

    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT 1"))
    assert explain_mod.main(["--sql-stdin"]) == 0, capsys.readouterr().err


@pytest.mark.parametrize("sql,expect", [
    ("INSERT INTO t VALUES (1)", 1),
    ("CREATE TABLE t(i int)", 1),
    ("SELECT 1; SELECT 2;", 1),
    # --- 以下每条都是实测绕过，不是补齐用的等价变形 ---
    # 前导注释让 DML 蒙混过关：og5 上 gsql 与 psycopg2 两条直连都真写了库
    ("/* c */ UPDATE t SET a = 1", 1),
    ("-- c\nUPDATE t SET a = 1", 1),
    ("/* c */ DELETE FROM t", 1),
    # 恰好一个分号的两条语句：原先数分号 `> 1`，这条漏了过去
    ("SELECT 1; SELECT pg_backend_pid()", 1),
    ("SELECT 1; GRANT ALL ON t TO public", 1),
    # 白名单挡住黑名单漏掉的那些
    ("COPY (SELECT 1) TO PROGRAM 'true'", 1),
    ("VACUUM t", 1),
    ("GRANT ALL ON t TO public", 1),
    ("-- 只有注释", 1),
])
def test_shape_checks_run_before_connecting(monkeypatch, sql, expect):
    """DML/非只读/多语句是**纯文本检查**，必须在连库之前就拒。

    原先放在取到计划之后：白跑一次 EXPLAIN、白建一次连接，而且拒绝理由与
    「已经拿到计划」同时出现，读起来自相矛盾。
    """
    def _boom(*a, **k):
        raise AssertionError("形态不合法时不该去建连接或取数")

    monkeypatch.setattr(explain_mod.access, "for_conn", _boom)
    monkeypatch.setattr(explain_mod.access, "connection_for", _boom)

    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(sql))
    assert explain_mod.main(["--sql-stdin"]) == expect


@pytest.mark.parametrize("sql", [
    "SELECT 'drop' AS x",
    "SELECT relname FROM pg_class WHERE relname = 'alter'",
    "SELECT comment FROM (SELECT 1 AS comment) t",
    "SELECT 1 AS x -- a;b;c",
    "SELECT 1 /* ; */ AS x",
    "SELECT 1; -- 收尾注释",
    "SELECT relname FROM t WHERE relname LIKE '%status%'",
])
def test_ordinary_queries_are_not_falsely_rejected(monkeypatch, sql):
    """**误拒也是 bug。**

    原先的 DDL 判定不带锚点、扫整串原文：任何叫 comment 的列、任何值是
    'drop' 的过滤条件都被拒 —— 而 comment 是真实业务表里极常见的列名。
    注释里的分号则让合法单语句在中间件模式下被判成多语句。
    """
    monkeypatch.setattr(explain_mod.access, "for_conn",
                        lambda *a, **k: _Runner())
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(sql))
    assert explain_mod.main(["--sql-stdin"]) == 0


def test_analyze_never_gets_a_writable_raw_session(monkeypatch):
    """**explain 不再建原始连接 —— 中间件与直连同一条路。**

    原先 `--analyze` 走回落时拿的是 read_only=False 的原始会话，用户 SQL
    不经 EXPLAIN 包裹直接下发（实测 default_transaction_read_only = off）。
    那条旁路是 `/* c */ UPDATE ...` 能写库的载体，已删除。
    """
    def _boom(*a, **k):
        raise AssertionError("explain 不该再去建原始连接")

    monkeypatch.setattr(explain_mod.access, "connection_for", _boom)
    monkeypatch.setattr(explain_mod.access, "for_conn",
                        lambda *a, **k: _Runner())
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT 1"))
    assert explain_mod.main(["--sql-stdin", "--analyze"]) == 0


def test_semicolon_inside_a_literal_is_not_a_second_statement(monkeypatch):
    """'a;b' 里的分号是数据不是语句分隔 —— 不能把它判成多语句。"""
    monkeypatch.setattr(explain_mod.access, "for_conn",
                        lambda *a, **k: _Runner())
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT 'a;b'"))
    assert explain_mod.main(["--sql-stdin"]) == 0


# --- --pid:有 gs_get_explain 走运行态计划,没有就取该会话的 SQL 退回 EXPLAIN --------------
#
# 现场(2026-09)脚本直接调 gs_get_explain 报「不存在」。我们的做法是先探测:GaussDB 有它就用,
# openGauss / 缺失就退回,两条路径同一份报告形状,只是注明来源;GaussDB 该有而没有时给中文说明。

from common import kernel_funcs as kf  # noqa: E402

GAUSS_VER = "(GaussDB Kernel V500R002C10 build 1) compiled at 2025"
OG_VER = "(openGauss-lite 5.0.3 build 89d144c2) compiled at 2024-07-31"


class _KernelRunner:
    """按脚本名分发的 runner:probe 行 / 运行态计划 / 会话查询 / EXPLAIN 模板。"""

    def __init__(self, version, explain_args=None, plan="Runtime: Index Scan on t", session_query="SELECT 1"):
        self.version, self.explain_args, self.plan, self.session_query = version, explain_args, plan, session_query
        self.calls = []

    def run(self, script, values=None):
        self.calls.append(script)
        if script == kf.PROBE_SCRIPT:
            rows = [{"item": "version", "detail": self.version}]
            if self.explain_args is not None:
                rows.append({"item": "func:gs_get_explain", "detail": self.explain_args})
            return rows
        if script in (kf.RUNTIME_PLAN_SCRIPT, kf.RUNTIME_PLAN_INT4_SCRIPT):
            return [{"plan": self.plan}]
        if script == kf.SESSION_BY_PID_SCRIPT:
            return [{"pid": str(values["pid"]), "query": self.session_query, "query_start": "", "state": "active"}]
        return [{"QUERY PLAN": "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)"}]


def test_pid_uses_runtime_plan_when_the_kernel_has_gs_get_explain(monkeypatch, capsys):
    r = _KernelRunner(GAUSS_VER, explain_args="integer")
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    assert explain_mod.main(["--pid", "4321"]) == 0
    out = capsys.readouterr().out
    assert "Runtime: Index Scan on t" in out
    assert "gs_get_explain" in out and "4321" in out and "运行态" in out       # 报告注明来源
    assert kf.RUNTIME_PLAN_INT4_SCRIPT in r.calls and "explain.plan_text" not in r.calls   # integer 签名走 int4 脚本


def test_pid_falls_back_to_explain_when_function_is_absent(monkeypatch, capsys):
    """openGauss:探到没有 → 取该 pid 的 SQL 走 EXPLAIN 模板,报告注明是估算计划。"""
    r = _KernelRunner(OG_VER, explain_args=None, session_query="SELECT count(*) FROM t")
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    assert explain_mod.main(["--pid", "4321"]) == 0
    out = capsys.readouterr().out
    assert "Seq Scan on t" in out and "SELECT count(*) FROM t" in out
    assert "估算" in out and "EXPLAIN" in out
    assert kf.SESSION_BY_PID_SCRIPT in r.calls and "explain.plan_text" in r.calls
    assert "应包含" not in out                                             # openGauss 不该说「该有而没有」


def test_pid_on_gaussdb_missing_function_explains_in_chinese_then_falls_back(monkeypatch, capsys):
    r = _KernelRunner(GAUSS_VER, explain_args=None)
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    assert explain_mod.main(["--pid", "4321"]) == 0
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "GaussDB" in out and "gs_get_explain" in out and "pg_proc" in out and "升级" in out
    assert "Seq Scan on t" in out                                          # 仍然给出了估算计划


def test_pid_with_no_such_session_is_a_clear_error(monkeypatch, capsys):
    class _Empty(_KernelRunner):
        def run(self, script, values=None):
            if script == kf.SESSION_BY_PID_SCRIPT:
                return []
            return super().run(script, values)
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: _Empty(OG_VER))
    assert explain_mod.main(["--pid", "99"]) == 2
    err = capsys.readouterr().err
    assert "99" in err and "会话" in err


def test_pid_runtime_plan_empty_reports_the_documented_preconditions(monkeypatch, capsys):
    """函数在、返回却为空:按文档前提说明(track_activities / plan_collect_thresh),再退回 EXPLAIN。"""
    r = _KernelRunner(GAUSS_VER, explain_args="integer", plan="")
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    assert explain_mod.main(["--pid", "4321"]) == 0
    out = capsys.readouterr().out
    assert "plan_collect_thresh" in out and "track_activities" in out and "Seq Scan on t" in out


def test_pid_and_sql_stdin_are_mutually_exclusive(monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT 1"))
    with pytest.raises(SystemExit):
        explain_mod.main(["--sql-stdin", "--pid", "1"])


class _FailingRunner:
    """模拟 SQL 到数据库那里执行失败（打错字、表不存在）。"""

    def __init__(self, message):
        self._message = message

    def run(self, script, values=None):
        from common.grmp.errors import QueryError
        raise QueryError(self._message)


@pytest.mark.parametrize("message", [
    'ERROR: syntax error at or near "SELEKT" (SQLSTATE 42601)',
    'ERROR: relation "nosuchtable" does not exist (SQLSTATE 42P01)',
])
def test_sql_execution_failure_is_reported_not_thrown(monkeypatch, capsys,
                                                      message):
    """**回归测试。**

    主 except 原先漏了 access.QueryError —— 它是本项目归一化的「取数失败」
    类型，runner.run() 在 SQL 本身执行失败时抛的就是它。漏掉的后果不是少一条
    错误信息，而是**直接吐 Traceback**：用户粘一条有 typo 的 SQL，看到的是
    Python 栈而不是「syntax error at or near "SELEKT"」。

    而这是最常见的用户路径之一 —— 全量矩阵就是在这里抓到它的。
    """
    monkeypatch.setattr(explain_mod.access, "for_conn",
                        lambda *a, **k: _FailingRunner(message))

    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT 1"))
    rc = explain_mod.main(["--sql-stdin"])

    assert rc != 0, "SQL 执行失败必须以非 0 退出"
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "SQLSTATE" in err, "要把数据库的原话带给用户，而不是包装成通用失败"


class _PlanRunner:
    """只回一段固定计划文本的 runner:用来看 --analyze 拿到估算计划时报告怎么写。"""

    def __init__(self, plan):
        self.plan = plan

    def run(self, script, values=None):
        return [{"QUERY PLAN": self.plan}]


def test_analyze_requested_but_plan_is_an_estimate_is_said_out_loud(monkeypatch, capsys):
    """现场脚本按客户只读要求把 ANALYZE 固定关闭:用户要了 --analyze,拿到的仍是估算计划。
    报告不能沉默——来源行与说明都要写明「这是估算」,JSON 里 analyzed 必须是 false。"""
    monkeypatch.setattr(explain_mod.access, "for_conn",
                        lambda *a, **k: _PlanRunner("Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)"))
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT 1"))
    assert explain_mod.main(["--sql-stdin", "--analyze"]) == 0
    out = capsys.readouterr().out
    assert "估算计划" in out and "ANALYZE" in out and "只读" in out

    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT 1"))
    assert explain_mod.main(["--sql-stdin", "--analyze", "--format", "json"]) == 0
    import json
    data = json.loads(capsys.readouterr().out)
    assert data["analyzed"] is False and "估算" in data["source"]


def test_analyze_that_really_ran_is_not_second_guessed(monkeypatch, capsys):
    monkeypatch.setattr(explain_mod.access, "for_conn",
                        lambda *a, **k: _PlanRunner(
                            "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4) (actual time=0.01..0.02 rows=1 loops=1)"))
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT 1"))
    assert explain_mod.main(["--sql-stdin", "--analyze", "--format", "json"]) == 0
    import json
    data = json.loads(capsys.readouterr().out)
    assert data["analyzed"] is True and "估算" not in data["source"] and not data["notes"]


def test_pid_bigint_signature_uses_runtime_plan_for_a_64bit_thread_id(monkeypatch, capsys):
    """505.2.1.SPC0600 实测 gs_get_explain(bigint):64 位线程号直接走运行态计划,不再被 integer 范围检查拦下。"""
    r = _KernelRunner(GAUSS_VER, explain_args="bigint")
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    assert explain_mod.main(["--pid", "281440978523808"]) == 0
    out = capsys.readouterr().out
    assert "Runtime: Index Scan on t" in out and "运行态" in out and "281440978523808" in out
    assert kf.RUNTIME_PLAN_SCRIPT in r.calls and kf.RUNTIME_PLAN_INT4_SCRIPT not in r.calls
    assert "integer 范围" not in out and "来源:EXPLAIN 估算计划" not in out


class _SchemaPlanRunner(_PlanRunner):
    def __init__(self, plan):
        super().__init__(plan)
        self.calls = []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if script == "explain.multi_stmt_probe":
            return [{"ok": "1"}]
        return super().run(script, values)


def test_schema_option_switches_search_path_and_says_so(monkeypatch, capsys):
    """用户给了 --schema:走两语句模板,来源行写明 search_path 切到了哪里。"""
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _SchemaPlanRunner("Seq Scan on orders  (cost=0.00..1.00 rows=1 width=4)")
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("select * from orders"))
    assert explain_mod.main(["--sql-stdin", "--schema", "app_trade"]) == 0
    out = capsys.readouterr().out
    assert ("explain.plan_text_schema", {"sql": "select * from orders", "schema": "app_trade"}) in r.calls
    assert "search_path" in out and "app_trade" in out


def test_schema_option_rejects_a_non_identifier_cleanly(monkeypatch, capsys):
    r = _SchemaPlanRunner("Seq Scan on orders  (cost=0.00..1.00 rows=1 width=4)")
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("select 1"))
    assert explain_mod.main(["--sql-stdin", "--schema", "app;drop"]) == 2
    err = capsys.readouterr().err
    assert "schema" in err and "Traceback" not in err


# ---------------------------------------------------------------- --sql-id(客户 09-09 早截图:贴文本那条路 schema 会丢)

class _IdRunner(_Runner):
    """from_history 给出 SQL 与它当初执行的 schema;探测脚本说两条语句不行;单语句模板记下收到的 SQL。"""

    def __init__(self):
        self.calls = []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if script == "explain.from_history":
            return [{"schema_name": "gmag", "query": "SELECT COUNT(1) FROM batch_job_status WHERE work_status IN (1, 2, 3)"}]
        if script == "explain.multi_stmt_probe":
            return []
        return super().run(script, values)


def test_sql_id_fetches_text_and_carries_the_recorded_schema(monkeypatch, capsys):
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _IdRunner()
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    assert explain_mod.main(["--sql-id", "1568852919"]) == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert ("explain.from_history", {"sid": 1568852919}) in r.calls
    plan_calls = [c for c in r.calls if c[0] == "explain.plan_text"]
    assert plan_calls and plan_calls[0][1]["sql"] == "SELECT COUNT(1) FROM gmag.batch_job_status WHERE work_status IN (1, 2, 3)"
    assert "1568852919" in out and "gmag" in out and "补全" in out and "Seq Scan" in out
    assert "statement_history" in out        # 来源行写明 schema 从哪来


def test_sql_id_and_stdin_are_exclusive(capsys):
    with pytest.raises(SystemExit):
        explain_mod.main(["--sql-id", "1", "--sql-stdin"])


def test_sql_id_must_be_an_integer(monkeypatch, capsys):
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: _IdRunner())
    assert explain_mod.main(["--sql-id", "abc"]) == 2
    assert "sql id" in capsys.readouterr().err.lower()


def test_sql_id_not_found_is_a_clean_error(monkeypatch, capsys):
    class _Empty(_IdRunner):
        def run(self, script, values=None):
            if script in ("explain.from_history", "explain.from_statement"):
                return []
            return super().run(script, values)
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: _Empty())
    assert explain_mod.main(["--sql-id", "42"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "查不到" in err

    # **报错要说清最可能的原因,并给出那条出路。** 只说 "not found ... check
    # enable_stmt_track" 会把人往配置方向带 —— 而最常见的原因是实例重启把
    # dbe_perf.statement 清零了(og5 实测重启后整个视图只剩 51 条),这时手上
    # 往往已经有 SQL 文本(WDR 报告的 SQL 附录),该走 --sql-stdin 而不是再查一次 id。
    assert "重启" in err, "没说出最常见的原因,用户会去查配置"
    assert "--sql-stdin" in err, "没给出路,用户只能卡在这里"


def test_sql_id_with_normalized_text_points_to_sqltune(monkeypatch, capsys):
    """og5 / 备机上 statement_history 没这条时退回 dbe_perf.statement,文本带 ? 占位符——EXPLAIN 会语法错。
    explain 不做占位符合成(那是 sqltune 的活),要明确拒绝并指路,不能把语法错甩给用户。"""
    class _Norm(_IdRunner):
        def run(self, script, values=None):
            if script == "explain.from_history":
                return []
            if script == "explain.from_statement":
                return [{"user_name": "kf_app", "query": "select count(*) from kf_orders where status = ? and amount > ?"}]
            return super().run(script, values)
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: _Norm())
    assert explain_mod.main(["--sql-id", "7"]) == 2
    err = capsys.readouterr().err
    assert "归一化" in err and "sqltune" in err and "--sql-stdin --schema kf_app" in err and "Traceback" not in err



# ---------------------------------------------------------------- 归一化文本 → SQL 历史里的真实计划(2026-10-21 测试)

_REAL_PLAN = ("Datanode Name: og5\nSort  (cost=260109.53..260134.53 rows=10000 width=123) "
              "(actual time=124133.645..124167.730 rows=197939 loops=1)\n  Sort Key: (sum(l2.amt)) DESC\n")


class _HistNorm(_IdRunner):
    """statement_history 里有这条,但 L2 跟踪记的是归一化文本;同一行的 query_plan 是真实计划。"""

    def __init__(self, plan_rows):
        super().__init__()
        self.plan_rows = plan_rows

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if script == "explain.from_history":
            return [{"schema_name": "fl, public", "query": "WITH lvl1 AS (SELECT ? FROM fl.items WHERE a > ?) SELECT 1"}]
        if script == "explain.plan_from_history":
            return self.plan_rows
        return super().run(script, values)


def test_normalized_text_uses_the_real_plan_from_history(monkeypatch, capsys):
    r = _HistNorm([{"schema_name": "fl, public", "start_time": "2026-10-21 19:12:56", "db_time_ms": "124287.3",
                    "n_returned_rows": "197939", "query_plan": _REAL_PLAN}])
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    assert explain_mod.main(["--sql-id", "3091214944"]) == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert ("explain.plan_from_history", {"sid": 3091214944}) in r.calls
    assert "真实执行计划" in out and "2026-10-21 19:12:56" in out and "actual time=124133.645" in out
    assert "Datanode Name" not in out, "节点名那一行不是计划"
    assert not [c for c in r.calls if c[0].startswith("explain.plan_text")], "归一化文本不该拿去 EXPLAIN"


def test_normalized_text_without_history_plan_says_the_text_is_normalized(monkeypatch, capsys):
    """原来一律说「statement_history 里没有这条」—— 实际有,只是记的就是归一化文本。"""
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: _HistNorm([]))
    assert explain_mod.main(["--sql-id", "3091214944"]) == 2
    err = capsys.readouterr().err
    assert "statement_history 里没有这条" not in err
    assert "记的就是归一化文本" in err and "没有记下这条的执行计划" in err and "sqltune" in err


# ---------------------------------------------------------------- 贴文本没带 --schema → 脚本按表名在目录里推断

class _InferRunner(_Runner):
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if script == "explain.relation_schemas":
            return [{"nspname": s, "relname": t} for s, t in self.rows]
        if script == "explain.multi_stmt_probe":
            return [{"ok": "1"}]
        return super().run(script, values)


def test_stdin_without_schema_infers_it_from_the_catalog(monkeypatch, capsys):
    import io
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _InferRunner([("gmag", "batch_job_status")])
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT COUNT(1) FROM batch_job_status WHERE x = 1"))
    assert explain_mod.main(["--sql-stdin"]) == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert ("explain.relation_schemas", {"names": "'batch_job_status'"}) in r.calls
    assert any(c[0] == "explain.plan_text_schema" and c[1]["schema"] == "gmag" for c in r.calls)
    assert "search_path=gmag" in out and "推断" in out


def test_stdin_with_ambiguous_tables_lists_candidates_and_stops(monkeypatch, capsys):
    import io
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _InferRunner([("gbatch", "t_lmpcdtl"), ("dsc_ora_public", "t_lmpcdtl")])
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: r)
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT * FROM t_lmpcdtl"))
    assert explain_mod.main(["--sql-stdin"]) == 2
    err = capsys.readouterr().err
    assert "gbatch" in err and "dsc_ora_public" in err and "--schema" in err
    assert not any(c[0].startswith("explain.plan_text") for c in r.calls)     # 没猜着去跑
