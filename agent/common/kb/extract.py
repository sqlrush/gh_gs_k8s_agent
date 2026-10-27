"""把一份文档变成纯文本:txt / md / docx / doc / pdf / pptx。

原来只长在导入 skill 的 kb.py 里;标准化 skill(gaussdb-kb-init)要走同一条路,
所以搬到 common,两边共用一份。kb.py 仍按旧名字重导出,对外行为一字不变。

这里最要紧的不是「怎么抽」,而是**抽不出来时不许假装抽出来了**:
扫描件 PDF 和缺 ToUnicode 的 CID 字体都会以「转换成功」的形态返回空/乱码,
入库之后表现成「这份规范没有条款」——没有报错,没有崩溃,只有沉默。
"""
from __future__ import annotations

import html
import pathlib
import re
import shutil
import subprocess
from typing import Optional

TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".sql", ".log")


def read_text_file(path: pathlib.Path) -> str:
    """utf-8 读不出来就按 gb18030 再试一次——客户的 txt 十有八九是后者。

    与 rulesfile 里的同名函数实现相同:本模块要在**未拆分**的部署里也能独立工作
    (那边没有 rulesfile),所以自带一份,不跨模块依赖。
    """
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


class ExtractError(Exception):
    """格式不支持 / 转换器缺失 / 转换结果不可信。消息直接给用户看,要写明下一步做什么。"""


class UnreadablePdf(ExtractError):
    """PDF 转换「成功」了,但文字层不可用:扫描件(字是图片)或乱码(CID 字体缺映射)。

    单独一类,是因为能看图的调用方(kb-init 的工单、kb.py ingest 的规范)有退路:把每页渲染成图片
    (common/kb/pdfpages.py),交给模型像读照片一样读。不能看图的调用方照旧当 ExtractError 处理。
    """

    def __init__(self, message: str, pages: int, reason: str):
        super().__init__(message)
        self.pages = pages            # 0 = 页数看不出来
        self.reason = reason          # scanned | garbled


# ---------------------------------------------------------------- docx

def extract_docx(path: pathlib.Path) -> str:
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ExtractError(f".docx 解析失败({exc});文件可能损坏或是旧版 .doc 改的后缀") from exc
    lines = []
    for para in re.split(r"</w:p>", xml):
        para = re.sub(r"<w:tab[^>]*/>", "\t", para)
        para = re.sub(r"<w:br[^>]*/>", "\n", para)
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S))
        lines.append(html.unescape(text))
    return "\n".join(lines)


# ---------------------------------------------------------------- doc

def extract_doc(path: pathlib.Path) -> str:
    converters = (
        (("textutil", "-convert", "txt", "-stdout", str(path)), "textutil(macOS)"),
        (("antiword", str(path)), "antiword"),
    )
    tried = []
    for cmd, label in converters:
        if not shutil.which(cmd[0]):
            tried.append(f"{label}:未安装")
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            tried.append(f"{label}:转换超时(120s)")
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.decode("utf-8", "replace")
        tried.append(f"{label}:退出码 {proc.returncode}")
    raise ExtractError(
        ".doc 转换失败(" + ";".join(tried) + ")。"
        "请先手工另存为 .txt 或 .docx 再导入"
    )


# ---------------------------------------------------------------- pdf
# A scanned PDF holds pictures of words, not words: it has no text operators at
# all, and pdftotext reports success (exit 0) while printing nothing. Copying the
# .doc pattern (exit 0 == success) would write an empty source.md, and the model
# would then state that the customer's spec "contains no clauses" — while the
# clauses sit right there in 30 pages of images. No error, no crash, just silence.
# Chinese specs are scanned far more often than not (anything with a red chop is).
#
# So extraction is only half the job; the other half is refusing to import a
# result we cannot vouch for. Uncertain == unusable, and we say so.
_PDF_MIN_CHARS = 100          # 绝对下限:低于此值的「提取成功」不可信
_PDF_MIN_CHARS_PER_PAGE = 50  # 每页字符数下限:混合扫描件(页眉是文本、正文是图)
_PDF_MAX_GARBAGE = 0.10       # 乱码字符占比上限

