"""common.kb.cases / graphfiles —— 案例文件与三元组文件的解析与校验(无库)。"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import cases as kbcases, graphfiles as gf  # noqa: E402

CASE_ID = "S1-20250224-CBST-偶现单条update慢"
CASE = f"""---
id: {CASE_ID}
title: 偶现单条 update 走索引耗时 3s
system: CBST
instance: 未知
occurred_at: 2025-02-24
engine: gaussdb
severity: S1
primary_factor: autovacuum 尾部空页回收持 8 级锁与 DML 互相 cancel
secondary_factors: []
conclusion: 已确认
source: sources/20250224-CBST-问题分析报告.v1.docx#前言
objects: [cbst.cosp_asyn_task_dtl, autovacuum_vacuum_threshold]
signals: [单条 update 偶发秒级, autovacuum 频繁触发]
---
## 现场
业务偶现单条 update 走索引执行耗时 3s。
## 判断
autovacuum 检测到表尾部空页时触发 page 回收,持 8 级锁。
## 处置
针对 cbst.cosp_asyn_task_dtl 小表调大 autovacuum_vacuum_threshold。
## 复发标志
单条 update 偶发 3s 且该表 autovacuum 次数异常高。
"""


def _kb(tmp_path, case_text=CASE, name=CASE_ID):
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / f"{name}.md").write_text(case_text, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- cases

def test_parse_case_reads_frontmatter_and_sections(tmp_path):
    case, errors = kbcases.parse_case(CASE, tmp_path / f"{CASE_ID}.md")
    assert errors == []
    assert case.id == CASE_ID and case.system == "CBST" and case.confidence == 1.0
    assert case.objects == ("cbst.cosp_asyn_task_dtl", "autovacuum_vacuum_threshold")
    assert case.signals[0] == "单条 update 偶发秒级"
    assert case.section("处置").startswith("针对")
    assert set(case.sections) == {"现场", "判断", "处置", "复发标志"}
    assert len(case.content_hash) == 16


@pytest.mark.parametrize("mutate, needle", [
    (lambda t: t.replace("conclusion: 已确认", "conclusion: 大概吧"), "conclusion"),
    (lambda t: t.replace("conclusion: 已确认\n", ""), "conclusion"),
    (lambda t: t.replace("## 处置\n针对 cbst.cosp_asyn_task_dtl 小表调大 autovacuum_vacuum_threshold。\n", ""), "处置"),
    (lambda t: t.replace("occurred_at: 2025-02-24", "occurred_at: 2025/02/24"), "occurred_at"),
    (lambda t: t.replace("severity: S1", "severity: P1"), "severity"),
    (lambda t: t.replace("## 复发标志", "## 教训"), "未知小节"),
    (lambda t: t.replace(f"id: {CASE_ID}", "id: CASE-0042"), "格式"),
])
def test_parse_case_rejects_bad_input(tmp_path, mutate, needle):
    case, errors = kbcases.parse_case(mutate(CASE), tmp_path / f"{CASE_ID}.md")
    assert any(needle in e for e in errors), errors


def test_id_must_match_filename(tmp_path):
    _, errors = kbcases.parse_case(CASE, tmp_path / "S1-20250224-CBST-别的名字.md")
    assert any("文件名" in e for e in errors)


def test_missing_frontmatter_is_an_error(tmp_path):
    case, errors = kbcases.parse_case("## 现场\nx\n", tmp_path / "a.md")
    assert case is None and errors


def test_load_cases_skips_broken_and_flags_duplicates(tmp_path):
    kb = _kb(tmp_path)
    other = "S1-20250225-CBMS-另一个案例"
    (kb / "cases" / f"{other}.md").write_text(CASE.replace(f"id: {CASE_ID}", f"id: {other}"), encoding="utf-8")
    dup = "S1-20250226-CBMS-重复ID"
    (kb / "cases" / f"{dup}.md").write_text(CASE.replace(f"id: {CASE_ID}", f"id: {dup}")
                                            .replace("conclusion: 已确认", "conclusion: 推测"), encoding="utf-8")
    (kb / "cases" / "broken.md").write_text("---\nid: x\n", encoding="utf-8")
    cases, findings = kbcases.load_cases(kb)
    assert {c.id for c in cases} == {CASE_ID, other, dup}
    errors = [m for lvl, m in findings if lvl == "error"]
    assert any("broken.md" in e for e in errors)
    assert any("推测" in m and "不入" in m for lvl, m in findings if lvl == "warn")


def test_load_cases_warns_without_signals(tmp_path):
    text = CASE.replace("signals: [单条 update 偶发秒级, autovacuum 频繁触发]\n", "").replace(
        "## 复发标志\n单条 update 偶发 3s 且该表 autovacuum 次数异常高。\n", "")
    kb = _kb(tmp_path, text)
    _, findings = kbcases.load_cases(kb)
    assert any("复发标志" in m for lvl, m in findings if lvl == "warn")


# ---------------------------------------------------------------- canonical

def test_canonical_id_uses_alias_table_then_slug():
    aliases = {"核心账户表": "object:core_acct", "core_acct": "object:core_acct"}
    assert gf.canonical_id("object", "核心账户表", aliases) == "object:core_acct"
    assert gf.canonical_id("object", "CORE_ACCT", aliases) == "object:core_acct"
    assert gf.canonical_id("object", "cbst.cosp_asyn_task_dtl", {}) == "object:cbst.cosp_asyn_task_dtl"
    assert gf.canonical_id("symptom", "单条 update 偶发秒级", {}) == "symptom:单条_update_偶发秒级"
    assert gf.canonical_id("wait_event", "LWLock:WALWriteLock", {}) == "wait_event:lwlock:walwritelock"


def test_load_canonical_rejects_conflicts(tmp_path):
    (tmp_path / "graph").mkdir()
    (tmp_path / "graph" / "canonical.yaml").write_text(
        "object:core_acct: [核心账户表, CORE_ACCT]\nobject:other: [核心账户表]\nbad-id: [x]\n", encoding="utf-8")
    aliases, findings = gf.load_canonical(tmp_path)
    assert aliases["核心账户表"] == "object:core_acct"
    assert aliases["core_acct"] == "object:core_acct"          # id 自身也是别名
    msgs = [m for _, m in findings]
    assert any("同时指向" in m for m in msgs) and any("bad-id" in m for m in msgs)


# ---------------------------------------------------------------- triples

TRIPLES = """
- src: {kind: symptom, name: 单条 update 偶发秒级}
  rel: caused_by
  dst: {kind: rootcause, name: autovacuum 尾部回收持 8 级锁}
  confidence: 1.0
  source: cases/S1-20250224-CBST-偶现单条update慢.md#判断
  case: S1-20250224-CBST-偶现单条update慢
  valid_from: 2025-02-24
