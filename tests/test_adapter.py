from __future__ import annotations

import logging
from datetime import date

import pytest

from permits_mcp.adapter import DESCRIPTION_PREVIEW, CityAdapter, Filters, NoPossibleMatch, normalize_address_query
from permits_mcp.city_config import CityConfig


@pytest.fixture
def chicago(cities: dict[str, CityConfig]) -> CityAdapter:
    return CityAdapter(cities["chicago"])


def row(**overrides: object) -> dict:
    base: dict[str, object] = {
        "permit_": "B200520605",
        "permit_status": "ACTIVE",
        "permit_milestone": "INSPECTIONS",
        "permit_type": "PERMIT – EXPRESS PERMIT PROGRAM",
        "application_start_date": "2026-11-19T00:00:00.000",
        "issue_date": "2026-11-20T00:00:00.000",
        "street_number": "1404",
        "street_direction": "N",
        "street_name": "LOREL AVE",
        "reported_cost": "3696",
        "work_description": "INSTALL SOLAR PANELS",
        ":@computed_region_rpca_8um6": "7",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_normalizes_a_live_shaped_row(chicago: CityAdapter) -> None:
    p = chicago.to_permit(row())
    assert p.permit_id == "B200520605"
    assert p.address == "1404 N LOREL AVE"
    assert (p.status, p.status_raw) == ("issued", "ACTIVE / INSPECTIONS")
    assert (p.permit_type, p.permit_type_raw) == ("repair", "PERMIT – EXPRESS PERMIT PROGRAM")
    assert (p.applied_date, p.issued_date) == (date(2026, 11, 19), date(2026, 11, 20))
    assert p.last_activity_date is None  # not published by Chicago: null, never derived
    assert p.valuation_usd == 3696.0
    assert p.source_url == "https://data.cityofchicago.org/resource/ydr8-5enu.json?permit_=B200520605"


@pytest.mark.parametrize(
    ("status", "milestone", "expected"),
    [
        ("COMPLETE", "COMPLETE", "finaled"),
        ("COMPLETE", "CANCELLED", "cancelled"),  # milestone overrides a COMPLETE status
        ("COMPLETE", "DENIED", "cancelled"),
        ("COMPLETE", "EXPIRED", "expired"),
        ("EXPIRED", "COMPLETE", "expired"),
        ("REVOKED", "INSPECTIONS", "cancelled"),
        ("SUSPENDED", "STOP WORK", "issued"),
        ("PHASED PERMITTING", "INSPECTIONS", "issued"),
        (None, None, "unknown"),
    ],
)
def test_status_rules(chicago: CityAdapter, status: str | None, milestone: str | None, expected: str) -> None:
    assert chicago.classify_status(row(permit_status=status, permit_milestone=milestone)) == expected


def test_unmapped_value_is_unknown_and_logged_once(chicago: CityAdapter, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert chicago.classify_status(row(permit_status="ON HOLD", permit_milestone="X")) == "unknown"
    warnings = [r for r in caplog.records if r.getMessage() == "unmapped raw value -> unknown"]
    assert len(warnings) == 1
    assert warnings[0].__dict__["raw_value"] == "ON HOLD / X"
    assert chicago.unmapped_seen[("status", "ON HOLD / X")] == 3


def test_blank_status_is_unknown_without_a_warning(chicago: CityAdapter, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        chicago.classify_status(row(permit_status=None, permit_milestone=None))
    assert not caplog.records


def test_address_without_direction_has_single_spaces(chicago: CityAdapter) -> None:
    assert chicago.to_permit(row(street_direction=None)).address == "1404 LOREL AVE"


def test_negative_valuation_is_null_but_zero_is_kept(chicago: CityAdapter) -> None:
    assert chicago.to_permit(row(reported_cost="-1000")).valuation_usd is None
    assert chicago.to_permit(row(reported_cost="0")).valuation_usd == 0.0
    assert chicago.to_permit(row(reported_cost=None)).valuation_usd is None


def test_description_preview_and_detail(chicago: CityAdapter) -> None:
    long = "X" * 500
    assert len(chicago.to_permit(row(work_description=long)).description or "") == DESCRIPTION_PREVIEW
    detail = chicago.to_detail(row(work_description=long))
    assert detail.description == long
    assert detail.raw["permit_"] == "B200520605"
    assert not any(k.startswith(":") for k in detail.raw)  # Socrata computed-region noise dropped


def test_date_placeholder_becomes_null(cities: dict[str, CityConfig]) -> None:
    config = cities["chicago"].model_copy(update={"date_null_before": date(1901, 1, 1)})
    p = CityAdapter(config).to_permit(row(application_start_date="1900-01-01T00:00:00.000"))
    assert p.applied_date is None
    assert p.issued_date == date(2026, 11, 20)


def test_source_url_is_url_encoded(chicago: CityAdapter) -> None:
    assert chicago.to_permit(row(permit_="A B/1")).source_url.endswith("permit_=A%20B%2F1")  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("121 North LaSalle Street", "121 N LASALLE ST"),
        ("  1404 n. lorel   avenue ", "1404 N LOREL AVE"),
        ("123-125 W Madison", "123-125 W MADISON"),
    ],
)
def test_address_query_normalization(text: str, expected: str) -> None:
    assert normalize_address_query(text) == expected


def test_where_rejects_impossible_status(chicago: CityAdapter) -> None:
    with pytest.raises(NoPossibleMatch):
        chicago.where(Filters(statuses=("in_review",)))  # Chicago publishes issued permits only


def test_where_combines_filters(chicago: CityAdapter) -> None:
    where = chicago.where(
        Filters(statuses=("issued",), date_from=date(2025, 1, 1), min_valuation_usd=1e6, address="Lorel")
    )
    assert where is not None
    assert "issue_date >= '2025-01-01T00:00:00'" in where
    assert "reported_cost >= 1000000" in where
    assert "like '%LOREL%'" in where


def test_where_raw_status_uses_the_city_status_column(chicago: CityAdapter) -> None:
    assert chicago.where(Filters(raw_statuses=("suspended",))) == (
        "permit_status IS NOT NULL AND upper(permit_status) IN ('SUSPENDED')"
    )
    both = chicago.where(Filters(statuses=("issued",), raw_statuses=("SUSPENDED",)))
    assert both is not None and both.startswith("(") and " AND (permit_status IS NOT NULL AND upper(" in both
