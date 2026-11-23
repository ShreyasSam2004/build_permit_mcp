"""Shared fixtures.

HTTP is replayed from recorded Socrata responses in ``tests/fixtures/<host>/``. Any request
without a recording fails the test, so unit tests can never touch the network by accident.
To (re-)record against the live portals: ``PERMITS_RECORD=1 uv run pytest``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from mcp import Client

from permits_mcp.city_config import CityConfig, load_cities
from permits_mcp.server import build_server
from permits_mcp.socrata import SocrataClient

FIXTURES = Path(__file__).parent / "fixtures"
FROZEN_TODAY = date(2026, 11, 22)  # the date fixtures were recorded; stale-permit cutoffs derive from it
RECORD = os.environ.get("PERMITS_RECORD") == "1"


def request_key(request: httpx.Request) -> tuple[str, str]:
    query = urlencode(sorted(parse_qsl(request.url.query.decode(), keep_blank_values=True)))
    canonical = f"{request.url.path}?{query}"
    return hashlib.sha256(canonical.encode()).hexdigest()[:16], canonical


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, root: Path = FIXTURES, record: bool = RECORD) -> None:
        self.root = root
        self.record = record
        self.live = httpx.AsyncHTTPTransport() if record else None
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key, canonical = request_key(request)
        self.requests.append(canonical)
        path = self.root / request.url.host / f"{key}.json"
        if self.live is not None:
            response = await self.live.handle_async_request(request)
            body = await response.aread()
            if response.status_code == 200:
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {"request": canonical, "status": 200, "body": json.loads(body)}
                path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            return httpx.Response(response.status_code, content=body, headers={"Content-Type": "application/json"})
        if not path.exists():
            raise AssertionError(
                f"Unrecorded request to {request.url.host}: {canonical}\nRecord it with: PERMITS_RECORD=1 uv run pytest"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return httpx.Response(data["status"], json=data["body"])


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def cities() -> dict[str, CityConfig]:
    return load_cities()


@pytest.fixture
def replay() -> ReplayTransport:
    return ReplayTransport()


@pytest.fixture
async def mcp_client(cities: dict[str, CityConfig], replay: ReplayTransport) -> AsyncIterator[Client]:
    socrata = SocrataClient(transport=replay, sleep=_no_sleep)
    server = build_server(cities, socrata, today=lambda _cfg: FROZEN_TODAY)
    async with Client(server, raise_exceptions=True) as client:
        yield client
    await socrata.aclose()
