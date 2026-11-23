"""SoQL builder tests.

The rule-inversion tests execute the generated predicates in SQLite, which shares SoQL's
three-valued NULL logic for these operators. They check every combination of raw values,
including NULLs and unmapped values, against the Python classifier. So "filter by status X"
and "classify a row as X" provably agree.
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import date

import pytest

from permits_mcp import soql
from permits_mcp.adapter import CityAdapter
from permits_mcp.city_config import CityConfig, Concat
from permits_mcp.schema import OPEN_STATUSES, PERMIT_TYPES, STATUSES


def _table(columns: list[str], domains: dict[str, list[str | None]]) -> tuple[sqlite3.Connection, list[dict]]:
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE t ({', '.join(f'{c} TEXT' for c in columns)})")
    rows = [dict(zip(columns, combo, strict=True)) for combo in itertools.product(*(domains[c] for c in columns))]
    db.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in columns)})", [tuple(r.values()) for r in rows])
    return db, rows


def _domain(rules, extra: list[str | None]) -> dict[str, list[str | None]]:
    domains: dict[str, list[str | None]] = {}
    for rule in rules:
        for column, values in rule.when.items():
            domains.setdefault(column, [])
            domains[column] += [v for v in values if v not in domains[column]]
    return {c: [*vals, *extra] for c, vals in domains.items()}


def _selected(db: sqlite3.Connection, predicate: str) -> set[int]:
    return {rowid - 1 for (rowid,) in db.execute(f"SELECT rowid FROM t WHERE {predicate}")}


@pytest.mark.parametrize("targets", [*[(s,) for s in STATUSES], OPEN_STATUSES, ("finaled", "unknown")])
def test_status_predicate_matches_classifier(cities: dict[str, CityConfig], targets: tuple[str, ...]) -> None:
    for config in cities.values():
        adapter = CityAdapter(config)
        columns = config.status.columns
        db, rows = _table(columns, _domain(config.status.rules, [None, "SOMETHING NEW"]))
        expected = {i for i, row in enumerate(rows) if adapter.classify_status(row) in targets}
        predicate = adapter.status_predicate(targets)  # type: ignore[arg-type]
        if predicate is None:
            assert expected == set(), f"{config.id}: {targets} has rows but no predicate"
        else:
            assert _selected(db, predicate) == expected, f"{config.id}: {targets}"


@pytest.mark.parametrize("target", PERMIT_TYPES)
def test_type_predicate_matches_classifier(cities: dict[str, CityConfig], target: str) -> None:
    for config in cities.values():
        adapter = CityAdapter(config)
        db, rows = _table(config.permit_type.columns, _domain(config.permit_type.rules, [None, "SOMETHING NEW"]))
        expected = {i for i, row in enumerate(rows) if adapter.classify_type(row) == target}
        predicate = adapter.type_predicate([target])  # type: ignore[list-item]
        if predicate is None:
            assert expected == set()
        else:
            assert _selected(db, predicate) == expected


def test_null_rows_survive_negation(cities: dict[str, CityConfig]) -> None:
    """The bug this design prevents: NOT (col IN (...)) is NULL for a NULL col and drops the row."""
    adapter = CityAdapter(cities["chicago"])
    db, _rows = _table(["permit_status", "permit_milestone"], {"permit_status": [None], "permit_milestone": [None]})
    unknown = adapter.status_predicate(["unknown"])
    assert unknown is not None
    assert _selected(db, unknown) == {0}


def test_quote_escapes_single_quotes() -> None:
    assert soql.quote("O'Brien") == "'O''Brien'"
    db = sqlite3.connect(":memory:")
    assert db.execute(f"SELECT {soql.quote("x' OR '1'='1")}").fetchone()[0] == "x' OR '1'='1"


def test_date_range_is_inclusive() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (d TEXT)")
    values = [
        "2025-05-31T00:00:00.000",
        "2025-06-01T00:00:00.000",
        "2025-06-30T00:00:00.000",
        "2025-07-01T00:00:00.000",
        None,
    ]
    db.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    pred = soql.date_range("d", date(2025, 6, 1), date(2025, 6, 30), None)
    assert pred is not None
    got = [r[0] for r in db.execute(f"SELECT d FROM t WHERE {pred} ORDER BY d")]
    assert got == ["2025-06-01T00:00:00.000", "2025-06-30T00:00:00.000"]
    assert soql.date_range("d", None, None, None) is None


def test_date_placeholder_excluded() -> None:
    pred = soql.date_present("d", date(1901, 1, 1))
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (d TEXT)")
    db.executemany("INSERT INTO t VALUES (?)", [("1900-01-01T00:00:00.000",), ("2020-01-01T00:00:00.000",), (None,)])
    assert [r[0] for r in db.execute(f"SELECT d FROM t WHERE {pred}")] == ["2020-01-01T00:00:00.000"]


def test_address_expr_drops_null_parts() -> None:
    expr = soql.address_expr(Concat(concat=["num", "dir", "name"]))
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (num TEXT, dir TEXT, name TEXT)")
    db.executemany("INSERT INTO t VALUES (?, ?, ?)", [("121", "N", "La Salle St"), ("1404", None, "Lorel Ave")])
    assert [r[0] for r in db.execute(f"SELECT {expr} FROM t")] == ["121 N LA SALLE ST", "1404 LOREL AVE"]


def test_like_contains_strips_wildcards() -> None:
    assert soql.like_contains("upper(a)", "LOREL%") == "upper(a) like '%LOREL%'"
    assert soql.like_contains("upper(a)", "O'NEIL") == "upper(a) like '%O''NEIL%'"


def test_number_literal() -> None:
    assert soql.number(1_000_000) == "1000000"
    assert soql.number(1500.5) == "1500.5"
