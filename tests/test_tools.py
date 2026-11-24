"""End-to-end tool tests through the real MCP path (in-memory Client -> MCPServer), with
Socrata responses replayed from tests/fixtures. Re-record: PERMITS_RECORD=1 uv run pytest."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx
import pytest
from mcp import Client

from permits_mcp.city_config import CityConfig
from permits_mcp.server import build_server
from permits_mcp.socrata import SocrataClient

from .conftest import FROZEN_TODAY, ReplayTransport

pytestmark = pytest.mark.anyio

TOOLS = ["list_cities", "search_permits", "get_permit", "find_stale_permits", "permit_stats"]
CITIES = ["austin", "chicago", "seattle"]
JUNE_2025 = {"date_from": "2025-06-01", "date_to": "2025-06-30"}
MARCH_2025 = {"date_from": "2025-03-01", "date_to": "2025-03-31"}
STATUSES = ["applied", "in_review", "issued", "finaled", "expired", "cancelled", "unknown"]
TYPES = [
    "new_construction", "addition_alteration", "repair", "demolition", "electrical",
    "mechanical", "plumbing", "sign", "site_work", "other", "unknown",
]  # fmt: skip
# Per city, a one-month window on the date that covers every status it publishes. Seattle's
# applications have no issue date yet, so its window is on the application date.
WINDOWS = {
    "chicago": {"date_field": "issued", **JUNE_2025},
    "austin": {"date_field": "issued", **JUNE_2025},
    "seattle": {"date_field": "applied", **JUNE_2025},
}


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def call_error(client: Client, tool: str, **args: Any) -> str:
    result = await client.call_tool(tool, args)
    assert result.is_error
    return result.content[0].text  # type: ignore[union-attr]


# ---- tools/list ---------------------------------------------------------------------------


async def test_tool_list_is_fixed_documented_and_read_only(mcp_client: Client) -> None:
    tools = (await mcp_client.list_tools()).tools
    assert [t.name for t in tools] == TOOLS
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.description and "Use when" in tool.description
        assert tool.output_schema is not None
        if tool.name != "list_cities":
            assert "Don't use" in tool.description
            assert tool.input_schema["properties"]["city"]["enum"] == CITIES
    params = {t.name: t.input_schema["properties"] for t in tools}
    assert "status_raw" in params["search_permits"] and "status_raw" in params["permit_stats"]
    assert "status_raw" in params["permit_stats"]["group_by"]["enum"]


# ---- list_cities --------------------------------------------------------------------------


async def test_list_cities_reports_gaps_and_freshness(mcp_client: Client) -> None:
    out = await call(mcp_client, "list_cities")
    assert out["warnings"] == []
    by_id = {c["id"]: c for c in out["cities"]}
    assert list(by_id) == CITIES
    for city in by_id.values():
        assert city["data_last_updated"] is not None
        assert city["issued_date_range"]["latest"] >= "2026-11-01"
        assert city["known_gaps"]

    chicago, seattle, austin = by_id["chicago"], by_id["seattle"], by_id["austin"]
    assert chicago["issued_date_range"]["earliest"] == "2006-01-03"
    assert "in_review" not in chicago["statuses_available"]
    assert chicago["fields_unavailable"] == ["last_activity_date"]
    assert any("Only issued permits" in gap for gap in chicago["known_gaps"])
    # Seattle publishes the full lifecycle; its 1900-01-01 placeholders don't leak into the range.
    assert {"applied", "in_review"} <= set(seattle["statuses_available"])
    assert seattle["issued_date_range"]["earliest"] > "1901-01-01"
    # Austin is the only city with a real last-activity date, and the only one with trade types.
    assert austin["fields_unavailable"] == []
    assert {"electrical", "mechanical", "plumbing"} <= set(austin["permit_types_available"])
    assert "sign" not in austin["permit_types_available"]


# ---- search_permits -----------------------------------------------------------------------


async def test_search_by_address(mcp_client: Client) -> None:
    out = await call(mcp_client, "search_permits", city="chicago", address="121 North La Salle Street", limit=5)
    assert out["total_count"] > 5 and out["returned"] == 5
    assert all("121 N LA SALLE" in p["address"] for p in out["permits"])
    issued = [p["issued_date"] for p in out["permits"]]
    assert issued == sorted(issued, reverse=True)
    assert out["next_cursor"] and "Showing 1-5 of" in out["hint"]


async def test_address_miss_suggests_street_name_only(mcp_client: Client) -> None:
    out = await call(mcp_client, "search_permits", city="chicago", address="121 N LaSalle St")
    assert out["total_count"] == 0
    assert "only the street name" in out["hint"]


async def test_pagination_with_cursor(mcp_client: Client) -> None:
    args = {"city": "chicago", "permit_type": ["new_construction"], "min_valuation_usd": 1_000_000, **JUNE_2025}
    page1 = await call(mcp_client, "search_permits", limit=10, **args)
    assert page1["total_count"] > 10
    page2 = await call(mcp_client, "search_permits", limit=10, cursor=page1["next_cursor"], city="chicago")
    ids1 = {p["permit_id"] for p in page1["permits"]}
    ids2 = {p["permit_id"] for p in page2["permits"]}
    assert ids2 and not ids1 & ids2
    assert all(p["permit_type"] == "new_construction" and p["valuation_usd"] >= 1e6 for p in page2["permits"])
    assert page2["total_count"] == page1["total_count"]


async def test_cursor_with_changed_filters_is_rejected(mcp_client: Client) -> None:
    args = {"city": "chicago", "permit_type": ["new_construction"], "min_valuation_usd": 1_000_000, **JUNE_2025}
    page1 = await call(mcp_client, "search_permits", limit=10, **args)
    message = await call_error(
        mcp_client, "search_permits", city="chicago", status=["finaled"], cursor=page1["next_cursor"]
    )
    assert "different query" in message


@pytest.mark.parametrize("city", CITIES)
@pytest.mark.parametrize("status", STATUSES)
async def test_status_filter_agrees_with_stats(mcp_client: Client, city: str, status: str) -> None:
    """Two independent paths must agree on live data: filtering by a normalized status (rules
    compiled to SoQL) and grouping by raw values then classifying them in Python."""
    window = WINDOWS[city]
    stats = await call(mcp_client, "permit_stats", city=city, group_by="status", **window)
    expected = next((g["count"] for g in stats["groups"] if g["key"] == status), 0)
    out = await call(mcp_client, "search_permits", city=city, status=[status], limit=10, **window)
    assert out["total_count"] == expected
    assert all(p["status"] == status for p in out["permits"])


@pytest.mark.parametrize("city", CITIES)
@pytest.mark.parametrize("permit_type", TYPES)
async def test_type_filter_agrees_with_stats(mcp_client: Client, city: str, permit_type: str) -> None:
    window = WINDOWS[city]
    stats = await call(mcp_client, "permit_stats", city=city, group_by="permit_type", **window)
    expected = next((g["count"] for g in stats["groups"] if g["key"] == permit_type), 0)
    out = await call(mcp_client, "search_permits", city=city, permit_type=[permit_type], limit=5, **window)
    assert out["total_count"] == expected
    assert all(p["permit_type"] == permit_type for p in out["permits"])


@pytest.mark.parametrize("city", CITIES)
async def test_no_unmapped_values_in_recorded_month(
    mcp_client: Client, city: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Every raw status/type value seen in the recorded month is covered by a rule. Explicit
    'unknown' rules are fine (they're documented in known_gaps); silent fall-through is not."""
    with caplog.at_level(logging.WARNING, logger="permits_mcp"):
        for group_by in ("status", "permit_type"):
            out = await call(mcp_client, "permit_stats", city=city, group_by=group_by, **WINDOWS[city])
            assert out["total_count"] > 0
    unmapped = [r.__dict__.get("raw_value") for r in caplog.records if r.getMessage().startswith("unmapped")]
    assert unmapped == []


