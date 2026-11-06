"""Rule evaluation (pure functions, no I/O).

check_statements() judges SQL text (from lexer); check_objects() judges catalog
facts (from objects.py). Both emit the same Finding type, so the report has one
shape regardless of where the review started.

`advisory` rules are not judged here: the script gathers the statements/objects
in scope as evidence and hands them, with the rule's criteria, to the LLM. That
keeps the deterministic/judgement boundary honest instead of pretending a regex
can decide whether an index design is sound.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from model import Finding, ObjectFacts, Rule, Statement

_MAX_EVIDENCE = 10          # advisory evidence lines kept per rule
_SNIPPET = 120              # chars of SQL echoed into a finding

_RE_PRIMARY_KEY = re.compile(r"\bPRIMARY\s+KEY\b", re.I)
_RE_FOREIGN_KEY = re.compile(r"\bFOREIGN\s+KEY\b|\bREFERENCES\b", re.I)
_RE_FK_NAME = re.compile(r"\bCONSTRAINT\s+([\w\"]+)\s+FOREIGN\s+KEY\b", re.I)
_RE_WHERE = re.compile(r"\bWHERE\b", re.I)
_RE_SELECT_STAR = re.compile(r"\bSELECT\s+(?:DISTINCT\s+)?(?:[\w\"]+\.)?\*", re.I)
_RE_LIKE_LITERAL = re.compile(r"\b(?:I?LIKE)\s+(:s\d+)", re.I)


def _fmt(template: str, **kw: Any) -> str:
    """Format a rule message, tolerating placeholders the checker didn't supply."""
    try:
        return template.format(**kw)
    except (KeyError, IndexError, ValueError):
        return template


def _finding(rule: Rule, location: str, message: str, *, snippet: str = "",
             advisory: bool = False, evidence: Iterable[str] = ()) -> Finding:
    return Finding(
        rule_id=rule.id,
        rule_name=rule.name,
        severity=rule.severity,
        message=message or rule.name,
        location=location,
        snippet=snippet[:_SNIPPET],
        rationale=rule.rationale,
        fix=rule.fix,
        advisory=advisory,
        evidence=tuple(evidence),
    )


def _at(stmt: Statement) -> str:
    return f"stmt#{stmt.idx} line {stmt.line}"


def _tbl(stmt: Statement) -> str:
    """Name to echo back at the author: what they wrote, not the folded form."""
    return stmt.table_raw or stmt.table


# --------------------------------------------------------------------------
# statement checkers: (Statement, Rule) -> list[Finding]
# --------------------------------------------------------------------------
def _s_regex(stmt: Statement, rule: Rule) -> list[Finding]:
    scope = str(rule.params.get("on", "normalized"))
    text = stmt.raw if scope == "raw" else stmt.normalized
    if re.search(str(rule.params["pattern"]), text):
        return [_finding(rule, _at(stmt), _fmt(rule.message, table=_tbl(stmt)),
                         snippet=stmt.raw)]
    return []


def _s_table_no_primary_key(stmt: Statement, rule: Rule) -> list[Finding]:
    if stmt.verb != "create_table" or _RE_PRIMARY_KEY.search(stmt.normalized):
        return []
    return [_finding(rule, _at(stmt), _fmt(rule.message, table=_tbl(stmt)),
                     snippet=stmt.raw)]


def _s_table_has_foreign_key(stmt: Statement, rule: Rule) -> list[Finding]:
    if stmt.verb not in ("create_table", "alter_table"):
        return []
    if not _RE_FOREIGN_KEY.search(stmt.normalized):
        return []
    m = _RE_FK_NAME.search(stmt.normalized)
    name = m.group(1).strip('"') if m else "(匿名)"
    return [_finding(rule, _at(stmt),
                     _fmt(rule.message, table=_tbl(stmt), constraint=name),
                     snippet=stmt.raw)]


def _s_naming_pattern(stmt: Statement, rule: Rule) -> list[Finding]:
    """Judges the name as written, not the folded one: `CREATE TABLE OrderItems`
    breaks a lower_snake standard even though the server stores `orderitems`."""
    target = str(rule.params["target"])
    pattern = str(rule.params["pattern"])

    if target == "table":
        if stmt.verb != "create_table" or not stmt.table_raw:
            return []
        if re.match(pattern, stmt.table_raw):
            return []
        return [_finding(rule, _at(stmt), _fmt(rule.message, table=stmt.table_raw),
                         snippet=stmt.raw)]

    if target == "index":
        if stmt.verb != "create_index" or not stmt.index_raw:
            return []
        if re.match(pattern, stmt.index_raw):
            return []
        return [_finding(rule, _at(stmt),
                         _fmt(rule.message, index=stmt.index_raw, table=stmt.table_raw),
                         snippet=stmt.raw)]

    if stmt.verb != "create_table":
        return []
    bad = [c for c in stmt.column_raw if not re.match(pattern, c)]
    return [_finding(rule, _at(stmt),
                     _fmt(rule.message, table=stmt.table_raw, column=c),
                     snippet=stmt.raw) for c in bad]


