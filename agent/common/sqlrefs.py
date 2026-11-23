"""SQL 文本里引用了哪些表 —— 纯文本解析,不连库。

原先 sqltune 与 proctune 的 evidence.py 各有一份一字不差的拷贝(port of tables.go ExtractTables);
2026-10-23 排除内核系统 SQL 时慢 SQL / Top SQL / 健康检查 / WDR 都要用,收到这里共用一份。
"""
from __future__ import annotations

import re

# 名字可以是反引号括起的(M 兼容库,v1.5.1):`orders`、`app`.`orders`、app.`orders`
_NAME = r"(?:`[^`\n]+`|[a-z_][\w]*)(?:\.(?:`[^`\n]+`|[a-z_][\w]*))*"
_TABLE_REF_RE = re.compile(r"(?is)\b(?:from|join|update|into)\s+(%s)" % _NAME)
_IDENT_RE = re.compile(r"(?i)^%s" % _NAME)
_SQL_KEYWORDS = frozenset({
    "select", "lateral", "unnest", "generate_series", "where", "on", "set",
    "values", "join", "left", "right", "inner", "outer", "cross", "full",
    "natural", "from", "into", "update",
})
_WS = " \t\r\n"


def extract_table_refs(sql_text: str, fold: bool = True) -> list[str]:
    """Lowercase, deduped table refs in first-appearance order, schema kept.

    反引号去掉(M 兼容库的标识符写法)。fold=False 时不折小写:M 兼容库 lower_case_table_names=0 时表名区分大小写,
    库里存的就是原样(common/compat.py 的 folds_unquoted 决定)。"""
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        name = raw.strip().replace("`", "")
        name = name.lower() if fold else name
        tail = name[name.rindex(".") + 1:] if "." in name else name
        if tail in ("", "(") or tail in _SQL_KEYWORDS or name in seen:
            return
        seen.add(name)
        out.append(name)

    for m in _TABLE_REF_RE.finditer(sql_text):
        add(m.group(1))
        pos = m.end(1)
        while True:
            while pos < len(sql_text) and sql_text[pos] in _WS:
                pos += 1
            if pos >= len(sql_text):
                break
            if sql_text[pos] == ",":
                pos += 1
                while pos < len(sql_text) and sql_text[pos] in _WS:
                    pos += 1
                im = _IDENT_RE.match(sql_text[pos:])
                if not im:
                    break
                add(im.group(0))
                pos += len(im.group(0))
            else:
                im = _IDENT_RE.match(sql_text[pos:])
                if not im:
                    break
                if im.group(0).lower() in _SQL_KEYWORDS:
                    break
                pos += len(im.group(0))  # alias — skip
                while pos < len(sql_text) and sql_text[pos] in _WS:
                    pos += 1
                if pos >= len(sql_text) or sql_text[pos] != ",":
                    break
                pos += 1
                while pos < len(sql_text) and sql_text[pos] in _WS:
                    pos += 1
                im2 = _IDENT_RE.match(sql_text[pos:])
                if not im2:
                    break
                add(im2.group(0))
                pos += len(im2.group(0))
    return out


def extract_tables(sql_text: str, fold: bool = True) -> list[str]:
    """Lowercase, deduped, schema-stripped table names in first-appearance order."""
    seen: set[str] = set()
    out: list[str] = []
    for ref in extract_table_refs(sql_text, fold):
        name = ref[ref.rindex(".") + 1:] if "." in ref else ref
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out
