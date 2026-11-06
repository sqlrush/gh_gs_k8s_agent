"""官方文档页面(华为 DITA 导出的 HTML)→ markdown 行。

只保留正文:标题、段落、列表、表格(转 markdown 表)、代码块、说明框;
导航(父主题、子主题链接列表)、页脚(版权、上一节 / 下一节)、脚本样式一律丢掉。
页内的 h4(`<h4 class="sectiontitle">`)是小节标题(其余层级输出成 `####`)——GUC 参数页里一个 h4 就是一个参数,
故障处理页里是「现象描述 / 可能原因 / 处理步骤」,切条目、切检索块都靠它,所以输出成 `### 标题`。

纯函数,无 I/O 以外的依赖;编码按页面声明(华为 CHM 是 gb2312),声明缺失时 utf-8 → gb18030 依次试。
"""
from __future__ import annotations

import pathlib
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

_SKIP_CLASSES = ("parentlink", "familylinks", "related-links", "ullinks", "footer", "navfooter", "relinfo")
_BLOCK = ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "pre", "div", "tr", "caption")
_FOOTER_RE = re.compile(r"^(版权所有|父主题[:：]|< 上一节|下一节 >|Copyright)")


class _Md(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.buf: List[str] = []
        self.stack: List[Tuple[bool, str]] = []   # (是否跳过区, 标签)
        self.skip = 0
        self.table: Optional[List[List[str]]] = None
        self.row: Optional[List[str]] = None
        self.cell: Optional[List[str]] = None
        self.pre = 0

    def _flush(self) -> str:
        text = "".join(self.buf)
        self.buf = []
        return text if self.pre else re.sub(r"\s+", " ", text).strip()

    def _emit(self) -> None:
        text = self._flush()
        if text:
            self.out.append(text)

    def handle_starttag(self, tag: str, attrs) -> None:
        cls = (dict(attrs).get("class") or "").split()
        if self.skip or tag in ("script", "style", "head", "title") or any(c in _SKIP_CLASSES for c in cls):
            if tag not in ("br", "img", "hr", "meta", "link", "input"):
                self.stack.append((True, tag))
                self.skip += 1
            return
        if tag in ("br", "img", "hr", "meta", "link", "input"):
            if tag == "br":
                (self.cell if self.cell is not None else self.buf).append(" " if self.cell is not None else "\n")
            return
        self.stack.append((False, tag))
        if self.cell is not None:
            if tag in ("p", "li", "div"):
                self.cell.append(" ")
            return
        if tag in _BLOCK and tag != "tr":
            self._emit()
        if tag == "li":
            self.buf.append("- ")
        elif tag == "pre":
            self.pre += 1
            self.buf.append("```\n")
        elif tag == "table":
            self._emit()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        skipped, t = self.stack.pop()
        while t != tag and self.stack:               # 容错:没闭合的标签一路弹到对得上为止
            if skipped:
                self.skip -= 1
            skipped, t = self.stack.pop()
        if skipped:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip().replace("|", "\\|"))
            self.cell = None
        elif self.cell is not None:
            return
        elif tag == "tr" and self.row is not None and self.table is not None:
            if any(self.row):
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            if self.table:
                self.out.append(md_table(self.table))
            self.table = None
        elif tag == "pre":
            self.pre -= 1
            self.out.append("".join(self.buf).rstrip() + "\n```")
            self.buf = []
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self._flush()
            if text:
                # h4 是小节(### );其余层级降成 ####,不与条目文件的「## 小节」撞
                self.out.append({"h1": "# ", "h4": "### "}.get(tag, "#### ") + text)
        elif tag in _BLOCK:
            self._emit()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        (self.cell if self.cell is not None else self.buf).append(data)

    def close(self) -> None:
        super().close()
        self._emit()


def md_table(rows: List[List[str]]) -> str:
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return "\n".join(["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
                     + ["| " + " | ".join(r) + " |" for r in rows[1:]])


def decode(raw: bytes) -> str:
    head = raw[:2048].decode("ascii", "ignore").lower()
    m = re.search(r'charset=["\']?([\w-]+)', head)
    declared = m.group(1) if m else ""
    encodings = (["gb18030"] if declared in ("gb2312", "gbk", "gb18030") else []) + ["utf-8", "gb18030"]
    for enc in encodings:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def to_lines(html: str) -> List[str]:
    """一页 HTML → markdown 行(标题 `# `、小节 `### `、表格整块一行)。导航与页脚已去掉。"""
    body = html[html.lower().find("<body"):] if "<body" in html.lower() else html
    p = _Md()
    p.feed(body)
    p.close()
    return [x for x in p.out if x.strip() and not _FOOTER_RE.match(x.strip())]


def page_lines(path: pathlib.Path) -> List[str]:
    return to_lines(decode(pathlib.Path(path).read_bytes()))


def body_chars(lines: List[str]) -> int:
    """正文字数(去掉页标题与空白):判断是不是只有目录的导航页。"""
    return len(re.sub(r"\s", "", "".join(x for x in lines if not x.startswith("# "))))


__all__ = ["to_lines", "page_lines", "decode", "md_table", "body_chars"]
