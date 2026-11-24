# permits-mcp

An MCP server that gives AI agents **one normalized building-permit schema across US cities'
Socrata open-data portals**, with five task-shaped, read-only tools.

> Work in progress. Phases 1–3 of 5 are done: Chicago, Seattle and Austin, schema-drift CI,
> and a deployable HTTP server. The eval and results come next. The design and the reasoning
> behind it are in [docs/PLAN.md](docs/PLAN.md).

| City | Dataset | What it covers |
|---|---|---|
| Chicago, IL | [`ydr8-5enu`](https://data.cityofchicago.org/d/ydr8-5enu) | Issued permits since 2006 |
| Seattle, WA | [`76t5-zqzr`](https://data.seattle.gov/d/76t5-zqzr) | Full lifecycle: applications, plan review, issued, closed |
| Austin, TX | [`3syk-w9eu`](https://data.austintexas.gov/d/3syk-w9eu) | Issued building and trade (electrical, mechanical, plumbing) permits |

Each city is a single config file in [`src/permits_mcp/cities/`](src/permits_mcp/cities/). Seattle
and Austin were added without changing any Python.

## Quickstart (local, stdio)

Requires [uv](https://docs.astral.sh/uv/). Python 3.12+ is installed by uv if needed.

```bash
git clone <this repo> && cd permits-mcp
uv sync
uv run pytest            # offline: Socrata responses are replayed from tests/fixtures
```

**Claude Code:**

```bash
claude mcp add permits -e SOCRATA_APP_TOKEN=<optional token> -- uv run --directory /absolute/path/to/permits-mcp permits-mcp
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "permits": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/permits-mcp", "permits-mcp"],
      "env": { "SOCRATA_APP_TOKEN": "" }
    }
  }
}
```

## Tools

| Tool | Use it for |
|---|---|
| `list_cities` | Which cities exist, data freshness, which statuses, types and fields each city really publishes (and which of the city's own statuses each normalized status includes), known gaps |
| `search_permits` | Listing permits by address, status (normalized, or the city's own via `status_raw`), type, date range (issued or applied) or minimum valuation; cursor pagination |
| `get_permit` | One permit's full record plus every raw source field |
| `find_stale_permits` | Permits still open past N days, oldest first, with excluded-unknown counts |
| `permit_stats` | Exact counts and valuation sums by status, the city's own status (`status_raw`), type, month or year, aggregated by the portal. The date range is optional: without one, permits with no date are counted too; with one, the result says how many it left out |

Statuses are normalized across cities, so each one folds several of a city's own values together:
Chicago's `issued` includes `ACTIVE`, `PHASED PERMITTING` and `SUSPENDED`. For a question about one
of those, pass `status_raw=["SUSPENDED"]` (case-insensitive) or group by `status_raw`.

Try asking: *"Which Chicago permits over $1M have been open more than 2 years?"*, *"How many
Seattle applications are in plan review?"* or *"Compare new-construction permits in Austin and
Chicago in 2025."*

## Configuration

All configuration is environment variables. There is no `.env` loading; set them in your MCP
client config or on the host.

| Variable | Default | Meaning |
|---|---|---|
| `SOCRATA_APP_TOKEN` | unset | Optional. Without it, Socrata throttles requests more aggressively. Create one on any Socrata portal under Profile > Developer Settings. |
| `PERMITS_API_KEY` | unset | HTTP only. When set, `/mcp` requires `Authorization: Bearer <key>`; `/healthz` stays open. |
| `PERMITS_ALLOWED_HOSTS` | unset | HTTP only. Comma-separated public hostnames. Enables DNS-rebinding protection; other Host headers get HTTP 421. |
| `PERMITS_CACHE_TTL` | `600` | Seconds identical Socrata queries are cached. |
| `PERMITS_LOG_LEVEL` | `INFO` | JSON logs go to stderr. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | HTTP bind address. The container uses `0.0.0.0:8080`. |

## Deploy (Fly.io)

The [`Dockerfile`](Dockerfile) builds a 242 MB image. It installs from `uv.lock` without dev
dependencies, runs as a non-root user and has a health check. [`fly.toml`](fly.toml) keeps
one machine warm in Chicago (`ord`).

```bash
# 1. Install flyctl (https://fly.io/docs/flyctl/install/) and sign in
fly auth login

# 2. Create the app (the name must be unique on Fly)
fly apps create <app-name>

# 3. Secrets: an API key for the endpoint, your Socrata token, and the public hostname
python -c "import secrets; print(secrets.token_hex(24))"      # use the output as <api-key>
fly secrets set -a <app-name> PERMITS_API_KEY=<api-key> SOCRATA_APP_TOKEN=<token> PERMITS_ALLOWED_HOSTS=<app-name>.fly.dev

# 4. Deploy and check
fly deploy -a <app-name>
curl https://<app-name>.fly.dev/healthz
```

Connect Claude Code to the deployed server:

```bash
claude mcp add --transport http permits-remote https://<app-name>.fly.dev/mcp --header "Authorization: Bearer <api-key>"
```

The same image runs on Cloud Run, Azure Container Apps or Render. Set the variables above and
route traffic to port 8080.

## Development

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
PERMITS_RECORD=1 uv run pytest   # re-record fixtures from the live portals
PERMITS_RECORD=missing uv run pytest   # record only requests with no fixture yet; keep the rest
uv run pytest -m live            # schema-drift check against the live portals
uv run permits-mcp --transport http --port 8000   # Streamable HTTP at /mcp, health at /healthz
```

CI ([`ci.yml`](.github/workflows/ci.yml)) runs lint, types and the offline tests on Python
3.12 and 3.14, and builds the container. [`schema-drift.yml`](.github/workflows/schema-drift.yml)
runs daily against the live portals:
- It **fails**, and opens or updates an issue, if a mapped column disappears or changes type.
- It **warns**, with a table in the job summary, if a new status or type value appears that no
  rule covers.

To add a city, see [docs/adding-a-city.md](docs/adding-a-city.md). It means adding one config
file; no Python code is needed.

## Eval (in progress)

The eval asks Claude 30 permit questions twice: once with this server, once with the generic
Socrata server [`opengov-mcp-server`](https://www.npmjs.com/package/opengov-mcp-server) (pinned
to 0.1.1). The model, prompt and tool-call cap are the same for both. Answers are graded against
hand-written SoQL run on the live portals right before each run. The questions and their
interpretations are in [`eval/questions.toml`](eval/questions.toml).

```bash
uv sync --group eval
(cd eval/baseline && npm ci --ignore-scripts)        # once: install the pinned baseline
echo "ANTHROPIC_API_KEY=..." >> .env                 # the eval calls claude-opus-5-5; .env is gitignored
uv run --env-file .env python -m eval.run --pilot    # 3 questions x 2 arms; reports measured and projected cost
uv run --env-file .env python -m eval.run            # all 30 questions
```

The key stays with the harness: each MCP server is started with only `PATH` and its own settings.

Each run writes `eval/results/runs/<id>/` with `results.md`, per-trial transcripts and an
`errors.jsonl` for unscored infrastructure failures. `uv run python -m eval.grade --selfcheck`
confirms the grader accepts every reference answer and rejects near misses.
