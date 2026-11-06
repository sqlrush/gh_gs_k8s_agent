"""内网网页剪藏 → 干净的 md(2026-10-27)。

需要登录的内网 Wiki / Confluence / 门户页面,服务端抓不到(带不上用户浏览器的登录状态)。所以由管理员在
**已登录的浏览器**里点书签「导入知识库」:书签取出页面(JS 渲染之后的 DOM)与同源图片,经剪藏页交给导入环境的
接收服务,落成一个剪藏批次:

    <kb>/inbox/clips/<批次>/
        meta.json            来源 URL、标题、抓取时间、工号、图片清单与没取回的原因
        <批次>.html          原样快照(或用户拖入的 <批次>.mhtml「网页,单个文件」)
        images/              页内图片

本模块把批次转成 page.md(找正文、去噪、保留结构),之后的整理入库走现有流程(工单 → kb-init 六要素;
规范 → ingest 条款化;其余 → 八类模板),原始快照作为 L3 证据 attach 到条目上。

**抽不出来不假装抽出来了**:剪到登录页、无权限页、空白页时拒绝并说明 —— 不把「请登录」入库成一条知识。
纯标准库;HTML 解析容错(内网页面的 HTML 往往不规范)。
"""
from __future__ import annotations

import datetime as _dt
import email
import email.policy
import hashlib
import json
import pathlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

from . import htmldoc

CLIPS = ("inbox", "clips")            # <kb>/inbox/clips
META = "meta.json"
PAGE_MD = "page.md"
INGEST = "ingest.json"
MAX_MD_CHARS = 400_000                # 再长的页面也不是一条知识:截断并说明

_VOID = frozenset("area base br col embed hr img input link meta source track wbr param".split())
_DROP = frozenset("script style noscript template svg iframe canvas object embed button input select textarea "
                  "option head link meta map audio video".split())
_BLOCK_NOISE_TAGS = frozenset({"nav", "footer", "aside"})
_ROLE_NOISE = frozenset({"navigation", "banner", "contentinfo", "complementary", "search", "dialog",
                         "menu", "menubar", "toolbar", "alert"})
_NOISE_RE = re.compile(
    r"(?:^|[-_\s])(?:nav|navbar|navigation|menu|menubar|sidebar|side-bar|sidenav|breadcrumbs?|footer|comments?|"
    r"comment-list|reply|share|social|likes?|like-button|toolbar|banner|advert|ads|related|recommend(?:ation)?s?|"
    r"pagination|pager|cookie|modal|popup|topbar|header-nav|page-metadata|labels?-section|tags|rating|feedback|"
    r"skip-?link|print-only|sr-only|visually-hidden)(?:$|[-_\s])", re.I)
# 正文容器:按顺序找,第一个文字够多的就是。Confluence 的正文在 #main-content.wiki-content
_MAIN_IDS = ("main-content", "maincontent", "content", "main", "article", "post", "page-content")
_MAIN_CLASSES = ("wiki-content", "markdown-body", "post-content", "entry-content", "article-content", "article-body",
                 "rich-text", "content-body", "page-content", "doc-content", "main-content")
_MIN_MAIN_CHARS = 140
_LOGIN_RE = re.compile(r"登录|登入|统一认证|单点登录|请先登|sign\s*in|log\s*in|login|身份认证|会话已过期|session expired", re.I)
_DENIED_RE = re.compile(r"没有权限|无权访问|权限不足|access denied|forbidden|403|404|页面不存在|not found", re.I)
_ADMONITION = (("warning", "警告"), ("note", "注意"), ("tip", "提示"), ("information", "说明"), ("info", "说明"),
               ("success", "提示"), ("danger", "警告"), ("caution", "注意"))


class ClipError(Exception):
    """批次不存在 / 格式不对 / 剪到的不是正文。消息直接给用户看,写明下一步。"""


# ---------------------------------------------------------------- 容错的 DOM

class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Optional[Dict[str, str]] = None, parent: "Optional[Node]" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: List[object] = []          # Node 或 str
        self.parent = parent

    def cls(self) -> str:
        return "%s %s" % (self.attrs.get("class", ""), self.attrs.get("id", ""))

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def text(self) -> str:
        out: List[str] = []

        def walk(n: "Node") -> None:
            for c in n.children:
                if isinstance(c, str):
                    out.append(c)
                elif c.tag not in _DROP:
                    walk(c)
        walk(self)
        return re.sub(r"\s+", " ", "".join(out)).strip()


