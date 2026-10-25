"""Hypothetical-index verification (port of internal/probe/hypoindex.go).

Asks gs_index_advise for candidate indexes, then hypothetically creates each
(hypopg) and re-EXPLAINs to measure the real cost delta — no real index is
built. Returns only candidates that reduce cost by at least min_speedup.
"""
from __future__ import annotations

from dataclasses import dataclass

from cost import explain_cost, quote_columns, quote_ident, quote_sql_literal

MIN_SPEEDUP = 1.3  # shared cost-improvement threshold (mirrors opendb)


@dataclass(frozen=True)
class IndexCandidate:
    schema: str
    table: str
    columns: str
    ddl: str
    orig_cost: float
    hypo_cost: float
    speedup: float
    used: bool


def _plan_uses_index(db, sql_text: str, index_name: str) -> bool:
    try:
        _, rows = db.query(f"EXPLAIN (FORMAT TEXT) {sql_text}")
    except Exception:
        return False
    plan = "\n".join(str(r[0]) for r in rows)
    return index_name in plan


def _reset_hypo(db) -> None:
    try:
        db.execute("SELECT hypopg_reset_index()")
    except Exception:
        pass


def verify_indexes(db, sql_text: str, min_speedup: float = MIN_SPEEDUP) -> list[IndexCandidate]:
    """gs_index_advise -> hypopg create -> re-EXPLAIN, keep speedup>=min_speedup.

    The common.Database is a single physical connection, so session-local GUC
    (enable_hypo_index) and hypopg virtual indexes persist across statements —
    matching the Go pinned-connection contract.
    """
    if not getattr(db, "provides_session", True):
        from common.backends.base import DBError
        raise DBError(
            "hypopg 索引验证需要持久会话:gsql 每条语句起独立子进程,"
            "会话级 GUC / hypopg 虚拟索引不跨语句留存,验证会失效。"
            "请对该连接改用 driver: psycopg2（gsql 后端不支持索引验证）。"
        )
    if min_speedup < 1.0:
        min_speedup = 1.0

    orig_cost = explain_cost(db, sql_text)
    if orig_cost <= 0:
        raise ValueError(f"baseline cost {orig_cost} <= 0, cannot compare")

    db.execute("SET enable_hypo_index = on")
    try:
        advise = (f"SELECT schema, \"table\", \"column\", indextype "
                  f"FROM gs_index_advise('{quote_sql_literal(sql_text)}')")
        try:
            _, adv_rows = db.query(advise)
        except Exception as exc:  # add the same context Go's hypoindex.go uses
            raise RuntimeError(f"gs_index_advise: {exc}") from exc

        accepted: list[IndexCandidate] = []
        for row in adv_rows:
            schema, table, column = row[0], row[1], row[2]
            if not column:
                continue
            ddl = f"CREATE INDEX ON {quote_ident(schema)}.{quote_ident(table)}({quote_columns(column)})"

            db.execute("SELECT hypopg_reset_index()")
            index_name = db.scalar(
                f"SELECT indexname FROM hypopg_create_index('{quote_sql_literal(ddl)}')")
            if index_name is None:
                continue

            hypo_cost = explain_cost(db, sql_text)
            if hypo_cost <= 0:
                _reset_hypo(db)
                continue

            speedup = orig_cost / hypo_cost
            if speedup >= min_speedup:
                used = _plan_uses_index(db, sql_text, index_name)
                accepted.append(IndexCandidate(
                    schema=schema, table=table, columns=column, ddl=ddl,
                    orig_cost=orig_cost, hypo_cost=hypo_cost,
                    speedup=speedup, used=used))
            db.execute("SELECT hypopg_reset_index()")
    finally:
        _reset_hypo(db)

    accepted.sort(key=lambda c: c.speedup, reverse=True)
    return accepted