# ---- status_raw: a city's own statuses inside the normalized ones ---------------------------

# Per city, one of its own statuses that a normalized status folds together with others, the
# normalized status it maps to, and a window (recorded separately from WINDOWS, so these agree).
RAW_CASES: dict[str, tuple[str, str, dict[str, str]]] = {
    "chicago": ("SUSPENDED", "issued", {"date_field": "issued", "date_from": "2024-01-01", "date_to": "2024-12-31"}),
    "seattle": ("Corrections Required", "in_review", {"date_field": "applied", **MARCH_2025}),
    "austin": ("VOID", "cancelled", {"date_field": "issued", **MARCH_2025}),
}


async def test_list_cities_shows_raw_statuses_inside_each_status(mcp_client: Client) -> None:
    by_id = {c["id"]: c for c in (await call(mcp_client, "list_cities"))["cities"]}
    assert by_id["chicago"]["status_raw_values"]["issued"] == ["ACTIVE", "PHASED PERMITTING", "SUSPENDED"]
    assert "Suspended" in by_id["austin"]["status_raw_values"]["issued"]
    assert "Corrections Required" in by_id["seattle"]["status_raw_values"]["in_review"]
    for city in by_id.values():
        assert set(city["status_raw_values"]) <= set(city["statuses_available"])


