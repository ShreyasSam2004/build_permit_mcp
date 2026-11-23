"""City config files: the only thing you write to add a city.

Each ``cities/<id>.toml`` is validated into a ``CityConfig``. Status and type mappings are
*ordered rules* over one or more raw columns (first match wins), because live data showed a
single column is often not enough (Chicago: status COMPLETE + milestone CANCELLED is a
cancellation; Austin: permit type needs two columns).
"""

from __future__ import annotations

import re
import tomllib
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schema import OPTIONAL_FIELDS, PermitType, Status

_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")


def _check_column(name: str) -> str:
    # Column names are interpolated into SoQL, so only plain identifiers are allowed.
    if not _COLUMN.fullmatch(name):
        raise ValueError(f"invalid column name {name!r}")
    return name


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Rule(_Strict):
    """Matches when every listed column holds one of its listed values (exact match)."""

    when: dict[str, list[str]] = Field(min_length=1)

    @field_validator("when")
    @classmethod
    def _valid(cls, when: dict[str, list[str]]) -> dict[str, list[str]]:
        for column, values in when.items():
            _check_column(column)
            if not values:
                raise ValueError(f"rule for column {column!r} lists no values")
        return when

    def matches(self, row: dict[str, object]) -> bool:
        return all(row.get(column) in values for column, values in self.when.items())


class StatusRule(Rule):
    then: Status


class TypeRule(Rule):
    then: PermitType


class StatusRules(_Strict):
    rules: list[StatusRule] = Field(min_length=1)

    @property
    def columns(self) -> list[str]:
        return _columns(self.rules)


class TypeRules(_Strict):
    rules: list[TypeRule] = Field(min_length=1)

    @property
    def columns(self) -> list[str]:
        return _columns(self.rules)


def _columns(rules: list[StatusRule] | list[TypeRule]) -> list[str]:
    seen: dict[str, None] = {}
    for rule in rules:
        seen.update(dict.fromkeys(rule.when))
    return list(seen)


class Concat(_Strict):
    concat: list[str] = Field(min_length=2)

    @field_validator("concat")
    @classmethod
    def _valid(cls, cols: list[str]) -> list[str]:
        return [_check_column(c) for c in cols]


class Template(_Strict):
    template: str

    @field_validator("template")
    @classmethod
    def _valid(cls, t: str) -> str:
        if "{permit_id}" not in t:
            raise ValueError("template must contain {permit_id}")
        return t


class Fields(_Strict):
    permit_id: str
    address: str | Concat | None = None
    applied_date: str | None = None
    issued_date: str | None = None
    last_activity_date: str | None = None
    valuation_usd: str | None = None
    description: str | None = None
    source_url: str | Template | None = None  # a column holding a URL, or a template

    @model_validator(mode="after")
    def _valid(self) -> Self:
        for value in self.model_dump().values():
            if isinstance(value, str) and "{" not in value:
                _check_column(value)
        return self

    def columns(self) -> list[str]:
        cols: list[str] = []
        for name in ("permit_id", *OPTIONAL_FIELDS):
            value = getattr(self, name)
            if isinstance(value, str):
                cols.append(value)
            elif isinstance(value, Concat):
                cols.extend(value.concat)
        return cols


class CityConfig(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    domain: str = Field(pattern=r"^[a-z0-9.-]+\.[a-z]{2,}$")
    dataset_id: str = Field(pattern=r"^[a-z0-9]{4}-[a-z0-9]{4}$")
    timezone: str
    permit_id_example: str
    known_gaps: list[str] = Field(default_factory=list)
    date_null_before: date | None = Field(
        default=None, description="Dates earlier than this are placeholders and normalize to null."
    )
    fields: Fields
    status: StatusRules
    permit_type: TypeRules

    @field_validator("timezone")
    @classmethod
    def _tz(cls, tz: str) -> str:
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {tz!r}") from exc
        return tz

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def portal_url(self) -> str:
        return f"https://{self.domain}/d/{self.dataset_id}"

    def mapped_columns(self) -> list[str]:
        """Every raw column this config depends on (what the schema-drift check guards)."""
        cols = [*self.fields.columns(), *self.status.columns, *self.permit_type.columns]
        return list(dict.fromkeys(cols))

    def unavailable_fields(self) -> list[str]:
        return [name for name in OPTIONAL_FIELDS if getattr(self.fields, name) is None]


def load_city(path: Path) -> CityConfig:
    with path.open("rb") as fh:
        config = CityConfig.model_validate(tomllib.load(fh))
    if config.id != path.stem:
        raise ValueError(f"{path.name}: id {config.id!r} must match the file name")
    return config


def load_cities(directory: Path | None = None) -> dict[str, CityConfig]:
    """Load every ``*.toml`` in ``directory`` (default: the packaged ``cities/``), sorted by id."""
    if directory is None:
        directory = Path(str(resources.files("permits_mcp") / "cities"))
    cities = [load_city(p) for p in sorted(directory.glob("*.toml"))]
    if not cities:
        raise RuntimeError(f"no city configs found in {directory}")
    return {c.id: c for c in cities}
