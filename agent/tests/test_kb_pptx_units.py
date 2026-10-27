"""PPT(.pptx)抽取(common/kb/extract.py,2026-10-27)。

页序以 presentation.xml 的 sldIdLst 为准(调整过页序的演示文稿里,slideN.xml 的 N 不是放映顺序);
每页的标题、分级要点、表格、演讲者备注都要在;页码 / 日期 / 页脚占位符不是内容;图片取出来给模型看。
"""
import io
import pathlib
import sys
import zipfile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import extract as x  # noqa: E402

NS = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _sp(ph: str, paras) -> str:
    phx = '<p:nvSpPr><p:cNvPr id="1" name="x"/><p:cNvSpPr/><p:nvPr>%s</p:nvPr></p:nvSpPr>' % (
        '<p:ph type="%s"/>' % ph if ph else "")
    body = "".join('<a:p>%s<a:r><a:t>%s</a:t></a:r></a:p>' % ('<a:pPr lvl="%d"/>' % lvl if lvl else "", t)
                   for lvl, t in paras)
    return '<p:sp>%s<p:txBody><a:bodyPr/>%s</p:txBody></p:sp>' % (phx, body)


def _slide(*shapes) -> str:
    return '<p:sld %s><p:cSld><p:spTree>%s</p:spTree></p:cSld></p:sld>' % (NS, "".join(shapes))


TABLE = ('<p:graphicFrame><a:graphic><a:graphicData><a:tbl>'
         '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>指标</a:t></a:r></a:p></a:txBody></a:tc>'
         '<a:tc><a:txBody><a:p><a:r><a:t>阈值</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
         '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>锁等待</a:t></a:r></a:p></a:txBody></a:tc>'
         '<a:tc><a:txBody><a:p><a:r><a:t>5 分钟 | 告警</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
         '</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')


def _pptx(path: pathlib.Path) -> pathlib.Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # 放映顺序:slide2(封面)在前,slide1 在后 —— 与文件名编号相反
        zf.writestr("ppt/presentation.xml", '<p:presentation %s><p:sldIdLst>'
                    '<p:sldId id="256" r:id="rId3"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst></p:presentation>' % NS)
        zf.writestr("ppt/_rels/presentation.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId2" Type="%s/slide" Target="slides/slide1.xml"/>'
                    '<Relationship Id="rId3" Type="%s/slide" Target="slides/slide2.xml"/></Relationships>' % (REL, REL))
        zf.writestr("ppt/slides/slide2.xml", _slide(_sp("ctrTitle", [(0, "锁等待专题")]), _sp("subTitle", [(0, "运维部 2026")])))
        zf.writestr("ppt/slides/slide1.xml", _slide(
            _sp("title", [(0, "阻塞链排查")]),
            _sp("", [(0, "先查 pg_locks"), (1, "看 granted = f 的行"), (0, "再定位根会话")]),
            _sp("sldNum", [(0, "2")]), TABLE))
        zf.writestr("ppt/slides/_rels/slide1.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="%s/notesSlide" Target="../notesSlides/notesSlide1.xml"/>'
                    '<Relationship Id="rId2" Type="%s/image" Target="../media/image1.png"/>'
                    '<Relationship Id="rId9" Type="%s/hyperlink" Target="https://x" TargetMode="External"/>'
                    '</Relationships>' % (REL, REL, REL))
        zf.writestr("ppt/notesSlides/notesSlide1.xml", _slide(_sp("body", [(0, "讲到这里提醒双人复核")]), _sp("sldNum", [(0, "2")])))
        zf.writestr("ppt/media/image1.png", b"\x89PNG\r\n\x1a\nfake")
    path.write_bytes(buf.getvalue())
    return path


def test_pptx_text_follows_the_show_order_with_titles_bullets_tables_and_notes(tmp_path):
    out = x.extract_pptx(_pptx(tmp_path / "专题.pptx"))
    first, second = out.split("## 第 2 页")
    assert first.startswith("## 第 1 页:锁等待专题") and "- 运维部 2026" in first
    assert second.startswith(":阻塞链排查")
    assert "- 先查 pg_locks\n  - 看 granted = f 的行\n- 再定位根会话" in second
    assert "| 指标 | 阈值 |\n|---|---|\n| 锁等待 | 5 分钟 \\| 告警 |" in second
    assert "> 备注:讲到这里提醒双人复核" in second
    assert "(本页有图片:image1.png)" in second
    assert "\n- 2\n" not in out and "备注:讲到这里提醒双人复核 2" not in out     # 页码占位符不是内容


def test_pptx_media_is_extracted_per_slide(tmp_path):
    got = x.pptx_media(_pptx(tmp_path / "p.pptx"), tmp_path / "media")
    assert [(n, p.name) for n, p in got] == [(2, "slide02-image1.png")]
    assert got[0][1].read_bytes().startswith(b"\x89PNG")


def test_extract_source_routes_pptx_and_explains_old_ppt(tmp_path):
    assert "阻塞链排查" in x.extract_source(_pptx(tmp_path / "p.pptx"))
    old = tmp_path / "old.ppt"
    old.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(x.ExtractError, match="另存为 .pptx"):
        x.extract_source(old)


def test_a_broken_pptx_is_reported_not_swallowed(tmp_path):
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(x.ExtractError, match="pptx 解析失败"):
        x.extract_pptx(bad)
    empty = tmp_path / "empty.pptx"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("ppt/presentation.xml", "<p:presentation %s/>" % NS)
    with pytest.raises(x.ExtractError, match="没有幻灯片"):
        x.extract_pptx(empty)