_PDF_TIMEOUT = 120


def pdf_page_count(raw: bytes) -> int:
    """Page count from the raw bytes, or 0 when it cannot be told (pure).

    PDF 1.5+ can bury page objects inside compressed object streams, where this
    finds nothing. Returning 0 says exactly that — the quality gate falls back to
    its absolute character floor rather than guessing a number.
    """
    return len(re.findall(rb"/Type\s*/Page[^s]", raw))


def _is_plain(ch: str) -> bool:
    """CJK, ASCII, and the punctuation a spec actually uses."""
    o = ord(ch)
    return (
        ch.isspace()
        or 0x20 <= o <= 0x7E                 # ASCII printable
        or 0x3000 <= o <= 0x303F             # CJK punctuation
        or 0x4E00 <= o <= 0x9FFF             # CJK unified ideographs
        or 0xFF00 <= o <= 0xFFEF             # fullwidth forms
    )


def pdf_extraction_problem(text: str, pages: int) -> Optional[str]:
    """Why this extraction must not be imported, or None if it looks sound (pure).

    Two failure modes, both of which come back as a *successful* conversion:
      - scanned pages   -> empty or near-empty text
      - CID font with no ToUnicode CMap -> private-use / replacement characters,
        i.e. text that looks like content but is garbage. Importing that is worse
        than importing nothing: the model reads the garbage as clauses.
    """
    body = "".join(text.split())
    if not body:
        return _scanned_msg(pages, 0)
    if len(body) < _PDF_MIN_CHARS:
        return _scanned_msg(pages, len(body))
    if pages > 0 and len(body) / pages < _PDF_MIN_CHARS_PER_PAGE:
        return _scanned_msg(pages, len(body))

    garbage = sum(1 for ch in body if not _is_plain(ch))
    if garbage / len(body) > _PDF_MAX_GARBAGE:
        return (
            f"PDF 提取结果疑似**乱码**({garbage}/{len(body)} 个字符无法识别,"
            f"超过 {_PDF_MAX_GARBAGE:.0%})。常见于 PDF 内嵌 CID 字体但缺少 ToUnicode "
            f"映射表——抠出来的是私用区码位,看着像内容,实际是垃圾。"
            f"**乱码入库比空文档更糟**:模型会把它当成规范条款。"
            f"请用 Adobe/WPS 的「PDF 转 Word」重新导出,或向客户索要 .docx 原件。"
        )
    return None


def _scanned_msg(pages: int, chars: int) -> str:
    where = f"{pages} 页,共 {chars} 个字符" if pages else f"共 {chars} 个字符"
    return (
        f"PDF 提取到的文本过少({where})。这几乎可以肯定是**扫描件/图片型 PDF**——"
        f"里面的字是图片,不是文本。**本工具不做 OCR**。\n"
        f"请先 OCR 转成文本(Adobe/WPS 的「PDF 转 Word」、macOS 预览的实时文字识别、"
        f"或向客户索要 .docx/.txt 原件),再导入。\n"
        f"绝不入库空文档:那会让模型误以为「这份规范没有条款」。"
    )


def extract_pdf(path: pathlib.Path) -> str:
    converters = (
        (("pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"), "pdftotext(poppler)"),
        (("mutool", "draw", "-F", "txt", str(path)), "mutool(mupdf)"),
    )
    tried = []
    for cmd, label in converters:
        if not shutil.which(cmd[0]):
            tried.append(f"{label}:未安装")
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=_PDF_TIMEOUT)
        except subprocess.TimeoutExpired:
            tried.append(f"{label}:转换超时({_PDF_TIMEOUT}s)")
            continue
        if proc.returncode != 0:
            tried.append(f"{label}:退出码 {proc.returncode}")
            continue

        text = proc.stdout.decode("utf-8", "replace")
        # Exit code 0 does NOT mean the text is usable — see the note above.
        pages = pdf_page_count(path.read_bytes())
        problem = pdf_extraction_problem(text, pages)
        if problem:
            raise UnreadablePdf(problem, pages, "garbled" if "乱码" in problem else "scanned")
        return text

    raise ExtractError(
        "PDF 转换失败(" + ";".join(tried) + ")。\n"
        "请安装 poppler:macOS `brew install poppler`,"
        "Debian/Ubuntu `apt install poppler-utils`,CentOS `yum install poppler-utils`。\n"
        "或先手工把 PDF 转成 .txt / .docx 再导入。"
    )


