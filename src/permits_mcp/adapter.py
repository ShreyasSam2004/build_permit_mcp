"""One generic, config-driven adapter: raw Socrata rows -> normalized permits, and
normalized filters -> SoQL. There is no per-city code; a city is entirely its config."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote as url_quote

from . import soql
from .city_config import CityConfig, Concat, Template
from .schema import DateField, Permit, PermitDetail, PermitType, Status

log = logging.getLogger(__name__)

DESCRIPTION_PREVIEW = 200

# Standard USPS abbreviations, so "121 North LaSalle Street" and "121 N LASALLE ST" agree.
_ADDRESS_WORDS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "ROAD": "RD", "DRIVE": "DR",
    "PLACE": "PL", "COURT": "CT", "LANE": "LN", "TERRACE": "TER", "PARKWAY": "PKWY",
    "HIGHWAY": "HWY", "SQUARE": "SQ", "CIRCLE": "CIR", "PLAZA": "PLZ",
}  # fmt: skip


def normalize_address_query(text: str) -> str:
    words = re.sub(r"[^\w\s/-]", " ", text.upper()).split()
    return " ".join(_ADDRESS_WORDS.get(w, w) for w in words)


class NoPossibleMatch(Exception):
    """The filters can never match in this city (e.g. a status it never publishes)."""


@dataclass(frozen=True)
class Filters:
    address: str | None = None
    statuses: tuple[Status, ...] | None = None
    raw_statuses: tuple[str, ...] | None = None  # values of the city's own status column, any case
    permit_types: tuple[PermitType, ...] | None = None
    date_field: DateField = "issued"
    date_from: date | None = None
    date_to: date | None = None
    min_valuation_usd: float | None = None


class CityAdapter:
    def __init__(self, config: CityConfig) -> None:
        self.config = config
        self.f = config.fields
        self._warned: set[tuple[str, str]] = set()
        self.unmapped_seen: dict[tuple[str, str], int] = {}

    # ---- row -> normalized ------------------------------------------------------------

    def classify_status(self, row: dict[str, Any]) -> Status:
        for rule in self.config.status.rules:
            if rule.matches(row):
                return rule.then
        self._note_unmapped("status", self.config.status.columns, row)
        return "unknown"

    def classify_type(self, row: dict[str, Any]) -> PermitType:
        for rule in self.config.permit_type.rules:
            if rule.matches(row):
                return rule.then
        self._note_unmapped("permit_type", self.config.permit_type.columns, row)
        return "unknown"

    def _note_unmapped(self, kind: str, columns: Sequence[str], row: dict[str, Any]) -> None:
        raw = _join_raw(row, columns)
        if raw is None:  # blank source value: missing data, not drift
            return
        key = (kind, raw)
        self.unmapped_seen[key] = self.unmapped_seen.get(key, 0) + 1
        if key not in self._warned:
            self._warned.add(key)
            log.warning(
                "unmapped raw value -> unknown",
                extra={"city": self.config.id, "kind": kind, "raw_value": raw, "columns": list(columns)},
            )

    def to_permit(self, row: dict[str, Any], *, preview: bool = True) -> Permit:
        description = self._text(row, self.f.description)
        if preview and description and len(description) > DESCRIPTION_PREVIEW:
            description = description[: DESCRIPTION_PREVIEW - 1].rstrip() + "…"
        permit_id = str(row[self.f.permit_id])
        return Permit(
            permit_id=permit_id,
            city=self.config.id,
            address=self._address(row),
            permit_type=self.classify_type(row),
            permit_type_raw=_join_raw(row, self.config.permit_type.columns),
            status=self.classify_status(row),
            status_raw=_join_raw(row, self.config.status.columns),
            applied_date=self._date(row, self.f.applied_date),
            issued_date=self._date(row, self.f.issued_date),
            last_activity_date=self._date(row, self.f.last_activity_date),
            valuation_usd=self._money(row, self.f.valuation_usd),
            description=description,
            source_url=self._source_url(row, permit_id),
        )

    def to_detail(self, row: dict[str, Any]) -> PermitDetail:
        base = self.to_permit(row, preview=False)
        raw = {k: v for k, v in row.items() if v is not None and not k.startswith(":")}
        return PermitDetail(**base.model_dump(), raw=raw)

    def _text(self, row: dict[str, Any], column: str | None) -> str | None:
        if column is None:
            return None
        value = row.get(column)
        text = str(value).strip() if value is not None else ""
        return text or None

    def _address(self, row: dict[str, Any]) -> str | None:
        spec = self.f.address
        if spec is None:
            return None
        if isinstance(spec, Concat):
            parts = [self._text(row, c) for c in spec.concat]
            joined = " ".join(p for p in parts if p)
            return joined or None
        return self._text(row, spec)

    def _date(self, row: dict[str, Any], column: str | None) -> date | None:
        text = self._text(row, column)
        if text is None:
            return None
        d = date.fromisoformat(text[:10])
        if self.config.date_null_before and d < self.config.date_null_before:
            return None  # placeholder like 1900-01-01: not a real date
        return d

    def _money(self, row: dict[str, Any], column: str | None) -> float | None:
        text = self._text(row, column)
        if text is None:
            return None
        value = float(text)
        return value if value >= 0 else None  # a negative cost is impossible, not data

    def _source_url(self, row: dict[str, Any], permit_id: str) -> str | None:
        spec = self.f.source_url
        if spec is None:
            return None
        if isinstance(spec, Template):
            return spec.template.format(permit_id=url_quote(permit_id, safe=""))
        value = row.get(spec)
        if isinstance(value, dict):  # Socrata "url" columns come back as {"url": ...}
            value = value.get("url")
        return str(value) if value else None

    # ---- normalized filters -> SoQL ---------------------------------------------------

    def date_column(self, field: DateField) -> str | None:
        return self.f.issued_date if field == "issued" else self.f.applied_date

    def status_predicate(self, statuses: Iterable[Status]) -> str | None:
        return soql.rules_predicate(self.config.status.rules, statuses, "unknown")

    def type_predicate(self, types: Iterable[PermitType]) -> str | None:
        return soql.rules_predicate(self.config.permit_type.rules, types, "unknown")

    def valuation_at_least(self, minimum: float) -> str | None:
        if self.f.valuation_usd is None:
            return None
        return f"{self.f.valuation_usd} >= {soql.number(minimum)}"

    def where(self, filters: Filters) -> str | None:
        """Compile filters to a ``$where``. Raises ``NoPossibleMatch`` if nothing can match."""
        parts: list[str | None] = []
        if filters.statuses:
            pred = self.status_predicate(filters.statuses)
            if pred is None:
                raise NoPossibleMatch("status")
            parts.append(pred)
        if filters.raw_statuses:
            parts.append(soql.in_ignoring_case(self.config.status.raw_column, filters.raw_statuses))
        if filters.permit_types:
            pred = self.type_predicate(filters.permit_types)
            if pred is None:
                raise NoPossibleMatch("permit_type")
            parts.append(pred)
        if filters.date_from or filters.date_to:
            column = self.date_column(filters.date_field)
            if column is None:
                raise NoPossibleMatch(f"{filters.date_field}_date")
            parts.append(soql.date_range(column, filters.date_from, filters.date_to, self.config.date_null_before))
        if filters.min_valuation_usd is not None:
            pred = self.valuation_at_least(filters.min_valuation_usd)
            if pred is None:
                raise NoPossibleMatch("valuation_usd")
            parts.append(pred)
        if filters.address:
            if self.f.address is None:
                raise NoPossibleMatch("address")
            needle = normalize_address_query(filters.address)
            parts.append(soql.like_contains(soql.address_expr(self.f.address), needle))
        return soql.and_(*parts)

    def list_columns(self) -> list[str]:
        """Columns needed to build a ``Permit`` (keeps list responses small)."""
        return self.config.mapped_columns()


def _join_raw(row: dict[str, Any], columns: Sequence[str]) -> str | None:
    values = [str(row[c]).strip() for c in columns if row.get(c) not in (None, "")]
    return " / ".join(values) if values else None
