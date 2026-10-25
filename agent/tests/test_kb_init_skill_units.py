"""gaussdb-kb-init 的命令行:scan → (模型写草稿) → render → validate。

这一层钉的是**流程不能静默丢单**:照片必须被点名交给模型,不支持的格式必须留在清单里,
草稿没写完 render 必须拒绝并说清是哪一单——任何一处「安静地少一单」,到知识库里都是查不到的先例。
"""
import io
import json
import pathlib
import sys
import zipfile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-kb-init" / "scripts"))

import kb_init  # noqa: E402
from common.kb import initstd  # noqa: E402

DRAFT = {
    "item_id": "ITSM-2026-000108", "title": "跑批 CPU 冲高",
    "occurred_at": "2026年01月08日", "system": "F-CIIS", "severity": "S2", "conclusion": "已确认",
    "现场": "跑批过程中 CPU 占用 80% 以上，持续 20 分钟。",
    "判断": "库中存在长事务，死行无法回收，可见性判断吃 CPU。",
    "处置": "查杀长事务；降低跑批并发。",
    "复发标志": "跑批时段 CPU>80% 且有 20 分钟以上长事务。",
}


def _kb(tmp_path):
    kb = tmp_path / "kb"
    (kb / "inbox").mkdir(parents=True)
    return kb


def _docx(path: pathlib.Path, text: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml",
                    "<w:document><w:body>%s</w:body></w:document>"
                    % "".join("<w:p><w:t>%s</w:t></w:p>" % ln for ln in text.splitlines()))
    path.write_bytes(buf.getvalue())


def _run(args, kb):
    return kb_init.main([*args, "--kb", str(kb)])


def _manifest(kb, slug="material"):
    return json.loads((kb / "init" / slug / "manifest.json").read_text(encoding="utf-8"))


def _work(kb, slug="material"):
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((kb / "init" / slug / "work").glob("*.json"))]


# ---------------------------------------------------------------- scan

def test_scan_snapshots_classifies_and_opens_work_orders(tmp_path, capsys):
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    (src / "工单.txt").write_text("# 偶现单条 update 慢\n表 t1 更新耗时 3s。", encoding="utf-8")
    _docx(src / "报告.docx", "标题\n业务反馈语句执行慢。")
    assert _run(["scan", str(src)], kb) == 2          # 还有草稿没写 → 2(有活没干完)
    out = capsys.readouterr().out

    m = _manifest(kb)
    assert {f["name"] for f in m["files"]} == {"工单.txt", "报告.docx"}
    assert (kb / "init" / "material" / "sources" / "工单.txt").is_file()   # 原件快照
    work = _work(kb)
    assert len(work) == 2
    assert all(w["text"] and w["extracted_by"] == "script" for w in work)
    assert all(set(initstd.SECTIONS) <= set(w["template"]) for w in work)
    assert "draft/" in out and "render" in out


def test_scan_names_the_photos_the_model_must_read(tmp_path, capsys):
    """照片脚本 OCR 不了。必须逐张点名,不能算作「已处理」。"""
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    (src / "IMG_1.jpg").write_bytes(b"\xff\xd8\xff")
    (src / "IMG_2.HEIC").write_bytes(b"\x00\x00\x00 ftypheic")
    _run(["scan", str(src)], kb)
    out = capsys.readouterr().out

    work = {w["source_file"]: w for w in _work(kb)}
    assert work["IMG_1.jpg"]["text"] is None
    assert work["IMG_1.jpg"]["extracted_by"] == "model"
    assert work["IMG_1.jpg"]["image_path"].endswith("IMG_1.jpg")
    assert "看图" in out and "IMG_1.jpg" in out
    assert "HEIC" in out and "jpg" in out             # HEIC 多半读不了,提前说


def test_scan_keeps_unsupported_files_visible_instead_of_dropping_them(tmp_path, capsys):
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    (src / "归档.zip").write_bytes(b"PK\x03\x04")
    (src / "工单.txt").write_text("内容够长的一段现场描述。", encoding="utf-8")
    _run(["scan", str(src)], kb)
    out = capsys.readouterr().out
    kinds = {f["name"]: f["kind"] for f in _manifest(kb)["files"]}
    assert kinds["归档.zip"] == "unsupported"
    assert "归档.zip" in out and "不支持" in out
    assert len(_work(kb)) == 1                        # 不支持的不开工作单,但清单里看得见


