"""The five tools' behavior, independent of MCP registration (see server.py for that).

Every anticipated failure is a ``ToolError`` whose message tells the agent how to recover;
the SDK delivers it as an ``is_error`` result the model can read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import cursor as cursors
from . import soql
from .adapter import CityAdapter, Filters, NoPossibleMatch
from .city_config import CityConfig
from .schema import (
    OPEN_STATUSES,
    PERMIT_TYPES,
    STATUSES,
    CitiesResult,
    CityInfo,
    DateField,
    DateRange,
    GroupBy,
    PermitDetail,
    PermitType,
    SearchResult,
    StalePermit,
    StaleResult,
    StatGroup,
    StatsResult,
    Status,
)
from .socrata import SocrataClient, SocrataError, SocrataUnavailable

GROUP_LIMIT = "50000"


def _today_in(config: CityConfig) -> date:
    return datetime.now(config.tz).date()


class PermitService:
    def __init__(
        self,
        cities: Mapping[str, CityConfig],
        client: SocrataClient,
        today: Callable[[CityConfig], date] = _today_in,
    ) -> None:
        self.adapters = {cid: CityAdapter(cfg) for cid, cfg in sorted(cities.items())}
        self.client = client
        self.today = today

    # ---- helpers ----------------------------------------------------------------------

    def _adapter(self, city: str) -> CityAdapter:
        adapter = self.adapters.get(city.strip().lower())
        if adapter is None:
            valid = ", ".join(self.adapters)
            raise ToolError(
                f"Unknown city '{city}'. Supported cities: {valid}. Call list_cities for coverage and gaps."
            )
        return adapter

    async def _query(self, adapter: CityAdapter, params: dict[str, str]) -> list[dict[str, Any]]:
        cfg = adapter.config
        try:
            return await self.client.query(cfg.domain, cfg.dataset_id, params)
        except SocrataUnavailable as exc:
            raise ToolError(
                f"{cfg.name}'s data portal is not responding ({exc}). Retry in a minute; other cities are unaffected."
            ) from exc
        except SocrataError as exc:
            raise ToolError(
                f"The query for {cfg.name} was rejected by the portal ({exc}). This is a server bug, not a problem "
                "with your arguments; try fewer filters."
            ) from exc

    @staticmethod
    def _check_range(date_from: date | None, date_to: date | None) -> None:
        if date_from and date_to and date_from > date_to:
            raise ToolError(f"date_from ({date_from}) is after date_to ({date_to}). Swap them.")

    @staticmethod
    def _date_column(adapter: CityAdapter, field: DateField) -> str:
        column = adapter.date_column(field)
        if column is None:
            other = "applied" if field == "issued" else "issued"
            raise ToolError(f"{adapter.config.name} doesn't publish {field} dates. Use date_field='{other}'.")
        return column

    @staticmethod
    def _gap_warnings(
        adapter: CityAdapter, statuses: Sequence[Status] | None, types: Sequence[PermitType] | None
    ) -> list[str]:
        cfg = adapter.config
        warnings: list[str] = []
        published_statuses = {r.then for r in cfg.status.rules} | {"unknown"}
        for s in statuses or ():
            if s not in published_statuses:
                warnings.append(
                    f"{cfg.name} never publishes status '{s}', so no permit can match it "
                    "(see list_cities known_gaps). This is a data gap, not a true zero."
                )
        published_types = {r.then for r in cfg.permit_type.rules} | {"unknown"}
        for t in types or ():
            if t not in published_types:
                warnings.append(
                    f"{cfg.name} never classifies permits as '{t}', so no permit can match it "
                    "(see list_cities known_gaps). This is a data gap, not a true zero."
                )
        return warnings

    @staticmethod
    def _no_match_warning(adapter: CityAdapter, what: str) -> str | None:
        if what in ("status", "permit_type"):
            return None  # _gap_warnings already named each value the city never publishes
        return f"{adapter.config.name} doesn't publish {what}, so this filter can't match any permit."

    # ---- list_cities ------------------------------------------------------------------

    async def list_cities(self) -> CitiesResult:
        results = await asyncio.gather(*(self._city_info(a) for a in self.adapters.values()))
        return CitiesResult(cities=[info for info, _ in results], warnings=[w for _, ws in results for w in ws])

    async def _city_info(self, adapter: CityAdapter) -> tuple[CityInfo, list[str]]:
        cfg = adapter.config
        warnings: list[str] = []
        issued = cfg.fields.issued_date
        range_params = {}
        if issued:
            range_params = {
                "$select": f"min({issued}) as earliest, max({issued}) as latest",
                "$where": soql.date_present(issued, cfg.date_null_before),
            }
        meta, rng = await asyncio.gather(
            self.client.view_metadata(cfg.domain, cfg.dataset_id),
            self.client.query(cfg.domain, cfg.dataset_id, range_params) if issued else _none(),
            return_exceptions=True,
        )
        updated: datetime | None = None
        if isinstance(meta, BaseException):
            warnings.append(f"Couldn't fetch {cfg.name} dataset metadata: {meta}")
        elif isinstance(meta, dict) and meta.get("rowsUpdatedAt"):
            updated = datetime.fromtimestamp(int(meta["rowsUpdatedAt"]), UTC)
        date_range: DateRange | None = None
        if isinstance(rng, BaseException):
            warnings.append(f"Couldn't fetch {cfg.name} date range: {rng}")
        elif rng:
            row = rng[0]
            date_range = DateRange(
                earliest=date.fromisoformat(row["earliest"][:10]) if row.get("earliest") else None,
                latest=date.fromisoformat(row["latest"][:10]) if row.get("latest") else None,
            )
        status_targets = {r.then for r in cfg.status.rules} | {"unknown"}
        type_targets = {r.then for r in cfg.permit_type.rules} | {"unknown"}
        info = CityInfo(
            id=cfg.id,
            name=cfg.name,
            portal_url=cfg.portal_url,
            dataset_id=cfg.dataset_id,
            data_last_updated=updated,
            issued_date_range=date_range,
            statuses_available=[s for s in STATUSES if s in status_targets],
            permit_types_available=[t for t in PERMIT_TYPES if t in type_targets],
            fields_unavailable=cfg.unavailable_fields(),
            known_gaps=list(cfg.known_gaps),
        )
        return info, warnings

    # ---- search_permits ---------------------------------------------------------------

    async def search_permits(
        self,
        city: str,
        address: str | None = None,
        status: Sequence[Status] | None = None,
        permit_type: Sequence[PermitType] | None = None,
        date_field: DateField = "issued",
        date_from: date | None = None,
        date_to: date | None = None,
        min_valuation_usd: float | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> SearchResult:
        adapter = self._adapter(city)
        cfg = adapter.config
        given = _canonical(
            address=address.strip() if address and address.strip() else None,
            status=status,
            permit_type=permit_type,
            date_field=date_field,
            date_from=date_from,
            date_to=date_to,
            min_valuation_usd=min_valuation_usd,
        )
        stored, offset = self._resolve_cursor(cursor, "search_permits", cfg.id, given, _canonical())
        filters = _filters(stored)
        self._check_range(filters.date_from, filters.date_to)
        date_col = self._date_column(adapter, filters.date_field)
        warnings = self._gap_warnings(adapter, filters.statuses, filters.permit_types)
        try:
            where = adapter.where(filters)
        except NoPossibleMatch as exc:
            warnings.extend(w for w in [self._no_match_warning(adapter, str(exc))] if w)
            return SearchResult(city=cfg.id, permits=[], total_count=0, returned=0, next_cursor=None, warnings=warnings)

        id_col = cfg.fields.permit_id
        rows_params = {
            "$select": ", ".join(adapter.list_columns()),
            "$order": f"{date_col} DESC, {id_col} ASC",
            "$limit": str(limit),
            "$offset": str(offset),
        }
        count_params = {"$select": "count(*) as n"}
        if where:
            rows_params["$where"] = count_params["$where"] = where
        rows, count_rows = await asyncio.gather(self._query(adapter, rows_params), self._query(adapter, count_params))
        total = int(count_rows[0]["n"]) if count_rows else 0
        permits = [adapter.to_permit(r) for r in rows]
        end = offset + len(permits)
        next_cursor = cursors.encode("search_permits", cfg.id, stored, end) if end < total else None

        hint = None
        if next_cursor:
            hint = _narrow_hint(
                offset, end, total, filters, ("date_from", "status", "permit_type", "address", "min_valuation_usd")
            )
        elif total == 0 and filters.address:
            needle = filters.address
            hint = (
                f"No permits matched address '{needle}'. Addresses are matched as substrings of strings like "
                f"'1404 N LOREL AVE'. Retry with only the street name (no number, direction or suffix); "
                "some names are split differently (e.g. 'LA SALLE' vs 'LASALLE')."
            )
        return SearchResult(
            city=cfg.id,
            permits=permits,
            total_count=total,
            returned=len(permits),
            next_cursor=next_cursor,
            hint=hint,
            warnings=warnings,
        )

    @staticmethod
    def _resolve_cursor(
        cursor: str | None, tool: str, city: str, given: dict[str, Any], defaults: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        try:
            return cursors.resolve(cursor, tool, city, given, defaults)
        except cursors.CursorError as exc:
            raise ToolError(str(exc)) from exc

    # ---- get_permit -------------------------------------------------------------------

    async def get_permit(self, city: str, permit_id: str) -> PermitDetail:
        adapter = self._adapter(city)
        cfg = adapter.config
        pid = permit_id.strip()
        if not pid:
            raise ToolError(f"permit_id is empty. {cfg.name} permit IDs look like '{cfg.permit_id_example}'.")
        candidates = list(dict.fromkeys([pid, pid.upper()]))
        id_col = cfg.fields.permit_id
        where = f"{id_col} IN ({', '.join(soql.quote(c) for c in candidates)})"
        rows = await self._query(adapter, {"$where": where, "$limit": "2"})
        if not rows:
            raise ToolError(
                f"No permit '{pid}' in {cfg.name}. {cfg.name} permit IDs look like '{cfg.permit_id_example}'. "
                "To find a permit by location, call search_permits with address=..."
            )
        return adapter.to_detail(rows[0])

    # ---- find_stale_permits -----------------------------------------------------------

    async def find_stale_permits(
        self,
        city: str,
        open_longer_than_days: int,
        permit_type: Sequence[PermitType] | None = None,
        min_valuation_usd: float | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> StaleResult:
        adapter = self._adapter(city)
        cfg = adapter.config
        given = _canonical(
            open_longer_than_days=open_longer_than_days, permit_type=permit_type, min_valuation_usd=min_valuation_usd
        )
        defaults = _canonical(open_longer_than_days=open_longer_than_days)
        stored, offset = self._resolve_cursor(cursor, "find_stale_permits", cfg.id, given, defaults)
        days = int(stored["open_longer_than_days"])
        filters = _filters(stored)
        today = self.today(cfg)
        cutoff = today - timedelta(days=days)

        app, iss = cfg.fields.applied_date, cfg.fields.issued_date
        if app is None and iss is None:
            raise ToolError(
                f"{cfg.name} publishes neither application nor issue dates, so permit age can't be computed."
            )
        nb = cfg.date_null_before
        age_parts: list[str] = []
        if app:
            age_parts.append(f"{soql.date_present(app, nb)} AND {app} < {soql.timestamp(cutoff)}")
        if iss:
            issued_old = f"{soql.date_present(iss, nb)} AND {iss} < {soql.timestamp(cutoff)}"
            age_parts.append(f"NOT ({soql.date_present(app, nb)}) AND {issued_old}" if app else issued_old)
        age = soql.or_(age_parts)
        open_since = f"case({soql.date_present(app, nb)}, {app}, true, {iss})" if app and iss else (app or iss)

        warnings = self._gap_warnings(adapter, None, filters.permit_types)

        def empty(reason: str | None) -> StaleResult:
            return StaleResult(
                city=cfg.id, as_of=today, open_longer_than_days=days, excluded_unknown_status_count=0,
                permits=[], total_count=0, returned=0, next_cursor=None,
                warnings=[*warnings, *([reason] if reason else [])],
            )  # fmt: skip

        open_pred = adapter.status_predicate(OPEN_STATUSES)
        unknown_pred = adapter.status_predicate(["unknown"])
        try:
            extra = adapter.where(
                Filters(permit_types=filters.permit_types, min_valuation_usd=filters.min_valuation_usd)
            )
        except NoPossibleMatch as exc:
            return empty(self._no_match_warning(adapter, str(exc)))
        if open_pred is None:
            return empty(f"{cfg.name} publishes no open statuses (applied, in_review, issued).")

        where_open = soql.and_(open_pred, age, extra)
        rows_params = {
            "$select": ", ".join(adapter.list_columns()),
            "$where": where_open or "",
            "$order": f"{open_since} ASC, {cfg.fields.permit_id} ASC",
            "$limit": str(limit),
            "$offset": str(offset),
        }
        count_params = {"$select": "count(*) as n", "$where": where_open or ""}
        unknown_params = {"$select": "count(*) as n", "$where": soql.and_(unknown_pred, age, extra) or ""}
        rows, count_rows, unknown_rows = await asyncio.gather(
            self._query(adapter, rows_params),
            self._query(adapter, count_params),
            self._query(adapter, unknown_params) if unknown_pred else _empty_count(),
        )
        total = int(count_rows[0]["n"]) if count_rows else 0
        unknown_count = int(unknown_rows[0]["n"]) if unknown_rows else 0

        permits: list[StalePermit] = []
        for row in rows:
            p = adapter.to_permit(row)
            since, basis = (p.applied_date, "applied") if p.applied_date else (p.issued_date, "issued")
            assert since is not None  # guaranteed by the age predicate
            permits.append(
                StalePermit(**p.model_dump(), open_since=since, open_since_basis=basis, days_open=(today - since).days)
            )
        end = offset + len(permits)
        next_cursor = cursors.encode("find_stale_permits", cfg.id, stored, end) if end < total else None
        if unknown_count:
            warnings.append(
                f"{unknown_count:,} permits matching these filters have an unknown status and were excluded; "
                "they may or may not still be open."
            )
        hint = _narrow_hint(offset, end, total, filters, ("permit_type", "min_valuation_usd")) if next_cursor else None
        return StaleResult(
            city=cfg.id,
            as_of=today,
            open_longer_than_days=days,
            excluded_unknown_status_count=unknown_count,
            permits=permits,
            total_count=total,
            returned=len(permits),
            next_cursor=next_cursor,
            hint=hint,
            warnings=warnings,
        )

    # ---- permit_stats -----------------------------------------------------------------

    async def permit_stats(
        self,
        city: str,
        group_by: GroupBy,
        date_from: date,
        date_to: date,
        date_field: DateField = "issued",
        status: Sequence[Status] | None = None,
        permit_type: Sequence[PermitType] | None = None,
    ) -> StatsResult:
        adapter = self._adapter(city)
        cfg = adapter.config
        self._check_range(date_from, date_to)
        date_col = self._date_column(adapter, date_field)
        filters = Filters(
            statuses=tuple(status) if status else None,
            permit_types=tuple(permit_type) if permit_type else None,
            date_field=date_field,
            date_from=date_from,
            date_to=date_to,
        )
        result = StatsResult(
            city=cfg.id, group_by=group_by, date_field=date_field, date_from=date_from, date_to=date_to,
            total_count=0, groups=[], warnings=self._gap_warnings(adapter, filters.statuses, filters.permit_types),
        )  # fmt: skip
        try:
            where = adapter.where(filters)
        except NoPossibleMatch as exc:
            result.warnings.extend(w for w in [self._no_match_warning(adapter, str(exc))] if w)
            return result

        val = cfg.fields.valuation_usd
        agg = "count(*) as n" + (f", sum(case({val} >= 0, {val})) as v" if val else "")
        if group_by in ("status", "permit_type"):
            cols = cfg.status.columns if group_by == "status" else cfg.permit_type.columns
            select, group = f"{', '.join(cols)}, {agg}", ", ".join(cols)
        else:
            trunc = "date_trunc_ym" if group_by == "month" else "date_trunc_y"
            select, group = f"{trunc}({date_col}) as period, {agg}", "period"
        params = {"$select": select, "$group": group, "$limit": GROUP_LIMIT}
        if where:
            params["$where"] = where
        rows = await self._query(adapter, params)

        buckets: dict[str, list[Any]] = {}
        for row in rows:
            if group_by == "status":
                key = adapter.classify_status(row)
            elif group_by == "permit_type":
                key = adapter.classify_type(row)
            else:
                period = row.get("period") or ""
                key = period[:7] if group_by == "month" else period[:4]
            bucket = buckets.setdefault(key, [0, None])
            bucket[0] += int(row["n"])
            if row.get("v") is not None:
                bucket[1] = (bucket[1] or 0.0) + float(row["v"])

        if group_by in ("month", "year"):
            for key in _periods(date_from, date_to, group_by):
                buckets.setdefault(key, [0, None])
            keys = sorted(buckets)
        else:
            keys = sorted(buckets, key=lambda k: (-buckets[k][0], k))
        result.groups = [StatGroup(key=k, count=buckets[k][0], total_valuation_usd=_round(buckets[k][1])) for k in keys]
        result.total_count = sum(g.count for g in result.groups)
        if group_by == "status" and "unknown" in buckets and buckets["unknown"][0]:
            result.warnings.append(
                f"{buckets['unknown'][0]:,} permits have status 'unknown' (blank or unrecognized in the source data)."
            )
        return result


# ---- module helpers -------------------------------------------------------------------


async def _none() -> list[dict[str, Any]]:
    return []


async def _empty_count() -> list[dict[str, Any]]:
    return [{"n": "0"}]


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _canonical(**kwargs: Any) -> dict[str, Any]:
    """JSON-safe, order-independent filter dict used both for queries and inside cursors."""
    out: dict[str, Any] = {
        "address": None,
        "status": None,
        "permit_type": None,
        "date_field": "issued",
        "date_from": None,
        "date_to": None,
        "min_valuation_usd": None,
    }
    for key, value in kwargs.items():
        if isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, (list, tuple)):
            value = sorted(set(value)) or None
        elif isinstance(value, int) and not isinstance(value, bool) and key == "min_valuation_usd":
            value = float(value)
        out[key] = value
    return out


def _filters(d: dict[str, Any]) -> Filters:
    return Filters(
        address=d.get("address"),
        statuses=tuple(d["status"]) if d.get("status") else None,
        permit_types=tuple(d["permit_type"]) if d.get("permit_type") else None,
        date_field=d.get("date_field") or "issued",
        date_from=date.fromisoformat(d["date_from"]) if d.get("date_from") else None,
        date_to=date.fromisoformat(d["date_to"]) if d.get("date_to") else None,
        min_valuation_usd=d.get("min_valuation_usd"),
    )


def _narrow_hint(offset: int, end: int, total: int, filters: Filters, options: Sequence[str]) -> str:
    unset = {
        "date_from": not (filters.date_from or filters.date_to),
        "status": not filters.statuses,
        "permit_type": not filters.permit_types,
        "address": not filters.address,
        "min_valuation_usd": filters.min_valuation_usd is None,
    }
    labels = {"date_from": "date_from/date_to"}
    suggestions = [labels.get(o, o) for o in options if unset.get(o)]
    narrow = f" To narrow, add {', '.join(suggestions)}." if suggestions else ""
    return (
        f"Showing {offset + 1}-{end} of {total:,}.{narrow} For counts or totals use permit_stats. "
        "For the next page, repeat the call with cursor=next_cursor."
    )


def _periods(start: date, end: date, unit: str) -> list[str]:
    if unit == "year":
        return [str(y) for y in range(start.year, end.year + 1)]
    keys, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return keys
