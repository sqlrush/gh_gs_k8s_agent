"""把一份文档变成纯文本:txt / md / docx / doc / pdf。

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
        problem = pdf_extraction_problem(text, pdf_page_count(path.read_bytes()))
        if problem:
            raise ExtractError(problem)
        return text

    raise ExtractError(
        "PDF 转换失败(" + ";".join(tried) + ")。\n"
        "请安装 poppler:macOS `brew install poppler`,"
        "Debian/Ubuntu `apt install poppler-utils`,CentOS `yum install poppler-utils`。\n"
        "或先手工把 PDF 转成 .txt / .docx 再导入。"
    )


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
    raise ExtractError(f"不支持的格式 {suffix}(支持 .txt/.md/.docx/.doc/.pdf)")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
