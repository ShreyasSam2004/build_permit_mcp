"""Opaque pagination cursors.

The 2026-07-28 spec has no sessions; cross-call state travels as "server-minted handles passed
as ordinary tool arguments" (SEP-2567). A cursor carries the tool, city, canonical filters and
offset, so any replica can serve the next page.

Cursors are not signed: they grant nothing the agent couldn't pass directly, and they're
validated like any other input.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

PREFIX = "c1."


class CursorError(ValueError):
    pass


def encode(tool: str, city: str, filters: dict[str, Any], offset: int) -> str:
    payload = json.dumps({"t": tool, "c": city, "f": filters, "o": offset}, sort_keys=True, separators=(",", ":"))
    return PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode(cursor: str, tool: str, city: str) -> tuple[dict[str, Any], int]:
    """Return ``(filters, offset)`` stored in ``cursor``, after checking it belongs to this tool and city."""
    if not cursor.startswith(PREFIX):
        raise CursorError("Malformed cursor. Pass back next_cursor exactly as returned, or omit cursor to start over.")
    body = cursor[len(PREFIX) :]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        stored_tool, stored_city, filters, offset = data["t"], data["c"], data["f"], data["o"]
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise CursorError(
            "Malformed cursor. Pass back next_cursor exactly as returned, or omit cursor to start over."
        ) from exc
    if stored_tool != tool:
        raise CursorError(f"This cursor came from {stored_tool}, not {tool}. Use it only with {stored_tool}.")
    if stored_city != city:
        raise CursorError(f"This cursor is for city '{stored_city}', not '{city}'. Omit cursor to start over.")
    if not isinstance(offset, int) or offset < 0 or not isinstance(filters, dict):
        raise CursorError("Malformed cursor. Omit cursor to start over.")
    return filters, offset


def resolve(
    cursor: str | None, tool: str, city: str, filters: dict[str, Any], defaults: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Decide which filters and offset a paged call uses.

    With no cursor: the given filters, offset 0. With a cursor: the cursor's filters. If the
    caller also passed filters they must match the cursor's (a changed filter means a new
    query); passing only the cursor, with every filter left at its default, is fine.
    """
    if cursor is None:
        return filters, 0
    stored, offset = decode(cursor, tool, city)
    if filters != stored and filters != defaults:
        raise CursorError(
            "This cursor belongs to a different query (the filters changed). "
            "Repeat the call without cursor to start over, or pass the same filters as the first call."
        )
    return stored, offset
