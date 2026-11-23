"""Async Socrata (SODA 2.1) client: app token, timeouts, retry with backoff, caching.

Retries cover what is plausibly transient (429, 500/502/503/504, timeouts, connection
errors) with exponential backoff and full jitter, honoring ``Retry-After``. Any other 4xx is
not retried: a 400 means this server built bad SoQL, which retrying can't fix.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import anyio
import httpx

from . import __version__
from .cache import TTLCache

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
Json = Any


class SocrataError(Exception):
    def __init__(self, message: str, *, status: int | None = None, attempts: int = 1) -> None:
        super().__init__(message)
        self.status = status
        self.attempts = attempts


class SocrataUnavailable(SocrataError):
    """Transient failure that persisted through every retry."""


class SocrataClient:
    def __init__(
        self,
        app_token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_ttl: float = 600,
        cache_size: int = 1024,
        max_attempts: int = 4,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": f"permits-mcp/{__version__}"}
        if app_token:
            headers["X-App-Token"] = app_token
        self._http = httpx.AsyncClient(
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=True,
        )
        self.cache: TTLCache[Json] = TTLCache(cache_ttl, cache_size)
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self.has_token = bool(app_token)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def query(self, domain: str, dataset_id: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        url = f"https://{domain}/resource/{dataset_id}.json"
        return await self._get(url, params)

    async def view_metadata(self, domain: str, dataset_id: str) -> dict[str, Any]:
        return await self._get(f"https://{domain}/api/views/{dataset_id}.json", {})

    async def _get(self, url: str, params: Mapping[str, str]) -> Json:
        key = (url, tuple(sorted(params.items())))
        return await self.cache.get_or_fetch(key, lambda: self._get_with_retry(url, params))

    async def _get_with_retry(self, url: str, params: Mapping[str, str]) -> Json:
        last: str = ""
        status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            retry_after: float | None = None
            try:
                response = await self._http.get(url, params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last, status = f"{type(exc).__name__}", None
            else:
                if response.status_code == 200:
                    return response.json()
                status = response.status_code
                last = _error_text(response)
                if status not in RETRY_STATUSES:
                    log.error(
                        "socrata request rejected",
                        extra={"url": url, "params": dict(params), "status": status, "error": last},
                    )
                    raise SocrataError(f"HTTP {status}: {last}", status=status, attempts=attempt)
                retry_after = _retry_after(response)
            if attempt == self.max_attempts:
                break
            backoff = random.uniform(0, min(self.max_delay, self.base_delay * 2 ** (attempt - 1)))
            delay = min(retry_after, 10.0) if retry_after is not None else backoff
            log.warning(
                "socrata request failed; retrying",
                extra={"url": url, "status": status, "error": last, "attempt": attempt, "delay_s": round(delay, 2)},
            )
            await self._sleep(delay)
        raise SocrataUnavailable(
            f"{'HTTP ' + str(status) if status else last} after {self.max_attempts} attempts",
            status=status,
            attempts=self.max_attempts,
        )


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None  # HTTP-date form: fall back to our own backoff


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)[:300]
    return str(body)[:300]
