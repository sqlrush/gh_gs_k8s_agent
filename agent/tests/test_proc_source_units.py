"""common.proc_source —— pg_proc 取不到时按 ADM_SOURCE 兜底取存储过程源码(procinfo / proctune 共用)。

需求(客户 2026-10-29):开了对象隔离的库,普通账号查 pg_proc / pg_namespace 受行级访问控制,联表 0 行;
ADM_SOURCE 是管理员视角的视图(owner / name / type / line / text,一行源码一条)。下面钉住:
  · 逐行拼接不假设行尾换行、空行可能是 NULL,拼出来第 N 行 = ADM_SOURCE 第 N 行;同一行号出现两次(重载)拒绝;
  · 过程头换成 DECLARE、行数不变,与 pg_proc.prosrc 同形,静态分析照常;
  · 包内过程从 package body 里切出来:局部子程序、CASE / IF / LOOP、字符串与注释里的 END; 都不能把边界切错;
  · owner 与 schema 的对应、两段名 a.b 的两种读法、同名多份拒绝不猜;
  · 走不通(没登记 / 没权限 / 视图不存在)时报告里写明原因,没权限的给出授权语句(由 DBA 决定)。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import proc_locate  # noqa: E402
from common import proc_source as ps  # noqa: E402
from common import procname  # noqa: E402
from common.grmp.errors import QueryError  # noqa: E402

PKG = """PACKAGE BODY pckg_report IS
  g_batch CONSTANT int := 500;
  CURSOR g_cur IS SELECT id FROM t_cfg WHERE flag = 'END;';
  PROCEDURE helper(p int);
  -- PROCEDURE fake IS BEGIN NULL; END; 注释里的不算
  FUNCTION fmt(p_v numeric) RETURN varchar2 IS
  BEGIN
    RETURN CASE WHEN p_v > 0 THEN 'pos' ELSE 'neg' END;
  END fmt;
  PROCEDURE proc_maketext(p_day IN date, p_cnt OUT int) IS
    v_total int := 0;
    CURSOR c_rows IS SELECT a, b FROM t_detail WHERE d = p_day;
    PROCEDURE log_it(m varchar2) IS
    BEGIN
      INSERT INTO t_log VALUES (m);
    END log_it;
  BEGIN
    FOR r IN c_rows LOOP
      IF r.a > 0 THEN
        UPDATE t_sum SET s = s + r.b WHERE k = r.a;
      END IF;
      CASE r.b WHEN 1 THEN v_total := v_total + 1; ELSE NULL; END CASE;
    END LOOP;
    p_cnt := v_total;
  END proc_maketext;
  PROCEDURE helper(p int) IS
  BEGIN
    NULL;
  END;
BEGIN
  NULL;
END pckg_report;
"""

PROC = """PROCEDURE proc_daily(p_day date) IS
  v_n int;
  CURSOR c1 IS SELECT id FROM t_a WHERE d = p_day;
BEGIN
  FOR r IN c1 LOOP
    DELETE FROM t_b WHERE id = r.id;
  END LOOP;
