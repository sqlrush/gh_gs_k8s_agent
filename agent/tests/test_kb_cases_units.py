"""kb_cases —— 工单 ingest / propose / review(选择列表)/ apply 的确定性半边(无库)。

钉住的纪律:出处回指失败的项作废;边没有默认接受;apply 只认明确的编号;
用户接受的边 confidence=1.0,未答的边留 candidate;案例 ID 由脚本生成且不撞。
"""
import importlib.util
import json
import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_SCRIPTS = _ROOT / "skills" / "gaussdb-kb-import" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import kb_cases  # noqa: E402
from common.kb import cases as kbcases, graphfiles as gf  # noqa: E402

spec = importlib.util.spec_from_file_location("kb", _SCRIPTS / "kb.py")
kbmain = importlib.util.module_from_spec(spec)
sys.modules["kb"] = kbmain
spec.loader.exec_module(kbmain)

TICKET = """---
item_id: "ITSM-018823"
title: "偶现单条update慢"
source: "tickets.xlsx#row=2"
---
# 偶现单条update慢

- id: ITSM-018823
- system: CBST
- occurred_at: 2025-02-24

## 问题描述

业务偶现单条update走索引执行耗时3s。

## 处理过程

查 autovacuum 日志,发现表尾部空页回收持有8级锁,与DML互相cancel。

## 解决方案

针对cbst.cosp_asyn_task_dtl小表,调大autovacuum_vacuum_threshold减少vacuum频率。
"""


def _inbox(tmp_path):
    for sub in ("cases", "graph", "inbox/q1/items"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "inbox" / "q1" / "items" / "ITSM-018823.md").write_text(TICKET, encoding="utf-8")
    return tmp_path


def _candidate(**over):
    c = {
        "schema": 1, "item_id": "ITSM-018823",
        "case": {"title": "偶现单条update慢", "system": "CBST", "instance": "未知", "occurred_at": "2025-02-24",
                 "engine": "gaussdb", "severity": "S2", "primary_factor": "autovacuum 尾部回收持8级锁与DML互相cancel",
                 "secondary_factors": [], "conclusion": "已确认",
                 "objects": ["cbst.cosp_asyn_task_dtl", "autovacuum_vacuum_threshold"],
                 "signals": ["单条update偶发秒级", "autovacuum频繁触发"], "rules": [],
                 "sections": {"现场": "业务偶现单条update走索引执行耗时3s。",
                              "判断": "表尾部空页回收持有8级锁,与DML互相cancel。",
                              "处置": "针对小表调大autovacuum_vacuum_threshold。",
                              "复发标志": "单条update偶发3s且autovacuum次数异常高。"}},
        "quotes": {"现场": "业务偶现单条update走索引执行耗时3s",
                   "primary_factor": "表尾部空页回收持有8级锁,与DML互相cancel", "处置": "调大autovacuum_vacuum_threshold"},
        "entities": [{"kind": "object", "name": "cbst.cosp_asyn_task_dtl", "quote": "cbst.cosp_asyn_task_dtl"},
                     {"kind": "guc", "name": "autovacuum_vacuum_threshold", "quote": "autovacuum_vacuum_threshold"}],
        "edges": [{"src": {"kind": "symptom", "name": "单条update偶发秒级"}, "rel": "caused_by",
                   "dst": {"kind": "rootcause", "name": "autovacuum尾部回收持8级锁"}, "quote": "持有8级锁", "confidence": 0.8},
                  {"src": {"kind": "rootcause", "name": "autovacuum尾部回收持8级锁"}, "rel": "handled_by",
                   "dst": {"kind": "action", "name": "调大autovacuum_vacuum_threshold"}, "quote": "调大autovacuum_vacuum_threshold", "confidence": 0.9}],
    }
    c.update(over)
    return c


# ---------------------------------------------------------------- pure helpers

def test_quote_found_ignores_whitespace_but_not_content():
    assert kb_cases.quote_found("持有 8 级锁,与DML互相cancel", TICKET)
    assert not kb_cases.quote_found("持有9级锁", TICKET)
    assert not kb_cases.quote_found("", TICKET)


def test_case_id_for_is_deterministic_and_filename_safe():
    cid = kb_cases.case_id_for({"severity": "S2", "occurred_at": "2025-02-24", "system": "CBST", "title": "偶现 update/慢:3s"})
    assert cid == "S2-20250224-CBST-偶现update慢3s"
    assert kbcases.CASE_ID_RE.match(cid)


def test_parse_numbers():
    assert kb_cases.parse_numbers("1-3,5 8") == [1, 2, 3, 5, 8]
    assert kb_cases.parse_numbers("") == []


# ---------------------------------------------------------------- ingest routing

