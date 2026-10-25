"""SQL lexer (pure functions, no I/O, stdlib only).

There is no SQL parser dependency in this project, so text rules must not be
fooled by comments and string literals. One span scanner underpins everything:

    spans()      classify every byte as code / comment / string / dollar / ident
    mask()       same-length view with comments blanked and literal bodies filled
                 -> lets us split on top-level ';' and recover line numbers
    normalize()  literal bodies replaced by :sN placeholders, comments dropped
                 -> what regex rules match against, so a DELETE inside a comment
                    or a '%' inside a string never produces a false hit
    split()      the pipeline: raw SQL -> tuple[Statement]

Because normalize() masks literals, a rule like "no leading wildcard LIKE"
cannot be a plain regex — `LIKE '%x'` becomes `LIKE :s1`. Such rules are
structured checks that look up Statement.literals. See checks.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from model import Statement

# Statement kinds by leading keyword.
_DDL_VERBS = {
    "create": "create", "alter": "alter", "drop": "drop",
    "truncate": "truncate", "comment": "comment",
}
_DML_VERBS = {"insert": "insert", "update": "update", "delete": "delete", "merge": "merge"}
_DQL_HEADS = {"select", "with", "values", "table", "explain"}

_FILL = "x"  # stand-in byte for literal bodies inside mask()


@dataclass(frozen=True)
class Span:
    kind: str   # code | line_comment | block_comment | string | dollar | ident
    start: int
    end: int    # exclusive


def _dollar_tag(sql: str, i: int) -> str | None:
    """Return the full `$tag$` opener at i, or None if this `$` is not a quote."""
    m = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$").match(sql, i)
    return m.group(0) if m else None


def spans(sql: str) -> tuple[Span, ...]:
    """Classify every byte of `sql`. Spans tile the whole string, in order."""
    out: list[Span] = []
    i, n, code_start = 0, len(sql), 0

    def flush_code(upto: int) -> None:
        if upto > code_start:
            out.append(Span("code", code_start, upto))

    while i < n:
        ch = sql[i]

        if ch == "-" and sql.startswith("--", i):
            flush_code(i)
            end = sql.find("\n", i)
            end = n if end < 0 else end          # newline itself stays code
            out.append(Span("line_comment", i, end))
            i = code_start = end

        elif ch == "/" and sql.startswith("/*", i):
            flush_code(i)
            depth, j = 1, i + 2
            while j < n and depth:               # PostgreSQL block comments nest
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(Span("block_comment", i, j))
            i = code_start = j

        elif ch == "'":
            flush_code(i)
            escapes = i > 0 and sql[i - 1] in "Ee" and (i < 2 or not sql[i - 2].isalnum())
            j = i + 1
            while j < n:
                if escapes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":   # '' is an escaped quote
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(Span("string", i, j))
            i = code_start = j

        elif ch == '"':
            flush_code(i)
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(Span("ident", i, j))
            i = code_start = j

        elif ch == "$" and (tag := _dollar_tag(sql, i)):
            flush_code(i)
            close = sql.find(tag, i + len(tag))
            j = n if close < 0 else close + len(tag)
            out.append(Span("dollar", i, j))
            i = code_start = j

        else:
            i += 1

    flush_code(n)
    return tuple(out)


def mask(sql: str) -> str:
    """Same-length view: comments blanked, literal bodies filled, newlines kept.

    Safe to scan for top-level ';' and to count lines against the original.
    """
    buf = list(sql)
    for sp in spans(sql):
        if sp.kind in ("line_comment", "block_comment"):
            for k in range(sp.start, sp.end):
                if buf[k] != "\n":
                    buf[k] = " "
        elif sp.kind == "string":
            for k in range(sp.start + 1, max(sp.start + 1, sp.end - 1)):
                if buf[k] != "\n":
                    buf[k] = _FILL
        elif sp.kind == "dollar":
            for k in range(sp.start, sp.end):
                if buf[k] not in ("\n", "$"):
                    buf[k] = _FILL
    return "".join(buf)


def normalize(sql: str) -> tuple[str, dict[str, str]]:
    """Drop comments, replace literal bodies with :sN. Returns (text, literals)."""
    parts: list[str] = []
    literals: dict[str, str] = {}
    for sp in spans(sql):
        body = sql[sp.start:sp.end]
        if sp.kind in ("line_comment", "block_comment"):
            parts.append(" ")
        elif sp.kind in ("string", "dollar"):
            name = f":s{len(literals) + 1}"
            literals[name] = _literal_body(sp.kind, body)
            parts.append(name)
        else:
            parts.append(body)
    return "".join(parts), literals


def _literal_body(kind: str, text: str) -> str:
    """The literal's content, without its quoting."""
    if kind == "string":
        inner = text[1:-1] if len(text) >= 2 and text.endswith("'") else text[1:]
        return inner.replace("''", "'")
    tag_end = text.find("$", 1) + 1        # dollar: strip $tag$ from both ends
    return text[tag_end:-tag_end] if tag_end > 0 else text


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def classify(normalized: str) -> tuple[str, str]:
    """Return (kind, verb) for a normalized statement."""
    words = _WORD.findall(normalized[:200].lower())
    if not words:
        return "other", ""
    head = words[0]

    if head in _DDL_VERBS:
        rest = words[1:6]
        if head == "create":
            if "table" in rest:
                return "ddl", "create_table"
            if "index" in rest:
                return "ddl", "create_index"
            if "function" in rest or "procedure" in rest:
                return "ddl", "create_function"
            return "ddl", "create_other"
        if head == "alter" and rest and rest[0] == "table":
            return "ddl", "alter_table"
        return "ddl", _DDL_VERBS[head]

    if head in _DML_VERBS:
        return "dml", _DML_VERBS[head]

    if head in _DQL_HEADS:
        return "dql", "select"

    return "other", head


