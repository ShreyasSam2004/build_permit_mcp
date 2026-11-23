from __future__ import annotations

import pytest

from permits_mcp import cursor

FILTERS = {"address": "LOREL", "status": ["issued"], "date_field": "issued"}
DEFAULTS = {"address": None, "status": None, "date_field": "issued"}


def test_round_trip() -> None:
    token = cursor.encode("search_permits", "chicago", FILTERS, 25)
    assert token.startswith("c1.")
    assert cursor.decode(token, "search_permits", "chicago") == (FILTERS, 25)


def test_cursor_alone_reuses_its_filters() -> None:
    token = cursor.encode("search_permits", "chicago", FILTERS, 50)
    assert cursor.resolve(token, "search_permits", "chicago", DEFAULTS, DEFAULTS) == (FILTERS, 50)


def test_same_filters_with_cursor_is_fine() -> None:
    token = cursor.encode("search_permits", "chicago", FILTERS, 50)
    assert cursor.resolve(token, "search_permits", "chicago", FILTERS, DEFAULTS) == (FILTERS, 50)


def test_changed_filters_with_cursor_is_an_error() -> None:
    token = cursor.encode("search_permits", "chicago", FILTERS, 50)
    changed = {**FILTERS, "status": ["finaled"]}
    with pytest.raises(cursor.CursorError, match="different query"):
        cursor.resolve(token, "search_permits", "chicago", changed, DEFAULTS)


@pytest.mark.parametrize(
    ("token", "message"),
    [
        ("garbage", "Malformed"),
        ("c1.!!!notbase64", "Malformed"),
        ("c1.eyJ4IjoxfQ", "Malformed"),  # valid base64 JSON, wrong shape
        (cursor.encode("find_stale_permits", "chicago", {}, 0), "came from find_stale_permits"),
        (cursor.encode("search_permits", "seattle", {}, 0), "for city 'seattle'"),
        (cursor.encode("search_permits", "chicago", {}, -5), "Malformed"),
    ],
)
def test_bad_cursors(token: str, message: str) -> None:
    with pytest.raises(cursor.CursorError, match=message):
        cursor.decode(token, "search_permits", "chicago")
