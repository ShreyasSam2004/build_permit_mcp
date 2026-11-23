"""The Streamable HTTP deployment: the SDK's ASGI app plus an optional bearer token.

Why a static bearer token and not OAuth: the data is public and read-only; the token only
stops strangers from spending this server's Socrata quota. The spec's OAuth flow is the right
tool when a server acts on a user's own data, which this one never does.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

OPEN_PATHS = frozenset({"/healthz"})  # platform health checks must not need the token


class BearerTokenMiddleware:
    """Require ``Authorization: Bearer <token>`` on every HTTP request except ``OPEN_PATHS``."""

    def __init__(self, app: ASGIApp, token: str, open_paths: Iterable[str] = OPEN_PATHS) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.open_paths = frozenset(open_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.open_paths:
            await self.app(scope, receive, send)
            return
        supplied = next((v for k, v in scope["headers"] if k == b"authorization"), b"")
        if hmac.compare_digest(supplied, self.expected):  # constant time: no timing oracle
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "unauthorized", "detail": "Send Authorization: Bearer <token>."}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="permits-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_http_app(
    server: MCPServer,
    *,
    api_key: str | None = None,
    allowed_hosts: list[str] | None = None,
    host: str = "127.0.0.1",
) -> ASGIApp:
    """ASGI app serving MCP at ``/mcp`` and a health check at ``/healthz``.

    ``json_response``: no tool streams or elicits, so one JSON body per POST is simpler and
    proxy-friendly. ``stateless_http``: only affects 2025-era clients (2026-07-28 traffic is
    already sessionless); it spares them sticky sessions too, so any replica can answer.
    ``allowed_hosts``: DNS-rebinding protection for a public hostname. The SDK enables it on
    its own only when bound to localhost; bound to 0.0.0.0 it would accept any Host header.
    """
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts],
        )
    app: ASGIApp = server.streamable_http_app(
        json_response=True, stateless_http=True, transport_security=security, host=host
    )
    if api_key:
        app = BearerTokenMiddleware(app, api_key)
    return app
