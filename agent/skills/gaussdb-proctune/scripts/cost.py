"""Plan-cost extraction + SQL quoting helpers.

Ports internal/probe/explaincost.go and the quoting helpers from hypoindex.go.
Shared by hypoindex.py and verify.py (both vendored in this skill).
"""
from __future__ import annotations

import json


def explain_cost(db, sql_text: str) -> float:
    """Run EXPLAIN (FORMAT JSON, COSTS TRUE) and return the root Total Cost."""
    _, rows = db.query(f"EXPLAIN (FORMAT JSON, COSTS TRUE) {sql_text}")
    if not rows:
        raise ValueError("explain cost: no rows returned")
    first = rows[0][0]
    if isinstance(first, (list, dict)):
        # psycopg2 auto-decodes the json column into a Python object.
        plans = first
    else:
        # Fallback: JSON document returned as text (possibly split across rows).
        plans = json.loads("".join(str(r[0]) for r in rows))
    if isinstance(plans, dict):
        plans = [plans]
    if not plans:
        raise ValueError("explain cost: empty plan array")
    return float(plans[0]["Plan"]["Total Cost"])


def quote_sql_literal(s: str) -> str:
    """Escape a string for embedding inside a single-quoted SQL literal."""
    return s.replace("'", "''")


def quote_ident(s: str) -> str:
    """Double-quote a SQL identifier, escaping embedded double quotes."""
    return '"' + s.replace('"', '""') + '"'


def quote_columns(cols: str) -> str:
    """Quote each comma-separated column: 'a,b' -> \"a\",\"b\"."""
    return ",".join(quote_ident(p.strip()) for p in cols.split(","))
