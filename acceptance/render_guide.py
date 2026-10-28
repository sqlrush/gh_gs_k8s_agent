#!/usr/bin/env python3
"""把《智能体验收方案》(docs/智能体验收方案.md)渲染成 GitHub Pages 上的单页网页:docs/acceptance/index.html。

    python3 acceptance/render_guide.py            # 重新生成
    python3 acceptance/render_guide.py --check    # 只核对已提交的网页是不是最新的(不一致退出 1;测试里也这么查)

网页就是这份 markdown 换了个样子,不另写内容:改方案只改 markdown,再跑一次本脚本,两样一起提交。
支持的语法够这份文档用:标题(带锚点与目录)、表格、代码块、列表、引用、粗体、行内代码、链接;
另有一种 ```steps 代码块,每行「标题|说明」,画成一行流程卡片。只用标准库。
"""
from __future__ import annotations

import html
import pathlib
import re
import sys
from typing import List, Optional, Sequence, Set, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "智能体验收方案.md"
OUT = ROOT / "docs" / "acceptance" / "index.html"
REPO = "https://github.com/sqlrush/gh_gs_k8s_agent"

CSS = """
:root{--fg:#1b2430;--muted:#5a6475;--bd:#d9dfe8;--bg-code:#f5f7fa;--accent:#2f6fdb;--navy:#123a73}
*{box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;line-height:1.75;color:var(--fg);margin:0;background:#f6f8fb}
.wrap{max-width:1120px;margin:0 auto;padding:0 24px 96px}
header{background:linear-gradient(120deg,#123a73,#2f6fdb 60%,#5b8def);color:#fff;padding:36px 0 28px;margin-bottom:24px}
header .wrap{padding-bottom:0}
header h1{margin:0 0 8px;font-size:28px}
header .meta{opacity:.9;font-size:14px}
main{background:#fff;border:1px solid var(--bd);border-radius:12px;padding:8px 32px 40px}
h2{font-size:1.45em;border-bottom:1px solid var(--bd);padding-bottom:.3em;margin-top:2em;color:var(--navy)}
h3{font-size:1.15em;margin-top:1.6em}
h2 a.anchor,h3 a.anchor{color:#c0c8d4;text-decoration:none;margin-left:6px;font-weight:400;visibility:hidden}
h2:hover a.anchor,h3:hover a.anchor{visibility:visible}
code{background:var(--bg-code);padding:.15em .4em;border-radius:4px;font-family:"SF Mono",Menlo,Consolas,monospace;font-size:.86em}
pre{background:var(--bg-code);border:1px solid var(--bd);border-radius:6px;padding:12px 16px;overflow-x:auto}
pre code{background:none;padding:0;font-size:.84em;line-height:1.55}
table{border-collapse:collapse;width:100%;margin:1em 0;font-size:.92em}
th,td{border:1px solid var(--bd);padding:7px 10px;text-align:left;vertical-align:top}
th{background:#eef3fb;font-weight:600}
tr:nth-child(even) td{background:#fafbfd}
blockquote{border-left:4px solid var(--accent);background:#f3f6fb;margin:1em 0;padding:10px 16px;color:var(--muted)}
blockquote p{margin:.2em 0}
a{color:var(--accent)}
ul,ol{padding-left:1.6em} li{margin:.25em 0}
nav.toc{background:#f8fafd;border:1px solid var(--bd);border-radius:10px;padding:14px 22px;margin:20px 0;columns:2;column-gap:32px;font-size:.93em}
nav.toc b{display:block;margin-bottom:6px;column-span:all}
nav.toc a{display:block;text-decoration:none;padding:1px 0}
nav.toc a.l3{padding-left:1.2em;color:var(--muted);font-size:.95em}
.flow{display:flex;flex-wrap:nowrap;gap:4px;align-items:stretch;margin:16px 0}
.flow .step{flex:1 1 0;min-width:0;background:#f3f7fe;border:1px solid #c9d8f2;border-radius:10px;padding:10px 12px;position:relative}
.flow .step b{display:block;color:var(--navy);font-size:.98em}
.flow .step b span{display:inline-block;background:var(--accent);color:#fff;border-radius:50%;width:1.5em;height:1.5em;
  line-height:1.5em;text-align:center;font-size:.8em;margin-right:6px}
.flow .step div{font-size:.84em;color:var(--muted);line-height:1.55;margin-top:4px}
.flow .arrow{align-self:center;color:#9fb3d6;font-size:16px}
@media(max-width:820px){.flow{flex-wrap:wrap}.flow .step{flex:1 1 140px}}
@media print{body{background:#fff}main{border:0;padding:0}nav.toc{columns:1}}
"""


def anchor_id(text: str, used: Set[str]) -> str:
    base = re.sub(r"[`*]", "", text).strip()
    base = re.sub(r"[^\w一-鿿.-]+", "-", base).strip("-").lower() or "sec"
    aid, n = base, 2
    while aid in used:
        aid, n = "%s-%d" % (base, n), n + 1
    used.add(aid)
    return aid