- src: {kind: rootcause, name: autovacuum 尾部回收持 8 级锁}
  rel: handled_by
  dst: {kind: action, name: 表级调大 autovacuum_vacuum_threshold}
  confidence: 0.6
  status: candidate
  source: cases/S1-20250224-CBST-偶现单条update慢.md#处置
  case: S1-20250224-CBST-偶现单条update慢
"""


def _graph_kb(tmp_path, text=TRIPLES):
    (tmp_path / "graph").mkdir(exist_ok=True)
    (tmp_path / "graph" / "cbst.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_load_triples_and_path_eligibility(tmp_path):
    kb = _graph_kb(tmp_path)
    triples, findings = gf.load_triples(kb, case_ids=[CASE_ID])
    assert len(triples) == 2
    assert triples[0].in_path is True
    assert triples[1].in_path is False                 # candidate + 0.6
    assert triples[0].src.id == "symptom:单条_update_偶发秒级"
    assert any("尚未确认" in m for lvl, m in findings if lvl == "warn")
    assert not [m for lvl, m in findings if lvl == "error"]


@pytest.mark.parametrize("mutate, needle", [
    (lambda t: t.replace("rel: caused_by", "rel: co_occurs"), "共现"),
    (lambda t: t.replace("confidence: 1.0", "confidence: 1.5"), "0..1"),
    (lambda t: t.replace("  source: cases/S1-20250224-CBST-偶现单条update慢.md#判断\n", ""), "source"),
    (lambda t: t.replace("kind: symptom", "kind: thing"), "kind"),
    (lambda t: t.replace("status: candidate", "status: maybe"), "status"),
    (lambda t: t.replace("valid_from: 2025-02-24", "valid_from: 昨天"), "valid_from"),
])
def test_load_triples_rejects_bad_edges(tmp_path, mutate, needle):
    kb = _graph_kb(tmp_path, mutate(TRIPLES))
    _, findings = gf.load_triples(kb, case_ids=[CASE_ID])
    assert any(needle in m for lvl, m in findings if lvl == "error"), findings


def test_load_triples_flags_unknown_case_and_duplicates(tmp_path):
    kb = _graph_kb(tmp_path, TRIPLES + TRIPLES.split("- src: {kind: rootcause")[0])
    triples, findings = gf.load_triples(kb, case_ids=["other"])
    errors = [m for lvl, m in findings if lvl == "error"]
    assert any("不存在" in e for e in errors)
    assert any("重复" in e for e in errors)


def test_nodes_of_dedups_by_id(tmp_path):
    kb = _graph_kb(tmp_path)
    triples, _ = gf.load_triples(kb)
    nodes = gf.nodes_of(triples)
    assert set(nodes) == {"symptom:单条_update_偶发秒级", "rootcause:autovacuum_尾部回收持_8_级锁",
                          "action:表级调大_autovacuum_vacuum_threshold"}
    assert nodes["symptom:单条_update_偶发秒级"].name == "单条 update 偶发秒级"
