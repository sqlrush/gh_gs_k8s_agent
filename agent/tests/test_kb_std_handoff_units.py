"""标准化(gaussdb-kb-init)→ 导入(gaussdb-kb-import)的交接。

kb-init 已经把时间 / 系统 / 出处定下来了,导入这一侧必须**认得**这些已定字段,否则:
  1. 案例的 `source` 会指向我们的中间文件,而不是客户原件——出处回指链断在自己手里(加 kb-init 引入的回退);
  2. 原始工单号(客户拿它跟 ITSM 交叉引用)被换成 `<批次>-N`;
  3. 模型在候选里把六要素**再抽一遍**,与标准化文档不一致时没有任何东西核对——静默漂移。
非 std 的材料必须一字不变地走老路(这里有回归守卫)。
"""
import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_SCRIPTS = _ROOT / "skills" / "gaussdb-kb-import" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import kb_cases  # noqa: E402
from common.kb import ingest as kbingest, initstd  # noqa: E402

spec = importlib.util.spec_from_file_location("kb", _SCRIPTS / "kb.py")
kbmain = importlib.util.module_from_spec(spec)
sys.modules["kb"] = kbmain
spec.loader.exec_module(kbmain)

DRAFT = {
    "item_id": "ITSM-2025-000910", "title": "高并发批量开户 insert 等待序列取号",
    "occurred_at": "2025-09-10", "system": "CBST", "severity": "S2", "conclusion": "已确认",
    "objects": ["cbst.seq_acct_no"], "signals": ["insert 等待集中在序列取号"],
    "现场": "批量开户高峰期 insert 集中等待，平均耗时从 5ms 涨到 80ms。",
    "判断": "cbst.seq_acct_no 的 CACHE 为 1，每次取号都要回写系统表，高并发下串行化。",
    "处置": "将 cbst.seq_acct_no 的 CACHE 改为 1000（本行口径不低于 500）。",
    "复发标志": "insert 等待集中在序列取号，且该序列 CACHE=1。",
}
# kb-init 写的出处指向**客户原件**
META = {"source": "工单导出-2025Q3.csv#row=2", "source_file": "工单导出-2025Q3.csv",
        "source_kind": "csv", "extracted_by": "script"}


def _std_file(tmp_path, pairs=None):
    text = initstd.render_ingest_file(pairs or [(DRAFT, META)])
    path = tmp_path / "示例案例-tickets.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 拆单认出 std 元数据

def test_split_lifts_the_original_ticket_id_and_source_from_a_std_block(tmp_path):
    items = kbingest.split_text_items(_std_file(tmp_path).read_text(encoding="utf-8"),
                                      "示例案例-tickets.md", "示例案例-tickets")
    assert len(items) == 1
    it = items[0]
    assert it.id == "ITSM-2025-000910"                       # 不是 示例案例-tickets
    assert it.locator == "工单导出-2025Q3.csv#row=2"          # 不是我们的中间文件
    assert it.fields["system"] == "CBST" and it.fields["occurred_at"] == "2025-09-10"
    assert it.fields["severity"] == "S2" and it.fields["conclusion"] == "已确认"
    assert it.fields["ingested_from"] == "示例案例-tickets.md#item=1"   # 中间环节仍可追


def test_split_keeps_each_block_distinct_when_several_std_tickets_share_a_file(tmp_path):
    second = {**DRAFT, "item_id": "ITSM-2025-000911", "title": "另一单"}
    path = _std_file(tmp_path, [(DRAFT, META), (second, {**META, "source": "工单导出-2025Q3.csv#row=3"})])
    items = kbingest.split_text_items(path.read_text(encoding="utf-8"), path.name, path.stem)
    assert [it.id for it in items] == ["ITSM-2025-000910", "ITSM-2025-000911"]
    assert [it.locator for it in items] == ["工单导出-2025Q3.csv#row=2", "工单导出-2025Q3.csv#row=3"]


def test_non_std_markdown_is_split_exactly_as_before(tmp_path):
    """回归守卫:客户直接丢进来的普通 md 不能受影响。"""
    text = "# 第一单\n\n现象 A。\n\n---\n\n# 第二单\n\n现象 B。\n"
    items = kbingest.split_text_items(text, "raw.md", "raw")
    assert [it.id for it in items] == ["raw-1", "raw-2"]
    assert [it.locator for it in items] == ["raw.md#item=1", "raw.md#item=2"]
    assert all(not it.fields for it in items)
    one = kbingest.split_text_items("# 只有一单\n\n现象。\n", "raw.md", "raw")
    assert one[0].id == "raw" and one[0].locator == "raw.md"