# ---------------------------------------------------------------- pptx
# .pptx 是 zip + XML(与 docx 同法,标准库读)。页序以 presentation.xml 的 sldIdLst 为准 —— slideN.xml 的 N
# 是建页顺序,调整过页序的演示文稿两者不一致。每页输出:标题、正文段落(按缩进层级)、表格、演讲者备注、
# 本页图片的文件名(图片本身由 pptx_media 取出,交给模型看)。

_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_PPTX_MAX_SLIDES = 500
_PH_SKIP = ("sldNum", "dt", "ftr", "hdr", "sldImg")


def _zip_xml(zf, name: str):
    import xml.etree.ElementTree as ET
    try:
        return ET.fromstring(zf.read(name))
    except KeyError:
        return None


def _rels(zf, part: str) -> dict:
    """part 的关系表:rId → (类型尾巴, 归一化后的 zip 内路径)。"""
    import posixpath
    d, base = posixpath.split(part)
    root = _zip_xml(zf, posixpath.join(d, "_rels", base + ".rels"))
    out = {}
    if root is None:
        return out
    for r in root.iter(_REL + "Relationship"):
        target = r.get("Target") or ""
        if r.get("TargetMode") == "External":
            continue
        path = posixpath.normpath(posixpath.join(d, target)) if not target.startswith("/") else target.lstrip("/")
        out[r.get("Id")] = ((r.get("Type") or "").rsplit("/", 1)[-1], path)
    return out


def _slide_parts(zf) -> list:
    pres = _zip_xml(zf, "ppt/presentation.xml")
    if pres is None:
        raise ExtractError(".pptx 解析失败(没有 ppt/presentation.xml);文件可能损坏,或是旧版 .ppt 改的后缀")
    rels = _rels(zf, "ppt/presentation.xml")
    parts = []
    for sid in pres.iter(_P + "sldId"):
        rel = rels.get(sid.get(_R + "id"))
        if rel and rel[0] == "slide":
            parts.append(rel[1])
    return parts[:_PPTX_MAX_SLIDES]


def _para_text(p) -> str:
    out = []
    for node in p.iter():
        if node.tag == _A + "t" and node.text:
            out.append(node.text)
        elif node.tag == _A + "br":
            out.append("\n")
    return "".join(out).strip()


def _cell_text(tc) -> str:
    return " ".join(t for t in (_para_text(p) for p in tc.iter(_A + "p")) if t).replace("|", "\\|")


def _slide_lines(sld) -> tuple:
    """(标题, 正文行)。占位符 title / ctrTitle 是标题;表格转 markdown 表;其余文本按层级缩进成列表。"""
    title, lines = "", []
    tree = sld.find(_P + "cSld/" + _P + "spTree")
    if tree is None:
        return title, lines
    for node in tree.iter():
        if node.tag == _P + "sp":
            ph = node.find(_P + "nvSpPr/" + _P + "nvPr/" + _P + "ph")
            body = node.find(_P + "txBody")
            if body is None:
                continue
            paras = [(int((p.find(_A + "pPr").get("lvl") if p.find(_A + "pPr") is not None else 0) or 0), _para_text(p))
                     for p in body.iter(_A + "p")]
            paras = [(lvl, t) for lvl, t in paras if t]
            if ph is not None and ph.get("type") in _PH_SKIP:           # 页码、日期、页脚、页眉不是内容
                continue
            if ph is not None and ph.get("type") in ("title", "ctrTitle") and not title:
                title = " ".join(t for _l, t in paras)
                continue
            for lvl, t in paras:
                lines.append("%s- %s" % ("  " * min(lvl, 4), t.replace("\n", " ")))
        elif node.tag == _A + "tbl":
            rows = [[_cell_text(tc) for tc in tr.iter(_A + "tc")] for tr in node.iter(_A + "tr")]
            rows = [r for r in rows if any(c for c in r)]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                lines.append("")
                lines.append("| " + " | ".join(rows[0]) + " |")
                lines.append("|" + "---|" * width)
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                lines.append("")
    return title, lines