@pytest.mark.parametrize("city", CITIES)
async def test_status_raw_groups_and_filter_agree(mcp_client: Client, city: str) -> None:
    """Grouping by a city's own status (classified in Python) and filtering on it (compiled to SoQL,
    ignoring case) must agree, and the raw groups must add up to the normalized breakdown."""
    value, status, window = RAW_CASES[city]
    raw = await call(mcp_client, "permit_stats", city=city, group_by="status_raw", **window)
    normalized = await call(mcp_client, "permit_stats", city=city, group_by="status", **window)
    assert raw["total_count"] == normalized["total_count"] > 0
    assert all(g["normalized_status"] is None for g in normalized["groups"])
    for g in raw["groups"]:
        assert g["normalized_status"], g
        if g["key"] == "(blank)":
            assert g["normalized_status"] == ["unknown"]
    group = next(g for g in raw["groups"] if g["key"] == value)
    assert status in group["normalized_status"] and group["count"] > 0

    filtered = await call(
        mcp_client, "permit_stats", city=city, group_by="status", status_raw=[value.lower()], **window
    )
    assert filtered["warnings"] == []
    assert filtered["total_count"] == group["count"]
    assert {g["key"] for g in filtered["groups"]} <= set(group["normalized_status"])

    found = await call(mcp_client, "search_permits", city=city, status_raw=[value], limit=5, **window)
    assert found["total_count"] == group["count"]
    assert found["permits"] and all(p["status_raw"].split(" / ")[0] == value for p in found["permits"])


async def test_status_raw_survives_pagination(mcp_client: Client) -> None:
    value, _, window = RAW_CASES["chicago"]
    page1 = await call(mcp_client, "search_permits", city="chicago", status_raw=[value], limit=5, **window)
    page2 = await call(mcp_client, "search_permits", city="chicago", cursor=page1["next_cursor"], limit=5)
    assert page2["total_count"] == page1["total_count"] > 5
    assert page2["permits"] and all(p["status_raw"].startswith(value) for p in page2["permits"])
    assert not {p["permit_id"] for p in page1["permits"]} & {p["permit_id"] for p in page2["permits"]}


async def test_status_raw_mistakes_are_explained(mcp_client: Client) -> None:
    window = RAW_CASES["chicago"][2]
    whole = await call(
        mcp_client, "permit_stats", city="chicago", group_by="status", status_raw=["SUSPENDED / INSPECTIONS"], **window
    )
    assert whole["total_count"] == 0
    assert "use 'SUSPENDED'" in whole["warnings"][0]
    typo = await call(mcp_client, "search_permits", city="chicago", status_raw=["SUSPENDID"], limit=1, **window)
    assert typo["total_count"] == 0
    assert "isn't a status Chicago, IL is known to publish" in typo["warnings"][0]
    assert "SUSPENDED" in typo["warnings"][0]


async def test_blank_status_raw_is_rejected(mcp_client: Client) -> None:
    message = await call_error(mcp_client, "search_permits", city="chicago", status_raw=["  "])
    assert "status_raw" in message and "at least 1 character" in message