def _s_index_column_count(stmt: Statement, rule: Rule) -> list[Finding]:
    limit = int(rule.params["max"])
    if stmt.verb != "create_index" or len(stmt.index_cols) <= limit:
        return []
    return [_finding(rule, _at(stmt),
                     _fmt(rule.message, index=stmt.index_raw or stmt.index_name,
                          table=_tbl(stmt), n=len(stmt.index_cols), max=limit),
                     snippet=stmt.raw)]


def _s_stmt_forbidden(stmt: Statement, rule: Rule) -> list[Finding]:
    if stmt.verb != str(rule.params["kind"]).lower():
        return []
    return [_finding(rule, _at(stmt), _fmt(rule.message, table=_tbl(stmt)),
                     snippet=stmt.raw)]


def _s_dml_without_where(stmt: Statement, rule: Rule) -> list[Finding]:
    if stmt.verb not in ("update", "delete") or _RE_WHERE.search(stmt.normalized):
        return []
    return [_finding(rule, _at(stmt), _fmt(rule.message, table=_tbl(stmt)),
                     snippet=stmt.raw)]


def _s_select_star(stmt: Statement, rule: Rule) -> list[Finding]:
    if not _RE_SELECT_STAR.search(stmt.normalized):
        return []
    return [_finding(rule, _at(stmt), _fmt(rule.message, table=_tbl(stmt)),
                     snippet=stmt.raw)]


def _s_leading_wildcard_like(stmt: Statement, rule: Rule) -> list[Finding]:
    """Literals are masked by the lexer, so match `LIKE :sN` then look :sN up."""
    for m in _RE_LIKE_LITERAL.finditer(stmt.normalized):
        body = stmt.literals.get(m.group(1), "")
        if body.startswith("%") or body.startswith("_"):
            return [_finding(rule, _at(stmt),
                             _fmt(rule.message, table=_tbl(stmt), value=body),
                             snippet=stmt.raw)]
    return []


_STMT_CHECKS: dict[str, Callable[[Statement, Rule], list[Finding]]] = {
    "regex": _s_regex,
    "table_no_primary_key": _s_table_no_primary_key,
    "table_has_foreign_key": _s_table_has_foreign_key,
    "naming_pattern": _s_naming_pattern,
    "index_column_count": _s_index_column_count,
    "stmt_forbidden": _s_stmt_forbidden,
    "dml_without_where": _s_dml_without_where,
    "select_star": _s_select_star,
    "leading_wildcard_like": _s_leading_wildcard_like,
}


def _advisory_over_statements(stmts: tuple[Statement, ...], rule: Rule) -> list[Finding]:
    scope = [s for s in stmts if s.kind in rule.applies_to]
    if not scope:
        return []
    evidence = [f"{_at(s)}: {s.raw[:_SNIPPET]}" for s in scope[:_MAX_EVIDENCE]]
    if len(scope) > _MAX_EVIDENCE:
        evidence.append(f"...（另有 {len(scope) - _MAX_EVIDENCE} 条同类语句）")
    return [Finding(
        rule_id=rule.id, rule_name=rule.name, severity=rule.severity,
        message=rule.message or rule.name,
        location=f"{len(scope)} 条语句在范围内",
        rationale=str(rule.params.get("criteria", "")).strip(),
        fix=rule.fix, advisory=True, evidence=tuple(evidence),
    )]


def check_statements(stmts: Iterable[Statement], rules: Iterable[Rule]) -> tuple[Finding, ...]:
    """Run every text rule against every statement it applies to."""
    stmts = tuple(stmts)
    out: list[Finding] = []
    for rule in rules:
        if rule.check == "advisory":
            out.extend(_advisory_over_statements(stmts, rule))
            continue
        fn = _STMT_CHECKS.get(rule.check)
        if fn is None:                      # object-only check (e.g. index_redundant)
            continue
        for stmt in stmts:
            if stmt.kind in rule.applies_to:
                out.extend(fn(stmt, rule))
    return tuple(out)