def test_ingest_routes_xlsx_to_tickets_and_md_to_spec(tmp_path, monkeypatch, capsys):
    kb = tmp_path / "kb"
    csv = tmp_path / "t.csv"
    csv.write_text("工单号,标题,问题描述\nA-1,分区表慢,exchange 后未 analyze\n", encoding="utf-8")
    rc = kbmain.main(["ingest", str(csv), "--kb", str(kb)])
    assert rc == 0
    assert (kb / "inbox" / "t" / "items" / "A-1.md").is_file()
    assert (kb / "inbox" / "t" / "manifest.json").is_file()
    assert (kb / "sources" / "t.csv").is_file()
    spec_md = tmp_path / "规范.md"
    spec_md.write_text("# 索引规范\n\n1. 索引列数不超过 4 列\n", encoding="utf-8")
    rc = kbmain.main(["ingest", str(spec_md), "--kb", str(kb)])
    assert rc == 0 and (kb / "inbox" / "规范" / "source.md").is_file()
    rc = kbmain.main(["ingest", str(spec_md), "--kb", str(kb), "--kind", "tickets", "--slug", "asmd"])
    assert rc == 0 and (kb / "inbox" / "asmd" / "items" / "规范.md").is_file()


def test_ingest_tickets_reimport_versions_snapshot(tmp_path):
    kb = _inbox(tmp_path)
    csv = tmp_path / "t.csv"
    csv.write_text("工单号,标题\nA-1,x\n", encoding="utf-8")
    kb_cases.ingest_tickets(kb, csv, None, False)
    kb_cases.ingest_tickets(kb, csv, None, False)
    assert (kb / "sources" / "t.csv").is_file() and (kb / "sources" / "t.v2.csv").is_file()


# ---------------------------------------------------------------- review

def test_review_builds_numbered_list_with_defaults(tmp_path):
    kb = _inbox(tmp_path)
    entries, errors = kb_cases.review_candidates(kb, "q1", [_candidate()])
    assert errors == []
    kinds = [e["kind"] for e in entries]
    assert kinds[0] == "case" and "field" in kinds and "entity" in kinds and "edge" in kinds
    assert [e["n"] for e in entries] == list(range(1, len(entries) + 1))
    edges = [e for e in entries if e["kind"] == "edge"]
    assert all(e["default"] == "ask" for e in edges) and all("无默认" in e["text"] for e in edges)
    assert entries[0]["case_id"] == "S2-20250224-CBST-偶现单条update慢"
    md = kb_cases.render_review("q1", entries, errors)
    assert "# 导入确认:q1(1 案例" in md and "  1. [案例]" in md and "全部接受(边除外)" in md


def test_review_rejects_quote_that_is_not_in_source(tmp_path):
    kb = _inbox(tmp_path)
    bad = _candidate(quotes={"primary_factor": "原文里没有这句话"})
    entries, errors = kb_cases.review_candidates(kb, "q1", [bad])
    assert any("出处回指失败" in e for e in errors) and entries == []


def test_review_entity_without_quote_defaults_to_reject(tmp_path):
    kb = _inbox(tmp_path)
    c = _candidate(entities=[{"kind": "object", "name": "幻觉表", "quote": "不存在的片段"}])
    entries, _ = kb_cases.review_candidates(kb, "q1", [c])
    ent = next(e for e in entries if e["kind"] == "entity")
    assert ent["default"] == "reject" and "未回指" in ent["text"]


def test_review_suggests_merge_with_similar_known_entity(tmp_path):
    kb = _inbox(tmp_path)
    (kb / "graph" / "canonical.yaml").write_text("object:cbst.cosp_asyn_task_dtl: [异步任务明细表]\n", encoding="utf-8")
    c = _candidate(entities=[{"kind": "object", "name": "cbst.cosp_asyn_task_dt", "quote": "cbst.cosp_asyn_task_dtl"}])
    entries, _ = kb_cases.review_candidates(kb, "q1", [c])
    merges = [e for e in entries if e["kind"] == "merge"]
    assert merges and merges[0]["payload"]["into"] == "object:cbst.cosp_asyn_task_dtl" and merges[0]["default"] == "ask"


def test_review_requires_scene_quote(tmp_path):
    kb = _inbox(tmp_path)
    c = _candidate(quotes={"primary_factor": "表尾部空页回收持有8级锁,与DML互相cancel"})   # 没有 现场 摘录
    entries, errors = kb_cases.review_candidates(kb, "q1", [c])
    assert any("quotes.现场" in e for e in errors) and entries == []