def _written(name: str) -> str:
    """`public."Orders"` -> `Orders`: strip schema and quotes, keep the case.

    Naming rules judge what the author typed, so they need this, not `_bare`.
    """
    return name.split(".")[-1].strip().strip('"')


def _bare(name: str) -> str:
    """`public."Orders"` -> `orders`: what the server will actually store."""
    return _written(name).lower()


_RE_CREATE_TABLE = re.compile(
    r"\bCREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:(?:TEMP|TEMPORARY|UNLOGGED)\s+)?TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", re.I)
_RE_CREATE_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([\w.\"]+)?\s*ON\s+([\w.\"]+)\s*\(([^)]*)\)", re.I)
_RE_ALTER_TABLE = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([\w.\"]+)", re.I)
_RE_DELETE = re.compile(r"\bDELETE\s+FROM\s+(?:ONLY\s+)?([\w.\"]+)", re.I)
_RE_UPDATE = re.compile(r"\bUPDATE\s+(?:ONLY\s+)?([\w.\"]+)", re.I)
_RE_INSERT = re.compile(r"\bINSERT\s+INTO\s+([\w.\"]+)", re.I)
_RE_FROM = re.compile(r"\bFROM\s+([\w.\"]+)", re.I)


def _names(verb: str, text: str) -> dict:
    """Best-effort object names for a normalized statement (folded + as-written)."""
    if verb == "create_index":
        m = _RE_CREATE_INDEX.search(text)
        if not m:
            return {}
        cols = [_WORD.search(c).group(0) for c in m.group(3).split(",") if _WORD.search(c)]
        return {
            "table": _bare(m.group(2)), "table_raw": _written(m.group(2)),
            "index_name": _bare(m.group(1) or ""), "index_raw": _written(m.group(1) or ""),
            "index_cols": tuple(_bare(c) for c in cols),
        }

    single = {
        "create_table": _RE_CREATE_TABLE, "alter_table": _RE_ALTER_TABLE,
        "delete": _RE_DELETE, "update": _RE_UPDATE, "insert": _RE_INSERT,
        "select": _RE_FROM,
    }.get(verb)
    m = single.search(text) if single else None
    if not m:
        return {}
    out = {"table": _bare(m.group(1)), "table_raw": _written(m.group(1))}
    if verb == "create_table":
        out["column_raw"] = column_names(text)
    return out


_CONSTRAINT_HEADS = frozenset(
    {"primary", "foreign", "unique", "check", "constraint", "exclude", "like", "partition"}
)


def column_names(normalized: str) -> tuple[str, ...]:
    """Column names declared by a CREATE TABLE, as written (constraints skipped)."""
    open_paren = normalized.find("(")
    if open_paren < 0:
        return ()

    depth, close = 0, -1
    for i in range(open_paren, len(normalized)):
        if normalized[i] == "(":
            depth += 1
        elif normalized[i] == ")":
            depth -= 1
            if depth == 0:
                close = i
                break
    if close < 0:
        return ()

    cols: list[str] = []
    depth, item = 0, []
    for ch in normalized[open_paren + 1:close] + ",":
        if ch == "," and depth == 0:
            head = _WORD.search("".join(item))
            if head and head.group(0).lower() not in _CONSTRAINT_HEADS:
                cols.append(_written(head.group(0)))
            item = []
            continue
        depth += (ch == "(") - (ch == ")")
        item.append(ch)
    return tuple(cols)


def split(sql: str) -> tuple[Statement, ...]:
    """Split `sql` into statements, comments and literals already handled."""
    masked = mask(sql)
    out: list[Statement] = []
    start = 0

    for end in [i for i, c in enumerate(masked) if c == ";"] + [len(masked)]:
        chunk = masked[start:end]
        if chunk.strip():
            offset = start + (len(chunk) - len(chunk.lstrip()))   # first code byte
            raw = sql[offset:end]
            normalized, literals = normalize(raw)
            kind, verb = classify(normalized)
            out.append(Statement(
                idx=len(out) + 1,
                line=sql.count("\n", 0, offset) + 1,
                kind=kind, verb=verb,
                raw=raw.strip(), normalized=normalized.strip(), literals=literals,
                **_names(verb, normalized),
            ))
        start = end + 1

    return tuple(out)
