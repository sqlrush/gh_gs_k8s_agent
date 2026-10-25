"""Static PL/pgSQL analysis (port of internal/procanalyze/*).

Pure text parsing — no DB. Extracts read-only cursor SELECTs (for verified
tuning) and flags structural anti-patterns (advisory). Parameterized / package /
REF cursors and FOR UPDATE / dynamic cursors are detected but marked ineligible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# types (types.go)
# ---------------------------------------------------------------------------

CURSOR_DECLARE_IS = "declare-is"
CURSOR_DECLARE_FOR = "declare-for"
CURSOR_OPEN_FOR = "open-for"
CURSOR_FOR_LOOP = "for-loop"


@dataclass(frozen=True)
class Arg:
    name: str
    type: str


@dataclass(frozen=True)
class ProcDef:
    schema: str
    name: str
    lang: str
    args: list
    source: str
    body: str
    vars: dict
    rollback_safe: bool
    package: str = ""      # 所属包(gs_package.pkgname);独立过程为空串


@dataclass
class CursorDecl:
    name: str
    kind: str
    select_sql: str = ""
    eligible: bool = True
    skip_reason: str = ""
    line: int = 0


@dataclass(frozen=True)
class StructuralFinding:
    line: int
    kind: str
    snippet: str


@dataclass(frozen=True)
class VarSub:
    var: str
    type: str
    value: str
    source: str  # rule | bind


@dataclass(frozen=True)
class VarSubResult:
    sql: str
    subs: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# low-level scanners (scan.go)
# ---------------------------------------------------------------------------

def _is_ident_char(c: str) -> bool:
    return c in "_$" or c.isalnum() and c.isascii()


def _is_ident_start(c: str) -> bool:
    return c == "_" or (c.isalpha() and c.isascii())


def _line_of(s: str, idx: int) -> int:
    if idx > len(s):
        idx = len(s)
    return 1 + s[:idx].count("\n")


def _looks_like_select(sql: str) -> bool:
    t = sql.lstrip(" \t\r\n(")
    up = t.upper()
    return up.startswith("SELECT") or up.startswith("WITH")


def _skip_single_quote(s: str, i: int) -> int:
    n = len(s)
    i += 1
    while i < n:
        if s[i] == "'":
            if i + 1 < n and s[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _dollar_tag_at(s: str, i: int) -> str:
    n = len(s)
    if i >= n or s[i] != "$":
        return ""
    j = i + 1
    while j < n and _is_ident_char(s[j]) and s[j] != "$":
        j += 1
    if j < n and s[j] == "$":
        return s[i:j + 1]
    return ""


def _skip_dollar(s: str, i: int, tag: str) -> int:
    start = i + len(tag)
    idx = s.find(tag, start)
    if idx < 0:
        return len(s)
    return idx + len(tag)


def _skip_line_comment(s: str, i: int) -> int:
    idx = s.find("\n", i)
    return len(s) if idx < 0 else idx + 1


def _skip_block_comment(s: str, i: int) -> int:
    idx = s.find("*/", i + 2)
    return len(s) if idx < 0 else idx + 2 + 2


def _match_word_at(s: str, i: int, lower_word: str) -> bool:
    w = len(lower_word)
    if i + w > len(s) or s[i:i + w].lower() != lower_word:
        return False
    if i > 0 and _is_ident_char(s[i - 1]):
        return False
    if i + w < len(s) and _is_ident_char(s[i + w]):
        return False
    return True


def _scan_top(s: str, frm: int, stop_char: str, stop_word: str) -> int:
    """First occurrence at paren-depth 0 of stop_char or whole-word stop_word."""
    depth = 0
    i, n = frm, len(s)
    while i < n:
        c = s[i]
        if c == "'":
            i = _skip_single_quote(s, i)
            continue
        if c == "$":
            tag = _dollar_tag_at(s, i)
            if tag:
                i = _skip_dollar(s, i, tag)
                continue
        if c == "-" and i + 1 < n and s[i + 1] == "-":
            i = _skip_line_comment(s, i)
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i = _skip_block_comment(s, i)
            continue
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if depth == 0:
            if stop_char and c == stop_char:
                return i
            if stop_word and _match_word_at(s, i, stop_word):
                return i
        i += 1
    return n


def _match_paren(s: str, open_idx: int) -> int:
    depth = 0
    i, n = open_idx, len(s)
    while i < n:
        c = s[i]
        if c == "'":
            i = _skip_single_quote(s, i)
            continue
        if c == "$":
            tag = _dollar_tag_at(s, i)
            if tag:
                i = _skip_dollar(s, i, tag)
                continue
        if c == "-" and i + 1 < n and s[i + 1] == "-":
            i = _skip_line_comment(s, i)
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i = _skip_block_comment(s, i)
            continue
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            if depth == 0:
                return i
            i += 1
            continue
        i += 1
    return n


# ---------------------------------------------------------------------------
# cursor extraction (cursors.go)
# ---------------------------------------------------------------------------

_RE_CUR_IS = re.compile(r"(?i)\bcursor\s+([a-z_][a-z0-9_$]*)\s*(\([^)]*\))?\s+is\b")
_RE_CUR_FOR = re.compile(r"(?i)\b([a-z_][a-z0-9_$]*)\s+(?:no\s+scroll\s+|scroll\s+)?cursor\s*(\([^)]*\))?\s+for\b")
_RE_OPEN_FOR = re.compile(r"(?i)\bopen\s+([a-z_][a-z0-9_$]*)\s+for\b")
_RE_FOR_IN = re.compile(r"(?i)\bfor\s+([a-z_][a-z0-9_$]*)\s+in\b")
_RE_FOR_UPDATE = re.compile(r"(?i)\bfor\s+update\b")
_RE_EXEC_KW = re.compile(r"(?i)^\s*execute\b")


def _classify(name: str, kind: str, sel: str, has_params: bool, line: int) -> CursorDecl:
    c = CursorDecl(name=name, kind=kind, select_sql=sel, line=line, eligible=True)
    if has_params:
        c.eligible, c.skip_reason = False, "参数化游标（v1 不支持，留 v2）"
    elif not _looks_like_select(sel):
        c.eligible, c.skip_reason = False, "非静态 SELECT（动态或不可解析）"
    elif _RE_FOR_UPDATE.search(sel):
        c.eligible, c.skip_reason = False, "FOR UPDATE / 定位更新游标"
    return c


def extract_cursors(body: str) -> list[CursorDecl]:
    out: list[CursorDecl] = []

    for m in _RE_CUR_IS.finditer(body):
        end = _scan_top(body, m.end(), ";", "")
        out.append(_classify(m.group(1), CURSOR_DECLARE_IS,
                             body[m.end():end].strip(), m.group(2) is not None,
                             _line_of(body, m.start())))
    for m in _RE_CUR_FOR.finditer(body):
        end = _scan_top(body, m.end(), ";", "")
        out.append(_classify(m.group(1), CURSOR_DECLARE_FOR,
                             body[m.end():end].strip(), m.group(2) is not None,
                             _line_of(body, m.start())))
    for m in _RE_OPEN_FOR.finditer(body):
        name = m.group(1)
        if _RE_EXEC_KW.search(body[m.end():]):
            out.append(CursorDecl(name=name, kind=CURSOR_OPEN_FOR, eligible=False,
                                  skip_reason="动态游标（OPEN ... FOR EXECUTE，文本静态不可知）",
                                  line=_line_of(body, m.start())))
            continue
        end = _scan_top(body, m.end(), ";", "")
        out.append(_classify(name, CURSOR_OPEN_FOR,
                             body[m.end():end].strip(), False, _line_of(body, m.start())))
    for m in _RE_FOR_IN.finditer(body):
        var_name = m.group(1)
        j = m.end()
        while j < len(body) and body[j] in " \t\r\n":
            j += 1
        if j < len(body) and body[j] == "(":
            cl = _match_paren(body, j)
            inner = body[j + 1:cl].strip()
            if not _looks_like_select(inner):
                continue
            sel = inner
        elif _looks_like_select(body[j:]):
            end = _scan_top(body, j, "", "loop")
            sel = body[j:end].strip()
        else:
            continue  # numeric range or cursor-name loop
        c = CursorDecl(name=var_name, kind=CURSOR_FOR_LOOP, select_sql=sel,
                       line=_line_of(body, m.start()), eligible=True)
        if _RE_FOR_UPDATE.search(sel):
            c.eligible, c.skip_reason = False, "FOR UPDATE / 定位更新游标"
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# structural anti-patterns (structure.go)
# ---------------------------------------------------------------------------

_RE_EXEC_ANY = re.compile(r"(?i)\bexecute\b")
_RE_DML = re.compile(r"(?i)(\binsert\s+into\b|\bupdate\s+[a-z_\"]|\bdelete\s+from\b|\bmerge\s+into\b)")
_RE_SELECT_KW = re.compile(r"(?i)\bselect\b")
_RE_EXC_KW = re.compile(r"(?i)\bexception\b")
_RE_UNSAFE_TXN = re.compile(r"(?i)\b(commit|rollback|autonomous_transaction)\b")


def _loop_spans(body: str) -> list[tuple[int, int]]:
    stack: list[int] = []
    spans: list[tuple[int, int]] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "'":
            i = _skip_single_quote(body, i)
            continue
        if c == "$":
            tag = _dollar_tag_at(body, i)
            if tag:
                i = _skip_dollar(body, i, tag)
                continue
        if c == "-" and i + 1 < n and body[i + 1] == "-":
            i = _skip_line_comment(body, i)
            continue
        if c == "/" and i + 1 < n and body[i + 1] == "*":
            i = _skip_block_comment(body, i)
            continue
        if _match_word_at(body, i, "end"):
            j = i + 3
            while j < n and body[j] in " \t\r\n":
                j += 1
            if _match_word_at(body, j, "loop"):
                if stack:
                    spans.append((stack.pop(), i))
                i = j + 4
                continue
        if _match_word_at(body, i, "loop"):
            stack.append(i)
            i += 4
            continue
        i += 1
    return spans


def _in_loop(idx: int, spans: list[tuple[int, int]]) -> bool:
    return any(a < idx < b for a, b in spans)


def _line_text(s: str, idx: int) -> str:
    start = s.rfind("\n", 0, idx) + 1
    end = s.find("\n", idx)
    if end < 0:
        end = len(s)
    return s[start:end].strip()


def scan_structure(body: str) -> list[StructuralFinding]:
    spans = _loop_spans(body)
    seen: set[str] = set()
    fs: list[StructuralFinding] = []

    def add(kind: str, idx: int) -> None:
        ln = _line_of(body, idx)
        key = f"{kind}:{ln}"
        if key in seen:
            return
        seen.add(key)
        fs.append(StructuralFinding(line=ln, kind=kind, snippet=_line_text(body, idx)))

    for m in _RE_EXEC_ANY.finditer(body):
        add("dynamic_sql", m.start())
    for m in _RE_DML.finditer(body):
        idx = m.start()
        if idx >= 4 and body[idx - 4:idx].lower() == "for ":
            continue
        if _in_loop(idx, spans):
            add("per_row_dml", idx)
    for m in _RE_SELECT_KW.finditer(body):
        if _in_loop(m.start(), spans):
            add("loop_sql", m.start())
    for m in _RE_EXC_KW.finditer(body):
        if _in_loop(m.start(), spans):
            add("exception_in_loop", m.start())

    fs.sort(key=lambda f: (f.line, f.kind))
    return fs


def detect_rollback_safe(body: str) -> bool:
    return not _RE_UNSAFE_TXN.search(body)


# ---------------------------------------------------------------------------
# args + variables (structure.go ParseArgs, vars.go)
# ---------------------------------------------------------------------------

def _split_top_comma(s: str) -> list[str]:
    out: list[str] = []
    depth = last = 0
    for i, c in enumerate(s):
        if c == "(":
            depth += 1
        elif c == ")":
            if depth > 0:
                depth -= 1
        elif c == "," and depth == 0:
            out.append(s[last:i])
            last = i + 1
    out.append(s[last:])
    return out


def parse_args(args_text: str) -> list[Arg]:
    out: list[Arg] = []
    for part in _split_top_comma(args_text):
        fields = part.strip().split()
        if not fields:
            continue
        k = 0
        if fields[0].upper() in ("IN", "OUT", "INOUT", "VARIADIC"):
            k = 1
        if k + 1 >= len(fields):
            continue
        name = fields[k]
        tparts: list[str] = []
        for f in fields[k + 1:]:
            if f.lower() == "default" or f == "=":
                break
            tparts.append(f)
        typ = " ".join(tparts)
        if name and typ:
            out.append(Arg(name=name, type=typ))
    return out


_RE_DECLARE_KW = re.compile(r"(?i)\bdeclare\b")
_RE_BEGIN_KW = re.compile(r"(?i)\bbegin\b")
_RE_CURSOR_KW = re.compile(r"(?i)\bcursor\b")


def _declare_section(body: str) -> str:
    d = _RE_DECLARE_KW.search(body)
    if not d:
        return ""
    b = _RE_BEGIN_KW.search(body)
    if not b or b.start() <= d.end():
        return ""
    return body[d.end():b.start()]


def _split_top_semi(s: str) -> list[str]:
    out: list[str] = []
    frm = 0
    while frm <= len(s):
        idx = _scan_top(s, frm, ";", "")
        out.append(s[frm:idx])
        if idx >= len(s):
            break
        frm = idx + 1
    return out


def extract_vars(body: str, args: list[Arg]) -> dict:
    vars_: dict = {a.name.lower(): a.type for a in args}
    for stmt in _split_top_semi(_declare_section(body)):
        s = stmt.strip()
        if not s or _RE_CURSOR_KW.search(s):
            continue
        fields = s.split()
        if len(fields) < 2 or not _is_ident_start(fields[0][0]):
            continue
        name = fields[0]
        ti = 1
        if fields[1].lower() == "constant":
            ti = 2
        if ti >= len(fields):
            continue
        tp: list[str] = []
        for f in fields[ti:]:
            if f in (":=", "=") or f.lower() == "default":
                break
            tp.append(f)
        typ = " ".join(tp).strip()
        k = typ.find(":=")
        if k >= 0:
            typ = typ[:k].strip()
        lw = name.lower()
        if typ and lw not in vars_:
            vars_[lw] = typ
    return vars_


def _contains_any(s: str, *subs: str) -> bool:
    return any(sub in s for sub in subs)


def _synth_by_type(typ: str) -> str:
    t = typ.lower().strip()
    if _contains_any(t, "timestamp", "datetime"):
        return "'2024-01-15 00:00:00'"
    if _contains_any(t, "date"):
        return "'2024-01-15'"
    if _contains_any(t, "time"):
        return "'12:00:00'"
    if _contains_any(t, "bool"):
        return "true"
    if _contains_any(t, "int", "serial", "numeric", "decimal", "number",
                     "double", "real", "float", "money"):
        return "1"
    return "'test'"


def _synth_var(lower_name: str, typ: str, binds: dict) -> tuple[str, str]:
    v = binds.get(lower_name)
    if v:
        return v, "bind"
    return _synth_by_type(typ), "rule"


def substitute_vars(sel: str, vars_: dict, binds: dict) -> VarSubResult:
    out: list[str] = []
    subs: list[VarSub] = []
    n = len(sel)
    prev = ""  # last non-space char emitted
    i = 0
    while i < n:
        c = sel[i]
        if c == "'":
            j = _skip_single_quote(sel, i)
            out.append(sel[i:j])
            i, prev = j, "'"
            continue
        if c == '"':
            j = i + 1
            while j < n and sel[j] != '"':
                j += 1
            if j < n:
                j += 1
            out.append(sel[i:j])
            i, prev = j, '"'
            continue
        if c == "-" and i + 1 < n and sel[i + 1] == "-":
            j = _skip_line_comment(sel, i)
            out.append(sel[i:j])
            i = j
            continue
        if c == "/" and i + 1 < n and sel[i + 1] == "*":
            j = _skip_block_comment(sel, i)
            out.append(sel[i:j])
            i = j
            continue
        if _is_ident_start(c):
            j = i
            while j < n and _is_ident_char(sel[j]):
                j += 1
            word = sel[i:j]
            if prev != ".":
                typ = vars_.get(word.lower())
                if typ is not None:
                    val, src = _synth_var(word.lower(), typ, binds)
                    out.append(val)
                    subs.append(VarSub(var=word, type=typ, value=val, source=src))
                    i, prev = j, "x"
                    continue
            out.append(word)
            i, prev = j, word[-1]
            continue
        out.append(c)
        if c not in " \t\r\n":
            prev = c
        i += 1
    return VarSubResult(sql="".join(out), subs=subs)


def references_record_var(sql: str, record_vars: list) -> str:
    """Return the first record/composite variable a SELECT references as
    `recvar.field`, or "" if none. Such a cursor depends on the enclosing loop's
    record and cannot be EXPLAINed standalone, so callers skip it cleanly.
    """
    low = sql.lower()
    for rv in record_vars:
        rv = rv.lower()
        i = low.find(rv + ".")
        while i != -1:
            before = low[i - 1] if i > 0 else " "
            if not (before.isalnum() or before in "_$"):
                return rv
            i = low.find(rv + ".", i + 1)
    return ""


def record_var_names(vars_: dict) -> list:
    """Names of declared record/%rowtype variables from a ProcDef.vars map."""
    return [n for n, t in vars_.items()
            if t and (t.strip().lower() == "record" or "rowtype" in t.lower())]


def analyze(schema: str, name: str, lang: str, body: str, args_text: str) -> ProcDef:
    args = parse_args(args_text)
    return ProcDef(
        schema=schema, name=name, lang=lang,
        source=body, body=body, args=args,
        vars=extract_vars(body, args),
        rollback_safe=detect_rollback_safe(body),
    )
