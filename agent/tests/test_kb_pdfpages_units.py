"""扫描件 PDF 渲染成页面图片(common/kb/pdfpages.py,2026-10-27)。"""
import pathlib
import shutil
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

from common.kb import extract as kbx  # noqa: E402
from common.kb import pdfpages  # noqa: E402
from test_kb_init_scanned_pptx_units import _scanned_pdf  # noqa: E402

needs = pytest.mark.skipif(not shutil.which("pdftoppm"), reason="本机没有 pdftoppm")


@needs
def test_pages_are_rendered_in_order_with_two_digit_names(tmp_path):
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(_scanned_pdf(3))
    r = pdfpages.render_pages(pdf, tmp_path / "pages")
    assert [p.name for p in r.pages] == ["page-01.jpg", "page-02.jpg", "page-03.jpg"]
    assert r.total == 3 and not r.truncated and "全部渲染" in r.note()
    assert r.pages[0].read_bytes()[:2] == b"\xff\xd8"


@needs
def test_page_limit_is_honoured_and_said(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_PDF_MAX_PAGES", "2")
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(_scanned_pdf(5))
    r = pdfpages.render_pages(pdf, tmp_path / "pages")
    assert len(r.pages) == 2 and r.total == 5 and r.truncated
    assert "只渲染了前 2 页" in r.note() and "拆开" in r.note()


def test_missing_renderer_is_reported_with_how_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(pdfpages.shutil, "which", lambda _n: None)
    with pytest.raises(kbx.ExtractError, match="poppler"):
        pdfpages.render_pages(tmp_path / "x.pdf", tmp_path / "pages")


def test_max_pages_env_is_clamped(monkeypatch):
    monkeypatch.setenv("KB_PDF_MAX_PAGES", "abc")
    assert pdfpages.max_pages() == pdfpages.DEFAULT_MAX_PAGES
    monkeypatch.setenv("KB_PDF_MAX_PAGES", "9999")
    assert pdfpages.max_pages() == 200