async def test_seattle_application_lifecycle(mcp_client: Client) -> None:
    out = await call(
        mcp_client, "search_permits", city="seattle", status=["in_review"], date_field="applied", limit=5,
        date_from="2026-01-01", date_to="2026-10-31",
    )  # fmt: skip
    assert out["total_count"] > 0
    for p in out["permits"]:
        assert p["status"] == "in_review" and p["issued_date"] is None and p["applied_date"] >= "2026-01-01"
        assert p["source_url"].startswith("https://services.seattle.gov/")


async def test_seattle_address_search(mcp_client: Client) -> None:
    out = await call(mcp_client, "search_permits", city="seattle", address="1420 5th Avenue", limit=5)
    assert out["total_count"] > 0
    assert all("1420 5TH AVE" in p["address"] for p in out["permits"])


async def test_austin_trade_permits_and_last_activity(mcp_client: Client) -> None:
    out = await call(mcp_client, "search_permits", city="austin", permit_type=["electrical"], limit=5, **JUNE_2025)
    assert out["total_count"] > 0
    for p in out["permits"]:
        assert p["permit_type"] == "electrical" and p["permit_type_raw"].startswith("EP")
        assert p["last_activity_date"] is not None


async def test_unpublished_status_is_a_gap_not_a_zero(mcp_client: Client, replay: ReplayTransport) -> None:
    out = await call(mcp_client, "search_permits", city="chicago", status=["in_review"])
    assert out["total_count"] == 0
    assert len(out["warnings"]) == 1 and "never publishes status 'in_review'" in out["warnings"][0]
    assert replay.requests == []  # answered from config, no wasted request


async def test_applied_date_field(mcp_client: Client) -> None:
    out = await call(
        mcp_client, "search_permits", city="chicago", date_field="applied", limit=5,
        date_from="2026-03-01", date_to="2026-03-31",
    )  # fmt: skip
    assert out["returned"] == 5
    assert all("2026-03-01" <= p["applied_date"] <= "2026-03-31" for p in out["permits"])


# ---- get_permit ---------------------------------------------------------------------------


async def test_get_permit_case_insensitive_with_raw_fields(mcp_client: Client) -> None:
    out = await call(mcp_client, "get_permit", city="chicago", permit_id=" b200520605 ")
    assert out["permit_id"] == "B200520605"
    assert out["raw"]["permit_"] == "B200520605"
    assert out["raw"]["permit_milestone"] == "INSPECTIONS"
    assert not out["description"].endswith("…")


@pytest.mark.parametrize(
    ("city", "permit_id", "raw_key"),
    [("seattle", "7148658-cn", "permitnum"), ("austin", "2026-142501 bp", "permit_number")],
)
async def test_get_permit_other_cities(mcp_client: Client, city: str, permit_id: str, raw_key: str) -> None:
    out = await call(mcp_client, "get_permit", city=city, permit_id=permit_id)
    assert out["permit_id"] == permit_id.upper() == out["raw"][raw_key]
    assert out["source_url"].startswith("https://")


async def test_get_permit_duplicate_id_returns_latest_and_warns(cities: dict[str, CityConfig]) -> None:
    # No city republishes an ID today (checked daily by tests/live); this pins the behavior if one does.
    base = {"permit_": "B100", "permit_type": "PERMIT - SIGNS", "street_number": "1", "street_name": "MAIN ST"}
    rows = [
        {**base, "permit_status": "REVOKED", "issue_date": "2019-03-01T00:00:00.000"},
        {**base, "permit_status": "ACTIVE", "issue_date": "2024-05-02T00:00:00.000"},
        {**base, "permit_status": "EXPIRED"},
    ]
    socrata = SocrataClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rows)))
    async with Client(build_server(cities, socrata)) as client:
        out = await call(client, "get_permit", city="chicago", permit_id="B100")
    await socrata.aclose()
    assert out["issued_date"] == "2024-05-02" and out["status"] == "issued"
    [warning] = out["warnings"]
    assert "3 records" in warning and "issued 2019-03-01" in warning and "status EXPIRED" in warning


async def test_get_permit_not_found_explains_format(mcp_client: Client) -> None:
    message = await call_error(mcp_client, "get_permit", city="chicago", permit_id="NOPE-123")
    assert "No permit 'NOPE-123'" in message and "B200520605" in message and "search_permits" in message


