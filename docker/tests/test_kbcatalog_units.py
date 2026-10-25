"""知识库目录(大盘知识条目 / 案例 / 条款 / 关系图页签的数据)。

最要紧的是**只端该端的**:收件目录里未审核的材料、客户原件、已废止条款、候选边都不能上大盘。
"""
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import kbcatalog  # noqa: E402

CID = "S2-20250908-CBST-灌数未ANALYZE计划跳变致CPU打满"


def _kb(tmp_path):
    kb = tmp_path / "kb"
    (kb / "cases").mkdir(parents=True)
    (kb / "cases" / (CID + ".md")).write_text(f"""---
id: {CID}
title: 灌数未 ANALYZE 计划跳变致 CPU 打满
system: CBST
occurred_at: '2025-09-08'
conclusion: 已确认
source: 工单导出-2025Q3.csv#row=3
severity: S2
primary_factor: 灌数后统计信息失真
rules: [GS-OPS-001]
---
## 现场
CPU 占 DB time 84%
## 判断
灌数后未 ANALYZE
## 处置
补 ANALYZE
""", encoding="utf-8")
    (kb / "rules").mkdir()
    (kb / "rules" / "ops.yaml").write_text("""# 运维操作规范
- id: GS-OPS-001
  severity: error
  rule: 批量灌数之后必须显式 ANALYZE
  keywords: [ANALYZE, 统计信息]
  source: 运维规范 §5.3
- id: GS-OPS-009
  severity: warn
  status: deprecated
  rule: 已废止的条款
""", encoding="utf-8")
    (kb / "graph").mkdir()
    (kb / "graph" / "canonical.yaml").write_text("symptom:dbtime_cpu_heavy:\n- CPU 占 DB time 84% 以上\n- DBTIME_CPU_HEAVY\n",
                                                  encoding="utf-8")
    (kb / "graph" / "t.yaml").write_text(f"""- src: {{kind: case, name: 灌数未 ANALYZE, canonical: 'case:{CID}'}}
  rel: exhibits
  dst: {{kind: symptom, name: DBTIME_CPU_HEAVY}}
  confidence: 1.0
  status: accepted
  source: cases/{CID}.md#现场
  case: {CID}
- src: {{kind: symptom, name: CPU 占 DB time 84% 以上}}
  rel: caused_by
  dst: {{kind: rootcause, name: 统计信息失真}}
  confidence: 1.0
  status: accepted
  source: cases/{CID}.md#判断
  case: {CID}
- src: {{kind: rootcause, name: 统计信息失真}}
  rel: handled_by
  dst: {{kind: action, name: 候选处置}}
  confidence: 0.6
  status: candidate
  source: cases/{CID}.md#处置
  case: {CID}
""", encoding="utf-8")
    # 这些都不许出现在目录里
    (kb / "inbox" / "x").mkdir(parents=True)
    (kb / "inbox" / "x" / "未审核.md").write_text("未审核的工单 SECRET-INBOX", encoding="utf-8")
    (kb / "sources").mkdir()
    (kb / "sources" / "客户原件.md").write_text("客户原件 SECRET-SOURCE", encoding="utf-8")
    return kb


def test_catalog_has_cases_active_rules_and_accepted_edges(tmp_path):
    cat = kbcatalog.build(_kb(tmp_path))
    assert cat["attached"] is True
    assert [c["id"] for c in cat["cases"]] == [CID]
    c = cat["cases"][0]
    assert c["sections"]["现场"] == "CPU 占 DB time 84%" and c["rules"] == ["GS-OPS-001"]
    assert [r["id"] for g in cat["groups"] for r in g["rules"]] == ["GS-OPS-001"], "已废止条款不进目录"
    assert cat["groups"][0]["title"] == "运维操作规范"
    assert all(e["rel"] != "handled_by" for e in cat["edges"]), "候选边不上大盘"
    assert len(cat["edges"]) == 2


def test_same_symptom_under_two_names_is_one_node_with_the_readable_name(tmp_path):
    """同一现象在案例边里叫 DBTIME_CPU_HEAVY、在因果边里叫原话 —— 必须是同一个节点,显示原话。"""
    cat = kbcatalog.build(_kb(tmp_path))
    ids = {e["dst"]["id"] for e in cat["edges"] if e["rel"] == "exhibits"} & {e["src"]["id"] for e in cat["edges"] if e["rel"] == "caused_by"}
    assert ids == {"symptom:dbtime_cpu_heavy"}
    labels = {e["dst"]["label"] for e in cat["edges"] if e["rel"] == "exhibits"}
    assert labels == {"CPU 占 DB time 84% 以上"}