def fix_link(url: str) -> str:
    """文档里的相对链接(相对 docs/)→ GitHub 上的文件或目录。"""
    if re.match(r"^(https?:|#|mailto:)", url):
        return url
    path = pathlib.PurePosixPath("docs") / url
    parts: List[str] = []
    for p in path.parts:
        if p == "..":
            if parts:
                parts.pop()
        elif p != ".":
            parts.append(p)
    rel = "/".join(parts)
    return "%s/%s/main/%s" % (REPO, "tree" if url.endswith("/") else "blob", rel)


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                 lambda m: '<a href="%s">%s</a>' % (html.escape(fix_link(html.unescape(m.group(2)))), m.group(1)), out)
    out = re.sub(r"(?<![\"=>/])(https?://[^\s<)，。、；（]+)", r'<a href="\1">\1</a>', out)
    return out


def table(rows: Sequence[str]) -> str:
    cells = [[c.strip() for c in re.split(r"(?<!\\)\|", r.strip().strip("|"))] for r in rows]
    head, body = cells[0], cells[2:]
    out = ["<table>", "<tr>" + "".join("<th>%s</th>" % inline(c.replace("\\|", "|")) for c in head) + "</tr>"]
    out += ["<tr>" + "".join("<td>%s</td>" % inline(c.replace("\\|", "|")) for c in row) + "</tr>" for row in body]
    out.append("</table>")
    return "\n".join(out)


def steps(lines: Sequence[str]) -> str:
    cards = []
    for i, line in enumerate(l for l in lines if l.strip()):
        title, _, desc = line.partition("|")
        cards.append('<div class="step"><b><span>%d</span>%s</b><div>%s</div></div>' % (i + 1, inline(title.strip()),
                                                                                          inline(desc.strip())))
    return '<div class="flow">%s</div>' % '<div class="arrow">→</div>'.join(cards)


def _fence(lines: List[str], i: int) -> Tuple[str, List[str], int]:
    lang = lines[i][3:].strip()
    i += 1
    buf = []
    while i < len(lines) and not lines[i].startswith("```"):
        buf.append(lines[i])
        i += 1
    return lang, buf, i + 1


def _list(lines: List[str], i: int) -> Tuple[str, int]:
    first = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
    tag = "ol" if re.match(r"\d+\.", first.group(2)) else "ul"
    items: List[str] = []
    while i < len(lines):
        mm = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
        if mm:
            items.append(mm.group(3))
        elif lines[i].startswith("  ") and lines[i].strip() and items:     # 列表项的续行
            items[-1] += " " + lines[i].strip()
        else:
            break
        i += 1
    return "<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline(x) for x in items), tag), i


def convert(md: str) -> Tuple[List[str], List[Tuple[int, str, str]]]:
    lines, out, toc, used, i = md.split("\n"), [], [], set(), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            lang, buf, i = _fence(lines, i)
            out.append(steps(buf) if lang == "steps" else
                       "<pre><code>%s</code></pre>" % html.escape("\n".join(buf), quote=False))
            continue
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                buf.append(lines[i])
                i += 1
            out.append(table(buf))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl, text = len(m.group(1)), m.group(2)
            i += 1
            if lvl == 1:
                continue                                         # 标题放进页头
            aid = anchor_id(text, used)
            if lvl in (2, 3):
                toc.append((lvl, aid, text))
            out.append('<h%d id="%s">%s<a class="anchor" href="#%s">#</a></h%d>' % (lvl, aid, inline(text), aid, lvl))
            continue
        if ln.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote>%s</blockquote>" % "".join("<p>%s</p>" % inline(x) for x in buf if x))
            continue
        if re.match(r"^(\s*)([-*+]|\d+\.)\s+", ln):
            block, i = _list(lines, i)
            out.append(block)
            continue
        if not ln.strip():
            i += 1
            continue
        buf = []
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", ">", "```", "|")) \
                and not re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i]):
            buf.append(lines[i])
            i += 1
        out.append("<p>%s</p>" % inline(" ".join(buf)))
    return out, toc


def render(md: str) -> str:
    title = re.search(r"^#\s+(.*)$", md, re.M).group(1)
    body, toc = convert(md)
    nav = ['<nav class="toc"><b>目录</b>'] + ['<a class="l%d" href="#%s">%s</a>' % (l, a, inline(t)) for l, a, t in toc]
    nav.append("</nav>")
    first = next((k for k, x in enumerate(body) if x.startswith("<blockquote>")), -1)
    body = body[:first + 1] + ["\n".join(nav)] + body[first + 1:]
    return ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex">\n<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n'
            '<header><div class="wrap"><h1>%s</h1><div class="meta">代码仓库 sqlrush/gh_gs_k8s_agent · '
            '本页由仓库 docs/智能体验收方案.md 生成(acceptance/render_guide.py)· 跑题与评分代码在 acceptance/</div>'
            '</div></header>\n<div class="wrap"><main>\n%s\n</main></div>\n</body>\n</html>\n'
            % (html.escape(title), CSS, html.escape(title), "\n".join(body)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    page = render(SRC.read_text(encoding="utf-8"))
    if "--check" in argv:
        same = OUT.is_file() and OUT.read_text(encoding="utf-8") == page
        print("✓ docs/acceptance/index.html 是最新的" if same else
              "✗ docs/acceptance/index.html 与 docs/智能体验收方案.md 不一致:跑 python3 acceptance/render_guide.py")
        return 0 if same else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print("%s(%d KB)" % (OUT.relative_to(ROOT), len(page.encode("utf-8")) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