# ---- find_stale_permits -------------------------------------------------------------------


async def test_find_stale_permits(mcp_client: Client) -> None:
    out = await call(
        mcp_client, "find_stale_permits", city="chicago", open_longer_than_days=730, min_valuation_usd=1_000_000
    )
    assert out["as_of"] == FROZEN_TODAY.isoformat()
    assert out["total_count"] > out["returned"] == 25
    assert out["excluded_unknown_status_count"] > 0
    assert any("unknown status" in w for w in out["warnings"])
    since = [p["open_since"] for p in out["permits"]]
    assert since == sorted(since)  # oldest first
    for p in out["permits"]:
        assert p["status"] in ("applied", "in_review", "issued")
        assert p["days_open"] > 730
        assert p["valuation_usd"] >= 1_000_000
        assert (date.fromisoformat(out["as_of"]) - date.fromisoformat(p["open_since"])).days == p["days_open"]


@pytest.mark.parametrize("city", ["seattle", "austin"])
async def test_find_stale_permits_other_cities(mcp_client: Client, city: str) -> None:
    out = await call(mcp_client, "find_stale_permits", city=city, open_longer_than_days=365, limit=10)
    assert out["total_count"] > 0
    for p in out["permits"]:
        assert p["status"] in ("applied", "in_review", "issued")
        assert p["days_open"] > 365
        assert p["open_since"] > "1901-01-01"  # Seattle's 1900 placeholders never count as an open date


async def test_seattle_stale_includes_applications_in_review(mcp_client: Client) -> None:
    out = await call(mcp_client, "find_stale_permits", city="seattle", open_longer_than_days=365, limit=50)
    assert {p["status"] for p in out["permits"]} & {"applied", "in_review"}


# ---- permit_stats -------------------------------------------------------------------------


async def test_stats_by_month_fills_empty_months(mcp_client: Client) -> None:
    out = await call(
        mcp_client, "permit_stats", city="chicago", group_by="month", permit_type=["new_construction"],
        date_from="2026-01-01", date_to="2026-04-30",
    )  # fmt: skip
    assert [g["key"] for g in out["groups"]] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert out["total_count"] == sum(g["count"] for g in out["groups"])


async def test_stats_by_type(mcp_client: Client) -> None:
    out = await call(mcp_client, "permit_stats", city="chicago", group_by="permit_type", **JUNE_2025)
    keys = [g["key"] for g in out["groups"]]
    counts = [g["count"] for g in out["groups"]]
    assert "repair" in keys and counts == sorted(counts, reverse=True)
    assert "electrical" not in keys  # Chicago files trade work under express permits


# ---- errors the agent can recover from ----------------------------------------------------


async def test_unknown_city_lists_valid_ones(mcp_client: Client) -> None:
    message = await call_error(mcp_client, "search_permits", city="nyc")
    assert "Unknown city 'nyc'" in message and "chicago" in message and "list_cities" in message


async def test_bad_status_lists_valid_values(mcp_client: Client) -> None:
    message = await call_error(mcp_client, "search_permits", city="chicago", status=["open"])
    assert "'applied', 'in_review', 'issued'" in message


async def test_reversed_dates(mcp_client: Client) -> None:
    message = await call_error(
        mcp_client, "permit_stats", city="chicago", group_by="year", date_from="2026-01-01", date_to="2025-01-01"
    )
    assert "Swap them" in message


async def test_portal_outage_says_retry_later(cities: dict[str, CityConfig]) -> None:
    def down(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def no_sleep(_: float) -> None:
        return None

    socrata = SocrataClient(transport=httpx.MockTransport(down), sleep=no_sleep)
    async with Client(build_server(cities, socrata)) as client:
        message = await call_error(client, "get_permit", city="chicago", permit_id="B200520605")
    assert "not responding" in message and "Retry in a minute" in message


async def test_non_finite_valuation_is_rejected(mcp_client: Client) -> None:
    message = await call_error(mcp_client, "search_permits", city="chicago", min_valuation_usd="inf")
    assert "min_valuation_usd" in message
