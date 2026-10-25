"""common.proc_locate —— 过程「找不到」时代为排查:连的库对不对、有没有权限、本身在不在,并给用户问题清单。

现场(客户 09-08 晚截图):procinfo 按三段名查 gmag.pckg_xxx.proc_maketext 得 0 行,脚本只报一句「找不到」,
模型手里没有任何可继续查的工具,写了两条跑不了的 SQL 后把问题推回用户「确认拼写」。
og5 实测(对象隔离 ENABLE PRIVATE OBJECT):隔离藏的是 pg_namespace 那一行——pg_proc 单表仍查得到、gs_package 仍查得到,
三表 JOIN 就成了 0 行;授 USAGE 后全部可见。所以「过程在但 schema 不可见」是可推断的,其余情况交给用户/DBA 回答。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import proc_locate as pl  # noqa: E402
from common import procname as pn  # noqa: E402

PREFIX = "procinfo"
CTX, SCH, SRCH = "procinfo.locate_context", "procinfo.locate_schema", "procinfo.locate_search"
REF = pn.ProcRef("gmag", "pckg_zxac_property_dtl", "proc_maketext")


class FakeRunner:
    """按脚本名给行;值是异常就抛(模拟脚本没灌白名单)。"""

    def __init__(self, table):
        self.table, self.calls = table, []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        v = self.table.get(script)
        if isinstance(v, Exception):
            raise v
        return list(v or [])


def _ctx(db="gbatch", usr="grmp_exec", dbs="gbatch, gmag_db, postgres"):
    return [{"db": db, "usr": usr, "databases": dbs}]


def _proc(schema, package, name):
    return {"kind": "proc", "nspname": schema, "pkgname": package, "proname": name}


def _pkg(schema, package):
    return {"kind": "package", "nspname": schema, "pkgname": package, "proname": ""}


# ---------------------------------------------------------------- probe

def test_probe_collects_context_schema_and_hits():
    r = FakeRunner({CTX: _ctx(), SCH: [{"nspname": "gmag", "usage": "t"}],
                    SRCH: [_proc("gmag", "pckg_other", "proc_maketext"), _pkg("gmag", "pckg_zxac_property_dtl")]})
    loc = pl.probe(r, PREFIX, REF)
    assert (loc.db, loc.user, loc.databases) == ("gbatch", "grmp_exec", ("gbatch", "gmag_db", "postgres"))
    assert loc.schema_seen is True and loc.schema_usage is True
    assert loc.hits == (pl.Hit("proc", "gmag", "pckg_other", "proc_maketext"),
                        pl.Hit("package", "gmag", "pckg_zxac_property_dtl", ""))
    assert loc.skipped == ()
    assert (SCH, {"schema": "gmag"}) in r.calls
    assert (SRCH, {"name": "proc_maketext", "package": "pckg_zxac_property_dtl"}) in r.calls


def test_probe_records_missing_scripts_and_keeps_going():
    r = FakeRunner({CTX: RuntimeError("脚本 procinfo.locate_context 不存在"), SCH: [], SRCH: []})
    loc = pl.probe(r, PREFIX, REF)
    assert loc.db == "" and loc.schema_seen is False
    assert len(loc.skipped) == 1 and "当前连接" in loc.skipped[0] and "不存在" in loc.skipped[0]


def test_probe_skips_schema_probe_for_bare_name():
    r = FakeRunner({CTX: _ctx(), SCH: [{"nspname": "x", "usage": "t"}], SRCH: []})
    loc = pl.probe(r, PREFIX, pn.ProcRef("", "", "proc_x"))
    assert loc.schema_seen is None and loc.schema_usage is None
    assert all(s != SCH for s, _ in r.calls)


@pytest.mark.parametrize("raw,want", [("t", True), (True, True), (1, True), ("true", True),
                                      ("f", False), (False, False), (0, False), ("", False)])
def test_probe_normalises_usage_flag(raw, want):
    r = FakeRunner({CTX: _ctx(), SCH: [{"nspname": "gmag", "usage": raw}], SRCH: []})
    assert pl.probe(r, PREFIX, REF).schema_usage is want


def test_probe_treats_null_schema_as_hidden():
    r = FakeRunner({CTX: _ctx(), SCH: [], SRCH: [_proc(None, "pckg_zxac_property_dtl", "proc_maketext")]})
    loc = pl.probe(r, PREFIX, REF)
    assert loc.hits[0].schema == ""


# ---------------------------------------------------------------- classify

def _loc(**kw):
    base = dict(db="gbatch", user="grmp_exec", databases=("gbatch", "gmag_db"),
                schema_seen=True, schema_usage=True, hits=(), skipped=())
    base.update(kw)
    return pl.Locate(**base)


def test_hidden_schema_means_object_isolation_without_usage():
    v = pl.classify(REF, _loc(schema_seen=False, hits=(pl.Hit("proc", "", "pckg_zxac_property_dtl", "proc_maketext"),)))
    assert v.code == "schema_hidden"
    assert "对象隔离" in v.summary
    assert any("GRANT USAGE ON SCHEMA gmag TO" in a for a in v.actions)


def test_same_name_elsewhere_asks_user_to_confirm_candidate():
    v = pl.classify(REF, _loc(hits=(pl.Hit("proc", "dsc_ora_public", "", "proc_maketext"),)))
    assert v.code == "elsewhere"
    assert any("dsc_ora_public.proc_maketext" in q for q in v.questions)


def test_missing_schema_points_at_other_databases_and_dba_check():
    v = pl.classify(REF, _loc(schema_seen=False))
    assert v.code == "no_schema"
    assert any("gmag_db" in q for q in v.questions)
    assert any("gbatch" in q for q in v.questions)          # 当前库要写明,用户才知道「换库」是换哪个
    assert any("pg_proc" in a and "ILIKE" in a for a in v.actions)


def test_schema_without_usage_names_the_privilege():
    v = pl.classify(REF, _loc(schema_usage=False))
    assert v.code == "no_usage"
    assert any("GRANT USAGE ON SCHEMA gmag TO" in a for a in v.actions)


def test_package_missing_lists_near_packages():
    v = pl.classify(REF, _loc(hits=(pl.Hit("package", "gmag", "pckg_zxac_property_dt2", ""),)))
    assert v.code == "package_missing"
    assert any("pckg_zxac_property_dt2" in q for q in v.questions)


def test_package_present_but_proc_missing_offers_listing_sql():
    v = pl.classify(REF, _loc(hits=(pl.Hit("package", "gmag", "pckg_zxac_property_dtl", ""),)))
    assert v.code == "name_missing"
    assert any("propackageid" in a for a in v.actions)


def test_probe_gaps_yield_unknown_with_missing_script_hint():
    v = pl.classify(REF, _loc(schema_seen=None, skipped=("schema:脚本未注册",)))
    assert v.code == "unknown"
    assert any("交付文档" in q or "白名单" in q for q in v.questions)


def test_bare_name_without_schema_is_name_missing_not_no_schema():
    v = pl.classify(pn.ProcRef("", "", "proc_x"), _loc(schema_seen=None, schema_usage=None))
    assert v.code == "name_missing"


# ---------------------------------------------------------------- render / report

def test_render_has_findings_verdict_questions_and_dba_block():
    loc = _loc(schema_seen=False)
    v = pl.classify(REF, loc)
    text = pl.render(REF, ("gmag.pckg_zxac_property_dtl.proc_maketext",), loc, v)
    assert text.startswith("# 过程未找到")
    assert "当前连接" in text and "schema gmag" in text and "近似名" in text
    assert "请用户协助回答" in text and "\n1. " in text
    assert "```sql" in text and "ILIKE" in text
    assert "不支持" not in text          # 不给模型任何「不支持包」的暗示


def test_report_runs_probe_and_returns_markdown_or_json():
    r = FakeRunner({CTX: _ctx(), SCH: [], SRCH: []})
    exc = pn.NotFound(REF, ("gmag.pckg_zxac_property_dtl.proc_maketext",), "过程找不到")
    md = pl.report(r, PREFIX, exc)
    assert "# 过程未找到" in md and "gmag_db" in md
    js = json.loads(pl.report(r, PREFIX, exc, fmt="json"))
    nf = js["not_found"]
    assert nf["proc"] == "gmag.pckg_zxac_property_dtl.proc_maketext"
    assert nf["verdict"] == "no_schema" and nf["questions"] and nf["context"]["db"] == "gbatch"


def test_report_never_raises_when_every_probe_fails():
    boom = RuntimeError("HTTP 400")
    r = FakeRunner({CTX: boom, SCH: boom, SRCH: boom})
    md = pl.report(r, PREFIX, pn.NotFound(REF, ("x",), "找不到"))
    assert "未能排查" in md and "HTTP 400" in md
