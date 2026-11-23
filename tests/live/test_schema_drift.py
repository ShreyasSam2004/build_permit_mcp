"""Schema-drift check against the live portals. Run daily by .github/workflows/schema-drift.yml.

    uv run pytest -m live

* FAILS if a column a city config maps has disappeared or changed type. That breaks the
  server outright (or worse, silently: a date column turning into text breaks date math).
* WARNS if raw status/type values appear that no rule covers. Those already degrade safely
  to "unknown" and get logged; the warning (plus a GitHub job-summary table) says which rule to add.
* WARNS if a permit ID appears on more than one record. get_permit then returns the most
  recently issued record with a warning, so this is degraded, not broken.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import AsyncIterator

import pytest

from permits_mcp.adapter import CityAdapter
from permits_mcp.city_config import CityConfig, Concat, load_cities
from permits_mcp.socrata import SocrataClient

pytestmark = [pytest.mark.live, pytest.mark.anyio]

CITIES = load_cities()

# Socrata dataTypeName values each role may have.
ALLOWED = {
    "date": {"calendar_date"},
    "number": {"number"},
    "text": {"text"},
    "id": {"text", "number"},
    "url": {"url", "text"},
    "any": {"text", "number"},
}


def expected_columns(config: CityConfig) -> dict[str, str]:
    f = config.fields
    roles: dict[str, str] = {f.permit_id: "id"}
    for column in (f.applied_date, f.issued_date, f.last_activity_date):
        if column:
            roles[column] = "date"
    if f.valuation_usd:
        roles[f.valuation_usd] = "number"
    if f.description:
        roles[f.description] = "text"
    if isinstance(f.address, Concat):
        roles.update(dict.fromkeys(f.address.concat, "any"))
    elif f.address:
        roles[f.address] = "text"
    if isinstance(f.source_url, str):
        roles[f.source_url] = "url"
    for column in [*config.status.columns, *config.permit_type.columns]:
        roles[column] = "text"
    return roles


def _summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


@pytest.fixture
async def socrata() -> AsyncIterator[SocrataClient]:
    client = SocrataClient(os.environ.get("SOCRATA_APP_TOKEN") or None, cache_ttl=0)
    yield client
    await client.aclose()


@pytest.mark.parametrize("city", list(CITIES))
async def test_mapped_columns_exist_with_expected_types(socrata: SocrataClient, city: str) -> None:
    config = CITIES[city]
    meta = await socrata.view_metadata(config.domain, config.dataset_id)
    live = {c["fieldName"]: c["dataTypeName"] for c in meta["columns"]}
    problems = []
    for column, role in expected_columns(config).items():
        if column not in live:
            problems.append(f"column `{column}` is gone")
        elif live[column] not in ALLOWED[role]:
            problems.append(f"column `{column}` is `{live[column]}`, expected one of {sorted(ALLOWED[role])}")
    if problems:
        _summary(f"### ❌ {config.name} ({config.portal_url})\n" + "\n".join(f"- {p}" for p in problems))
    assert not problems, f"{config.name}: " + "; ".join(problems)


@pytest.mark.parametrize("kind", ["status", "permit_type"])
@pytest.mark.parametrize("city", list(CITIES))
async def test_no_unmapped_raw_values(socrata: SocrataClient, city: str, kind: str) -> None:
    config = CITIES[city]
    adapter = CityAdapter(config)
    columns = config.status.columns if kind == "status" else config.permit_type.columns
    classify = adapter.classify_status if kind == "status" else adapter.classify_type
    rows = await socrata.query(
        config.domain,
        config.dataset_id,
        {"$select": f"{', '.join(columns)}, count(*) as n", "$group": ", ".join(columns), "$limit": "50000"},
    )
    assert rows, f"{config.name}: no {kind} values returned"
    rules = config.status.rules if kind == "status" else config.permit_type.rules
    unmapped = [
        (" / ".join(str(r.get(c, "∅")) for c in columns), int(r["n"]))
        for r in rows
        if not any(rule.matches(r) for rule in rules) and any(r.get(c) for c in columns)
    ]
    for row in rows:
        classify(row)  # exercises the same code path the server uses
    if unmapped:
        unmapped.sort(key=lambda item: -item[1])
        lines = "\n".join(f"| `{value}` | {n:,} |" for value, n in unmapped)
        _summary(
            f"### ⚠️ {config.name}: {len(unmapped)} unmapped {kind} value(s) → `unknown`\n"
            f"Add a rule in `cities/{city}.toml`.\n\n| raw value ({' / '.join(columns)}) | rows |\n|---|---|\n{lines}"
        )
        warnings.warn(f"{config.name}: unmapped {kind} values: {unmapped[:10]}", stacklevel=1)


@pytest.mark.parametrize("city", list(CITIES))
async def test_permit_ids_are_unique(socrata: SocrataClient, city: str) -> None:
    config = CITIES[city]
    id_col = config.fields.permit_id
    rows = await socrata.query(
        config.domain,
        config.dataset_id,
        {"$select": f"{id_col}, count(*) as n", "$group": id_col, "$having": "n > 1", "$limit": "20"},
    )
    if rows:
        lines = "\n".join(f"| `{r.get(id_col)}` | {r['n']} |" for r in rows)
        _summary(
            f"### ⚠️ {config.name}: permit IDs on more than one record\n"
            f"get_permit returns the most recently issued one with a warning.\n\n| {id_col} | records |\n|---|---|\n"
            + lines
        )
        warnings.warn(f"{config.name}: duplicate permit IDs: {rows[:10]}", stacklevel=1)