def test_a_block_missing_the_required_std_keys_is_not_treated_as_std(tmp_path):
    """只有 `- id:` 没有时间 / 系统的,是普通 md,不能按 std 认——认错了出处就被这一行顶替。"""
    text = "# 某单\n\n- id: X-1\n\n## 现场\n\n现象。\n"
    it = kbingest.split_text_items(text, "raw.md", "raw")[0]
    assert it.id == "raw" and it.locator == "raw.md"


# ---------------------------------------------------------------- 落盘:出处指回客户原件

def test_written_item_cites_the_customers_file_not_our_intermediate_one(tmp_path):
    items = kbingest.split_text_items(_std_file(tmp_path).read_text(encoding="utf-8"),
                                      "示例案例-tickets.md", "示例案例-tickets")
    paths = kbingest.write_items(tmp_path / "items", items, "示例案例-tickets.md")
    assert paths[0].name == "ITSM-2025-000910.md"
    meta, _, _ = __import__("common.kb.cases", fromlist=["x"]).split_frontmatter(
        paths[0].read_text(encoding="utf-8"))
    assert meta["source"] == "工单导出-2025Q3.csv#row=2"
    assert meta["ingested_from"] == "示例案例-tickets.md#item=1"
    assert meta["system"] == "CBST" and meta["occurred_at"] == "2025-09-10"
    # 这一条就是缺陷本身:案例的 source 取自 item frontmatter
    assert kb_cases._source_of(paths[0]) == "工单导出-2025Q3.csv#row=2"


# ---------------------------------------------------------------- propose / review

def _kb(tmp_path):
    kb = tmp_path / "kb"
    for sub in ("cases", "graph", "rules", "inbox"):
        (kb / sub).mkdir(parents=True, exist_ok=True)
    (kb / "VERSION").write_text("2026.09\n", encoding="utf-8")
    return kb


def _ingested(tmp_path):
    kb = _kb(tmp_path)
    src = _std_file(tmp_path)
    assert kbmain.main(["ingest", str(src), "--kind", "tickets", "--kb", str(kb)]) == 0
    return kb, "示例案例-tickets"


def test_propose_hands_the_model_the_fields_kb_init_already_settled(tmp_path):
    kb, slug = _ingested(tmp_path)
    assert kbmain.main(["propose", slug, "--use-defaults", "--kb", str(kb)]) == 0
    work = json.loads((kb / "inbox" / slug / "work" / "001.json").read_text(encoding="utf-8"))
    assert work["item_id"] == "ITSM-2025-000910"
    known = work["known"]
    assert known["system"] == "CBST" and known["occurred_at"] == "2025-09-10"
    assert known["severity"] == "S2" and known["conclusion"] == "已确认"
    assert any("known" in r for r in work["rules"])            # 规则里说清「以 known 为准」


def _candidate(**case_over):
    case = {"title": DRAFT["title"], "system": "CBST", "instance": "未知", "occurred_at": "2025-09-10",
            "engine": "gaussdb", "severity": "S2", "primary_factor": "CACHE 为 1 导致取号串行化",
            "secondary_factors": [], "conclusion": "已确认", "objects": ["cbst.seq_acct_no"],
            "signals": ["insert 等待集中在序列取号"], "rules": [],
            "sections": {"现场": DRAFT["现场"], "判断": DRAFT["判断"],
                         "处置": DRAFT["处置"], "复发标志": DRAFT["复发标志"]}}
    case.update(case_over)
    return {"schema": 1, "item_id": "ITSM-2025-000910", "case": case,
            "quotes": {"现场": "批量开户高峰期 insert 集中等待",
                       "primary_factor": "每次取号都要回写系统表，高并发下串行化",
                       "处置": "CACHE 改为 1000"},
            "entities": [], "edges": []}


def test_review_passes_when_the_candidate_agrees_with_the_standardized_doc(tmp_path):
    kb, slug = _ingested(tmp_path)
    _, errors = kb_cases.review_candidates(kb, slug, [_candidate()])
    assert errors == [], errors


