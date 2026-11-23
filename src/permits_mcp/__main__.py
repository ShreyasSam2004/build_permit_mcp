"""CLI: ``permits-mcp`` (stdio, for Claude Code / Claude Desktop) or ``permits-mcp --transport http``."""

from __future__ import annotations

import argparse
import logging
import os

from . import log
from .city_config import load_cities
from .server import build_server
from .socrata import SocrataClient

logger = logging.getLogger("permits_mcp")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="permits-mcp", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--log-level", default=os.environ.get("PERMITS_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    log.configure(args.log_level)
    token = os.environ.get("SOCRATA_APP_TOKEN") or None
    if not token:
        logger.warning("SOCRATA_APP_TOKEN is not set; Socrata throttles anonymous requests more aggressively")
    client = SocrataClient(token, cache_ttl=float(os.environ.get("PERMITS_CACHE_TTL", "600")))
    cities = load_cities()
    server = build_server(cities, client)
    logger.info("starting", extra={"transport": args.transport, "cities": list(cities)})

    if args.transport == "stdio":
        server.run()
        return

    import uvicorn

    from .http_app import build_http_app

    api_key = os.environ.get("PERMITS_API_KEY") or None
    allowed_hosts = [h.strip() for h in os.environ.get("PERMITS_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not api_key:
        logger.warning("PERMITS_API_KEY is not set; the HTTP endpoint is open to anyone who can reach it")
    app = build_http_app(server, api_key=api_key, allowed_hosts=allowed_hosts or None, host=args.host)
    # log_config=None: uvicorn logs through the root JSON handler instead of its own formatters.
    # proxy_headers: behind a platform proxy (Fly, Cloud Run), log the real client address.
    uvicorn.run(app, host=args.host, port=args.port, log_config=None, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