def test_scan_of_a_table_prefills_what_the_columns_already_say(tmp_path):
    kb = _kb(tmp_path)
    csv = tmp_path / "工单导出.csv"
    csv.write_text("工单号,系统,发生时间,级别,问题描述,根因,解决方案\n"
                   "T-1,CBST,2025-02-24,S2,单条 update 偶发 3s,autovacuum 持锁,调大阈值\n"
                   "T-2,CBMS,2025-03-01,S3,报表慢,统计信息过期,手动 analyze\n", encoding="utf-8")
    assert _run(["scan", str(csv), "--batch", "q1"], kb) == 2
    work = _work(kb, "q1")
    assert len(work) == 2                              # 一行一单
    assert work[0]["known"]["system"] == "CBST" and work[0]["known"]["occurred_at"] == "2025-02-24"
    assert "单条 update 偶发 3s" in work[0]["suggest"]["现场"]
    assert work[0]["source"].endswith("#row=2")        # 出处能指回具体哪一行


def test_scan_refuses_a_path_that_does_not_exist(tmp_path):
    kb = _kb(tmp_path)
    assert _run(["scan", str(tmp_path / "nope")], kb) == 1


def test_rescan_does_not_silently_wipe_drafts_already_written(tmp_path, capsys):
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    (src / "a.txt").write_text("一段够长的现场描述内容。", encoding="utf-8")
    _run(["scan", str(src)], kb)
    draft = kb / "init" / "material" / "draft" / "001.json"
    draft.write_text(json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
    _run(["scan", str(src)], kb)
    assert draft.is_file(), "重扫把已写的草稿删了"
    assert "已有" in capsys.readouterr().out


# ---------------------------------------------------------------- render

def _scan_one(tmp_path, kb, text="一段够长的现场描述内容，用于测试。"):
    src = tmp_path / "material"
    src.mkdir(exist_ok=True)
    (src / "a.txt").write_text(text, encoding="utf-8")
    _run(["scan", str(src)], kb)
    return kb / "init" / "material"


def test_render_turns_drafts_into_the_unified_format(tmp_path, capsys):
    kb = _kb(tmp_path)
    batch = _scan_one(tmp_path, kb)
    (batch / "draft" / "001.json").write_text(json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
    assert _run(["render"], kb) == 0

    std = list((batch / "std").glob("*.md"))
    assert len(std) == 1
    text = std[0].read_text(encoding="utf-8")
    assert initstd.validate_std(text) == []
    fm, sections = initstd.parse_std(text)
    assert fm["occurred_at"] == "2026-01-08" and fm["system"] == "F-CIIS"
    assert fm["source_file"] == "a.txt" and fm["extracted_by"] == "script"
    assert sections["处置"].startswith("查杀长事务")
    assert "kb.py ingest" in capsys.readouterr().out       # 告诉模型下一步


def test_render_emits_a_file_kb_import_can_split(tmp_path):
    kb = _kb(tmp_path)
    batch = _scan_one(tmp_path, kb)
    (batch / "draft" / "001.json").write_text(
        json.dumps([DRAFT, {**DRAFT, "item_id": "ITSM-2", "title": "第二单"}], ensure_ascii=False),
        encoding="utf-8")                                   # 一份材料里两个工单
    assert _run(["render"], kb) == 0
    out = list((batch / "out").glob("*.md"))
    assert len(out) == 1
    from common.kb import ingest as kbingest
    items = kbingest.split_text_items(out[0].read_text(encoding="utf-8"), "batch.md", "batch")
    assert len(items) == 2
    assert len(list((batch / "std").glob("*.md"))) == 2


def test_pages_of_one_report_merge_into_one_item_and_the_rest_write_empty_arrays(tmp_path):
    """手机拍报告是一页一张(2026-10-22 e2e:两张 HEIC 是同一份报告的第 1、2 页)。

    合并成一单写在第一张的草稿里、source 列出全部照片;其余几张写 [] —— 「这份材料不单独出单」,
    render 照常通过,不会因为第二页「缺草稿」整批拒绝,也不会同一单出两份。
    """
    kb = _kb(tmp_path)
    src = tmp_path / "pages"
    src.mkdir()
    (src / "p1.txt").write_text("# 跑批 CPU 冲高(第 1 页)\n", encoding="utf-8")
    (src / "p2.txt").write_text("前言(第 2 页)\n", encoding="utf-8")
    _run(["scan", str(src), "--batch", "pages"], kb)
    batch = kb / "init" / "pages"
    (batch / "draft" / "001.json").write_text(
        json.dumps({**DRAFT, "source": "p1.txt、p2.txt"}, ensure_ascii=False), encoding="utf-8")
    (batch / "draft" / "002.json").write_text("[]", encoding="utf-8")
    assert _run(["render", "--batch", "pages"], kb) == 0
    std = list((batch / "std").glob("*.md"))
    assert len(std) == 1
    fm, _ = initstd.parse_std(std[0].read_text(encoding="utf-8"))
    assert fm["source"] == "p1.txt、p2.txt"


def test_render_refuses_and_names_the_item_when_a_draft_is_missing(tmp_path, capsys):
    kb = _kb(tmp_path)
    _scan_one(tmp_path, kb)
    assert _run(["render"], kb) == 2
    out = capsys.readouterr().out
    assert "001" in out and "a.txt" in out


def test_render_refuses_a_draft_with_a_hole_in_it(tmp_path, capsys):
    kb = _kb(tmp_path)
    batch = _scan_one(tmp_path, kb)
    (batch / "draft" / "001.json").write_text(
        json.dumps({**DRAFT, "复发标志": ""}, ensure_ascii=False), encoding="utf-8")
    assert _run(["render"], kb) == 2
    assert "复发标志" in capsys.readouterr().out
    assert not list((batch / "std").glob("*.md")), "有问题的草稿不该落盘"


def test_render_refuses_a_draft_that_is_not_valid_json(tmp_path, capsys):
    kb = _kb(tmp_path)
    batch = _scan_one(tmp_path, kb)
    (batch / "draft" / "001.json").write_text("{不是 json", encoding="utf-8")
    assert _run(["render"], kb) == 2
    assert "JSON" in capsys.readouterr().out


# ---------------------------------------------------------------- validate / status

def test_validate_checks_the_rendered_files(tmp_path, capsys):
    kb = _kb(tmp_path)
    batch = _scan_one(tmp_path, kb)
    (batch / "draft" / "001.json").write_text(json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
    _run(["render"], kb)
    assert _run(["validate"], kb) == 0

    std = next((batch / "std").glob("*.md"))
    std.write_text(std.read_text(encoding="utf-8").replace("## 复发标志", "## 复发信号"), encoding="utf-8")
    assert _run(["validate"], kb) == 2
    assert "复发标志" in capsys.readouterr().out


def test_status_counts_what_is_left_to_do(tmp_path, capsys):
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    (src / "a.txt").write_text("一段够长的现场描述内容。", encoding="utf-8")
    (src / "b.jpg").write_bytes(b"\xff\xd8\xff")
    _run(["scan", str(src)], kb)
    (kb / "init" / "material" / "draft" / "001.json").write_text(
        json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
    assert _run(["status"], kb) == 2
    out = capsys.readouterr().out
    assert "1/2" in out or ("待写草稿" in out and "1" in out)
    assert "看图" in out


def test_list_shows_every_batch(tmp_path, capsys):
    kb = _kb(tmp_path)
    _scan_one(tmp_path, kb)
    assert _run(["list"], kb) == 0
    assert "material" in capsys.readouterr().out


def test_commands_need_no_database_and_never_touch_credentials(tmp_path):
    """本 skill 只搬文件与文本,不连库、不碰凭据——脚本里不该出现这些入口。"""
    text = (_ROOT / "skills" / "gaussdb-kb-init" / "scripts" / "kb_init.py").read_text(encoding="utf-8")
    for forbidden in ("credential", "psycopg2", "Database.connect", "GRMP", "gsql"):
        assert forbidden not in text, forbidden


def test_render_never_overwrites_two_tickets_that_share_a_date_and_have_no_id(tmp_path):
    """真实材料(华为问题分析报告)没有工单号:item_id 落成 unknown。同一天两单要是按
    「日期-unknown」命名就会同名覆盖——静默丢一单。模型级 e2e 抓到的。"""
    kb = _kb(tmp_path)
    batch = _scan_one(tmp_path, kb)
    a = {k: v for k, v in DRAFT.items() if k != "item_id"}
    b = {**a, "title": "另一单同一天", "现场": "另一单的现场描述内容也够长。"}
    (batch / "draft" / "001.json").write_text(json.dumps([a, b], ensure_ascii=False), encoding="utf-8")
    assert _run(["render"], kb) == 0
    names = sorted(p.name for p in (batch / "std").glob("*.md"))
    assert len(names) == 2, names
    assert not any("unknown" in n for n in names), names          # 没号就用标题
    c = {**a, "title": DRAFT["title"]}                              # 连标题都撞了也不能覆盖
    (batch / "draft" / "001.json").write_text(json.dumps([a, c], ensure_ascii=False), encoding="utf-8")
    assert _run(["render"], kb) == 0
    assert len(list((batch / "std").glob("*.md"))) == 2
