"""The HTTP deployment surface: bearer token gate, open health check, and a real MCP call
over Streamable HTTP through the gate (uvicorn on a free local port)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from permits_mcp.city_config import load_cities
from permits_mcp.http_app import build_http_app
from permits_mcp.server import build_server
from permits_mcp.socrata import SocrataClient

from .conftest import FROZEN_TODAY, ReplayTransport

TOKEN = "test-token-123"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    socrata = SocrataClient(transport=ReplayTransport())
    server = build_server(load_cities(), socrata, today=lambda _c: FROZEN_TODAY)
    app = build_http_app(server, api_key=TOKEN)
    port = _free_port()
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    uv.should_exit = True
    thread.join(timeout=5)


def test_health_check_needs_no_token(base_url: str) -> None:
    r = httpx.get(f"{base_url}/healthz")
    assert r.status_code == 200
    assert r.json()["cities"] == ["austin", "chicago", "seattle"]


@pytest.mark.parametrize("header", [None, "Bearer wrong", f"Basic {TOKEN}", TOKEN])
def test_mcp_endpoint_rejects_missing_or_wrong_token(base_url: str, header: str | None) -> None:
    headers = {"Authorization": header} if header else {}
    r = httpx.post(f"{base_url}/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_real_mcp_call_through_the_gate(base_url: str, anyio_backend: str) -> None:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
    async with Client(streamable_http_client(f"{base_url}/mcp", http_client=http)) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == 5 and tools.tools[0].name == "list_cities"
        result = await client.call_tool("get_permit", {"city": "chicago", "permit_id": "B200520605"})
        assert not result.is_error
        assert result.structured_content is not None and result.structured_content["permit_id"] == "B200520605"
