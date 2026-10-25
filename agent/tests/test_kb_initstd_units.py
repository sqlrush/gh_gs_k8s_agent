"""common.kb.initstd —— 入库前的标准化格式:各种工单材料 → 统一的六要素 md。

这一层是**格式**的确定性半边:分类文件、日期归一、渲染、校验。语义(六要素写什么)是模型的活。
钉住的三件事:格式必须逐字统一(否则这个 skill 没有意义)、六要素缺一不可、
「原文没写」必须显式写出来而不是空着(空着 = 静默编造的入口)。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import initstd  # noqa: E402

DRAFT = {
    "item_id": "ITSM-2026-000108",
    "title": "跑批期间 CPU 占用 80% 以上",
    "occurred_at": "2026年01月08日",
    "system": "F-CIIS",
    "severity": "S2",
    "conclusion": "已确认",
    "现场": "2026年01月08日业务执行跑批过程中 CPU 占用 80% 以上。",
    "判断": "库中存在执行 20 分钟左右的长事务，死行无法回收，可见性判断吃 CPU。",
    "处置": "应急查杀长事务语句；降低跑批 QPS 并发量。",
    "复发标志": "跑批时段 CPU > 80% 且存在 20 分钟以上长事务。",
}
META = {"source": "【2026-01-08】某行CIIS CPU使用率高问题分析报告.docx#p1",
        "source_file": "【2026-01-08】某行CIIS CPU使用率高问题分析报告.docx",
        "source_kind": "docx", "extracted_by": "script"}


# ---------------------------------------------------------------- 文件分类

@pytest.mark.parametrize("name, kind", [
    ("工单.txt", "text"), ("工单.md", "text"),
    ("报告.docx", "doc"), ("报告.doc", "doc"), ("报告.pdf", "doc"),
    ("导出.csv", "table"), ("导出.xlsx", "table"),
    ("IMG_2357.HEIC", "image"), ("截图.png", "image"), ("照片.jpg", "image"),
    ("归档.zip", "unsupported"), ("视频.mp4", "unsupported"),
])
def test_classify_routes_each_format_to_the_right_half(name, kind):
    assert initstd.classify(pathlib.Path("/tmp") / name).kind == kind


def test_image_classification_says_the_model_must_read_it():
    """照片脚本 OCR 不了。必须点名让模型看图,不能悄悄跳过——跳过就是整单丢失。"""
    c = initstd.classify(pathlib.Path("/tmp/IMG_1.jpg"))
    assert c.needs_vision and "看图" in c.reason
    assert not initstd.classify(pathlib.Path("/tmp/a.docx")).needs_vision


def test_heic_is_flagged_as_probably_unreadable_in_container():
    """容器里没有 sips/heif-convert,HEIC 多半读不了——如实说,别等模型读空。"""
    c = initstd.classify(pathlib.Path("/tmp/IMG_2357.HEIC"))
    assert c.kind == "image" and "HEIC" in c.reason and "jpg" in c.reason


# ---------------------------------------------------------------- 日期归一

@pytest.mark.parametrize("raw, want", [
    ("2026年01月08日", "2026-01-08"), ("2026年1月8日", "2026-01-08"),
    ("2026-1-8", "2026-01-08"), ("2026/01/08", "2026-01-08"),
    ("20260108", "2026-01-08"), ("2026-01-08 20:15:00", "2026-01-08"),
    ("2026.01.08", "2026-01-08"),
])
def test_normalize_date_accepts_the_shapes_tickets_actually_use(raw, want):
    assert initstd.normalize_date(raw) == want


@pytest.mark.parametrize("bad", ["", "去年底", "2026-13-01", "01/08/2026", "2026-02-30"])
def test_unparseable_date_raises_instead_of_guessing(bad):
    """猜错日期 = 案例挂到错的时间线上,而且没人会发现。宁可停下来问用户。"""
    with pytest.raises(initstd.StdFormatError):
        initstd.normalize_date(bad)


# ---------------------------------------------------------------- 渲染

def test_render_produces_the_fixed_six_element_layout():
    text = initstd.render_std(DRAFT, META)
    assert text.startswith("---\n")
    fm, sections = initstd.parse_std(text)
    assert fm["std_version"] == initstd.STD_VERSION
    assert fm["occurred_at"] == "2026-01-08"           # 归一过
    assert fm["system"] == "F-CIIS" and fm["source"] == META["source"]
    assert list(sections) == list(initstd.SECTIONS)     # 段名与顺序固定
    assert sections["现场"].startswith("2026年01月08日业务执行")


def test_render_is_byte_stable_for_the_same_input():
    """统一格式的意义就在于此:同样的内容渲染两次必须逐字相同。"""
    assert initstd.render_std(DRAFT, META) == initstd.render_std(dict(DRAFT), dict(META))


def test_render_rejects_a_missing_element_naming_it():
    for missing in ("occurred_at", "system", "现场", "判断", "处置", "复发标志"):
        draft = {k: v for k, v in DRAFT.items() if k != missing}
        with pytest.raises(initstd.StdFormatError) as info:
            initstd.render_std(draft, META)
        assert missing in str(info.value)


def test_render_requires_a_source_that_points_back():
    with pytest.raises(initstd.StdFormatError) as info:
        initstd.render_std(DRAFT, {**META, "source": ""})
    assert "出处" in str(info.value)


def test_unknown_must_be_written_out_not_left_blank():
    """原文真没写根因时,写「原文未写明」是合格的;空着或写 TODO 不是。"""
    ok = {**DRAFT, "判断": "原文未写明", "conclusion": "推测"}
    assert "原文未写明" in initstd.render_std(ok, META)
    for junk in ("", "  ", "TODO", "待填", "?", "无"):
        with pytest.raises(initstd.StdFormatError):
            initstd.render_std({**DRAFT, "判断": junk}, META)


def test_conclusion_defaults_to_the_cautious_value_and_rejects_junk():
    fm, _ = initstd.parse_std(initstd.render_std({k: v for k, v in DRAFT.items() if k != "conclusion"}, META))
    assert fm["conclusion"] == "推测"
    with pytest.raises(initstd.StdFormatError):
        initstd.render_std({**DRAFT, "conclusion": "大概吧"}, META)


def test_objects_and_signals_are_optional_lists():
    text = initstd.render_std({**DRAFT, "objects": ["cbst.t1"], "signals": ["CPU>80%"]}, META)
    fm, _ = initstd.parse_std(text)
    assert fm["objects"] == ["cbst.t1"] and fm["signals"] == ["CPU>80%"]
    fm2, _ = initstd.parse_std(initstd.render_std(DRAFT, META))
    assert fm2["objects"] == [] and fm2["signals"] == []


def test_vision_extracted_items_are_marked_so_they_can_be_spot_checked():
    """照片抽出来的内容没有可 diff 的原文,复核成本不一样——必须在文件里看得出来。"""
    fm, _ = initstd.parse_std(initstd.render_std(DRAFT, {**META, "source_kind": "image",
                                                         "extracted_by": "model"}))
    assert fm["extracted_by"] == "model" and fm["source_kind"] == "image"


# ---------------------------------------------------------------- 校验

def test_validate_accepts_what_render_produced():
    assert initstd.validate_std(initstd.render_std(DRAFT, META)) == []


def test_validate_catches_hand_edits_that_break_the_format():
    text = initstd.render_std(DRAFT, META)
    assert any("复发标志" in p for p in initstd.validate_std(text.replace("## 复发标志", "## 复发信号")))
    assert any("顺序" in p for p in initstd.validate_std(
        text.replace("## 现场", "## __X__").replace("## 判断", "## 现场").replace("## __X__", "## 判断")))
    assert initstd.validate_std("没有 frontmatter 的文件")


def test_validate_catches_an_emptied_section():
    text = initstd.render_std(DRAFT, META)
    broken = text.replace(DRAFT["处置"], "")
    assert any("处置" in p for p in initstd.validate_std(broken))


# ---------------------------------------------------------------- 交给 kb-import 的合并文件

def test_ingest_block_has_no_frontmatter_so_the_splitter_survives():
    """kb.py ingest 用 \\n---\\n 切多单;块里再带 frontmatter 会被从中间劈开。"""
    block = initstd.render_ingest_block(DRAFT, META)
    assert not block.lstrip().startswith("---")
    assert "\n---\n" not in block
    assert block.startswith("# ")
    for k in ("现场", "判断", "处置", "复发标志"):
        assert "## " + k in block
    assert "- system: F-CIIS" in block and "- occurred_at: 2026-01-08" in block


def test_ingest_file_joins_blocks_with_the_separator_ingest_expects():
    text = initstd.render_ingest_file([(DRAFT, META), (DRAFT, META)])
    assert text.count("\n---\n") == 1                  # 两单之间一个分隔符
    from common.kb import ingest as kbingest
    items = kbingest.split_text_items(text, "batch.md", "batch")
    assert len(items) == 2 and items[0].title


def test_slug_is_filesystem_safe_and_keeps_chinese():
    assert initstd.batch_slug("工单导出 2025Q1.csv") == "工单导出-2025q1-csv"
    assert initstd.batch_slug("///") == "batch"