_AUTO_CLOSE = {"li": ("ul", "ol"), "dt": ("dl",), "dd": ("dl",), "tr": ("table", "tbody", "thead", "tfoot"),
               "td": ("tr",), "th": ("tr",), "p": (), "option": ("select",)}
_P_CLOSERS = frozenset("address article aside blockquote div dl fieldset figure footer form h1 h2 h3 h4 h5 h6 "
                       "header hr main nav ol p pre section table ul".split())


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def _open(self, tag: str) -> Optional[Node]:
        n = self.cur
        while n is not None and n is not self.root:
            if n.tag == tag:
                return n
            n = n.parent
        return None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _AUTO_CLOSE:
            stop = _AUTO_CLOSE[tag]
            n = self.cur
            while n is not self.root and n.tag not in stop:
                if n.tag == tag:
                    self.cur = n.parent
                    break
                n = n.parent
        if tag in _P_CLOSERS and self.cur.tag == "p":
            self.cur = self.cur.parent
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        self.cur.children.append(Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        tag = tag.lower()
        n = self._open(tag)
        if n is not None:
            self.cur = n.parent or self.root

    def handle_data(self, data):
        if data:
            self.cur.children.append(data)


def parse_html(html: str) -> Node:
    t = _Tree()
    t.feed(html)
    t.close()
    return t.root


def _find(root: Node, tag: str) -> Optional[Node]:
    return next((n for n in root.iter() if n.tag == tag), None)


def _hidden(n: Node) -> bool:
    style = n.attrs.get("style", "").replace(" ", "").lower()
    return ("hidden" in n.attrs and n.tag not in ("input",)) or "display:none" in style or n.attrs.get("aria-hidden") == "true"


# ---------------------------------------------------------------- 找正文

def find_main(root: Node) -> Node:
    """正文容器:<main> → <article> → role=main → 常见 id / class(含 Confluence)→ body。要文字够多才算。"""
    body = _find(root, "body") or root
    preds = [
        lambda n: n.tag == "main",
        lambda n: n.attrs.get("role") == "main",
        lambda n: n.attrs.get("id", "").lower() in _MAIN_IDS[:2],
        lambda n: any(c in n.attrs.get("class", "").lower().split() for c in _MAIN_CLASSES),
        lambda n: n.tag == "article",
        lambda n: n.attrs.get("id", "").lower() in _MAIN_IDS,
    ]
    for pred in preds:
        cands = [n for n in body.iter() if pred(n) and not _hidden(n)]
        cands = [n for n in cands if len(n.text()) >= _MIN_MAIN_CHARS]
        if cands:
            # 同一条件命中多个(如两个 article):取文字最多的
            return max(cands, key=lambda n: len(n.text()))
    return body


def prune_noise(main: Node) -> int:
    """去掉正文里的导航、页头页脚、侧栏、评论、分享等。占正文六成以上的块不删(防止把内容容器当噪音)。"""
    total = max(1, len(main.text()))
    removed = 0

    def walk(n: Node) -> None:
        nonlocal removed
        keep: List[object] = []
        for c in n.children:
            if isinstance(c, Node):
                noisy = (c.tag in _DROP or _hidden(c)
                         or c.tag in _BLOCK_NOISE_TAGS
                         or (c.tag == "header" and not _inside(c, ("article", "main", "section")))
                         or c.attrs.get("role") in _ROLE_NOISE
                         or bool(_NOISE_RE.search(c.cls())))
                if noisy and (c.tag in _DROP or len(c.text()) < 0.6 * total):
                    removed += 1
                    continue
                walk(c)
            keep.append(c)
        n.children = keep
    walk(main)
    return removed


def _inside(n: Node, tags: Sequence[str]) -> bool:
    p = n.parent
    while p is not None:
        if p.tag in tags:
            return True
        p = p.parent
    return False


# ---------------------------------------------------------------- md 渲染

_INLINE_WS = re.compile(r"[ \t\r\n\f\v\u00a0\u200b\u3000]+")


class _Md:
    def __init__(self, base_url: str, images: Dict[str, str]):
        self.base = base_url
        self.images = images                      # 绝对 URL → 相对路径(images/xxx)
        self.missing: List[str] = []
        self.used: List[str] = []
        self.tables = 0
        self.headings = 0

    # 行内 ------------------------------------------------------------------
    def inline(self, n: Node) -> str:
        out: List[str] = []
        for c in n.children:
            if isinstance(c, str):
                out.append(_INLINE_WS.sub(" ", c))
                continue
            if c.tag in _DROP or _hidden(c):
                continue
            t = c.tag
            if t == "br":
                out.append("\n")
            elif t == "img":
                out.append(self.image(c))
            elif t in ("code", "kbd", "samp", "tt") and not _inside(c, ("pre",)):
                txt = c.text()
                if txt:
                    out.append("`%s`" % txt.replace("`", "'"))
            elif t in ("strong", "b"):
                txt = self.inline(c).strip()
                if txt:
                    out.append("**%s**" % txt)
            elif t == "a":
                txt = self.inline(c).strip()
                href = c.attrs.get("href", "").strip()
                url = urljoin(self.base, href) if href else ""
                if txt and url.startswith(("http://", "https://")) and url.split("#")[0] != self.base.split("#")[0] \
                        and txt != url:
                    out.append("[%s](%s)" % (txt, url))
                else:
                    out.append(txt)
            elif t in ("ul", "ol", "table", "pre", "blockquote", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
                out.append(" " + self.inline(c) + " ")          # 块元素出现在行内上下文里:压成一行
            else:
                out.append(self.inline(c))
        return "".join(out)

    def image(self, n: Node) -> str:
        src = (n.attrs.get("src") or n.attrs.get("data-src") or "").strip()
        alt = (n.attrs.get("alt") or n.attrs.get("title") or "").strip().replace("]", "")
        if not src or src.startswith("data:image/gif"):           # 1×1 占位、表情
            return ""
        url = urljoin(self.base, src)
        local = self.images.get(url) or self.images.get(src)
        if local:
            self.used.append(local)
            return "![%s](%s)" % (alt or "图", local)
        self.missing.append(url)
        return "[图片未取回:%s](%s)" % (alt or url.rsplit("/", 1)[-1][:40], url)

    # 块 --------------------------------------------------------------------
    def blocks(self, n: Node, depth: int = 0) -> List[str]:
        out: List[str] = []
        para: List[Node] = []

        def flush() -> None:
            if para:
                holder = Node("#p")
                holder.children = list(para)
                txt = _clean_lines(self.inline(holder))
                if txt:
                    out.append(txt)
                para.clear()

        for c in n.children:
            if isinstance(c, str) or c.tag in ("a", "span", "strong", "b", "em", "i", "code", "img", "br", "font",
                                                "small", "sup", "sub", "u", "mark", "abbr", "label", "time", "kbd"):
                para.append(c)
                continue
            if c.tag in _DROP or _hidden(c):
                continue
            flush()
            out += self.block(c, depth)
        flush()
        return out

    def block(self, c: Node, depth: int) -> List[str]:
        t = c.tag
        if re.fullmatch(r"h[1-6]", t):
            txt = _clean_lines(self.inline(c)).replace("\n", " ")
            if not txt:
                return []
            self.headings += 1
            return ["%s %s" % ("#" * int(t[1]), txt)]
        if t == "p":
            txt = _clean_lines(self.inline(c))
            return [txt] if txt else []
        if t in ("ul", "ol"):
            return [self.list_md(c, depth)] if c.children else []
        if t == "pre":
            return [self.code(c)]
        if t == "table":
            return self.table(c, depth)
        if t == "blockquote":
            inner = self.blocks(c, depth)
            return ["\n".join("> " + l if l else ">" for l in "\n\n".join(inner).splitlines())] if inner else []
        if t == "hr":
            return ["---"]
        if t == "dl":
            lines: List[str] = []
            for d in c.children:
                if isinstance(d, Node) and d.tag == "dt":
                    lines.append("**%s**" % _clean_lines(self.inline(d)))
                elif isinstance(d, Node) and d.tag == "dd":
                    lines.append(_clean_lines(self.inline(d)))
            return ["\n".join(l for l in lines if l.strip("*"))] if lines else []
        if t == "figure":
            return self.blocks(c, depth)
        if t == "figcaption":
            txt = _clean_lines(self.inline(c))
            return ["*%s*" % txt] if txt else []
        if t == "div" and _admonition(c):
            label = _admonition(c)
            inner = self.blocks(c, depth)
            if not inner:
                return []
            lines = "\n\n".join(inner).splitlines()
            first = "> **%s**:%s" % (label, (" " + lines[0]) if lines else "")
            return ["\n".join([first] + ["> " + l if l else ">" for l in lines[1:]])]
        return self.blocks(c, depth)                  # div / section / article / main / form / td 等容器

    def list_md(self, n: Node, depth: int) -> str:
        lines: List[str] = []
        k = 0
        for li in n.children:
            if not isinstance(li, Node) or li.tag != "li":
                continue
            k += 1
            mark = "%d." % k if n.tag == "ol" else "-"
            inline_part = Node("#li")
            nested: List[Node] = []
            for c in li.children:
                if isinstance(c, Node) and c.tag in ("ul", "ol"):
                    nested.append(c)
                elif isinstance(c, Node) and c.tag in ("pre", "table"):
                    nested.append(c)
                else:
                    inline_part.children.append(c)
            txt = _clean_lines(self.inline(inline_part)).replace("\n", " ")
            lines.append("%s%s %s" % ("  " * depth, mark, txt or ""))
            for c in nested:
                if c.tag in ("ul", "ol"):
                    lines.append(self.list_md(c, depth + 1))
                else:
                    for b in self.block(c, depth + 1):
                        lines.append("\n".join("  " * (depth + 1) + l for l in b.splitlines()))
        return "\n".join(lines)

    def code(self, n: Node) -> str:
        txt = _raw_text(n).strip("\n")
        cls = " ".join("%s %s %s" % (c.cls(), c.attrs.get("data-syntaxhighlighter-params", ""),
                                     c.attrs.get("data-language", "") and "language-" + c.attrs["data-language"])
                       for c in n.iter() if c is n or c.tag == "code")
        m = re.search(r"(?:language-|lang-|brush:\s*)([a-z0-9+#-]+)", cls, re.I)
        lang = (m.group(1).lower() if m else "")
        fence = "````" if "```" in txt else "```"
        return "%s%s\n%s\n%s" % (fence, lang, txt, fence)

    def table(self, n: Node, depth: int) -> List[str]:
        rows: List[List[Node]] = []
        for tr in n.iter():
            if tr.tag == "tr" and _nearest(tr, "table") is n:
                cells = [c for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")]
                if cells:
                    rows.append(cells)
        if not rows:
            return []
        nested = any(x.tag == "table" for r in rows for c in r for x in c.iter() if x is not c)
        layout = max(len(r) for r in rows) == 1 or nested
        if layout:                                    # 排版用的表:按块输出
            out: List[str] = []
            for r in rows:
                for c in r:
                    out += self.blocks(c, depth)
            return out
        self.tables += 1
        width = max(len(r) for r in rows)
        grid = []
        for r in rows:
            cells = [_clean_lines(self.inline(c)).replace("\n", "<br>").replace("|", "\\|") for c in r]
            grid.append(cells + [""] * (width - len(cells)))
        head, body = grid[0], grid[1:]
        md = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
        md += ["| " + " | ".join(r) + " |" for r in body]
        return ["\n".join(md)]


def _nearest(n: Node, tag: str) -> Optional[Node]:
    p = n.parent
    while p is not None and p.tag != tag:
        p = p.parent
    return p


def _admonition(n: Node) -> str:
    cls = n.attrs.get("class", "").lower()
    if not any(k in cls for k in ("information-macro", "admonition", "callout", "alert", "panel-warning", "panel-info")):
        return ""
    for key, label in _ADMONITION:
        if key in cls:
            return label
    return "说明"


def _raw_text(n: Node) -> str:
    out: List[str] = []

    def walk(x: Node) -> None:
        for c in x.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag == "br":
                out.append("\n")
            elif c.tag not in _DROP:
                walk(c)
                if c.tag in ("div", "p", "li", "tr") and out and not out[-1].endswith("\n"):
                    out.append("\n")
    walk(n)
    return "".join(out).replace("\u00a0", " ")


def _clean_lines(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n")]
    return "\n".join(l for l in lines if l).strip()


def html_to_markdown(html: str, base_url: str = "", images: Optional[Dict[str, str]] = None,
                     title: str = "") -> Tuple[str, Dict[str, object]]:
    """网页 → (md 正文, 统计)。统计:chars / tables / headings / images_used / images_missing / noise_removed / login_hint。"""
    root = parse_html(html)
    page_title = title or (_find(root, "title").text() if _find(root, "title") else "")
    has_password = any(n.tag == "input" and n.attrs.get("type", "").lower() == "password" for n in root.iter())
    main = find_main(root)
    removed = prune_noise(main)
    r = _Md(base_url, images or {})
    blocks = [b for b in r.blocks(main) if b.strip()]
    md = "\n\n".join(blocks)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    if page_title and not re.match(r"#\s", md):
        md = "# %s\n\n%s" % (page_title.strip(), md)
    plain = re.sub(r"!\[[^\]]*\]\([^)]*\)|\[图片未取回:[^\]]*\]\([^)]*\)|[#>*`|\-]", "", md)
    chars = len(re.sub(r"\s+", "", plain)) - len(re.sub(r"\s+", "", page_title))
    stats = {"chars": max(chars, 0), "tables": r.tables, "headings": r.headings,
             "images_used": len(set(r.used)), "images_missing": sorted(set(r.missing)), "noise_removed": removed,
             "has_password_input": has_password, "title": page_title.strip()}
    return md, stats


def refusal(stats: Dict[str, object], url: str = "") -> Optional[str]:
    """剪到的不是正文时的拒绝理由(None = 正常)。纯函数。"""
    chars = int(stats.get("chars") or 0)
    head = "%s %s" % (stats.get("title") or "", url)
    if chars < 200 and (stats.get("has_password_input") or _LOGIN_RE.search(head)):
        return ("剪到的是**登录页**(正文只有 %d 个字,页面里有登录框或标题是登录)。"
                "请先在浏览器里登录这个网站、打开要导入的那一页,再点书签「导入知识库」。" % chars)
    if chars < 60 and _DENIED_RE.search(head):
        return "剪到的是**无权限或不存在的页面**(%s)。请确认账号能打开这一页后再剪藏。" % (stats.get("title") or url)
    if chars < 30:
        return ("这一页**抽不到正文**(只有 %d 个字)。可能是正文在内嵌框架里、或页面还没加载完。"
                "等页面完全加载后再点一次书签;仍不行就用浏览器「另存为 → 网页,单个文件(.mhtml)」拖进剪藏页上传。" % chars)
    return None


# ---------------------------------------------------------------- mhtml

def parse_mhtml(raw: bytes) -> Tuple[str, str, Dict[str, Tuple[str, bytes]]]:
    """「网页,单个文件」→ (html, 页面 URL, {资源 URL: (类型, 字节)})。只收图片资源。"""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    html_part = None
    res: Dict[str, Tuple[str, bytes]] = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        loc = (part.get("Content-Location") or "").strip()
        if ctype == "text/html" and html_part is None:
            html_part = part
        elif ctype.startswith("image/") and loc:
            data = part.get_payload(decode=True) or b""
            if data:
                res[loc] = (ctype, data)
    if html_part is None:
        raise ClipError("这不是有效的 .mhtml(找不到网页正文部分);请用浏览器「另存为 → 网页,单个文件」重新保存")
    payload = html_part.get_payload(decode=True) or b""
    charset = html_part.get_content_charset()
    html = None
    if charset:
        try:
            html = payload.decode(charset)
        except (LookupError, UnicodeDecodeError):
            html = None
    if html is None:
        html = htmldoc.decode(payload)
    base = (msg.get("Snapshot-Content-Location") or html_part.get("Content-Location") or "").strip()
    return html, base, res


# ---------------------------------------------------------------- 批次

def clips_dir(kb: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(kb).joinpath(*CLIPS)


def batch_path(kb: pathlib.Path, batch: str) -> pathlib.Path:
    name = batch.strip().strip("/").split("/")[-1]
    if not re.fullmatch(r"clip-[0-9A-Za-z-]{6,64}", name):
        raise ClipError("剪藏批次名不对:%s(形如 clip-20261027-103000-ab12;kb.py clip list 查看)" % batch)
    p = clips_dir(kb) / name
    if not (p / META).is_file():
        raise ClipError("剪藏批次 %s 不存在(kb.py clip list 查看有哪些)" % name)
    return p


def load_meta(batch: pathlib.Path) -> Dict[str, object]:
    try:
        return json.loads((batch / META).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClipError("剪藏批次 %s 的 meta.json 读不了:%s" % (batch.name, exc)) from exc


def list_batches(kb: pathlib.Path) -> List[Tuple[pathlib.Path, Dict[str, object]]]:
    root = clips_dir(kb)
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir(), key=lambda x: x.name, reverse=True):
        if p.is_dir() and (p / META).is_file():
            try:
                out.append((p, load_meta(p)))
            except ClipError:
                continue
    return out


def latest(kb: pathlib.Path, user_id: str = "") -> pathlib.Path:
    for p, meta in list_batches(kb):
        if not user_id or meta.get("user_id") in ("", None, user_id):
            return p
    raise ClipError("还没有剪藏批次:先在要导入的网页上点书签「导入知识库」")


FILE_SUFFIXES = (".mhtml", ".mht", ".html", ".htm")


def _file_origin(name: str, raw: bytes) -> Tuple[str, str]:
    """浏览器「另存为」的网页文件 → (原网页地址, 标题),尽力而为;内容与后缀不符就拒(与剪藏接收同一规则)。"""
    head = raw[:65536]
    if name.endswith((".mhtml", ".mht")):
        if not re.search(rb"(?im)^(mime-version:|content-type:\s*multipart/related)", head[:8192]):
            raise ClipError("%s 不是「网页,单个文件」格式(.mhtml):请用浏览器「另存为 → 网页,单个文件」重新保存" % name)
        hdr = email.message_from_bytes(head, policy=email.policy.default)
        return str(hdr.get("Snapshot-Content-Location") or "").strip(), str(hdr.get("Subject") or "").strip()
    if not head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        raise ClipError("%s 不是网页文件" % name)
    m = re.search(rb"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->", head[:4096], re.I)
    return (m.group(1).decode("ascii", "ignore") if m else ""), ""


def add_file(kb: pathlib.Path, src: pathlib.Path, user_id: str = "", url: str = "") -> pathlib.Path:
    """本地的 .mhtml / .html → 剪藏批次(之后照常 clip ingest)。

    给没有剪藏页的环境(单机版)与书签用不了的时候:管理员在浏览器里「另存为 → 网页,单个文件」,传到收件目录再加进来。
    批次的样子与剪藏接收落的一样(meta.json + <批次>.mhtml),先写临时目录再整体改名,不会出现写了一半的批次。
    """
    src = pathlib.Path(src)
    name = src.name.lower()
    suffix = pathlib.Path(name).suffix
    if suffix not in FILE_SUFFIXES:
        raise ClipError("只收 .mhtml / .mht / .html / .htm(浏览器「另存为 → 网页,单个文件」):%s" % src.name)
    try:
        raw = src.read_bytes()
    except OSError as exc:
        raise ClipError("读不了 %s:%s" % (src, exc)) from exc
    if not raw:
        raise ClipError("%s 是空文件" % src.name)
    found, title = _file_origin(name, raw)
    now = _dt.datetime.now().astimezone()
    batch = "clip-%s-%s" % (now.strftime("%Y%m%d-%H%M%S"), hashlib.sha256(raw + str(now).encode()).hexdigest()[:4])
    root = clips_dir(kb)
    tmp = root / ".incoming" / batch
    tmp.mkdir(parents=True)
    meta = {"batch": batch, "kind": "file", "source": "file", "user_id": user_id, "file_name": src.name,
            "url": url or found, "title": title, "fetched_at": now.isoformat(timespec="seconds"),
            "received_at": now.isoformat(timespec="seconds"), "snapshot": batch + suffix, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "images": [], "skipped": []}
    try:
        (tmp / (batch + suffix)).write_bytes(raw)
        (tmp / META).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.rename(root / batch)
    except OSError:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return root / batch


def _snapshot(batch: pathlib.Path) -> pathlib.Path:
    for suffix in (".html", ".htm", ".mhtml", ".mht"):
        p = batch / (batch.name + suffix)
        if p.is_file():
            return p
    raise ClipError("剪藏批次 %s 里没有网页快照(%s.html / .mhtml)" % (batch.name, batch.name))


@dataclass
class Converted:
    batch: pathlib.Path
    title: str
    url: str
    page_md: pathlib.Path
    chars: int
    tables: int
    headings: int
    images_used: int
    images_missing: List[str] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    snapshot: Optional[pathlib.Path] = None
    sha256: str = ""
    changed_from: str = ""            # 同一 URL 以前剪藏过且内容不同:上一次的批次名


def convert(batch: pathlib.Path, kb: Optional[pathlib.Path] = None) -> Converted:
    """批次 → page.md(+ ingest.json)。剪到的不是正文就抛 ClipError,不写 page.md。"""
    meta = load_meta(batch)
    snap = _snapshot(batch)
    raw = snap.read_bytes()
    url = str(meta.get("url") or "")
    images: Dict[str, str] = {}
    for im in meta.get("images") or []:
        if isinstance(im, dict) and im.get("src") and im.get("file"):
            images[str(im["src"])] = "images/%s" % im["file"]
    if snap.suffix in (".mhtml", ".mht"):
        html, base, res = parse_mhtml(raw)
        url = url or base
        (batch / "images").mkdir(exist_ok=True)
        for k, (loc, (ctype, data)) in enumerate(sorted(res.items()), 1):
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
                   "image/bmp": ".bmp"}.get(ctype)
            if not ext:
                continue
            name = "img-%03d%s" % (k, ext)
            (batch / "images" / name).write_bytes(data)
            images[loc] = "images/%s" % name
    elif meta.get("encoding") == "utf-8":
        # 书签交来的是浏览器里的 JS 字符串,接收服务按 utf-8 存:页面里 <meta charset=gbk> 的声明已经不作数,
        # 按声明解码会把每个汉字都解错
        html = raw.decode("utf-8", "replace")
    else:
        html = htmldoc.decode(raw)
    md, stats = html_to_markdown(html, url, images, str(meta.get("title") or ""))
    why = refusal(stats, url)
    if why:
        raise ClipError(why)
    if len(md) > MAX_MD_CHARS:
        md = md[:MAX_MD_CHARS] + "\n\n(以下内容超过 %d 字,已截断;请把页面拆开分别剪藏)\n" % MAX_MD_CHARS
    sha = hashlib.sha256(raw).hexdigest()
    changed_from = ""
    if kb is not None and url:
        for other, om in list_batches(kb):
            if other != batch and om.get("url") == url and om.get("sha256") and om.get("sha256") != sha:
                changed_from = other.name
                break
    fetched = str(meta.get("fetched_at") or _dt.datetime.now().isoformat(timespec="seconds"))
    front = ["---", "kind: web-clip", "batch: %s" % batch.name, "title: %s" % json.dumps(stats["title"] or "", ensure_ascii=False),
             "source_url: %s" % json.dumps(url, ensure_ascii=False), "fetched_at: %s" % fetched,
             "snapshot: %s" % snap.name, "sha256: %s" % sha,
             "images: %d" % stats["images_used"], "images_missing: %d" % len(stats["images_missing"]),
             "extracted_by: script", "---", ""]
    page = batch / PAGE_MD
    page.write_text("\n".join(front) + md + "\n", encoding="utf-8")
    info = {"converted_at": _dt.datetime.now().isoformat(timespec="seconds"), "chars": stats["chars"],
            "tables": stats["tables"], "headings": stats["headings"], "images_used": stats["images_used"],
            "images_missing": stats["images_missing"], "noise_removed": stats["noise_removed"], "sha256": sha,
            "changed_from": changed_from}
    (batch / INGEST).write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return Converted(batch=batch, title=str(stats["title"]), url=url, page_md=page, chars=int(stats["chars"]),
                     tables=int(stats["tables"]), headings=int(stats["headings"]), images_used=int(stats["images_used"]),
                     images_missing=list(stats["images_missing"]), skipped=list(meta.get("skipped") or []),
                     snapshot=snap, sha256=sha, changed_from=changed_from)


def origin_of(url: str) -> str:
    s = urlsplit(url)
    return "%s://%s" % (s.scheme, s.netloc) if s.scheme and s.netloc else ""


def allowed_origins(raw: str) -> List[str]:
    """KB_CLIP_ORIGINS(逗号或空白分隔)→ 规整后的源列表:scheme://host[:port],不带路径与末尾 /。"""
    out = []
    for item in re.split(r"[,\s]+", raw or ""):
        o = origin_of(item.strip().rstrip("/"))
        if o and o not in out:
            out.append(o.lower())
    return out
