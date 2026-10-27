"""扫描件 / 乱码 PDF 的退路:把每页渲染成图片,交给模型像读照片一样读(2026-10-27)。

文字层不可用的 PDF(common/kb/extract.UnreadablePdf)以前一律拒绝、请用户先 OCR。银行的规范和工单报告
大量是盖章后扫描的,这条路走不通就等于这类材料进不了知识库。现在:渲染成 jpg(poppler 的 pdftoppm,
导入镜像里本来就有,不加依赖),由模型逐页看图,读出的内容标 extracted_by: model —— 与照片同一套规矩:
看不清的地方问用户、不猜。

只用标准库;页数上限由 KB_PDF_MAX_PAGES 控制(默认 30),超出的页不渲染并如实告诉用户。
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List

from .extract import ExtractError, pdf_page_count

DPI = 150                 # A4 约 1240×1754:正文小字也看得清,单页 jpg 两三百 KB
TIMEOUT = 300
DEFAULT_MAX_PAGES = 30


@dataclass
class Rendered:
    pages: List[pathlib.Path] = field(default_factory=list)   # 按页序
    total: int = 0                                           # 原件总页数(看不出来时 = 已渲染页数)
    truncated: bool = False                                  # 超过上限,后面的页没渲染

    def note(self) -> str:
        if not self.truncated:
            return "共 %d 页,已全部渲染成图片" % len(self.pages)
        return ("共 %d 页,只渲染了前 %d 页(上限 KB_PDF_MAX_PAGES);其余页请把 PDF 拆开分批导入"
                % (self.total, len(self.pages)))


def max_pages() -> int:
    try:
        n = int(os.environ.get("KB_PDF_MAX_PAGES") or DEFAULT_MAX_PAGES)
    except ValueError:
        n = DEFAULT_MAX_PAGES
    return max(1, min(n, 200))


def _total_pages(pdf: pathlib.Path) -> int:
    if shutil.which("pdfinfo"):
        try:
            out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
            m = re.search(r"^Pages:\s+(\d+)", out, re.M)
            if m:
                return int(m.group(1))
        except subprocess.TimeoutExpired:
            pass
    return pdf_page_count(pdf.read_bytes())


def render_pages(pdf: pathlib.Path, out_dir: pathlib.Path, limit: int = 0) -> Rendered:
    """渲染成 out_dir/page-NN.jpg。pdftoppm 不在时抛 ExtractError(说明装什么)。"""
    exe = shutil.which("pdftoppm")
    if not exe:
        raise ExtractError("扫描件 PDF 要渲染成图片才能读,但这里没有 pdftoppm(poppler):"
                           "macOS `brew install poppler`,Debian/Ubuntu `apt install poppler-utils`")
    limit = limit or max_pages()
    total = _total_pages(pdf)
    last = min(total, limit) if total else limit
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("page-*.jpg"):
        old.unlink()
    cmd = [exe, "-r", str(DPI), "-jpeg", "-jpegopt", "quality=85", "-f", "1", "-l", str(last), str(pdf), str(out_dir / "page")]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ExtractError("PDF 渲染超时(%d 秒);请把 PDF 拆成几份再导入" % TIMEOUT) from None
    if proc.returncode != 0:
        raise ExtractError("PDF 渲染失败(pdftoppm 退出码 %d):%s"
                           % (proc.returncode, proc.stderr.decode("utf-8", "replace").strip()[:200]))
    # pdftoppm 按总页数补零(page-1 / page-01 / page-001):统一改成两位以上、按页号排序
    pages = []
    for f in out_dir.glob("page-*.jpg"):
        m = re.match(r"page-(\d+)\.jpg$", f.name)
        if m:
            dst = out_dir / ("page-%02d.jpg" % int(m.group(1)))
            if dst != f:
                f.rename(dst)
            pages.append(dst)
    pages.sort(key=lambda p: int(re.search(r"(\d+)", p.name).group(1)))
    if not pages:
        raise ExtractError("PDF 渲染没有产出任何页面;文件可能损坏或加了密码")
    total = total or len(pages)
    return Rendered(pages=pages, total=total, truncated=total > len(pages))
