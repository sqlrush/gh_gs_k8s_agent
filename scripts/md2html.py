#!/usr/bin/env python3
"""把交付文档转成**自带样式的单个 HTML**,给客户直接用浏览器打开或打印成 PDF。

  scripts/md2html.py docs/命令卡-测试环境搭建.md dist/命令卡.html

为什么要这个:命令卡是给实施人员照着敲的,而 markdown 源文件里满屏 ``` 和 | ——
对不熟悉 markdown 的人来说,表格挤成一行、代码块分不出边界,照着敲很容易敲错。
渲染成 HTML 之后代码块有底色、表格有边框、可以直接打印带走。

只用标准库:现场往往装不了 pandoc,也不该为一份文档引入依赖。
支持的语法够这几份文档用:标题、代码块、表格、列表、引用、粗体、行内代码、分隔线。
"""
from __future__ import annotations

import html
import pathlib
import re
import sys

_CSS = """
:root { --fg:#24292f; --muted:#57606a; --bd:#d0d7de; --bg-code:#f6f8fa; --accent:#0969da; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
       line-height: 1.75; color: var(--fg); max-width: 900px; margin: 0 auto; padding: 32px 20px 96px; }
h1 { font-size: 2em; border-bottom: 2px solid var(--bd); padding-bottom: .3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid var(--bd); padding-bottom: .3em; margin-top: 2em; }
h3 { font-size: 1.2em; margin-top: 1.6em; }
code { background: var(--bg-code); padding: .2em .4em; border-radius: 4px;
       font-family: "SF Mono", Menlo, Consolas, monospace; font-size: .88em; }
pre { background: var(--bg-code); border: 1px solid var(--bd); border-radius: 6px;
      padding: 14px 16px; overflow-x: auto; }
pre code { background: none; padding: 0; font-size: .86em; line-height: 1.55; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid var(--bd); padding: 8px 12px; text-align: left; vertical-align: top; }
th { background: var(--bg-code); font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
blockquote { border-left: 4px solid var(--accent); background: #f6f8fa;
             margin: 1em 0; padding: 12px 16px; color: var(--muted); }
blockquote strong { color: var(--fg); }
hr { border: 0; border-top: 1px solid var(--bd); margin: 2.5em 0; }
a { color: var(--accent); }
ul, ol { padding-left: 1.6em; }
li { margin: .3em 0; }
@media print {
  body { max-width: none; padding: 0; }
  pre, blockquote, table { page-break-inside: avoid; }
  h2, h3 { page-break-after: avoid; }
}
"""


def _inline(text: str) -> str:
    """行内元素。**先转义再替换**,否则文档里的 < > 会被当成标签。"""
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)
    # 裸 URL(不在已生成的 href 里)
    out = re.sub(r"(?<![\"=>])(https?://[^\s<)]+)", r'<a href="\1">\1</a>', out)
    return out


def _table(rows: list) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    if len(cells) < 2:
        return ""
    head, body = cells[0], cells[2:]        # cells[1] 是 |---|---| 分隔行
    out = ["<table>", "<tr>" + "".join("<th>%s</th>" % _inline(c) for c in head) + "</tr>"]
    for row in body:
        out.append("<tr>" + "".join("<td>%s</td>" % _inline(c) for c in row) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def convert(md: str, title: str) -> str:
    lines = md.split("\n")
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]

        if ln.startswith("```"):                       # 代码块
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>" % html.escape("\n".join(buf)))
            continue

        if ln.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                buf.append(lines[i]); i += 1
            out.append(_table(buf))
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lvl, _inline(m.group(2)), lvl)); i += 1
            continue

        if re.match(r"^\s*(---|\*\*\*|___)\s*$", ln):
            out.append("<hr>"); i += 1
            continue

        if ln.startswith(">"):                         # 引用(可多行)
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip()); i += 1
            out.append("<blockquote>%s</blockquote>" % _paras(buf))
            continue

        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", ln)
        if m:                                          # 列表(不处理嵌套,够用)
            ordered = bool(re.match(r"\d+\.", m.group(2)))
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines):
                mm = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
                if not mm:
                    break
                items.append(_inline(mm.group(3))); i += 1
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % x for x in items), tag))
            continue

        if not ln.strip():
            i += 1
            continue

        buf = []                                       # 普通段落
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", ">", "```", "|")) \
                and not re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i]):
            buf.append(lines[i]); i += 1
        out.append("<p>%s</p>" % _inline(" ".join(buf)))

    return ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n%s\n</body>\n</html>\n"
            % (html.escape(title), _CSS, "\n".join(out)))


def _paras(lines_: list) -> str:
    text = " ".join(x for x in lines_ if x.strip())
    return "<p>%s</p>" % _inline(text) if text else ""


def main(argv=None) -> int:
    a = (argv or sys.argv[1:])
    if len(a) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    src, dst = pathlib.Path(a[0]), pathlib.Path(a[1])
    dst.parent.mkdir(parents=True, exist_ok=True)
    md = src.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.*)$", md, re.MULTILINE)
    dst.write_text(convert(md, m.group(1) if m else src.stem), encoding="utf-8")
    print("%s → %s (%d KB)" % (src.name, dst, dst.stat().st_size // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
