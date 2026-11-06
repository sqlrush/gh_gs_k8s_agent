"""Load and validate rules.yaml (pure functions apart from the file read).

Everything is validated at the boundary and fails fast with the offending rule
id in the message: an unknown check name, a bad severity, a pattern that will
not compile, or a missing required parameter is a hard error, not a silent skip.
A rule that survives loading is guaranteed executable by checks.py.
"""
from __future__ import annotations

import pathlib
import re
from typing import Any, Mapping

import yaml

from model import Rule, RuleError, severity_from

_HERE = pathlib.Path(__file__).resolve()
DEFAULT_RULES_PATH = _HERE.parent.parent / "references" / "rules.yaml"

_KINDS = frozenset({"ddl", "dml", "dql", "object"})
_TARGETS = frozenset({"table", "index", "column"})
_STMT_KINDS = frozenset({"delete", "truncate", "drop"})
_REGEX_SCOPES = frozenset({"normalized", "raw"})

# Reserved top-level keys; anything else on a rule becomes a checker parameter.
_RESERVED = frozenset(
    {"id", "name", "severity", "applies_to", "check", "message",
     "rationale", "fix", "enabled"}
)

# The checker whitelist: name -> required parameter names.
CHECKS: Mapping[str, tuple[str, ...]] = {
    "regex": ("pattern",),
    "advisory": ("criteria",),
    "table_no_primary_key": (),
    "table_has_foreign_key": (),
    "naming_pattern": ("target", "pattern"),
    "index_column_count": ("max",),
    "stmt_forbidden": ("kind",),
    "dml_without_where": (),
    "select_star": (),
    "leading_wildcard_like": (),
    "index_redundant": (),
}

# Which statement kinds each checker can actually judge. Declaring a rule
# outside its checker's scope would make it a silent no-op, so it is an error.
_SCOPES: Mapping[str, frozenset] = {
    "regex": frozenset({"ddl", "dml", "dql"}),
    "advisory": frozenset({"ddl", "dml", "dql", "object"}),
    "table_no_primary_key": frozenset({"ddl", "object"}),
    "table_has_foreign_key": frozenset({"ddl", "object"}),
    "naming_pattern": frozenset({"ddl", "object"}),
    "index_column_count": frozenset({"ddl", "object"}),
    "stmt_forbidden": frozenset({"ddl", "dml"}),
    "dml_without_where": frozenset({"dml"}),
    "select_star": frozenset({"dml", "dql"}),
    "leading_wildcard_like": frozenset({"dml", "dql"}),
    "index_redundant": frozenset({"object"}),
}


def _require(cond: bool, rid: str, origin: str, msg: str) -> None:
    if not cond:
        raise RuleError(f"{origin}: rule {rid or '<no id>'}: {msg}")


def _validate_params(rid: str, origin: str, check: str, params: dict[str, Any]) -> None:
    for want in CHECKS[check]:
        _require(want in params, rid, origin,
                 f"check '{check}' requires the '{want}' parameter")

    if "pattern" in params:
        try:
            re.compile(str(params["pattern"]))
        except re.error as exc:
            raise RuleError(
                f"{origin}: rule {rid}: pattern does not compile: {exc}") from exc

    if check == "naming_pattern":
        _require(str(params.get("target")) in _TARGETS, rid, origin,
                 f"target must be one of: {', '.join(sorted(_TARGETS))}")

    if check == "stmt_forbidden":
        _require(str(params.get("kind")).lower() in _STMT_KINDS, rid, origin,
                 f"kind must be one of: {', '.join(sorted(_STMT_KINDS))}")

    if check == "index_column_count":
        try:
            _require(int(params["max"]) > 0, rid, origin, "max must be a positive int")
        except (TypeError, ValueError) as exc:
            raise RuleError(f"{origin}: rule {rid}: max must be an int") from exc

    if check == "regex" and "on" in params:
        _require(str(params["on"]) in _REGEX_SCOPES, rid, origin,
                 f"on must be one of: {', '.join(sorted(_REGEX_SCOPES))}")


def _parse_one(raw: Any, origin: str) -> Rule | None:
    _require(isinstance(raw, dict), "", origin, "each rule must be a mapping")
    rid = str(raw.get("id", "")).strip()
    _require(bool(rid), "", origin, "rule is missing 'id'")

    if raw.get("enabled", True) is False:
        return None

    check = str(raw.get("check", "")).strip()
    _require(check in CHECKS, rid, origin,
             f"unknown check {check!r} (known checks: {', '.join(sorted(CHECKS))})")

    applies = raw.get("applies_to") or []
    _require(isinstance(applies, list) and bool(applies), rid, origin,
             "applies_to must be a non-empty list")
    for kind in applies:
        _require(str(kind) in _KINDS, rid, origin,
                 f"applies_to has unknown kind {kind!r} "
                 f"(known: {', '.join(sorted(_KINDS))})")
        _require(str(kind) in _SCOPES[check], rid, origin,
                 f"check '{check}' cannot judge {kind!r} "
                 f"(it applies to: {', '.join(sorted(_SCOPES[check]))})")

    params = {k: v for k, v in raw.items() if k not in _RESERVED}
    _validate_params(rid, origin, check, params)

    return Rule(
        id=rid,
        name=str(raw.get("name", rid)),
        severity=severity_from(raw.get("severity", "warn")),
        applies_to=tuple(str(k) for k in applies),
        check=check,
        message=str(raw.get("message", "")),
        rationale=str(raw.get("rationale", "")),
        fix=str(raw.get("fix", "")),
        params=params,
    )


def parse_rules(data: Any, origin: str) -> tuple[Rule, ...]:
    """Validate a parsed rules document. Disabled rules are dropped."""
    _require(isinstance(data, dict), "", origin, "rules file must be a YAML mapping")
    raw_rules = data.get("rules")
    _require(isinstance(raw_rules, list) and bool(raw_rules), "", origin,
             "rules file must define a non-empty 'rules' list")

    out: list[Rule] = []
    seen: set[str] = set()
    for raw in raw_rules:
        rule = _parse_one(raw, origin)
        if rule is None:
            continue
        _require(rule.id not in seen, rule.id, origin, "duplicate rule id")
        seen.add(rule.id)
        out.append(rule)
    return tuple(out)


def load_rules(path: pathlib.Path | str = DEFAULT_RULES_PATH) -> tuple[Rule, ...]:
    """Read and validate a rules.yaml. Raises RuleError on anything malformed."""
    p = pathlib.Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleError(f"cannot read rules file {p}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleError(f"{p}: invalid YAML: {exc}") from exc
    return parse_rules(data, str(p))
