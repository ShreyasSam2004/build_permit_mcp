"""SoQL building blocks.

Two rules keep this safe and correct:

* User text only ever enters a query through ``quote`` (single quotes doubled). Column names
  come only from validated config, never from tool input.
* Every rule predicate is *two-valued*. In SoQL, as in SQL, ``col IN (...)`` is NULL when
  ``col`` is NULL, and ``NOT NULL`` is still NULL, so a naive ``NOT (...)`` silently drops
  rows with blank columns (303k of Chicago's rows have a blank status). Wrapping each test
  as ``(col IS NOT NULL AND col IN (...))`` makes it plain TRUE/FALSE, so negation is safe.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Protocol

from .city_config import Concat


class MappingRule(Protocol):
    @property
    def when(self) -> dict[str, list[str]]: ...
    @property
    def then(self) -> str: ...


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def number(value: float) -> str:
    return repr(float(value)) if value != int(value) else str(int(value))


def timestamp(d: date) -> str:
    """Socrata floating-timestamp literal for midnight at the start of ``d``."""
    return quote(f"{d.isoformat()}T00:00:00")


def and_(*parts: str | None) -> str | None:
    kept = [p for p in parts if p]
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else " AND ".join(f"({p})" for p in kept)


def or_(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else " OR ".join(f"({p})" for p in parts)


def rule_predicate(rule: MappingRule) -> str:
    tests = [
        f"{col} IS NOT NULL AND {col} IN ({', '.join(quote(v) for v in values)})" for col, values in rule.when.items()
    ]
    return " AND ".join(f"({t})" for t in tests)


def in_ignoring_case(column: str, values: Iterable[str]) -> str:
    """``column`` equals one of ``values``, ignoring case; two-valued like rule predicates."""
    return f"{column} IS NOT NULL AND upper({column}) IN ({', '.join(quote(v.upper()) for v in values)})"


def rules_predicate(rules: Sequence[MappingRule], targets: Iterable[str], unknown: str) -> str | None:
    """Predicate selecting rows whose first-matching rule maps into ``targets``.

    Rule *i* with a wanted target contributes ``P_i AND NOT P_j`` for each earlier rule *j*
    with an unwanted target (first match wins). ``unknown`` selects rows no rule matches.
    Returns ``None`` when no row can possibly match (the caller can skip the request).
    """
    wanted = set(targets)
    clauses: list[str] = []
    for i, rule in enumerate(rules):
        if rule.then not in wanted:
            continue
        earlier = [f"NOT ({rule_predicate(r)})" for r in rules[:i] if r.then not in wanted]
        clauses.append(" AND ".join([f"({rule_predicate(rule)})", *earlier]))
    if unknown in wanted:
        clauses.append(" AND ".join(f"NOT ({rule_predicate(r)})" for r in rules))
    return or_(clauses) if clauses else None


def date_present(column: str, null_before: date | None) -> str:
    """Column holds a real date (not null, not a placeholder)."""
    if null_before is None:
        return f"{column} IS NOT NULL"
    return f"{column} IS NOT NULL AND {column} >= {timestamp(null_before)}"


def date_range(column: str, start: date | None, end: date | None, null_before: date | None) -> str | None:
    """Inclusive ``[start, end]`` on a date column."""
    parts = [date_present(column, null_before)]
    if start is not None:
        parts.append(f"{column} >= {timestamp(start)}")
    if end is not None:
        parts.append(f"{column} < {timestamp(end + timedelta(days=1))}")
    return " AND ".join(parts) if (start or end) else None


def address_expr(address: str | Concat) -> str:
    """Upper-cased address expression. Each concatenated part is wrapped so a NULL part
    drops out instead of nulling the whole string (``NULL || 'x'`` is NULL in SoQL)."""
    if isinstance(address, str):
        return f"upper({address})"
    *head, last = address.concat
    pieces = [f"coalesce({c} || ' ', '')" for c in head] + [f"coalesce({last}, '')"]
    return "upper(" + " || ".join(pieces) + ")"


def like_contains(expr: str, needle: str) -> str:
    # '%' would let input widen the match arbitrarily; '_' (any one char) is harmless.
    return f"{expr} like {quote('%' + needle.replace('%', '') + '%')}"