def test_inbox_and_sources_never_leak(tmp_path):
    text = json.dumps(kbcatalog.build(_kb(tmp_path)), ensure_ascii=False)
    assert "SECRET-INBOX" not in text and "SECRET-SOURCE" not in text


def test_missing_kb_is_not_an_error(tmp_path):
    cat = kbcatalog.build(tmp_path / "nope")
    assert cat["attached"] is False and cat["cases"] == []


def test_cache_rebuilds_when_a_file_changes(tmp_path):
    import os
    kb = _kb(tmp_path)
    c = kbcatalog.Cached(kb)
    first = c.get()
    assert c.get() is first, "没变就用缓存"
    p = kb / "rules" / "ops.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("必须显式 ANALYZE", "必须显式 ANALYZE 相关表"), encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    assert "相关表" in c.get()["groups"][0]["rules"][0]["rule"]


# ---------------------------------------------------------------- 八类知识的类型与专题标签(2026-10-24)

def _typed(kb):
    (kb / "runbooks").mkdir()
    (kb / "runbooks" / "RB-LCK-001.md").write_text("""---
id: RB-LCK-001
type: runbook
title: 终止阻塞根会话
topics: [lock]
source: 运维手册 §4.2
verification: 已验证
approval: [GS-OPS-001]
applies: {product: GaussDB 集中式, versions: '>= 503'}
---
## 摘要
只终止根会话。
## 回退步骤
无需回退。
""", encoding="utf-8")
    (kb / "profiles").mkdir()
    (kb / "profiles" / "EP-CBST.md").write_text("---\nid: EP-CBST\ntype: profile\ntitle: CBST 画像\nsystem: CBST\n"
                                                 "version: 505.2.0\naliases: [og5]\n---\n## 摘要\n核心账务。\n", encoding="utf-8")
    (kb / "wiki").mkdir()
    (kb / "wiki" / "WK-0001.md").write_text("---\nid: WK-0001\ntype: wiki\ntitle: 坏条目\n---\n没有摘要也没有出处\n", encoding="utf-8")
    return kb


def test_typed_items_carry_type_topics_and_facts(tmp_path):
    cat = kbcatalog.build(_typed(_kb(tmp_path)))
    by = {i["id"]: i for i in cat["items"]}
    assert set(by) == {"RB-LCK-001", "EP-CBST"}, "校验不过的条目(WK-0001 缺摘要 / 出处)不上大盘,与检索同一口径"
    rb = by["RB-LCK-001"]
    assert rb["type"] == "runbook" and rb["topics"] == ["lock"] and rb["verification"] == "已验证"
    assert rb["applies"] == {"product": "GaussDB 集中式", "versions": ">= 503"}
    assert rb["facts"]["approval"] == ["GS-OPS-001"] and rb["sections"]["回退步骤"] == "无需回退。"
    assert by["EP-CBST"]["facts"]["aliases"] == ["og5"] and by["EP-CBST"]["file"] == "profiles/EP-CBST.md"


def test_type_counts_topics_and_tags_on_cases_and_rules(tmp_path):
    cat = kbcatalog.build(_typed(_kb(tmp_path)))
    assert cat["types"] == {"norm": 1, "case": 1, "runbook": 1, "profile": 1}, "已废止条款不计数"
    slugs = {t["slug"]: t for t in cat["topics"]}
    assert slugs["lock"]["name"] and slugs["lock"]["count"] >= 1
    assert all(t["count"] for t in cat["topics"]), "没有条目的专题不列"
    assert isinstance(cat["cases"][0]["topics"], list) and "topics" in cat["groups"][0]["rules"][0]


def test_typed_dirs_feed_the_cache_key_but_inbox_does_not(tmp_path):
    import os
    kb = _typed(_kb(tmp_path))
    c = kbcatalog.Cached(kb)
    first = c.get()
    p = kb / "runbooks" / "RB-LCK-001.md"
    p.write_text(p.read_text(encoding="utf-8").replace("只终止根会话", "只终止阻塞链的根会话"), encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    second = c.get()
    assert second is not first and "阻塞链" in {i["id"]: i for i in second["items"]}["RB-LCK-001"]["summary"]
    assert "inbox" not in kbcatalog.READ_DIRS and "sources" not in kbcatalog.READ_DIRS


def test_missing_kb_has_empty_typed_fields(tmp_path):
    cat = kbcatalog.build(tmp_path / "nope")
    assert cat["items"] == [] and cat["types"] == {} and cat["topics"] == []