@pytest.mark.parametrize("field, bad, shown", [
    ("occurred_at", "2025-09-11", "2025-09-10"),
    ("system", "CBMS", "CBST"),
    ("severity", "S1", "S2"),
    ("conclusion", "推测", "已确认"),
])
def test_review_flags_a_candidate_that_drifts_from_the_standardized_doc(tmp_path, field, bad, shown):
    """两边说法不一必须当场点名——静默采用候选那一版,知识库与标准化文档就此分叉。"""
    kb, slug = _ingested(tmp_path)
    _, errors = kb_cases.review_candidates(kb, slug, [_candidate(**{field: bad})])
    hit = [e for e in errors if field in e]
    assert hit, errors
    assert bad in hit[0] and shown in hit[0]                   # 两个值都要写出来,让用户裁决


def test_review_does_not_invent_a_check_for_materials_without_std_metadata(tmp_path):
    """普通 md 没有已定字段,不能因此报错。"""
    kb = _kb(tmp_path)
    src = tmp_path / "raw.md"
    src.write_text("# 某单\n\n## 现场\n\n业务反馈慢。\n", encoding="utf-8")
    assert kbmain.main(["ingest", str(src), "--kind", "tickets", "--kb", str(kb)]) == 0
    cand = _candidate()
    cand["item_id"] = "raw"
    _, errors = kb_cases.review_candidates(kb, "raw", [cand])
    assert not any("标准化" in e for e in errors), errors


# ---------------------------------------------------------------- 两侧的取值口径一致

def test_std_format_accepts_every_conclusion_the_import_side_allows():
    """kb-init 只让写两种、导入侧认三种,模型在候选里改成第三种就会触发漂移报错——口径必须一致。"""
    from common.kb import cases as kbcases
    assert set(initstd.CONCLUSIONS) == set(kbcases.CONCLUSION_CONFIDENCE)
    for value in kbcases.CONCLUSION_CONFIDENCE:
        text = initstd.render_std({**DRAFT, "conclusion": value}, META)
        assert initstd.validate_std(text) == []


# ---------------------------------------------------------------- 没有工单号(2026-10-24 测试环境)

def _no_id(title, date, system):
    return {**DRAFT, "item_id": "", "title": title, "occurred_at": date, "system": system}


def test_reports_without_ticket_ids_are_not_collapsed_into_one(tmp_path):
    """三份问题分析报告都没有工单号,标准化后都是 unknown;导入时曾经三单写进同一个 unknown.md,前两单无声丢失。"""
    pairs = [(_no_id("PAYS 主键插入排队", "2026-04-16", "F-PAYS"), META),
             (_no_id("GOLD 咨询锁未释放", "2026-03-09", "F-GOLD"), META),
             (_no_id("LOAN 外键父子表互等", "2026-02-27", "F-LOAN"), META)]
    items = kbingest.split_text_items(_std_file(tmp_path, pairs).read_text(encoding="utf-8"), "x-tickets.md", "x-tickets")
    assert len({it.id for it in items}) == 3 and not any(it.id == "unknown" for it in items)
    assert items[0].id == "2026-04-16-PAYS 主键插入排队"
    written = kbingest.write_items(tmp_path / "items", items, "x-tickets.md")
    assert len({p.name for p in written}) == 3


def test_same_ids_in_one_batch_get_suffixes_instead_of_overwriting(tmp_path):
    same = [kbingest.Item(id="T-1", title="a", text="a\n", locator="x#1"), kbingest.Item(id="T-1", title="b", text="b\n", locator="x#2")]
    written = kbingest.write_items(tmp_path / "items", same, "x")
    assert [p.name for p in written] == ["T-1.md", "T-1-2.md"]
    assert 'item_id: "T-1-2"' in written[1].read_text(encoding="utf-8"), "item_id 跟着改,review 才对得上"


def test_a_single_std_document_imports_as_one_ticket(tmp_path):
    """std/*.md 带 YAML frontmatter:整篇一单,已定字段照认,不能按 --- 把 frontmatter 切成几单。"""
    std = initstd.render_std(_no_id("PAYS 主键插入排队", "2026-04-16", "F-PAYS"), META)
    items = kbingest.split_text_items(std, "2026-04-16-pays.md", "2026-04-16-pays")
    assert len(items) == 1
    it = items[0]
    assert it.id == "2026-04-16-PAYS 主键插入排队" and it.fields["system"] == "F-PAYS" and it.fields["_std"] == "1"
    assert it.locator == "工单导出-2025Q3.csv#row=2" and "## 现场" in it.text and not it.text.startswith("---")