# --------------------------------------------------------------------------
# object checkers: (ObjectFacts, Rule) -> list[Finding]
# --------------------------------------------------------------------------
def _o_at_table(t) -> str:
    return f"{t.schema}.{t.table}"


def _o_table_no_primary_key(facts: ObjectFacts, rule: Rule) -> list[Finding]:
    return [_finding(rule, _o_at_table(t), _fmt(rule.message, table=t.table))
            for t in facts.tables if not t.has_pk]


def _o_table_has_foreign_key(facts: ObjectFacts, rule: Rule) -> list[Finding]:
    return [_finding(rule, _o_at_table(t),
                     _fmt(rule.message, table=t.table, constraint=", ".join(t.fks)))
            for t in facts.tables if t.fks]


def _o_naming_pattern(facts: ObjectFacts, rule: Rule) -> list[Finding]:
    target = str(rule.params["target"])
    pattern = str(rule.params["pattern"])
    out: list[Finding] = []

    if target == "table":
        out += [_finding(rule, _o_at_table(t), _fmt(rule.message, table=t.table))
                for t in facts.tables if not re.match(pattern, t.table)]
    elif target == "index":
        out += [_finding(rule, f"{i.schema}.{i.table}",
                         _fmt(rule.message, index=i.name, table=i.table))
                for i in facts.indexes
                if not i.is_primary and not re.match(pattern, i.name)]
    else:
        for t in facts.tables:
            out += [_finding(rule, _o_at_table(t),
                             _fmt(rule.message, table=t.table, column=c))
                    for c in t.columns if not re.match(pattern, c)]
    return out


def _o_index_column_count(facts: ObjectFacts, rule: Rule) -> list[Finding]:
    limit = int(rule.params["max"])
    return [_finding(rule, f"{i.schema}.{i.table}",
                     _fmt(rule.message, index=i.name, table=i.table,
                          n=len(i.columns), max=limit))
            for i in facts.indexes if len(i.columns) > limit]


def _o_index_redundant(facts: ObjectFacts, rule: Rule) -> list[Finding]:
    """An index whose columns are a strict prefix of another index on the table."""
    out: list[Finding] = []
    for a in facts.indexes:
        if a.is_primary or a.is_unique or not a.columns:
            continue                        # constraint-bearing indexes are not spare
        for b in facts.indexes:
            if b.name == a.name or b.table != a.table or b.schema != a.schema:
                continue
            if len(b.columns) > len(a.columns) and b.columns[:len(a.columns)] == a.columns:
                out.append(_finding(
                    rule, f"{a.schema}.{a.table}",
                    _fmt(rule.message, index=a.name, table=a.table, covered_by=b.name)
                    or f"索引 {a.name} 被 {b.name} 的前缀覆盖"))
                break
    return out


_OBJ_CHECKS: dict[str, Callable[[ObjectFacts, Rule], list[Finding]]] = {
    "table_no_primary_key": _o_table_no_primary_key,
    "table_has_foreign_key": _o_table_has_foreign_key,
    "naming_pattern": _o_naming_pattern,
    "index_column_count": _o_index_column_count,
    "index_redundant": _o_index_redundant,
}


def _advisory_over_objects(facts: ObjectFacts, rule: Rule) -> list[Finding]:
    evidence = [f"{i.schema}.{i.table}.{i.name} ({', '.join(i.columns)}) "
                f"scans={i.scans}" for i in facts.indexes[:_MAX_EVIDENCE]]
    if not evidence:
        evidence = [f"{t.schema}.{t.table}" for t in facts.tables[:_MAX_EVIDENCE]]
    if not evidence:
        return []
    return [Finding(
        rule_id=rule.id, rule_name=rule.name, severity=rule.severity,
        message=rule.message or rule.name,
        location=f"{len(facts.tables)} 表 / {len(facts.indexes)} 索引",
        rationale=str(rule.params.get("criteria", "")).strip(),
        fix=rule.fix, advisory=True, evidence=tuple(evidence),
    )]


def check_objects(facts: ObjectFacts, rules: Iterable[Rule]) -> tuple[Finding, ...]:
    """Run every object rule against the catalog snapshot."""
    out: list[Finding] = []
    for rule in rules:
        if "object" not in rule.applies_to:
            continue
        if rule.check == "advisory":
            out.extend(_advisory_over_objects(facts, rule))
            continue
        fn = _OBJ_CHECKS.get(rule.check)
        if fn is None:                      # text-only check (e.g. select_star)
            continue
        out.extend(fn(facts, rule))
    return tuple(out)