END proc_daily;
"""


def _adm(owner, name, kind, text, newline=True):
    """一个对象在 ADM_SOURCE 里的样子:一行一条;newline 控制每行 text 是否带行尾换行(两种都要认)。"""
    return [{"owner": owner, "name": name, "type": kind, "line": str(i),
             "text": (line + "\n") if newline else line}
            for i, line in enumerate(text.splitlines(), 1)]


class Runner:
    """假 pg_proc(<skill>.proc_def)+ 假 ADM_SOURCE(<skill>.source_adm)。"""

    def __init__(self, pg=(), adm=(), adm_error=None):
        self.pg, self.adm, self.adm_error, self.calls = list(pg), list(adm), adm_error, []

    def run(self, script, values=None):
        v = dict(values or {})
        self.calls.append((script, v))
        if script.endswith(".proc_def"):
            return [r for r in self.pg if r["proname"] == v["name"]
                    and (not v["schema"] or r["nspname"] == v["schema"])
                    and (not v["package"] or r["package"] == v["package"])]
        if script.endswith(".source_adm"):
            if self.adm_error is not None:
                raise self.adm_error
            return [r for r in self.adm if r["name"].lower() == v["name"].lower()]
        raise QueryError("未注册脚本 %s。已注册的有:…" % script)


def _resolve(runner, name):
    return ps.resolve(runner, "proctune.proc_def", "proctune.source_adm", name)


# ---------------------------------------------------------------- 拼接

def test_assemble_strips_line_ends_fills_gaps_and_keeps_line_numbers():
    text = ps.assemble([(1, "BEGIN\n"), (2, "  x := 1;"), (4, "END;\r\n")])
    assert text.splitlines() == ["BEGIN", "  x := 1;", "", "END;"]


def test_assemble_treats_null_text_as_blank_line():
    """A 兼容库里空串就是 NULL;协议把 NULL 渲染成空串 —— 空行不能丢,否则后面的行号全错位。"""
    rows = _adm("gmag", "p", "procedure", "BEGIN\n\nEND;", newline=False)
    rows[1]["text"] = None
    lines = ps._groups(rows, ("procedure",))[("gmag", "procedure")]
    assert ps.assemble(lines).splitlines() == ["BEGIN", "", "END;"]


def test_assemble_refuses_duplicate_line_numbers_as_overloads():
    with pytest.raises(ValueError) as ei:
        ps.assemble([(1, "PROCEDURE p(a int) IS"), (1, "PROCEDURE p(a text) IS")], "gmag.p")
    assert "重载" in str(ei.value) and "不猜" in str(ei.value)


# ---------------------------------------------------------------- 过程头规整

def test_normalize_oracle_header_becomes_declare_and_keeps_line_count():
    args, body, lang = ps.normalize(PROC)
    assert args == "p_day date" and lang == "plpgsql"
    assert body.splitlines()[0] == "DECLARE"
    assert len(body.splitlines()) == len(PROC.splitlines())
    assert body.splitlines()[5] == PROC.splitlines()[5]          # 第 6 行还是第 6 行


def test_normalize_multiline_header_keeps_line_count():
    text = "CREATE OR REPLACE PROCEDURE gmag.p(\n  a int,\n  b text\n) AS\nBEGIN\n  NULL;\nEND;\n"
    args, body, _ = ps.normalize(text)
    assert args.split() == ["a", "int,", "b", "text"]
    assert body.splitlines()[:5] == ["DECLARE", "", "", "", "BEGIN"]


def test_normalize_function_with_return_clause():
    args, body, _ = ps.normalize("FUNCTION f(p numeric) RETURN varchar2 IS\nBEGIN\n  RETURN 'x';\nEND f;\n")
    assert args == "p numeric" and body.startswith("DECLARE\nBEGIN")


def test_normalize_dollar_quoted_body_and_language():
    text = "FUNCTION f(a int) RETURNS int AS $body$\nSELECT a + 1\n$body$ LANGUAGE sql;\n"
    args, body, lang = ps.normalize(text)
    assert args == "a int" and lang == "sql"
    assert body.splitlines()[1] == "SELECT a + 1"                 # 第 2 行还是第 2 行


def test_normalize_passes_prosrc_shaped_text_through():
    text = "DECLARE\n  v int;\nBEGIN\n  NULL;\nEND"
    assert ps.normalize(text) == ("", text, "plpgsql")


# ---------------------------------------------------------------- 包体切分

def test_package_subprograms_finds_top_level_ones_only():
    decls, spans = ps.package_subprograms(PKG)
    assert [s.name for s in spans] == ["fmt", "proc_maketext", "helper"]   # 局部的 log_it、前置声明、注释里的都不算
    assert "g_batch CONSTANT int := 500" in decls and "CURSOR g_cur" in decls
    sp = spans[1]
    assert PKG[sp.start:sp.end].startswith("PROCEDURE proc_maketext")
    assert PKG[sp.start:sp.end].endswith("END proc_maketext;")
    assert ps._line(PKG, sp.start) == 10 and ps._line(PKG, sp.end - 1) == 25


def test_package_subprograms_survives_strings_comments_and_case_expressions():
    """字符串里的 'END;'、注释里的 PROCEDURE、CASE 表达式的 END —— 任何一个认错,边界就切到别的过程里去。"""
    _, spans = ps.package_subprograms(PKG)
    fmt = PKG[spans[0].start:spans[0].end]
    assert fmt.endswith("END fmt;") and "proc_maketext" not in fmt


# ---------------------------------------------------------------- 兜底流程

def test_pg_proc_hit_never_touches_adm_source():
    r = Runner(pg=[{"nspname": "gmag", "proname": "p", "lanname": "plpgsql", "prosrc": "BEGIN NULL; END",
                    "args": "", "package": ""}])
    res = _resolve(r, "gmag.p")
    assert res.origin == "pg_proc" and res.note == ""
    assert all(not c[0].endswith(".source_adm") for c in r.calls)


def test_standalone_from_adm_when_pg_proc_misses():
    r = Runner(adm=_adm("gmag", "proc_daily", "procedure", PROC, newline=False))
    res = _resolve(r, "gmag.proc_daily")
    assert res.origin == "adm_source" and res.schema == "gmag" and res.package == ""
    assert res.args == "p_day date" and res.body.startswith("DECLARE")
    assert "pg_proc 里查不到" in res.note and "ADM_SOURCE" in res.note and "所有者 gmag" in res.note


def test_package_proc_from_adm_carries_line_range_and_package_decls():
    r = Runner(adm=_adm("gmag", "pckg_report", "package body", PKG))
    res = _resolve(r, "gmag.pckg_report.proc_maketext")
    assert res.package == "pckg_report" and res.name == "proc_maketext"
    assert res.args == "p_day IN date, p_cnt OUT int"
    assert "包体第 10–25 行" in res.note
    assert "g_batch" in res.context and "proc_maketext" not in res.context
    assert ("proctune.source_adm", {"name": "pckg_report"}) in r.calls


def test_two_part_name_is_tried_as_package_proc_too():
    """`pckg_report.proc_maketext`:没有叫 pckg_report 的所有者,再当包名查 —— 与 pg_proc 那一路同一个顺序。"""
    r = Runner(adm=_adm("gmag", "pckg_report", "package body", PKG))
    res = _resolve(r, "pckg_report.proc_maketext")
    assert res.package == "pckg_report" and res.schema == "gmag"


def test_owner_mismatch_with_single_object_is_used_and_said_out_loud():
    r = Runner(adm=_adm("app_owner", "proc_daily", "procedure", PROC))
    res = _resolve(r, "gmag.proc_daily")
    assert "app_owner" in res.note and "gmag" in res.note and "只有这一份" in res.note


def test_several_owners_without_match_is_refused_with_candidates():
    r = Runner(adm=_adm("u1", "proc_daily", "procedure", PROC) + _adm("u2", "proc_daily", "procedure", PROC))
    with pytest.raises(ValueError) as ei:
        _resolve(r, "proc_daily")
    assert "u1.proc_daily" in str(ei.value) and "u2.proc_daily" in str(ei.value) and "不猜" in str(ei.value)


def test_overloads_inside_package_are_refused():
    body = PKG.replace("  PROCEDURE helper(p int) IS", "  PROCEDURE proc_maketext(p int) IS")
    r = Runner(adm=_adm("gmag", "pckg_report", "package body", body))
    with pytest.raises(ValueError) as ei:
        _resolve(r, "gmag.pckg_report.proc_maketext")
    assert "重载" in str(ei.value)


def test_proc_missing_from_package_lists_what_is_there():
    r = Runner(adm=_adm("gmag", "pckg_report", "package body", PKG))
    with pytest.raises(ValueError) as ei:
        _resolve(r, "gmag.pckg_report.proc_nope")
    assert "fmt" in str(ei.value) and "proc_maketext" in str(ei.value)


def test_empty_prosrc_falls_back_to_adm():
    r = Runner(pg=[{"nspname": "gmag", "proname": "proc_daily", "lanname": "plpgsql", "prosrc": "",
                    "args": "", "package": ""}],
               adm=_adm("gmag", "proc_daily", "procedure", PROC))
    res = _resolve(r, "gmag.proc_daily")
    assert res.origin == "adm_source" and "源码为空" in res.note


def test_too_many_lines_is_refused_not_truncated():
    rows = [{"owner": "gmag", "name": "big", "type": "procedure", "line": i, "text": "NULL;"}
            for i in range(1, ps.MAX_LINES + 2)]
    with pytest.raises(ValueError) as ei:
        _resolve(Runner(adm=rows), "gmag.big")
    assert str(ps.MAX_LINES) in str(ei.value)


@pytest.mark.parametrize("err,denied,says", [
    # 中间件的原话(og5 实测形态):任务号、库名、SQLSTATE 都不该进给用户看的那一句
    (QueryError("执行 proctune.source_adm 失败（status='failed'，task_id=grmp-1）：ERROR: permission denied for "
                "relation adm_source on og5 (SQLSTATE 42501)"), True,
     "执行账号没有 ADM_SOURCE 的读权限(permission denied for relation adm_source)"),
    (QueryError("中间件未注册脚本 proctune.source_adm。已注册的有：a、b、c"), False, "中间件里还没有登记这条脚本"),
    (QueryError('执行脚本 proctune.source_adm 失败：ERROR: relation "pg_catalog.adm_source" does not exist '
                'on og5 (SQLSTATE 42P01)'), False,
     '本库没有 ADM_SOURCE 视图(relation "pg_catalog.adm_source" does not exist)'),
])
def test_adm_unavailable_keeps_not_found_and_says_why(err, denied, says):
    with pytest.raises(procname.NotFound) as ei:
        _resolve(Runner(adm_error=err), "gmag.proc_daily")
    nf = ei.value
    assert nf.adm.startswith("没取到 —— " + says)
    assert nf.adm_denied is denied
    assert "已注册的有" not in nf.adm and "task_id" not in nf.adm


def test_adm_empty_keeps_not_found_with_note():
    with pytest.raises(procname.NotFound) as ei:
        _resolve(Runner(), "gmag.pckg_report.proc_x")
    assert ei.value.adm.startswith("也没有") and "包 pckg_report" in ei.value.adm


def test_locate_report_shows_adm_line_and_grant_only_when_denied():
    runner = Runner(adm_error=QueryError("permission denied for relation adm_source"))
    with pytest.raises(procname.NotFound) as ei:
        _resolve(runner, "gmag.proc_daily")
    md = proc_locate.report(runner, "proctune", ei.value)
    assert "- ADM_SOURCE 兜底:没取到 —— 执行账号没有 ADM_SOURCE 的读权限" in md
    assert "GRANT SELECT ON pg_catalog.adm_source TO" in md
    js = proc_locate.report(runner, "proctune", ei.value, fmt="json")
    assert '"denied": true' in js

    runner = Runner()
    with pytest.raises(procname.NotFound) as ei:
        _resolve(runner, "gmag.proc_daily")
    md = proc_locate.report(runner, "proctune", ei.value)
    assert "- ADM_SOURCE 兜底:也没有" in md and "adm_source TO" not in md


# ---------------------------------------------------------------- 接进两个技能

def _load(skill, mod):
    path = _ROOT / "skills" / skill / "scripts" / ("%s.py" % mod)
    sys.modules.pop("render", None)
    sys.modules.pop("procanalyze", None)
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("%s_%s_adm_under_test" % (skill, mod), path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("skill,mod,report", [
    ("gaussdb-procinfo", "procinfo", "proc_info_report"),
    ("gaussdb-proctune", "proctune", "proc_collect_report"),
])
def test_skills_analyse_package_proc_from_adm_and_label_the_source(skill, mod, report):
    m = _load(skill, mod)
    assert m.SOURCE_ADM_SCRIPT == "%s.source_adm" % mod

    class R(Runner):
        def run(self, script, values=None):
            if script.endswith(".key_gucs"):
                return [{"name": "work_mem", "setting": "64", "unit": "MB"}]
            return super().run(script, values)

    pe = m.proc_collect(R(adm=_adm("gmag", "pckg_report", "package body", PKG)), "gmag.pckg_report.proc_maketext")
    d = pe.proc
    assert d.origin == "adm_source" and d.package == "pckg_report" and "v_total" in d.vars
    assert [a.name for a in d.args] == ["p_day", "p_cnt"]
    assert any(e.kind == "cursor:c_rows" for e in pe.embedded)
    assert any(f.kind == "per_row_dml" for f in pe.structure)
    md = getattr(m, report)(pe)
    assert "- Source: `ADM_SOURCE` — pg_proc 里查不到,源码取自 ADM_SOURCE:包 pckg_report 的包体第 10–25 行" in md
    assert "## Package Declarations" in md and "g_batch" in md


@pytest.mark.parametrize("skill,mod,report", [
    ("gaussdb-procinfo", "procinfo", "proc_info_report"),
    ("gaussdb-proctune", "proctune", "proc_collect_report"),
])
def test_skills_output_unchanged_when_pg_proc_answers(skill, mod, report):
    m = _load(skill, mod)

    class R(Runner):
        def run(self, script, values=None):
            if script.endswith(".key_gucs"):
                return []
            return super().run(script, values)

    pg = [{"nspname": "gmag", "proname": "p", "lanname": "plpgsql", "prosrc": "BEGIN NULL; END",
           "args": "", "package": ""}]
    md = getattr(m, report)(m.proc_collect(R(pg=pg), "gmag.p"))
    assert "- Source:" not in md and "## Package Declarations" not in md
