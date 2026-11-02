"""处置的「取代」(2026-11-02,user:同一个方案、策略变了就覆盖,要模型判断;覆盖 = 标记取代、保留历史)。

钉住的纪律:
  · 同一根因下已有别的处置,模型必须逐条判 取代 / 并存(漏判 review 报 error);处置一字不差的不用判;
  · 取代没有默认:用户不点名就不取代;谁取代谁按日期(补导旧工单时被取代的是旧工单自己);
  · 落盘只给旧处置边标 valid_to / superseded_by,旧案例文件不动;路径不再给被取代的处置,
    历史相似 / 专题页把它标「已被取代」并排到现行做法后面;
  · 「N 案例支持」按每条处置自己的案例算(以前三条处置都显示「3 案例支持」);
  · 现象 / 根因名与已有节点很像时出 [归一],相似的根因也要判取代 / 并存。
"""
import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_SCRIPTS = _ROOT / "skills" / "gaussdb-kb-import" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import kb_cases  # noqa: E402
from common.kb import graphfiles as gf, query, store_files as sf, supersede as kbsup  # noqa: E402
from common.kb import render as kbrender  # noqa: E402

if "kb" in sys.modules:
    kbmain = sys.modules["kb"]
else:
    _spec = importlib.util.spec_from_file_location("kb", _SCRIPTS / "kb.py")
    kbmain = importlib.util.module_from_spec(_spec)
    sys.modules["kb"] = kbmain
    _spec.loader.exec_module(kbmain)

SYMPTOM = "CPU使用率高"
ROOT = "并发活跃语句过多"
# 工单 id → (标题, 日期, 现场, 根因, 处理过程, 处置)
TICKETS = {
    "T-006": ("CPU 冲高 活跃语句堆积", "2025-06-10", "CPU 冲高到 96%,活跃语句 300 个", ROOT,
              "将 max_active_statements 调整为 100 后 CPU 回落", "将 max_active_statements 调整为 100", "max_active_statements"),
    "T-007": ("CPU 冲高 活跃语句再次堆积", "2025-10-20", "CPU 冲高到 95%,活跃语句 260 个", ROOT,
              "将 max_active_statements 调整为 50 后 CPU 回落", "将 max_active_statements 调整为 50", "max_active_statements"),
    "T-008": ("CPU 冲高 并行查询过多", "2025-10-25", "CPU 冲高到 94%,多条报表查询开了并行", ROOT,
              "将 query_dop 调整为 1 后 CPU 回落", "将 query_dop 调整为 1", "query_dop"),
}


def _cid(tid: str) -> str:
    title, date = TICKETS[tid][0], TICKETS[tid][1]
    return kb_cases.case_id_for({"severity": "S2", "occurred_at": date, "system": "CORE", "title": title})


def _ticket_md(tid: str) -> str:
    title, date, scene, cause, act, _a, _g = TICKETS[tid]
    return (f'---\nitem_id: "{tid}"\ntitle: "{title}"\nsource: "t.csv#{tid}"\n---\n# {title}\n\n'
            f"- id: {tid}\n- system: CORE\n- occurred_at: {date}\n\n## 问题描述\n\n{scene}\n\n"
            f"## 处理过程\n\n{act}\n\n## 根因\n\n{cause}\n")


def _cand(tid: str, judgments=None, symptom: str = SYMPTOM, root: str = ROOT, action: str = "") -> dict:
    title, date, scene, cause, act, act_name, guc = TICKETS[tid]
    action = action or act_name
    return {
        "schema": 1, "item_id": tid,
        "case": {"title": title, "system": "CORE", "instance": "未知", "occurred_at": date, "engine": "gaussdb",
                 "severity": "S2", "primary_factor": cause, "secondary_factors": [], "conclusion": "已确认",
                 "objects": [guc], "signals": [symptom, scene], "rules": [], "topics": ["param"],
                 "sections": {"现场": scene, "判断": cause, "处置": act, "复发标志": symptom}},
        "quotes": {"现场": scene, "primary_factor": cause, "处置": act},
        "entities": [{"kind": "guc", "name": guc, "quote": guc}],
        "edges": [{"src": {"kind": "symptom", "name": symptom}, "rel": "caused_by",
                   "dst": {"kind": "rootcause", "name": root}, "quote": cause, "confidence": 0.8},
                  {"src": {"kind": "rootcause", "name": root}, "rel": "handled_by",
                   "dst": {"kind": "action", "name": action}, "quote": act, "confidence": 0.8}],
        "judgments": list(judgments or []),
    }


def _j(prior: str, action: str, verdict: str, quote: str, reason: str = "同一个参数改了值") -> dict:
    return {"prior": prior, "action": action, "verdict": verdict, "reason": reason, "quote": quote}


