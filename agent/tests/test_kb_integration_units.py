"""诊断 skill 脚本层接入「客户知识库参照」——接入不可静默(无库)。

九个接入的脚本:health / sqltune / lockwait,以及走 common/kbsection.py 的 waitevent / vacuum / wdr /
memanalyze / proctune / sqlreview。两条断言贯穿:① 知识库不存在时小节仍在,只是「未接入(原因)」,
主报告照常;② 有命中时每条 finding 一段,且小节位置正确(health:两段固定小节之后、维度正文之前;
sqltune:证据之后、结论性小节之前;其余见各用例)。另钉一条:被 health 当子进程拉起的 waitevent / vacuum
走 --format json,JSON 里不检索 —— health 自己对汇总后的发现查一次,不能同一份发现查两遍。
"""
import importlib
import pathlib
import sys
from types import SimpleNamespace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.finding import Finding, Severity  # noqa: E402
from common.kb import query as kbquery  # noqa: E402


def _is_skill_module(mod) -> bool:
    file = getattr(mod, "__file__", "") or ""
    return "/skills/" in file and "/scripts/" in file


@pytest.fixture
def load(request):
    """按真实模块名从某个 skill 的 scripts/ 目录导入(它们互相 import 兄弟模块)。

    十几个 skill 都有 report.py / model.py / render.py:全套里谁先导入谁占名。这里把来自
    skills/*/scripts 的已加载模块暂时挪开、把目标目录钉到 sys.path[0];**测试结束原样放回**——
    上一版直接清掉,让后面 wdr 的懒加载找不到模块。
    """
    saved_modules = {n: m for n, m in sys.modules.items() if _is_skill_module(m)}
    saved_path = list(sys.path)

    def restore():
        for n in [n for n, m in sys.modules.items() if _is_skill_module(m)]:
            sys.modules.pop(n, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path
    request.addfinalizer(restore)

    def _load(scripts_dir: str, module: str):
        for n in list(saved_modules):
            sys.modules.pop(n, None)
        path = str(_ROOT / "skills" / scripts_dir / "scripts")
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
        return importlib.import_module(module)
    return _load


def _finding(code="VAC_FREQ"):
    return Finding(dimension="vacuum", code=code, severity=Severity.WARN, metric="autovacuum 次数/h",
                   value="37", threshold="20", evidence="cbst.cosp_asyn_task_dtl autovacuum 次数异常高")


def _canned(findings, kb_dir=None, **_kw):     # **_kw:alias 等后加的参数(2026-10-24 起传连接别名)
    items = tuple(kbquery.FindingRefs(key=getattr(f, "code", "x"), label=f"🟠 {getattr(f, 'code', 'x')}",
                                      query="q",
                                      cases=(kbquery.Ref(id="case:S2-20250224-CBST-偶现单条update慢", kind="case",
                                                         title="偶现单条update慢", score=0.9,
                                                         meta={"conclusion": "已确认", "occurred_at": "2025-02-24"},
                                                         sections={"处置": "调大 autovacuum_vacuum_threshold"}),))
                  for f in findings)
    return kbquery.QueryResult(status=kbquery.KbStatus(attached=True, version="2026.09",
                                                       counts={"docs.rule": 1, "docs.case": 1, "docs.raw": 0},
                                                       vector="未启用", graph="Neo4j 5 条已确认边"), items=items)


# ---------------------------------------------------------------- health

def test_health_report_has_kb_section_even_when_unattached(monkeypatch, load):
    monkeypatch.setenv("GSDB_KB_DIR", "/nonexistent/kb")
    report = load("gaussdb-health", "report")
    model = sys.modules["model"]
    ev = model.HealthEvidence(conn="og", dims=[], findings=[_finding()], overall=Severity.WARN)
    out = report.render_health(ev)
    assert "## 客户知识库参照" in out
    assert "> 知识库未接入(" in out and "不存在" in out
    # 位置:两段固定小节之后、Deterministic Findings 之前
    assert out.index("## 未纳入汇总的能力") < out.index("## 客户知识库参照") < out.index("## Deterministic Findings")
    import json
    d = json.loads(report.render_health_json(ev))
    assert d["kb_refs"]["status"]["attached"] is False


def test_health_report_renders_hits_per_finding(monkeypatch, load):
    report = load("gaussdb-health", "report")
    model = sys.modules["model"]
    monkeypatch.setattr(kbquery, "from_findings", _canned)
    ev = model.HealthEvidence(conn="og", dims=[], findings=[_finding("VAC_FREQ"), _finding("XACT_LONG")],
                              overall=Severity.WARN)
    out = report.render_health(ev)
    assert "### 对 🟠 VAC_FREQ" in out and "### 对 🟠 XACT_LONG" in out
    assert "**历史相似** S2-20250224-CBST-偶现单条update慢(结论强度:已确认,2025-02-24):处置 = 调大 autovacuum_vacuum_threshold" in out
    assert "> 知识库 v2026.09 · 条款 1 · 案例 1" in out


# ---------------------------------------------------------------- sqltune

def _tr():
    ev = SimpleNamespace(sql="SELECT * FROM cbst.cosp_asyn_task_dtl WHERE id = 1", version="", plan="",
                         tables=[SimpleNamespace(schema="cbst", name="cosp_asyn_task_dtl")],
                         findings=[SimpleNamespace(kind="seq_scan", severity="warn",
                                                   detail="Seq Scan on cosp_asyn_task_dtl", advice="")],
                         indexes=[], columns=[], gucs=[], analyzed=False)
    return SimpleNamespace(sql_id="", source="", schema="", original_sql=ev.sql, evidence=ev,
                           substitution=SimpleNamespace(sql=ev.sql, placeholders=0, substitutions=[]),
                           verified_indexes=[], index_verify_note="", derivation_report="")


def test_sqltune_kb_items_cover_sql_and_plan_findings(load):
    sqltune = load("gaussdb-sqltune", "sqltune")
    items = sqltune.kb_items(_tr())
    assert items[0].code == "SQL" and "cbst.cosp_asyn_task_dtl" in items[0].evidence
    assert items[1].code == "PLAN_SEQ_SCAN" and "Seq Scan" in items[1].evidence and items[1].severity == "warn"


def test_sqltune_report_places_kb_section_before_verified_indexes(monkeypatch, load):
    sqltune = load("gaussdb-sqltune", "sqltune")
    monkeypatch.setattr(sqltune, "evidence_report", lambda ev: "## Evidence\n\n")
    monkeypatch.setattr(kbquery, "from_findings", _canned)
    out = sqltune.sqltune_report(_tr())
    assert "## 客户知识库参照" in out
    assert out.index("## Evidence") < out.index("## 客户知识库参照") < out.index("## Verified Index Candidates")
    assert "### 对 🟠 SQL" in out and "### 对 🟠 PLAN_SEQ_SCAN" in out
    assert "kb_refs" in sqltune._to_jsonable(_tr())


def test_sqltune_report_unattached_is_explicit(monkeypatch, load):
    monkeypatch.setenv("GSDB_KB_DIR", "/nonexistent/kb")
    sqltune = load("gaussdb-sqltune", "sqltune")
    monkeypatch.setattr(sqltune, "evidence_report", lambda ev: "")
    out = sqltune.sqltune_report(_tr())
    assert "> 知识库未接入(" in out


# ---------------------------------------------------------------- lockwait

def test_lockwait_report_has_kb_section_even_when_empty(monkeypatch, load):
    """锁等待为空时也要有小节——空白会被读成「这项没查」。"""
    monkeypatch.setenv("GSDB_KB_DIR", "/nonexistent/kb")
    lockwait = load("gaussdb-lockwait", "lockwait")
    rep = lockwait.LockReport(pairs=[], edges=[], deadlocks=[], findings=[])
    out = lockwait.render_markdown(rep)
    assert "当前无锁等待" in out and "## 客户知识库参照" in out and "> 知识库未接入(" in out


def test_lockwait_report_renders_hits_before_detail(monkeypatch, load):
    lockwait = load("gaussdb-lockwait", "lockwait")
    monkeypatch.setattr(kbquery, "from_findings", _canned)
    pair = {"waiter_sessionid": 2, "holder_sessionid": 1, "waiter_mode": "ShareLock", "holder_mode": "ExclusiveLock",
            "locktype": "transactionid", "lock_object": "", "waiter_wait_s": 3.0, "holder_query": "UPDATE t",
            "waiter_pid": 20, "holder_pid": 10, "waiter_query": "UPDATE t", "holder_state": "idle in transaction",
            "holder_user": "u", "holder_app": "cbst-batch-adjust", "holder_xact_age_s": 40.0, "waiter_user": "u",
            "waiter_app": "cbst-online", "locktag": ""}
    f = Finding(dimension="locks", code="LOCK_ROOT_IDLE_XACT", severity=Severity.WARN, metric="根阻塞会话",
                value="1", threshold="0", evidence="idle in transaction 40s cbst-batch-adjust")
    rep = lockwait.LockReport(pairs=[pair], edges=[(2, 1)], deadlocks=[], findings=[f])
    out = lockwait.render_markdown(rep)
    assert "## 客户知识库参照" in out and "### 对 🟠 LOCK_ROOT_IDLE_XACT" in out
    assert out.index("## 客户知识库参照") < out.index("## 阻塞明细")


# ---------------------------------------------------------------- 公共入口 common/kbsection.py

def _recording(calls):
    """替身 from_findings:记下每次检索收到的发现,再给命中。"""
    def fake(findings, kb_dir=None, **_kw):
        calls.append(list(findings))
        return _canned(findings, kb_dir)
    return fake


def test_kbsection_degrades_when_common_kb_is_missing(monkeypatch):
    from common import kbsection
    monkeypatch.setitem(sys.modules, "common.kb", None)          # 旧安装:import common.kb 失败
    text, d = kbsection.section([_finding()])
    assert text.startswith("## 客户知识库参照\n> 知识库未接入(common/kb 未安装")
    assert d["status"]["attached"] is False and d["items"] == []


def test_kbsection_queries_identical_findings_once(monkeypatch):
    """vacuum 的 R4 在每张命中表上各报一条一模一样的:只查一次,参照不重复 N 遍。"""
    from common import kbsection
    calls = []
    monkeypatch.setattr(kbquery, "from_findings", _recording(calls))
    out = kbsection.markdown([_finding("VAC_XMIN"), _finding("VAC_XMIN"), _finding("VAC_FREQ")])
    assert [f.code for f in calls[0]] == ["VAC_XMIN", "VAC_FREQ"]
    assert out.count("### 对 🟠 VAC_XMIN") == 1


# ---------------------------------------------------------------- waitevent

def _wait_finding():
    return Finding(dimension="DB Time", code="WAIT_LWLOCK_HEAVY", severity=Severity.WARN, metric="轻量锁等待占比",
                   value="45.0%", threshold="30%", evidence="WALInsertLock 等待占 DB time 45%")


def test_waitevent_markdown_has_kb_section_with_hits(monkeypatch, load):
    waitevent = load("gaussdb-waitevent", "waitevent")
    monkeypatch.setattr(kbquery, "from_findings", _canned)
    out = waitevent.render_markdown(waitevent.WaitReport(windows=[], findings=[_wait_finding()]))
    assert out.startswith("# DB Time 分解与等待事件下钻")
    assert "## 客户知识库参照" in out and "### 对 🟠 WAIT_LWLOCK_HEAVY" in out
    assert out.index("gaussdb-lockwait") < out.index("## 客户知识库参照")      # 放在报告末尾


def test_waitevent_markdown_unattached_still_renders(monkeypatch, load):
    monkeypatch.setenv("GSDB_KB_DIR", "/nonexistent/kb")
    waitevent = load("gaussdb-waitevent", "waitevent")
    out = waitevent.render_markdown(waitevent.WaitReport(windows=[], findings=[_wait_finding()]))
    assert "没有可用窗口" in out and "> 知识库未接入(" in out


def test_waitevent_json_for_health_does_not_query_the_kb(monkeypatch, load, capsys):
    """health 以 --format json 拉起本脚本:JSON 里不检索,markdown 里检索一次。"""
    waitevent = load("gaussdb-waitevent", "waitevent")
    calls = []
    monkeypatch.setattr(kbquery, "from_findings", _recording(calls))
    monkeypatch.setattr(waitevent.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(waitevent, "collect",
                        lambda runner, **k: waitevent.WaitReport(windows=[], findings=[_wait_finding()]))
    assert waitevent.main(["-c", "x", "--format", "json"]) == 0
    import json
    assert json.loads(capsys.readouterr().out)["findings"][0]["code"] == "WAIT_LWLOCK_HEAVY"
    assert calls == []
    assert waitevent.main(["-c", "x"]) == 0
    assert "## 客户知识库参照" in capsys.readouterr().out and len(calls) == 1


# ---------------------------------------------------------------- vacuum

def test_vacuum_markdown_places_kb_section_before_manual_cleanup(monkeypatch, load):
    vacuum = load("gaussdb-vacuum", "vacuum")
    monkeypatch.setattr(kbquery, "from_findings", _canned)
    out = vacuum.render_markdown(vacuum.VacuumReport(findings=[_finding("VACUUM_DEAD_RATIO")]))
    assert "### 对 🟠 VACUUM_DEAD_RATIO" in out
    assert out.index("## autovacuum 近期运行情况") < out.index("## 客户知识库参照") < out.index("## 手工清理评估")


def test_vacuum_markdown_unattached_still_renders(monkeypatch, load):
    monkeypatch.setenv("GSDB_KB_DIR", "/nonexistent/kb")
    vacuum = load("gaussdb-vacuum", "vacuum")
    out = vacuum.render_markdown(vacuum.VacuumReport(findings=[_finding("VACUUM_DEAD_RATIO")]))
    assert "# 死元组 / autovacuum 健康度评估" in out and "## 手工清理评估" in out
    assert "> 知识库未接入(" in out


def test_vacuum_json_for_health_does_not_query_the_kb(monkeypatch, load, capsys):
    vacuum = load("gaussdb-vacuum", "vacuum")
    calls = []
    monkeypatch.setattr(kbquery, "from_findings", _recording(calls))
    monkeypatch.setattr(vacuum.access, "for_conn", lambda *a, **k: object())
    monkeypatch.setattr(vacuum, "collect", lambda *a, **k: vacuum.VacuumReport(findings=[_finding("VACUUM_DEAD_RATIO")]))
    assert vacuum.main(["-c", "x", "--format", "json"]) == 0
    assert "客户知识库" not in capsys.readouterr().out and calls == []


# ---------------------------------------------------------------- wdr(collect 的证据包)

def _wdr_ev(model):
    f = model.Finding(model.DIM_DBSTAT, "WDR_DEADLOCK", model.Severity.WARN, "死锁数", "6", "≥5", "e")
    return model.Evidence(conn="og", window=model.Window(begin_id=1, end_id=2, scope="node"),
                          dims=[], findings=[f], overall=model.Severity.WARN)


def test_wdr_evidence_places_kb_section_after_findings(monkeypatch, load):
    report = load("gaussdb-wdr", "report")
    model = sys.modules["model"]
    monkeypatch.setattr(kbquery, "from_findings", _canned)
    ev = _wdr_ev(model)
    out = report.render_evidence(ev)
    assert "### 对 🟠 WDR_DEADLOCK" in out
    assert out.index("## Deterministic Findings") < out.index("## 客户知识库参照") < out.index("## Collection Notes")
    assert "kb_refs" not in report.render_evidence_json(ev)          # 证据包 JSON 是 render 的输入,形状不变


def test_wdr_evidence_unattached_still_renders(monkeypatch, load):
    monkeypatch.setenv("GSDB_KB_DIR", "/nonexistent/kb")
    report = load("gaussdb-wdr", "report")
    out = report.render_evidence(_wdr_ev(sys.modules["model"]))
    assert "WDR_DEADLOCK" in out and "## Collection Notes" in out and "> 知识库未接入(" in out


# ---------------------------------------------------------------- memanalyze

def _mem_finding():
    return Finding(dimension="SQL", code="MEM_SQL_SPILL", severity=Severity.WARN, metric="落盘",
                   value="512MB", threshold="0", evidence="排序落盘 512MB")


def test_memanalyze_places_kb_section_after_findings(monkeypatch, load):
    report = load("gaussdb-memanalyze", "report")
    model = sys.modules["model"]
    monkeypatch.setattr(kbquery, "from_findings", _canned)
    dim = model.DimResult(dimension="SQL 级", available=True, headline="h")
    out = report.render_markdown(model.MemEvidence(conn="og", dims=[dim], findings=[_mem_finding()]))
    assert "### 对 🟠 MEM_SQL_SPILL" in out
    assert out.index("## Deterministic Findings") < out.index("## 客户知识库参照") < out.index("## SQL 级")
    assert "kb_refs" not in report.render_json(model.MemEvidence(conn="og", findings=[_mem_finding()]))


def test_memanalyze_unattached_still_renders(monkeypatch, load):
    monkeypatch.setenv("GSDB_KB_DIR", "/nonexistent/kb")
    report = load("gaussdb-memanalyze", "report")
    out = report.render_markdown(sys.modules["model"].MemEvidence(conn="og", findings=[_mem_finding()]))
    assert "MEM_SQL_SPILL" in out and "> 知识库未接入(" in out


# ---------------------------------------------------------------- proctune

def _proc(pa):
    body = ("BEGIN\n  FOR r IN SELECT id FROM cbst.acct LOOP\n"
            "    SELECT count(*) INTO v_cnt FROM cbst.acct_dtl WHERE acct_id = r.id;\n"
            "    UPDATE cbst.acct_dtl SET flag = 1 WHERE acct_id = r.id;\n  END LOOP;\nEND")
    return pa.ProcDef(schema="cbst", name="p_settle", lang="plpgsql", args=[], source=body, body=body,
                      vars={"v_cnt": "integer"}, rollback_safe=True)


def test_proctune_collect_items_and_section(monkeypatch, load):
    proctune = load("gaussdb-proctune", "proctune")
    pa = sys.modules["procanalyze"]
    proc = _proc(pa)
    pe = proctune.ProcEvidence(proc=proc, structure=pa.scan_structure(proc.body), embedded=[],
                               runtime_note="n", gucs=[])
    items = proctune.kb_items_collect(pe)
    assert items[0].code == "PROC" and "cbst.p_settle" in items[0].evidence
    assert "cbst.acct_dtl" in items[0].evidence and "v_cnt" not in items[0].evidence   # INTO 变量不当表名
    codes = [i.code for i in items]
    assert "PROC_PER_ROW_DML" in codes and len(codes) == len(set(codes))              # 同类只查一次
    calls = []
    monkeypatch.setattr(kbquery, "from_findings", _recording(calls))
    out = proctune.proc_collect_report(pe)
    assert out.index("## Key Parameters (GUC)") < out.index("## 客户知识库参照")
    assert "### 对 🟠 PROC" in out and [f.code for f in calls[0]] == codes


def test_proctune_tune_cursor_section_and_unattached(monkeypatch, load):
    monkeypatch.setenv("GSDB_KB_DIR", "/nonexistent/kb")
    proctune = load("gaussdb-proctune", "proctune")
    pa, ev_mod = sys.modules["procanalyze"], sys.modules["evidence"]
    ev = ev_mod.Evidence(sql="SELECT 1", version="v", plan="Seq Scan", analyzed=False,
                         tables=[ev_mod.TableInfo("cbst", "acct_dtl", 1, 1, "r", 1.0)],
                         findings=[ev_mod.Finding("seq_scan", "warn", "Seq Scan on acct_dtl", "加索引")])
    ce = proctune.CursorEvidence(name="c1", kind="explicit", orig_sql="SELECT 1", var_subs=[], evidence=ev)
    tr = proctune.CursorTuneResult(proc=_proc(pa), cursors=[ce])
    items = proctune.kb_items_tune(tr)
    assert [i.code for i in items] == ["PLAN_SEQ_SCAN"] and "cbst.acct_dtl" in items[0].evidence
    out = proctune.cursor_tune_report(tr)
    assert "## Cursor c1" in out and "## Skipped Cursors" in out and "> 知识库未接入(" in out


# ---------------------------------------------------------------- sqlreview

def _review(model):
    def f(loc):
        return model.Finding(rule_id="SEL001", rule_name="禁止 SELECT *", severity=model.Severity.WARN,
                             message="SELECT * 读出全部列", location=loc, snippet="SELECT * FROM cbst.acct")
    adv = model.Finding(rule_id="IDX005", rule_name="索引列数", severity=model.Severity.INFO, message="待判断",
                        location="cbst.acct", advisory=True)
    return model.ReviewResult(source="file:a.sql", findings=(f("stmt#1"), f("stmt#2"), adv), statements=2)


def test_sqlreview_one_item_per_rule_and_section_at_end(monkeypatch, load):
    report = load("gaussdb-sqlreview", "report")
    res = _review(sys.modules["model"])
    items = report.kb_items(res)
    assert [i.code for i in items] == ["SEL001", "IDX005"]
    assert items[0].value == "2 处" and "stmt#1 stmt#2" in items[0].evidence and items[1].severity == "advisory"
    monkeypatch.setattr(kbquery, "from_findings", _canned)
    out = report.render_markdown(res)
    assert out.index("## Advisory") < out.index("## 客户知识库参照")
    assert "### 对 🟠 SEL001" in out and "### 对 🟠 IDX005" in out
    assert "客户知识库" not in report.render_json(res)


def test_sqlreview_unattached_still_renders(monkeypatch, load):
    monkeypatch.setenv("GSDB_KB_DIR", "/nonexistent/kb")
    report = load("gaussdb-sqlreview", "report")
    out = report.render_markdown(_review(sys.modules["model"]))
    assert "# SQL 规范审查" in out and "SEL001" in out and "> 知识库未接入(" in out