def test_review_confirmed_conclusion_needs_root_cause_quote_but_guess_does_not(tmp_path):
    kb = _inbox(tmp_path)
    scene_only = {"现场": "业务偶现单条update走索引执行耗时3s"}
    confirmed = _candidate(quotes=scene_only)                       # 已确认 却指不回根因
    _, errors = kb_cases.review_candidates(kb, "q1", [confirmed])
    assert any("primary_factor" in e and "已确认" in e for e in errors)
    guess = _candidate(quotes=scene_only)
    guess["case"] = {**guess["case"], "conclusion": "推测"}          # 原文没写根因:推测 不要求摘录
    entries, errors = kb_cases.review_candidates(kb, "q1", [guess])
    assert errors == [] and entries and entries[0]["kind"] == "case" and "推测" in entries[0]["text"]


def test_review_refuses_duplicate_case_id(tmp_path):
    kb = _inbox(tmp_path)
    cid = "S2-20250224-CBST-偶现单条update慢"
    (kb / "cases" / f"{cid}.md").write_text(f"---\nid: {cid}\ntitle: t\nsystem: CBST\noccurred_at: 2025-02-24\n"
                                             "conclusion: 已确认\nsource: s#x\n---\n## 现场\na\n## 判断\nb\n## 处置\nc\n",
                                             encoding="utf-8")
    _, errors = kb_cases.review_candidates(kb, "q1", [_candidate()])
    assert any("已存在" in e for e in errors)


# ---------------------------------------------------------------- apply

def _run_review_and_apply(kb, cand_list, apply_args):
    (kb / "inbox" / "q1" / "candidates.json").write_text(json.dumps(cand_list, ensure_ascii=False), encoding="utf-8")
    rc = kbmain.main(["review", "q1", "--kb", str(kb)])
    assert rc == 2                                  # 有待定项
    return kbmain.main(["apply", "q1", "--kb", str(kb)] + apply_args)


def test_apply_accepts_edges_only_when_told(tmp_path):
    kb = _inbox(tmp_path)
    rc = _run_review_and_apply(kb, [_candidate()], ["--all-but-edges", "--user", "12345"])
    assert rc == 0
    cid = "S2-20250224-CBST-偶现单条update慢"
    case_md = (kb / "cases" / f"{cid}.md").read_text(encoding="utf-8")
    case, errors = kbcases.parse_case(case_md, kb / "cases" / f"{cid}.md")
    assert errors == [] and case.confidence == 1.0 and case.entered_by == "12345"
    assert case.objects == ("cbst.cosp_asyn_task_dtl", "autovacuum_vacuum_threshold")
    triples, findings = gf.load_triples(kb, case_ids=[cid])
    rels = {(t.rel, t.status, t.confidence) for t in triples}
    assert ("exhibits", "accepted", 1.0) in rels
    assert ("caused_by", "candidate", 0.8) in rels          # 未答的边:候选,不进路径
    assert ("handled_by", "candidate", 0.9) in rels
    assert not (kb / "inbox" / "q1" / "items" / "ITSM-018823.md").exists()   # 处理完的工单移走
    assert not (kb / "inbox" / "q1" / "candidates.json").exists()


def test_apply_explicit_accept_makes_edge_confirmed_and_reject_drops(tmp_path):
    kb = _inbox(tmp_path)
    (kb / "inbox" / "q1" / "candidates.json").write_text(json.dumps([_candidate()], ensure_ascii=False), encoding="utf-8")
    kbmain.main(["review", "q1", "--kb", str(kb)])
    review = json.loads((kb / "inbox" / "q1" / "review.json").read_text(encoding="utf-8"))
    edge_ns = [e["n"] for e in review["entries"] if e["kind"] == "edge"]
    rc = kbmain.main(["apply", "q1", "--kb", str(kb), "--all-but-edges",
                      "--accept", str(edge_ns[0]), "--reject", str(edge_ns[1])])
    assert rc == 0
    triples, _ = gf.load_triples(kb)
    by_rel = {t.rel: t for t in triples if t.rel != "exhibits"}
    assert by_rel["caused_by"].status == "accepted" and by_rel["caused_by"].confidence == 1.0
    assert by_rel["handled_by"].status == "rejected"


def test_apply_edit_and_merge(tmp_path):
    kb = _inbox(tmp_path)
    (kb / "graph" / "canonical.yaml").write_text("object:cbst.cosp_asyn_task_dtl: [异步任务明细表]\n", encoding="utf-8")
    c = _candidate(entities=[{"kind": "object", "name": "cbst.cosp_asyn_task_dt", "quote": "cbst.cosp_asyn_task_dtl"}])
    (kb / "inbox" / "q1" / "candidates.json").write_text(json.dumps([c], ensure_ascii=False), encoding="utf-8")
    kbmain.main(["review", "q1", "--kb", str(kb)])
    review = json.loads((kb / "inbox" / "q1" / "review.json").read_text(encoding="utf-8"))
    sev_n = next(e["n"] for e in review["entries"] if e["kind"] == "field" and e["field"] == "severity")
    merge_n = next(e["n"] for e in review["entries"] if e["kind"] == "merge")
    rc = kbmain.main(["apply", "q1", "--kb", str(kb), "--all-but-edges", "--edit", f"{sev_n}:S1", "--accept", str(merge_n)])
    assert rc == 0
    cid = "S2-20250224-CBST-偶现单条update慢"       # ID 在 review 时定下,改级别不改 ID
    case, _ = kbcases.parse_case((kb / "cases" / f"{cid}.md").read_text(encoding="utf-8"), kb / "cases" / f"{cid}.md")
    assert case.severity == "S1"
    canon = yaml.safe_load((kb / "graph" / "canonical.yaml").read_text(encoding="utf-8"))
    assert "cbst.cosp_asyn_task_dt" in canon["object:cbst.cosp_asyn_task_dtl"]


