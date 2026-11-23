"""The normalized permit schema every city maps into, and the tool result envelopes.

Fields a city doesn't publish are ``None``; nothing here is ever derived or guessed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

Status = Literal["applied", "in_review", "issued", "finaled", "expired", "cancelled", "unknown"]
STATUSES: tuple[Status, ...] = get_args(Status)
OPEN_STATUSES: tuple[Status, ...] = ("applied", "in_review", "issued")

PermitType = Literal[
    "new_construction",
    "addition_alteration",
    "repair",
    "demolition",
    "electrical",
    "mechanical",
    "plumbing",
    "sign",
    "site_work",
    "other",
    "unknown",
]
PERMIT_TYPES: tuple[PermitType, ...] = get_args(PermitType)

DateField = Literal["issued", "applied"]
GroupBy = Literal["status", "permit_type", "month", "year"]

# Normalized fields a city config may leave unmapped (published as always-null).
OPTIONAL_FIELDS = (
    "address",
    "applied_date",
    "issued_date",
    "last_activity_date",
    "valuation_usd",
    "description",
    "source_url",
)


class Permit(BaseModel):
    permit_id: str
    city: str
    address: str | None
    permit_type: PermitType
    permit_type_raw: str | None = Field(description="Exact source value(s) the type was mapped from.")
    status: Status
    status_raw: str | None = Field(description="Exact source value(s) the status was mapped from.")
    applied_date: date | None
    issued_date: date | None
    last_activity_date: date | None
    valuation_usd: float | None = Field(description="Applicant-reported project cost, as published.")
    description: str | None
    source_url: str | None


class PermitDetail(Permit):
    raw: dict[str, Any] = Field(description="Every non-null column exactly as the city publishes it.")


class StalePermit(Permit):
    open_since: date
    open_since_basis: Literal["applied", "issued"]
    days_open: int


class _Page(BaseModel):
    total_count: int = Field(description="All records matching the filters, not just this page.")
    returned: int
    next_cursor: str | None = Field(description="Pass back as `cursor` with the same filters for the next page.")
    hint: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SearchResult(_Page):
    city: str
    permits: list[Permit]


class StaleResult(_Page):
    city: str
    as_of: date = Field(description="Today's date in the city's timezone; days_open is counted to this date.")
    open_longer_than_days: int
    excluded_unknown_status_count: int = Field(
        description="Permits that match every other filter but whose status is unknown, so they can't be "
        "classified as open. Mention these when answering."
    )
    permits: list[StalePermit]


class StatGroup(BaseModel):
    key: str
    count: int
    total_valuation_usd: float | None = Field(description="Sum of non-null valuations in this group; null if none.")


class StatsResult(BaseModel):
    city: str
    group_by: GroupBy
    date_field: DateField
    date_from: date
    date_to: date
    total_count: int
    groups: list[StatGroup]
    warnings: list[str] = Field(default_factory=list)


class DateRange(BaseModel):
    earliest: date | None
    latest: date | None


class CityInfo(BaseModel):
    id: str
    name: str
    portal_url: str
    dataset_id: str
    data_last_updated: datetime | None = Field(description="When the city last updated the dataset (UTC).")
    issued_date_range: DateRange | None
    statuses_available: list[Status]
    permit_types_available: list[PermitType]
    fields_unavailable: list[str] = Field(description="Normalized fields this city never publishes; always null.")
    known_gaps: list[str]


class CitiesResult(BaseModel):
    cities: list[CityInfo]
    warnings: list[str] = Field(default_factory=list)
