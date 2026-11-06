"""sqlreview data model (immutable value types, no I/O).

Severity / Rule / Statement / Finding / ObjectFacts / ReviewResult are the only
types crossing module boundaries: lexer produces Statement, objects produces
ObjectFacts, checks consumes both and produces Finding, report consumes
ReviewResult.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


class RuleError(Exception):
    """rules.yaml is missing, malformed, or references an unknown check."""


class Severity(enum.IntEnum):
    INFO = 0
    WARN = 1
    ERROR = 2


_SEV_BY_NAME = MappingProxyType({
    "info": Severity.INFO,
    "warn": Severity.WARN,
    "error": Severity.ERROR,
})

_SEV_LABEL = MappingProxyType({
    Severity.INFO: "info",
    Severity.WARN: "warn",
    Severity.ERROR: "error",
})

_SEV_ICON = MappingProxyType({
    Severity.INFO: "🟢",
    Severity.WARN: "🟡",
    Severity.ERROR: "🔴",
})


def severity_from(name: str) -> Severity:
    """Parse a severity name; raise RuleError on anything outside the whitelist."""
    sev = _SEV_BY_NAME.get(str(name).strip().lower())
    if sev is None:
        raise RuleError(
            f"invalid severity {name!r} (expected one of: "
            f"{', '.join(_SEV_BY_NAME)})"
        )
    return sev


def severity_name(sev: Severity) -> str:
    return _SEV_LABEL[sev]


def severity_icon(sev: Severity) -> str:
    return _SEV_ICON[sev]


def _freeze(d: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(d or {}))


@dataclass(frozen=True)
class Rule:
    """One reviewable standard, loaded from rules.yaml."""
    id: str
    name: str
    severity: Severity
    applies_to: tuple[str, ...]      # ddl | dml | dql | object
    check: str                       # whitelisted checker name
    message: str = ""
    rationale: str = ""
    fix: str = ""
    enabled: bool = True
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", _freeze(self.params))
        object.__setattr__(self, "applies_to", tuple(self.applies_to))


@dataclass(frozen=True)
class Statement:
    """One SQL statement, after comment stripping and literal placeholding."""
    idx: int                         # 1-based position in the input
    line: int                        # 1-based starting line in the input
    kind: str                        # ddl | dml | dql | other
    verb: str                        # create_table | delete | select | ...
    raw: str                         # original text
    normalized: str                  # comments gone, literals -> :sN
    literals: Mapping[str, str] = field(default_factory=dict)   # :sN -> literal body
    table: str = ""                  # folded: what the catalog will actually hold
    index_name: str = ""
    index_cols: tuple[str, ...] = ()
    # As written in the source, case preserved. Naming rules must judge what the
    # author typed (CREATE TABLE OrderItems violates a lower_snake standard even
    # though the server folds it to `orderitems`).
    table_raw: str = ""
    index_raw: str = ""
    column_raw: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "literals", _freeze(self.literals))
        object.__setattr__(self, "index_cols", tuple(self.index_cols))
        object.__setattr__(self, "column_raw", tuple(self.column_raw))


@dataclass(frozen=True)
class TableFact:
    """An existing table, as reported by the catalog."""
    schema: str
    table: str
    has_pk: bool
    fks: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fks", tuple(self.fks))
        object.__setattr__(self, "columns", tuple(self.columns))


@dataclass(frozen=True)
class IndexFact:
    """An existing index, as reported by the catalog."""
    schema: str
    table: str
    name: str
    columns: tuple[str, ...] = ()
    is_unique: bool = False
    is_primary: bool = False
    scans: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))


@dataclass(frozen=True)
class ObjectFacts:
    """Catalog snapshot for one schema. `notes` carries degraded dimensions."""
    tables: tuple[TableFact, ...] = ()
    indexes: tuple[IndexFact, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", tuple(self.tables))
        object.__setattr__(self, "indexes", tuple(self.indexes))
        object.__setattr__(self, "notes", tuple(self.notes))


@dataclass(frozen=True)
class Finding:
    """One rule hit. `advisory=True` means the script gathered evidence but did
    not judge — the LLM decides, using `rationale` (the rule's criteria)."""
    rule_id: str
    rule_name: str
    severity: Severity
    message: str
    location: str
    snippet: str = ""
    rationale: str = ""
    fix: str = ""
    advisory: bool = False
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class ReviewResult:
    source: str                      # file:a.sql | sql_id:xxx | schema:public
    findings: tuple[Finding, ...] = ()
    statements: int = 0
    objects: int = 0
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "notes", tuple(self.notes))


def sort_findings(findings) -> tuple[Finding, ...]:
    """Most severe first; stable within a severity (input order preserved)."""
    return tuple(sorted(findings, key=lambda f: -int(f.severity)))