def test_apply_refuses_when_review_has_errors(tmp_path):
    kb = _inbox(tmp_path)
    bad = _candidate(quotes={"primary_factor": "没有的话"})
    (kb / "inbox" / "q1" / "candidates.json").write_text(json.dumps([bad], ensure_ascii=False), encoding="utf-8")
    kbmain.main(["review", "q1", "--kb", str(kb)])
    assert kbmain.main(["apply", "q1", "--kb", str(kb), "--all-but-edges"]) == 1


# ---------------------------------------------------------------- propose

def test_propose_gate_asks_strategy_and_writes_no_worksheets(tmp_path, capsys):
    kb = _inbox(tmp_path)
    rc = kbmain.main(["propose", "q1", "--kb", str(kb)])
    assert rc == 2                                                  # 策略未确认:不出工作单,退出 2
    assert not (kb / "inbox" / "q1" / "work").exists()
    out = capsys.readouterr().out
    assert "首次导入工单" in out and "strategies/tickets.yaml" in out and "--use-defaults" in out
    # 问的是转化策略(因果链 / 关系 / 复发标志 / 合并 / 向量),不是元数据缺省值
    for key in ("因果链", "复发标志", "合并", "向量", "默认:"):
        assert key in out, key
    # 用户说「全按默认」:脚本代写 yaml,同一次调用就出工作单
    rc = kbmain.main(["propose", "q1", "--kb", str(kb), "--use-defaults"])
    assert rc == 0
    strat = yaml.safe_load((kb / "strategies" / "tickets.yaml").read_text(encoding="utf-8"))
    assert set(strat) == {q["key"] for q in kb_cases.STRATEGY_QUESTIONS} and strat["chain"] == "一单一条主链"
    ws = json.loads((kb / "inbox" / "q1" / "work" / "001.json").read_text(encoding="utf-8"))
    assert ws["item_id"] == "ITSM-018823" and "candidate_template" in ws and "known_entities" in ws
    assert ws["strategy"]["merge"] == "合并" and "只抽一条主链" in " ".join(ws["rules"])
    out2 = capsys.readouterr().out
    assert "首次导入工单" not in out2 and "转化策略" in out2


def test_propose_uses_user_written_strategy_and_warns_unknown_keys(tmp_path, capsys):
    kb = _inbox(tmp_path)
    (kb / "strategies").mkdir()
    (kb / "strategies" / "tickets.yaml").write_text(
        "target: 案例\nchain: 允许多条\nrelations: 案例→涉及对象\nsignals: 材料里的告警代码\nmerge: 各自新建\nvector: 只现场 + 复发标志\n"
        "typo_key: x\n",
        encoding="utf-8")
    assert kbmain.main(["propose", "q1", "--kb", str(kb)]) == 0
    out2 = capsys.readouterr().out
    assert "首次导入工单" not in out2 and "转化策略" in out2
    assert "不认识的 key:typo_key" in out2                           # 模型手写 yaml 写错 key 要能看见
    ws = json.loads((kb / "inbox" / "q1" / "work" / "001.json").read_text(encoding="utf-8"))
    assert ws["strategy"]["chain"] == "允许多条"
    rules = " ".join(ws["rules"])
    assert "允许多条因果链" in rules and "不抽 引用条款" in rules and "只取材料里的告警代码" in rules
    assert "各自新建" in rules and "向量只索引" in rules


def test_strategy_rules_defaults_are_single_chain_and_merge():
    rules = " ".join(kb_cases.strategy_rules({}))
    assert "只抽一条主链" in rules and "同义节点合并" in rules and "告警代码" in rules


def test_index_builds_files_and_the_sqlite_index_without_any_service(tmp_path, capsys):
    kb = _inbox(tmp_path)
    (kb / "rules").mkdir()
    rc = kbmain.main(["index", "--kb", str(kb)])
    assert rc == 0 and (kb / "INDEX.md").is_file() and (kb / "RULES.md").is_file()
    assert (kb / "index" / "store.sqlite").is_file() and "文档      :" in capsys.readouterr().out
