"""In-process TTL + LRU cache with single-flight.

Single-flight matters for agents: they often fire identical tool calls in parallel, and
without it each one would hit Socrata. Failures are never cached. The cache is per process
on purpose; a shared store (Redis) is the next step only if replicas multiply.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable


class TTLCache[V]:
    def __init__(self, ttl_seconds: float, maxsize: int = 1024, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl_seconds
        self.maxsize = maxsize
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Future[V]] = {}
        self.hits = 0
        self.misses = 0

    async def get_or_fetch(self, key: Hashable, fetch: Callable[[], Awaitable[V]]) -> V:
        entry = self._data.get(key)
        if entry is not None:
            expires, value = entry
            if self._clock() < expires:
                self._data.move_to_end(key)
                self.hits += 1
                return value
            del self._data[key]

        pending = self._inflight.get(key)
        if pending is not None:
            self.hits += 1
            return await asyncio.shield(pending)

        self.misses += 1
        future: asyncio.Future[V] = asyncio.get_running_loop().create_future()
        # Mark any exception as retrieved so asyncio doesn't warn when nobody else was waiting.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._inflight[key] = future
        try:
            value = await fetch()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                future.cancel()
            else:
                future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)
        future.set_result(value)
        self._store(key, value)
        return value

    def _store(self, key: Hashable, value: V) -> None:
        if self.ttl <= 0:
            return
        self._data[key] = (self._clock() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
