#!/usr/bin/env python3
"""检索镜像里的根规范(/workspace/docs/)。本行知识库不可用或查不到时,智能体用它找规范原文。

    python3 $GSDB_ROOTSPEC_HOME/search_docs.py "关键词" ["关键词2" ...]      搜索:按命中的关键词数排序,给出所在章节与原文行
    python3 $GSDB_ROOTSPEC_HOME/search_docs.py --show <文件> [--from N] [--lines M]   读全文(docx / pdf 先转成文字)
    python3 $GSDB_ROOTSPEC_HOME/search_docs.py --list                         列出有哪些根规范

规范目录:$ROOTSPEC_DIR > $GSDB_ROOTSPEC_HOME/docs > 本脚本旁边的 docs/。

支持 .md / .markdown / .txt / .docx;.pdf 在装了 pdftotext 时也能搜。文件第一行带 ROOTSPEC-TEMPLATE 标记的是镜像里预留的模板,
还没换成正式规范:列出来但标明「模板」,不参与检索。只读,不写任何文件,不联网。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile
from typing import Dict, List, Optional, Tuple

DOCS = pathlib.Path(os.environ.get("ROOTSPEC_DIR")
                    or (os.environ["GSDB_ROOTSPEC_HOME"].rstrip("/") + "/docs" if os.environ.get("GSDB_ROOTSPEC_HOME") else "")
                    or pathlib.Path(__file__).resolve().parent / "docs")
SELF = "$GSDB_ROOTSPEC_HOME/search_docs.py"      # 提示里写变量,不写镜像里的绝对路径
TEMPLATE_MARK = "ROOTSPEC-TEMPLATE"
SUFFIXES = (".md", ".markdown", ".txt", ".docx", ".pdf")
HEADING_MD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
HEADING_CN = re.compile(r"^(第[一二三四五六七八九十百零\d]+[章节条部分篇]|[一二三四五六七八九十]+、|\d+(\.\d+){0,3}[\s、.])")
MAX_FILES, MAX_HITS = 10, 6


HEADING_STYLE = re.compile(r"^(?:heading|标题)\s*(\d)$", re.I)


def docx_heading_levels(z: zipfile.ZipFile) -> Dict[str, int]:
    """样式 ID → 标题级别。中文版 Word 的标题样式 ID 常是「1」「2」,名字(heading 1)只在 styles.xml 里。"""
    try:
        xml = z.read("word/styles.xml").decode("utf-8", "replace")
    except KeyError:
        return {}
    levels = {}
    for sid, body in re.findall(r'<w:style\b[^>]*w:styleId="([^"]+)"[^>]*>(.*?)</w:style>', xml, re.S):
        name = re.search(r'<w:name w:val="([^"]+)"', body)
        m = HEADING_STYLE.match(name.group(1).strip()) if name else None
        if m:
            levels[sid] = int(m.group(1))
    return levels


def docx_lines(path: pathlib.Path) -> List[str]:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
        levels = docx_heading_levels(z)
    out = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)).strip()
        if not text:
            continue
        style = re.search(r'<w:pStyle w:val="([^"]+)"', p)
        sid = style.group(1) if style else ""
        level = levels.get(sid) or (int(sid[-1]) if re.match(r"^(Heading|heading|标题)\s*\d$", sid) else 0)
        out.append(("#" * level + " " + text) if level else text)
    return out


def pdf_lines(path: pathlib.Path) -> Optional[List[str]]:
    if not shutil.which("pdftotext"):
        return None
    p = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=120)
    return [l.rstrip() for l in p.stdout.splitlines()] if p.returncode == 0 else None


def read_lines(path: pathlib.Path) -> Optional[List[str]]:
    suf = path.suffix.lower()
    try:
        if suf == ".docx":
            return docx_lines(path)
        if suf == ".pdf":
            return pdf_lines(path)
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        print("读不了 %s:%s" % (path.name, exc), file=sys.stderr)
        return None


def is_template(lines: List[str]) -> bool:
    return any(TEMPLATE_MARK in l for l in lines[:3])


def title_of(path: pathlib.Path, lines: List[str]) -> str:
    for l in lines[:40]:
        m = HEADING_MD.match(l)
        if m and len(m.group(1)) == 1:
            return m.group(2).strip()
    return path.stem


def docs() -> List[pathlib.Path]:
    if not DOCS.is_dir():
        return []
    return sorted(p for p in DOCS.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIXES and not p.name.startswith("."))


def heading_path(lines: List[str], upto: int) -> str:
    """第 upto 行所在的章节路径(最近的各级标题)。"""
    stack: List[Tuple[int, str]] = []
    for l in lines[:upto + 1]:
        m = HEADING_MD.match(l)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
        elif HEADING_CN.match(l.strip()) and len(l.strip()) <= 40:
            text = l.strip()
            level = 2 if text.startswith("第") else 3
        else:
            continue
        stack = [s for s in stack if s[0] < level] + [(level, text)]
    return " > ".join(t for lv, t in stack if lv > 1) or "(开头)"


def search(terms: List[str]) -> int:
    files = docs()
    if not files:
        print("%s 里还没有根规范。" % DOCS)
        return 0
    terms = [t.lower() for t in terms if t.strip()]
    results, templates, unreadable = [], [], []
    for path in files:
        lines = read_lines(path)
        if lines is None:
            unreadable.append(path.name)
            continue
        if is_template(lines):
            templates.append(path.name)
            continue
        hits, found = [], set()
        for i, line in enumerate(lines):
            low = line.lower()
            got = {t for t in terms if t in low}
            if got:
                found |= got
                hits.append((i, line.strip()))
        if hits:
            results.append((len(found), len(hits), path, lines, hits))
    results.sort(key=lambda r: (-r[0], -r[1], r[2].name))
    if not results:
        print("根规范里没有找到「%s」。" % "、".join(terms))
    for n_terms, n_hits, path, lines, hits in results[:MAX_FILES]:
        print("\n## 《%s》 %s  (命中 %d/%d 个关键词,%d 处)" % (title_of(path, lines), path, n_terms, len(terms), n_hits))
        for i, text in hits[:MAX_HITS]:
            print("  第 %d 行 [%s] %s" % (i + 1, heading_path(lines, i), text[:160]))
        if n_hits > MAX_HITS:
            print("  …… 另有 %d 处" % (n_hits - MAX_HITS))
        how = "read 工具" if path.suffix.lower() in (".md", ".markdown", ".txt") else "python3 %s --show %s" % (SELF, path)
        print("  读全文:%s" % how)
    if templates:
        print("\n(模板,还没有换成正式规范,不得引用:%s)" % "、".join(templates))
    if unreadable:
        print("(读不了:%s;pdf 需要 pdftotext)" % "、".join(unreadable))
    return 0


def show(name: str, start: int, count: int) -> int:
    path = pathlib.Path(name)
    if not path.is_absolute():
        path = DOCS / name
    try:
        path.resolve().relative_to(DOCS.resolve())
    except ValueError:
        print("只读 %s 下的根规范" % DOCS, file=sys.stderr)
        return 2
    lines = read_lines(path) if path.is_file() else None
    if lines is None:
        print("读不了 %s" % path, file=sys.stderr)
        return 1
    if is_template(lines):
        print("(这是模板,还没有换成正式规范,不得引用)")
    begin = max(1, start)
    for i, line in enumerate(lines[begin - 1:begin - 1 + count], begin):
        print("%5d  %s" % (i, line))
    if begin - 1 + count < len(lines):
        print("…… 共 %d 行,接着看加 --from %d" % (len(lines), begin + count))
    return 0


def list_docs() -> int:
    files = docs()
    if not files:
        print("%s 里还没有根规范。" % DOCS)
    for path in files:
        lines = read_lines(path) or []
        print("%s  《%s》%s" % (path.name, title_of(path, lines), "  (模板,未替换)" if is_template(lines) else ""))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="检索镜像里的根规范(%s)" % DOCS)
    ap.add_argument("terms", nargs="*", help="关键词(多个按命中数排序)")
    ap.add_argument("--show", metavar="文件", help="读一份根规范的全文")
    ap.add_argument("--from", dest="start", type=int, default=1, help="--show 从第几行开始")
    ap.add_argument("--lines", type=int, default=200, help="--show 读多少行")
    ap.add_argument("--list", action="store_true", help="列出有哪些根规范")
    a = ap.parse_args(argv)
    if a.show:
        return show(a.show, a.start, a.lines)
    if a.list:
        return list_docs()
    if not a.terms:
        ap.print_usage()
        return 2
    return search(a.terms)


if __name__ == "__main__":
    sys.exit(main())
