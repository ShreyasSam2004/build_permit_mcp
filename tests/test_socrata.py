"""Retry, backoff, caching and token handling, against a scripted transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest

from permits_mcp.cache import TTLCache
from permits_mcp.socrata import SocrataClient, SocrataError, SocrataUnavailable

pytestmark = pytest.mark.anyio


class Scripted(httpx.AsyncBaseTransport):
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(body: object = ()) -> httpx.Response:
    return httpx.Response(200, json=list(body) if isinstance(body, tuple) else body)


def make(transport: Scripted, **kw) -> tuple[SocrataClient, list[float]]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return SocrataClient(transport=transport, sleep=sleep, **kw), sleeps


async def test_retries_transient_errors_then_succeeds() -> None:
    transport = Scripted(httpx.Response(503), httpx.ConnectTimeout("slow"), ok([{"n": "1"}]))
    client, sleeps = make(transport)
    assert await client.query("example.org", "abcd-1234", {"$limit": "1"}) == [{"n": "1"}]
    assert len(transport.requests) == 3
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 0.5 and 0 <= sleeps[1] <= 1.0  # full jitter within the exponential cap


async def test_honors_retry_after() -> None:
    client, sleeps = make(Scripted(httpx.Response(429, headers={"Retry-After": "3"}), ok()))
    await client.query("example.org", "abcd-1234", {})
    assert sleeps == [3.0]


async def test_gives_up_after_max_attempts() -> None:
    client, sleeps = make(Scripted(*(httpx.Response(502) for _ in range(4))))
    with pytest.raises(SocrataUnavailable, match="HTTP 502 after 4 attempts"):
        await client.query("example.org", "abcd-1234", {})
    assert len(sleeps) == 3


async def test_client_errors_are_not_retried() -> None:
    transport = Scripted(httpx.Response(400, json={"message": "No such column: nope"}))
    client, sleeps = make(transport)
    with pytest.raises(SocrataError, match="No such column") as info:
        await client.query("example.org", "abcd-1234", {})
    assert not isinstance(info.value, SocrataUnavailable)
    assert sleeps == [] and len(transport.requests) == 1


async def test_identical_queries_are_cached_and_failures_are_not() -> None:
    transport = Scripted(*(httpx.Response(500) for _ in range(4)), ok([{"a": 1}]))
    client, _ = make(transport)
    with pytest.raises(SocrataUnavailable):
        await client.query("example.org", "abcd-1234", {"q": "1"})
    first = await client.query("example.org", "abcd-1234", {"q": "1"})
    second = await client.query("example.org", "abcd-1234", {"q": "1"})
    assert first == second == [{"a": 1}]
    assert len(transport.requests) == 5


async def test_app_token_header_is_sent() -> None:
    transport = Scripted(ok())
    client, _ = make(transport, app_token="secret-token")
    await client.query("example.org", "abcd-1234", {})
    assert transport.requests[0].headers["X-App-Token"] == "secret-token"
    assert client.has_token


async def test_no_token_header_when_unset() -> None:
    transport = Scripted(ok())
    client, _ = make(transport)
    await client.query("example.org", "abcd-1234", {})
    assert "X-App-Token" not in transport.requests[0].headers


# ---- cache -------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def counter() -> tuple[Callable[[], Awaitable[int]], list[int]]:
    calls: list[int] = []

    async def fetch() -> int:
        calls.append(1)
        await asyncio.sleep(0)
        return len(calls)

    return fetch, calls


async def test_cache_expires_after_ttl() -> None:
    clock = Clock()
    cache: TTLCache[int] = TTLCache(600, clock=clock)
    fetch, calls = counter()
    assert await cache.get_or_fetch("k", fetch) == 1
    clock.now = 599
    assert await cache.get_or_fetch("k", fetch) == 1
    clock.now = 601
    assert await cache.get_or_fetch("k", fetch) == 2
    assert len(calls) == 2


async def test_single_flight_collapses_concurrent_calls() -> None:
    cache: TTLCache[int] = TTLCache(600)
    fetch, calls = counter()
    results = await asyncio.gather(*(cache.get_or_fetch("k", fetch) for _ in range(10)))
    assert results == [1] * 10
    assert len(calls) == 1


async def test_lru_eviction() -> None:
    cache: TTLCache[int] = TTLCache(600, maxsize=2)
    fetch, calls = counter()
    for key in ("a", "b", "a", "c", "a", "b"):
        await cache.get_or_fetch(key, fetch)
    assert len(calls) == 4  # a, b, c, then b again (evicted by c; a was recently used)