def _kb(tmp_path) -> pathlib.Path:
    kb = tmp_path / "kb"
    for sub in ("cases", "graph"):
        (kb / sub).mkdir(parents=True, exist_ok=True)
    (kb / "kb.yaml").write_text("embeddings: {source: none}\n", encoding="utf-8")
    return kb


def _review(kb, slug, cands):
    items = kb / "inbox" / slug / "items"
    items.mkdir(parents=True, exist_ok=True)
    for c in cands:
        (items / f"{c['item_id']}.md").write_text(_ticket_md(c["item_id"]), encoding="utf-8")
    (kb / "inbox" / slug / "candidates.json").write_text(json.dumps(cands, ensure_ascii=False), encoding="utf-8")
    kb_cases.cmd_review(SimpleNamespace(slug=slug), kb)
    return json.loads((kb / "inbox" / slug / "review.json").read_text(encoding="utf-8"))


def _apply(kb, slug, review, accept="all", all_but_edges=False):
    spec = f"1-{max(e['n'] for e in review['entries'])}" if accept == "all" else accept
    kb_cases.cmd_apply(SimpleNamespace(slug=slug, accept=spec, reject=None, edit=None, all_but_edges=all_but_edges,
                                       decisions=None, user="tester"), kb)


def _import(kb, slug, cands, accept="all"):
    review = _review(kb, slug, cands)
    assert not review["errors"], review["errors"]
    _apply(kb, slug, review, accept)
    return review


def _index(kb):
    ns = SimpleNamespace(kb=str(kb), rebuild=False, fill_missing=False, q=None, from_findings=None, json=False)
    assert kbmain.cmd_index_all(ns) in (0, 2)


def _handled(kb):
    triples, _ = gf.load_triples(kb)
    return {(t.case_id, t.dst.name): t for t in triples if t.rel == "handled_by"}


def _kinds(review, kind):
    return [e for e in review["entries"] if e["kind"] == kind]


# ---------------------------------------------------------------- propose:工作单给模型看现行处置