def pptx_slides(path: pathlib.Path) -> list:
    """[(页码, 标题, 正文行, 备注, 本页图片的 zip 内路径)]。"""
    import zipfile
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ExtractError(f".pptx 解析失败({exc});文件可能损坏,或是旧版 .ppt 改的后缀") from exc
    with zf:
        out = []
        for n, part in enumerate(_slide_parts(zf), 1):
            sld = _zip_xml(zf, part)
            if sld is None:
                continue
            title, lines = _slide_lines(sld)
            rels = _rels(zf, part)
            notes, media = "", []
            for kind, target in rels.values():
                if kind == "notesSlide":
                    ns = _zip_xml(zf, target)
                    if ns is not None:
                        _t, nlines = _slide_lines(ns)
                        notes = " ".join(l.lstrip("- ").strip() for l in nlines if l.strip() and not l.startswith("|"))
                elif kind == "image":
                    media.append(target)
            out.append((n, title, lines, notes, sorted(set(media))))
        return out


def extract_pptx(path: pathlib.Path) -> str:
    slides = pptx_slides(path)
    if not slides:
        raise ExtractError(".pptx 里没有幻灯片")
    blocks = []
    for n, title, lines, notes, media in slides:
        block = ["## 第 %d 页%s" % (n, ":" + title if title else ""), ""]
        block += lines or ["(本页没有文字)"]
        if notes:
            block += ["", "> 备注:" + notes]
        if media:
            block += ["", "(本页有图片:%s)" % "、".join(m.rsplit("/", 1)[-1] for m in media)]
        blocks.append("\n".join(block))
    return "\n\n".join(blocks) + "\n"


def pptx_media(path: pathlib.Path, out_dir: pathlib.Path, max_files: int = 60, max_bytes: int = 20 * 1024 * 1024) -> list:
    """把幻灯片里的图片取到 out_dir(文件名 slideNN-原名),返回 [(页码, 路径)]。只取常见位图;超出上限的不取。"""
    import zipfile
    keep = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")
    out, total = [], 0
    with zipfile.ZipFile(path) as zf:
        slides = _slide_parts(zf)
        seen = set()
        for n, part in enumerate(slides, 1):
            for kind, target in sorted(_rels(zf, part).values()):
                if kind != "image" or not target.lower().endswith(keep) or (n, target) in seen:
                    continue
                seen.add((n, target))
                try:
                    data = zf.read(target)
                except KeyError:
                    continue
                if len(out) >= max_files or total + len(data) > max_bytes:
                    return out
                out_dir.mkdir(parents=True, exist_ok=True)
                dst = out_dir / ("slide%02d-%s" % (n, target.rsplit("/", 1)[-1]))
                dst.write_bytes(data)
                total += len(data)
                out.append((n, dst))
    return out


# ---------------------------------------------------------------- 入口

def extract_source(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return read_text_file(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".doc":
        return extract_doc(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".pptx":
        return extract_pptx(path)
    if suffix == ".ppt":
        raise ExtractError("老格式 .ppt 读不了:请用 PowerPoint / WPS 另存为 .pptx 再导入")
    raise ExtractError(f"不支持的格式 {suffix}(支持 .txt/.md/.docx/.doc/.pdf/.pptx)")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
