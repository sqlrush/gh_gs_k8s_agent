"""kb-init 扫描:扫描件 PDF 与 PPT(2026-10-27)。

扫描件以前直接进「没能处理的」;现在每页渲染成图片,整份一张工作单交给模型逐页看(标 extracted_by: model)。
pptx 文字由脚本抽,幻灯片里的图片作为补充一并列给模型。老格式明确说另存成什么。
"""
import io
import json
import pathlib
import shutil
import sys
import zipfile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-kb-init" / "scripts"))

import kb_init  # noqa: E402
from common.kb import extract as kbx  # noqa: E402
from common.kb import pdfpages  # noqa: E402


def _pdf(body_objs):
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(body_objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(body_objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(body_objs) + 1, xref)
    return bytes(out)


def _scanned_pdf(pages=3):
    c = b"0.2 g 100 500 400 200 re f\n"
    kids = b" ".join(b"%d 0 R" % (3 + i) for i in range(pages))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % pages]
    objs += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R >>" % (3 + pages + i)
             for i in range(pages)]
    objs += [b"<< /Length %d >>\nstream\n" % len(c) + c + b"endstream" for _ in range(pages)]
    return _pdf(objs)


def _pptx(path):
    ns = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ppt/presentation.xml", '<p:presentation %s><p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst></p:presentation>' % ns)
        zf.writestr("ppt/_rels/presentation.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId2" Type="%s/slide" Target="slides/slide1.xml"/></Relationships>' % rel)
        zf.writestr("ppt/slides/slide1.xml", '<p:sld %s><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>'
                    '2026-01-08 跑批 CPU 冲高复盘:长事务导致死行无法回收</a:t></a:r></a:p></p:txBody></p:sp>'
                    '</p:spTree></p:cSld></p:sld>' % ns)
        zf.writestr("ppt/slides/_rels/slide1.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId2" Type="%s/image" Target="../media/image1.png"/></Relationships>' % rel)
        zf.writestr("ppt/media/image1.png", b"\x89PNG\r\n\x1a\nfake")
    path.write_bytes(buf.getvalue())


def _kb(tmp_path):
    kb = tmp_path / "kb"
    (kb / "inbox").mkdir(parents=True)
    return kb


def _work(kb, slug="material"):
    return {w["source_file"]: w for w in (json.loads(p.read_text(encoding="utf-8"))
                                          for p in sorted((kb / "init" / slug / "work").glob("*.json")))}


def _manifest(kb, slug="material"):
    return {f["name"]: f for f in json.loads((kb / "init" / slug / "manifest.json").read_text(encoding="utf-8"))["files"]}


needs_poppler = pytest.mark.skipif(not (shutil.which("pdftoppm") and shutil.which("pdftotext")), reason="本机没有 poppler")


@needs_poppler
def test_a_scanned_pdf_becomes_one_work_order_of_page_images(tmp_path, capsys):
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    (src / "故障报告扫描件.pdf").write_bytes(_scanned_pdf(3))
    assert kb_init.main(["scan", str(src), "--kb", str(kb)]) == 2
    out = capsys.readouterr().out
    w = _work(kb)["故障报告扫描件.pdf"]
    assert w["text"] is None and w["extracted_by"] == "model"
    assert len(w["image_paths"]) == 3 and all(pathlib.Path(p).is_file() for p in w["image_paths"])
    assert w["image_path"] == w["image_paths"][0] and w["image_paths"][0].endswith("page-01.jpg")
    assert _manifest(kb)["故障报告扫描件.pdf"]["kind"] == "scanned"
    assert "扫描件 PDF" in out and "image_paths" in out and "没能处理" not in out


def test_a_scanned_pdf_that_cannot_be_rendered_stays_visible_as_unprocessed(tmp_path, capsys, monkeypatch):
    def scanned(_p):
        raise kbx.UnreadablePdf("PDF 提取到的文本过少……扫描件", 3, "scanned")

    def no_render(*_a, **_k):
        raise kbx.ExtractError("这里没有 pdftoppm")

    monkeypatch.setattr(kbx, "extract_source", scanned)
    monkeypatch.setattr(pdfpages, "render_pages", no_render)
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    (src / "扫描.pdf").write_bytes(b"%PDF-1.4")
    kb_init.main(["scan", str(src), "--kb", str(kb)])
    out = capsys.readouterr().out
    assert _manifest(kb)["扫描.pdf"]["kind"] == "unsupported" and not _work(kb)
    assert "没能处理" in out and "渲染成图片也没成功" in out


def test_a_pptx_is_read_by_script_with_its_slide_images_listed(tmp_path, capsys):
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    _pptx(src / "复盘.pptx")
    assert kb_init.main(["scan", str(src), "--kb", str(kb)]) == 2
    out = capsys.readouterr().out
    w = _work(kb)["复盘.pptx"]
    assert "长事务导致死行无法回收" in w["text"] and w["extracted_by"] == "script" and w["image_path"] is None
    assert len(w["image_paths"]) == 1 and w["image_paths"][0].endswith("slide01-image1.png")
    assert "幻灯片里有 1 张图片" in out


def test_plain_text_orders_carry_no_images(tmp_path):
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    (src / "a.txt").write_text("一段够长的现场描述内容,用于测试。", encoding="utf-8")
    kb_init.main(["scan", str(src), "--kb", str(kb)])
    w = _work(kb)["a.txt"]
    assert w["image_paths"] == [] and w["image_path"] is None


def test_a_web_clip_page_keeps_the_original_url_as_its_source(tmp_path):
    """剪藏转出的 page.md 处理完就随批次清掉:出处要写原网页地址,否则入库的案例指回一个已经不在的 page.md。"""
    kb = _kb(tmp_path)
    src = tmp_path / "clip"
    src.mkdir()
    (src / "page.md").write_text('---\nkind: web-clip\nbatch: clip-20261027-103000-ab12\ntitle: "锁等待复盘"\n'
                                 'source_url: "https://wiki.corp/display/OPS/lock"\nfetched_at: 2026-10-27T10:30:00+08:00\n'
                                 'extracted_by: script\n---\n# 锁等待复盘\n\n现场:日终批量卡住,大量会话等 transactionid。\n',
                                 encoding="utf-8")
    (src / "plain.md").write_text("# 普通文档\n\n" + "现场描述。" * 10, encoding="utf-8")
    kb_init.main(["scan", str(src), "--kb", str(kb)])
    work = _work(kb, "clip")
    assert work["page.md"]["source"] == "https://wiki.corp/display/OPS/lock(网页剪藏,抓取于 2026-10-27T10:30:00+08:00)"
    assert work["plain.md"]["source"] == "plain.md"


@pytest.mark.parametrize("name,want", [("台账.xls", ".xlsx"), ("说明.rtf", ".docx"), ("讲稿.ppt", ".pptx")])
def test_old_formats_say_what_to_resave_as(tmp_path, capsys, name, want):
    kb = _kb(tmp_path)
    src = tmp_path / "material"
    src.mkdir()
    (src / name).write_bytes(b"\xd0\xcf\x11\xe0")
    kb_init.main(["scan", str(src), "--kb", str(kb)])
    out = capsys.readouterr().out
    assert _manifest(kb)[name]["kind"] == "unsupported" and ("另存为 %s" % want) in out