def test_work_order_lists_current_handlings_and_anchor_nodes_first(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006")])
    items = kb / "inbox" / "b2" / "items"
    items.mkdir(parents=True)
    (items / "T-007.md").write_text(_ticket_md("T-007"), encoding="utf-8")
    kb_cases.cmd_propose(SimpleNamespace(slug="b2", offset=0, batch=8, use_defaults=True), kb)
    work = json.loads((kb / "inbox" / "b2" / "work" / "001.json").read_text(encoding="utf-8"))
    assert {"case": _cid("T-006"), "date": "2025-06-10", "rootcause": ROOT,
            "action": "将 max_active_statements 调整为 100"} in work["prior_handlings"]
    assert work["known_entities"][0].split(":")[0] in ("symptom", "rootcause")
    assert any("judgments" in r and "取代" in r for r in work["rules"])
    assert "judgments" in work["candidate_template"]


def test_known_entities_keep_every_symptom_and_rootcause_past_the_limit(tmp_path):
    kb = _kb(tmp_path)
    rows = [{"src": {"kind": "case", "name": f"c{i}"}, "rel": "involves", "dst": {"kind": "object", "name": f"t{i:03d}"},
             "confidence": 1.0, "source": "x", "status": "accepted"} for i in range(80)]
    rows.append({"src": {"kind": "symptom", "name": SYMPTOM}, "rel": "caused_by", "dst": {"kind": "rootcause", "name": ROOT},
                 "confidence": 1.0, "source": "x", "status": "accepted"})
    (kb / "graph" / "g.yaml").write_text(yaml.safe_dump(rows, allow_unicode=True), encoding="utf-8")
    known = kb_cases._known_entities(kb, limit=60)
    assert known[:2] == ["symptom:cpu使用率高", "rootcause:并发活跃语句过多"]
    assert len(known) == 60


# ---------------------------------------------------------------- review:漏判就拦,取代没有默认

def test_review_blocks_a_missing_judgment_on_the_same_rootcause(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006")])
    review = _review(kb, "b2", [_cand("T-007")])
    assert any("没判断取代还是并存" in e and "调整为 100" in e for e in review["errors"]), review["errors"]


def test_identical_action_needs_no_judgment(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006")])
    review = _review(kb, "b2", [_cand("T-007", action="将 max_active_statements 调整为 100")])
    assert not [e for e in review["errors"] if "没判断" in e]


def test_supersede_has_no_default_and_coexist_is_informational(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006")])
    old = "将 max_active_statements 调整为 100"
    review = _review(kb, "b2", [_cand("T-007", [_j(_cid("T-006"), old, "取代", "调整为 50")])])
    sup = _kinds(review, "supersede")
    assert len(sup) == 1 and sup[0]["default"] == "ask"
    late, early = sup[0]["text"].split(" 取代 ")
    assert "调整为 50" in late and "调整为 100" in early
    review = _review(kb, "b3", [_cand("T-008", [_j(_cid("T-006"), old, "并存", "query_dop", "另一个参数")])])
    assert not review["errors"] and _kinds(review, "coexist") and not _kinds(review, "supersede")


def test_bad_judgments_are_errors(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006")])
    old = "将 max_active_statements 调整为 100"
    cases = {
        "不存在": [_j("S2-NOPE", old, "取代", "调整为 50")],
        "verdict": [_j(_cid("T-006"), old, "覆盖", "调整为 50")],
        "摘录": [_j(_cid("T-006"), old, "取代", "原文里没有这句")],
        "理由": [_j(_cid("T-006"), old, "取代", "调整为 50", reason="")],
    }
    for must, js in cases.items():
        review = _review(kb, f"bad-{len(must)}-{must}", [_cand("T-007", js)])
        assert any(must in e for e in review["errors"]), (must, review["errors"])


def test_all_but_edges_does_not_accept_a_supersede(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006")])
    review = _review(kb, "b2", [_cand("T-007", [_j(_cid("T-006"), "将 max_active_statements 调整为 100", "取代", "调整为 50")])])
    edges = ",".join(str(e["n"]) for e in _kinds(review, "edge"))
    _apply(kb, "b2", review, accept=edges, all_but_edges=True)
    assert not _handled(kb)[(_cid("T-006"), "将 max_active_statements 调整为 100")].superseded_by


# ---------------------------------------------------------------- apply → index → query 端到端

def _scenario(tmp_path):
    """6 月调 100;10-20 改 50(取代);10-25 换 query_dop(与 50 并存)。"""
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006")])
    _import(kb, "b2", [_cand("T-007", [_j(_cid("T-006"), "将 max_active_statements 调整为 100", "取代", "调整为 50")])])
    _import(kb, "b3", [_cand("T-008", [_j(_cid("T-007"), "将 max_active_statements 调整为 50", "并存", "query_dop", "另一个参数")])])
    _index(kb)
    return kb


def test_apply_marks_the_old_edge_and_keeps_the_old_case(tmp_path):
    kb = _scenario(tmp_path)
    h = _handled(kb)
    old = h[(_cid("T-006"), "将 max_active_statements 调整为 100")]
    assert old.superseded_by == _cid("T-007") and old.valid_to == "2025-10-20"
    assert not h[(_cid("T-007"), "将 max_active_statements 调整为 50")].superseded_by
    assert not h[(_cid("T-008"), "将 query_dop 调整为 1")].superseded_by
    assert (kb / "cases" / f"{_cid('T-006')}.md").is_file()
    assert "superseded" not in (kb / "cases" / f"{_cid('T-006')}.md").read_text(encoding="utf-8")


def test_query_drops_the_superseded_path_and_counts_support_per_action(tmp_path):
    kb = _scenario(tmp_path)
    item = query.from_text(SYMPTOM, kb_dir=kb).items[0]
    actions = {p.action: p.cases for p in item.paths}
    assert "将 max_active_statements 调整为 100" not in actions
    assert actions.get("将 max_active_statements 调整为 50") == (_cid("T-007"),)
    assert actions.get("将 query_dop 调整为 1") == (_cid("T-008"),)


def test_query_ranks_the_superseded_case_last_and_says_so(tmp_path):
    kb = _scenario(tmp_path)
    res = query.from_text(SYMPTOM, kb_dir=kb)
    ids = [c.short_id for c in res.items[0].cases]
    assert ids[-1] == _cid("T-006"), ids
    text = kbrender.render_query(res)
    line = next(l for l in text.splitlines() if _cid("T-006") in l and "历史相似" in l)
    assert f"已被 {_cid('T-007')} 取代" in line
    js = query.result_to_dict(res)["items"][0]["cases"]
    assert next(c for c in js if c["id"].endswith(_cid("T-006")))["superseded"][0]["by"] == _cid("T-007")


def test_topic_page_marks_and_demotes_the_superseded_case(tmp_path):
    kb = _scenario(tmp_path)
    page = (kb / "topics" / "param.md").read_text(encoding="utf-8")
    lines = [l for l in page.splitlines() if l.startswith("- 〔案例")]
    assert _cid("T-006") in lines[-1] and "已被" in lines[-1], lines
    assert all("已被" not in l for l in lines[:-1])


def test_same_batch_later_ticket_supersedes_the_earlier_one(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006"),
                       _cand("T-007", [_j("T-006", "将 max_active_statements 调整为 100", "取代", "调整为 50")])])
    old = _handled(kb)[(_cid("T-006"), "将 max_active_statements 调整为 100")]
    assert old.superseded_by == _cid("T-007")


def test_same_batch_earlier_ticket_is_not_asked_to_judge_the_later_one(tmp_path):
    kb = _kb(tmp_path)
    review = _review(kb, "b1", [_cand("T-006"), _cand("T-007")])
    errs = [e for e in review["errors"] if "没判断" in e]
    assert len(errs) == 1 and errs[0].startswith("候选 T-007"), errs


def test_backfilled_older_ticket_is_the_one_superseded(tmp_path):
    """先有 10 月的 50,后补导 6 月的 100:按日期,被取代的是补导进来的那单。"""
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-007")])
    review = _import(kb, "b2", [_cand("T-006", [_j(_cid("T-007"), "将 max_active_statements 调整为 50", "取代", "调整为 100")])])
    assert "调整为 50" in _kinds(review, "supersede")[0]["text"].split(" 取代 ")[0]
    h = _handled(kb)
    assert h[(_cid("T-006"), "将 max_active_statements 调整为 100")].superseded_by == _cid("T-007")
    assert not h[(_cid("T-007"), "将 max_active_statements 调整为 50")].superseded_by


# ---------------------------------------------------------------- 归一:现象 / 根因名

def test_near_identical_symptom_name_gets_a_merge_item(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006")])
    review = _review(kb, "b2", [_cand("T-008", [_j(_cid("T-006"), "将 max_active_statements 调整为 100", "并存", "query_dop")],
                                      symptom="CPU使用率过高")])
    merges = _kinds(review, "merge")
    assert any(m["payload"]["into"] == "symptom:cpu使用率高" for m in merges), merges


def test_similar_rootcause_also_requires_a_judgment(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-006")])
    review = _review(kb, "b2", [_cand("T-007", root="并发活跃语句太多")])
    assert any(m["payload"]["into"] == "rootcause:并发活跃语句过多" for m in _kinds(review, "merge"))
    assert any("没判断取代还是并存" in e for e in review["errors"]), review["errors"]


def test_entity_merge_only_compares_the_same_kind(tmp_path):
    kb = _kb(tmp_path)
    _import(kb, "b1", [_cand("T-007")])
    review = _review(kb, "b2", [_cand("T-006", [_j(_cid("T-007"), "将 max_active_statements 调整为 50", "取代", "调整为 100")])])
    assert not [m for m in _kinds(review, "merge") if m["payload"]["into"].startswith("action:")]


# ---------------------------------------------------------------- 纯函数与 validate

def test_chain_cases_intersects_and_falls_back_to_union():
    assert sf.chain_cases(["a", "b", "c"], ["b"]) == ["b"]
    assert sf.chain_cases(["a"], []) == ["a"]
    assert sf.chain_cases(["a"], ["x"]) == ["a", "x"]


def test_validate_flags_bad_superseded_by_and_lookalike_nodes(tmp_path):
    kb = _kb(tmp_path)
    rows = [{"src": {"kind": "rootcause", "name": ROOT}, "rel": "handled_by", "dst": {"kind": "action", "name": "调参"},
             "confidence": 1.0, "source": "x", "case": "C1", "superseded_by": "C-NOPE", "valid_to": "2025-01-01"},
            {"src": {"kind": "rootcause", "name": ROOT}, "rel": "handled_by", "dst": {"kind": "action", "name": "限流"},
             "confidence": 1.0, "source": "y", "case": "C1", "superseded_by": "C2"},
            {"src": {"kind": "symptom", "name": "CPU使用率高"}, "rel": "caused_by", "dst": {"kind": "rootcause", "name": ROOT},
             "confidence": 1.0, "source": "z", "case": "C1"},
            {"src": {"kind": "symptom", "name": "CPU使用率过高"}, "rel": "caused_by", "dst": {"kind": "rootcause", "name": ROOT},
             "confidence": 1.0, "source": "w", "case": "C2"}]
    (kb / "graph" / "g.yaml").write_text(yaml.safe_dump(rows, allow_unicode=True), encoding="utf-8")
    triples, _ = gf.load_triples(kb)
    found = kbsup.graph_findings(triples, {"C1", "C2"})
    assert any(lvl == "error" and "C-NOPE" in msg for lvl, msg in found)
    assert any(lvl == "warn" and "没写 valid_to" in msg for lvl, msg in found)
    assert any(lvl == "warn" and "CPU使用率高" in msg and "CPU使用率过高" in msg for lvl, msg in found)
