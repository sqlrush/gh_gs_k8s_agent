#!/usr/bin/env python3
"""从官方文档集生成 common/sysdict.py:GaussDB 自带的系统表、系统视图(含 A 兼容的数据字典视图)名单。

排行排除「查数据字典的 SQL」要认得这些名字。只靠 pg_ / gs_ 前缀认不全:ADM_* / DB_* / MY_* / V$* / DUAL / DICTIONARY
这类 A 兼容视图没有前缀,不带 schema 写时旧规则把它们当成业务表。名单取自文档「系统表和系统视图」章节下
标题就是对象名的条目(每个对象一页),换了文档版本重跑即可:

    python3 tools/gen_sysdict.py <文档集目录>/catalog.json > common/sysdict.py
"""
from __future__ import annotations

import json
import re
import sys

_SECTION = re.compile(r"系统表|系统视图|系统目录")
_NAME = re.compile(r"^[A-Z][A-Z0-9_$#]+$")


def names(catalog: list) -> list:
    out = set()
    for it in catalog:
        if _SECTION.search(" > ".join(it.get("path") or [])):
            title = (it.get("title") or "").strip()
            if _NAME.match(title):
                out.add(title.lower())
    return sorted(out)


def render(catalog: list, source: str) -> str:
    items = names(catalog)
    body = "\n".join("    %r," % n for n in items)
    return ('"""GaussDB 自带的系统表与系统视图(含 A 兼容的数据字典视图),小写。由 tools/gen_sysdict.py 生成,不要手改。\n\n'
            "来源:%s(文档「系统表和系统视图」章节,共 %d 个对象)。\n"
            "用途:排行排除查数据字典的 SQL(common/syssql.py);不带 schema 的名字命中这张名单就是系统对象\n"
            "—— 不带 schema 时 pg_catalog 排在 search_path 最前,同名时引用的就是系统视图。\n"
            '"""\n\nNAMES = frozenset({\n%s\n})\n' % (source, len(items), body))


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        catalog = json.load(f)
    sys.stdout.write(render(catalog, "GaussDB 25.1.32 集中式官方文档集 G25132C"))
