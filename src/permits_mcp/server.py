"""MCP registration: tool names, agent-facing descriptions, parameter schemas, annotations.

Deliberately no ``from __future__ import annotations``: the SDK evaluates parameter
annotations, and ``CityId`` is built at runtime from the loaded city configs so the valid
city ids appear as an enum in every tool's input schema.
"""

from collections.abc import Callable, Mapping
from datetime import date
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic.config import JsonValue
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .city_config import CityConfig
from .schema import (
    CitiesResult,
    DateField,
    GroupBy,
    PermitDetail,
    PermitType,
    SearchResult,
    StaleResult,
    StatsResult,
    Status,
)
from .service import PermitService
from .socrata import SocrataClient

INSTRUCTIONS = """\
US building permits from city open-data portals, normalized to one schema across cities.
Start with list_cities to see which cities, statuses and fields exist (a city that doesn't publish
something returns null or a warning, not a real zero). Use permit_stats for any count or total,
search_permits to list permits, find_stale_permits for permits open too long, get_permit for one permit."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

DateFrom = Annotated[date | None, Field(description="Inclusive start date, YYYY-MM-DD.")]
DateTo = Annotated[date | None, Field(description="Inclusive end date, YYYY-MM-DD.")]
StatusList = Annotated[
    list[Status] | None,
    Field(
        min_length=1, description="Keep only these normalized statuses. 'Open' = ['applied', 'in_review', 'issued']."
    ),
]
TypeList = Annotated[
    list[PermitType] | None, Field(min_length=1, description="Keep only these normalized permit types.")
]
DateFieldParam = Annotated[
    DateField,
    Field(description="Which date the date range filters (and results sort by): 'issued' (default) or 'applied'."),
]
MinValuation = Annotated[
    float | None,
    Field(
        ge=0,
        allow_inf_nan=False,
        description="Keep permits whose reported valuation is at least this many US dollars. "
        "Permits with no published valuation are excluded.",
    ),
]
Limit = Annotated[int, Field(ge=1, le=50, description="Records per page (max 50).")]
Cursor = Annotated[
    str | None, Field(description="next_cursor from a previous call. Pass it back with the same other arguments.")
]


def build_server(
    cities: Mapping[str, CityConfig],
    client: SocrataClient,
    *,
    today: Callable[[CityConfig], date] | None = None,
) -> MCPServer:
    service = PermitService(cities, client, **({"today": today} if today else {}))
    city_ids = list(service.adapters)
    CityId = Annotated[  # noqa: N806 - used as a type
        str,
        Field(
            description=f"City id: one of {', '.join(city_ids)}. Call list_cities for coverage.",
            json_schema_extra={"enum": list[JsonValue](city_ids)},
        ),
    ]

    mcp = MCPServer("permits", version=__version__, instructions=INSTRUCTIONS)

    # Registered in a fixed order so tools/list is deterministic (spec 2026-07-28).

    @mcp.tool(title="List cities", annotations=READ_ONLY)
    async def list_cities() -> CitiesResult:
        """List the cities this server covers, with data freshness, which statuses, permit types and fields
        each city actually publishes, and its known data gaps.

        Use when: starting a task, picking a city id, or before trusting a zero or null result (the city may
        simply not publish that data).
        Don't use: to get permit data. Use search_permits, get_permit, find_stale_permits or permit_stats."""
        return await service.list_cities()

    @mcp.tool(title="Search permits", annotations=READ_ONLY)
    async def search_permits(
        city: CityId,  # pyright: ignore[reportInvalidTypeForm]
        address: Annotated[
            str | None,
            Field(
                description="Street address or part of one, e.g. '1404 N Lorel Ave' or 'LOREL'. Case-insensitive "
                "substring match; words like North/Street are abbreviated automatically."
            ),
        ] = None,
        status: StatusList = None,
        permit_type: TypeList = None,
        date_field: DateFieldParam = "issued",
        date_from: DateFrom = None,
        date_to: DateTo = None,
        min_valuation_usd: MinValuation = None,
        limit: Limit = 25,
        cursor: Cursor = None,
    ) -> SearchResult:
        """Find individual building permits in one city, newest first. Returns up to `limit` normalized
        records plus total_count of all matches.

        Use when: you need specific permits or a short list (e.g. permits at an address, recent new
        construction over $1M).
        Don't use when: you need a count, total or breakdown (use permit_stats: exact, one call); you want
        permits still open after N days (use find_stale_permits); you already have a permit_id (use get_permit).

        Notes: date_from/date_to filter the issue date unless date_field='applied'. Descriptions are cut to
        200 characters; get_permit has the full text and raw source fields. If total_count > returned,
        narrow the filters or pass next_cursor back with the same arguments."""
        return await service.search_permits(
            city, address, status, permit_type, date_field, date_from, date_to, min_valuation_usd, limit, cursor
        )

    @mcp.tool(title="Get permit", annotations=READ_ONLY)
    async def get_permit(
        city: CityId,  # pyright: ignore[reportInvalidTypeForm]
        permit_id: Annotated[str, Field(min_length=1, description="The permit's id, as in search results.")],
    ) -> PermitDetail:
        """Get one permit's full normalized record plus every raw field the city publishes for it.

        Use when: you have a permit_id (from a search result or the user) and need full details, or want
        to check a normalized value against the source.
        Don't use: to discover permits. Use search_permits."""
        return await service.get_permit(city, permit_id)

    @mcp.tool(title="Find stale permits", annotations=READ_ONLY)
    async def find_stale_permits(
        city: CityId,  # pyright: ignore[reportInvalidTypeForm]
        open_longer_than_days: Annotated[
            int, Field(ge=1, le=36500, description="Minimum days open, e.g. 730 for 'more than 2 years'.")
        ],
        permit_type: TypeList = None,
        min_valuation_usd: MinValuation = None,
        limit: Limit = 25,
        cursor: Cursor = None,
    ) -> StaleResult:
        """Find permits still open (status applied, in_review or issued) for longer than a threshold,
        oldest first, with total_count of all matches.

        Age counts from the application date, or from the issue date when there is none; each record
        gives open_since, open_since_basis and days_open.
        Use when: the question is about permits open too long, lingering, stalled or never closed out.
        Don't use when: you want closed, expired or cancelled permits (use search_permits with status), or
        only counts by status (use permit_stats).
        Always check excluded_unknown_status_count: permits whose status is unknown can't be classified as
        open and are excluded, so mention them in your answer."""
        return await service.find_stale_permits(
            city, open_longer_than_days, permit_type, min_valuation_usd, limit, cursor
        )

    @mcp.tool(title="Permit statistics", annotations=READ_ONLY)
    async def permit_stats(
        city: CityId,  # pyright: ignore[reportInvalidTypeForm]
        group_by: Annotated[GroupBy, Field(description="'status', 'permit_type', 'month' or 'year'.")],
        date_from: Annotated[date, Field(description="Inclusive start date, YYYY-MM-DD.")],
        date_to: Annotated[date, Field(description="Inclusive end date, YYYY-MM-DD.")],
        date_field: DateFieldParam = "issued",
        status: StatusList = None,
        permit_type: TypeList = None,
    ) -> StatsResult:
        """Count permits and sum their valuations in one city over a date range, grouped by status,
        permit_type, month or year. Aggregated by the city's portal, so counts are exact and cover every
        matching permit, not a sample.

        Use when: the question asks how many, how much, which is most common, or a trend over time.
        Don't use: to list permits. Use search_permits.
        The date range filters the issue date unless date_field='applied'. Optional status and
        permit_type filters narrow what is counted."""
        return await service.permit_stats(city, group_by, date_from, date_to, date_field, status, permit_type)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__, "cities": city_ids})

    return mcp
