# permits-mcp: Phase 1 plan

A read-only MCP server that gives agents one normalized building-permit schema across US cities' Socrata portals. It exposes five task-shaped tools, and an eval measures whether agents answer more accurately with it than with a generic Socrata MCP server.

Status: **Phase 1 approved 2026-11-23 (all section-10 decisions accepted). Phases 2 and 3 done. Phase 4: the pilot (3/3 both arms) and the first full run are done; `status_raw` was added after it, so the next run measures that change.** Every "Why" line is the one-sentence defense for an interview. The repo tree below reflects the code as built.

First full run (2026-11-24, `eval/results/runs/20261124T191239Z`, 30 questions × 2 arms × 1 rep, $7.24):
- **Accuracy tied at 28/30** (95% CI 79%–98% for both). permits cost $0.082 per question against the baseline's $0.160, on about half the input tokens; tool calls and latency were the same.
- **Every failure was read in its transcript and labelled in `eval/results/failure_notes.toml`.** permits: chi-06 (it can't reach Chicago's raw `SUSPENDED`, folded into `issued`) and aus-08 (it read "issued permits" as the status `issued`). Baseline: chi-07 (counted `ACTIVE` only as open) and sea-07 (the key's issue-date fallback isn't stated in the question: 4978 + 36 − 10 = 5004).
- **Both baseline failures rest on conventions the server shares with the answer key.** So the honest reading is that the server wins on cost, not accuracy.
- **Changed after the run: `status_raw` (for chi-06).** `search_permits` and `permit_stats` filter on a city's own status, ignoring case; `permit_stats` groups by it, giving each value's normalized status; and `list_cities` lists the values inside each normalized status. Chicago 2024 `SUSPENDED` is 5,912 via the server, matching the key. This is a change made after seeing results, so it gets its own run rather than rewriting this one.

Phase 4 notes (2026-11-23):
- **30 questions with ground truth for all of them** (`eval/questions.toml`); `eval/ground_truth.py` fails the snapshot on tied labels, a cross-city winner within 10%, or empty id lists. Grader self-check: all 30 reference answers pass; empty, non-answers and near misses (±1, ×1.01, a dropped id, the next month) all fail.
- **The ground truth was cross-checked against the server's own tools on 11 questions; 9 match exactly.** The two differences are server weaknesses, not ground-truth errors:
  - `permit_stats` requires a date range, so it silently drops records with no date. Seattle has 5,154 applications in review, but 510 of them have no application date, so `permit_stats` reports 4,644. `search_permits` without dates gets 5,154. The server instructions ("use permit_stats for any count") steer agents to the wrong number.
  - Address normalization turns "LaSalle" into `LASALLE`; Chicago spells it `LA SALLE ST`, so the search returns 0 (52 expected). The no-match hint tells the agent to retry with the street name only.
- **Seattle `Closed` demolitions are mixed:** of 895, 561 have a completion date but 314 were never issued. The config maps all of them to `finaled`; the questions avoid this case.
- **Gap questions were checked against each portal's whole catalog**, because the baseline can search it. Seattle publishes electrical and trade permits separately and Austin publishes plan-review cases, so only Chicago's "applications in review" is a true gap.
- **Baseline pinned with a lockfile** (`eval/baseline/package-lock.json`, `opengov-mcp-server@0.1.1` and its 109 dependencies). Its source was read before use: one `get_data` tool passing SoQL to Socrata, no app token, and it loads a `.env` from its working directory, so the harness runs it from `eval/baseline` with only `PATH` and the portal URL in its environment.

Phase 3 notes:
- **Seattle and Austin were added with zero Python changes.** All 12 `src/**/*.py` files hashed identically before and after; only `cities/*.toml`, tests and fixtures changed.
- **Mapping decisions were checked against each city's own date columns, not inferred from status names.**
  - Seattle `Active` has no application or issue date, so it maps to `unknown`.
  - Seattle `Closed` is an abandoned application for building permits (65 of 7,954 were issued) but usually a finished job for demolitions. It maps to `cancelled` and `finaled` respectively.
  - Austin's legacy statuses that contradict their own dates map to `unknown` explicitly.
  - Explicit `then = "unknown"` rules mean "known value, deliberately unclassified": no warning is logged, and each case is listed in `known_gaps`.
- **The schema-drift check covers every row.** Grouping each full dataset found no unmapped status or type values across about 3.4M records. I verified it fails or warns on deliberately broken configs: a renamed column, a changed type, and a removed rule.
- **The HTTP deployment adds an optional static bearer token,** compared in constant time, with `/healthz` left open. OAuth would suit per-user data; here the token only protects the Socrata quota.
- **`PERMITS_ALLOWED_HOSTS` turns on the SDK's DNS-rebinding protection for public hostnames.** Bound to `0.0.0.0` the SDK accepts any Host header. Verified: a forged Host gets HTTP 421.
- **Deploy target: Fly.io, run by the owner** (the README has the commands). The same container also runs on Cloud Run, Azure Container Apps or Render.

---

## 0. What I verified before planning (2026-11-23)

I did not plan from memory. These facts come from the live APIs and current docs:

| Area | Finding | Consequence for the design |
|---|---|---|
| MCP Python SDK | `mcp` **2.3.0** on PyPI. v2 renamed `FastMCP` to **`MCPServer`** (`from mcp.server import MCPServer`). It implements spec **2026-07-28**, serves 2025-era clients from the same endpoint, and answers `server/discover` automatically. | Pin `mcp>=2.3,<3`. Use only v2 APIs. |
| SDK errors | `ToolError` (`mcp.server.mcpserver.exceptions`) becomes an `is_error` result that **the model can read**. Any other exception reaches the model only as "Error executing tool X". | Every recoverable failure is a `ToolError` with recovery instructions. |
| SDK output | A Pydantic return type produces an `outputSchema` and `structured_content`, plus JSON text for the model. | Each tool returns a Pydantic model. |
| Spec 2026-07-28 | Removes sessions and the `initialize` handshake. Any cross-call state uses "explicit, server-minted handles passed as ordinary tool arguments" (SEP-2567). MCP Logging is deprecated in favor of stderr or OpenTelemetry. `tools/list` SHOULD be deterministic. | The pagination cursor *is* that handle. Logs go to stderr. |
| Chicago `ydr8-5enu` | 853,716 rows. **Every row has `issue_date`**, so the dataset has only issued permits. `permit_status` is null on 303k rows (35%), mostly before 2024. There is no last-activity column and no per-permit URL. The type value `PERMIT – EXPRESS PERMIT PROGRAM` uses an **en dash**, while every other type uses a hyphen. | Chicago never has `applied`/`in_review` statuses. `last_activity_date` is always null. Mappings must be copied from live values, never retyped. |
| Seattle `76t5-zqzr` | 194,449 rows covering the full lifecycle (application, review, issued, completed), with a `link` column. Uses **`1900-01-01` as a placeholder** date. | The config needs a date sentinel that turns placeholders into `null`. |
| Austin `3syk-w9eu` | 2.39M rows, issued permits only. **`statusdate`** holds the last status change, making Austin the only city with a real `last_activity_date`. Permit type needs **two columns** (`permittype` BP/EP/MP/PP/DS plus `work_class`). | Mappings must be **ordered rules over one or more columns**, not flat lookups. |
| SoQL | Tested live: `||` concatenation, `coalesce()`, `date_trunc_ym()`, `$group` with `count(*)`. | Chicago's three-column address and monthly stats run server-side. |
| Baseline | `opengov-mcp-server` (Node, stdio) has one tool, `get_data(type=catalog\|dataset-metadata\|column-info\|data-access\|…, domain?, datasetId?, soqlQuery?, limit, offset)`. Its portal comes from `DATA_PORTAL_URL`. | The eval harness drives both servers the same way, over stdio. |

---

## 1. Repo structure

```
permits-mcp/
├── pyproject.toml              # uv-managed; ruff, pyright, pytest config
├── uv.lock
├── .env.example                # SOCRATA_APP_TOKEN=
├── Dockerfile                  # multi-stage, locked deps, non-root, HEALTHCHECK
├── fly.toml                    # Fly.io: one warm machine in ord, /healthz check
├── .github/workflows/
│   ├── ci.yml                  # ruff + pyright + unit tests (no network) on push/PR
│   └── schema-drift.yml        # daily cron: live column check, opens an issue on failure
├── docs/
│   ├── PLAN.md                 # this file
│   └── adding-a-city.md        # the "one file" workflow, step by step
├── src/permits_mcp/
│   ├── __main__.py             # `permits-mcp --transport stdio|http`
│   ├── server.py               # MCPServer: tool registration, agent-facing descriptions, param schemas
│   ├── service.py              # the five tools' behavior (MCP-free, unit-testable)
│   ├── http_app.py             # Streamable HTTP app: optional bearer token, allowed hosts
│   ├── schema.py               # Pydantic: Permit, PermitDetail, enums, result envelopes
│   ├── city_config.py          # Pydantic model of a city TOML file + loader/validator
│   ├── cities/                 # ← adding a city = adding a file here
│   │   ├── chicago.toml
│   │   ├── seattle.toml        # Phase 3
│   │   └── austin.toml         # Phase 3
│   ├── adapter.py              # one generic, config-driven adapter: rows → Permit, filters → SoQL
│   ├── soql.py                 # SoQL builder: escaping, null-safe predicates, rule inversion
│   ├── socrata.py              # httpx client: app token, timeouts, retry/backoff, cache
│   ├── cache.py                # TTL+LRU cache with single-flight
│   ├── cursor.py               # opaque pagination cursor encode/decode/validate
│   └── log.py                  # JSON-lines logging to stderr
├── tests/
│   ├── fixtures/<portal host>/ # recorded Socrata responses (JSON), keyed by request hash
│   ├── conftest.py             # in-memory MCP Client + replay transport (PERMITS_RECORD=1 re-records)
│   ├── test_city_configs.py    # every city file validates; bad configs are rejected
│   ├── test_soql.py            # escaping, null handling, rule inversion executed in SQLite
│   ├── test_cursor.py
│   ├── test_adapter.py         # row normalization, status/type rules, address normalization
│   ├── test_socrata.py         # retry/backoff, Retry-After, cache TTL, single-flight, LRU
│   ├── test_tools.py           # end-to-end via MCP Client, all cities: filter/stats agreement, errors, paging
│   ├── test_http_app.py        # token gate, open /healthz, real MCP call over HTTP through the gate
│   └── live/test_schema_drift.py   # pytest -m live; used by the daily workflow
└── eval/                       # Phase 4
    ├── questions.toml          # 30 questions, each with its ground-truth SoQL and interpretation
    ├── common.py               # city map + question loader (independent of permits_mcp)
    ├── ground_truth.py         # runs the SoQL, writes a timestamped snapshot
    ├── harness.py              # same model, same loop, either server; fresh server per trial
    ├── grade.py                # deterministic graders + self-check + results.md
    ├── run.py                  # one command: ground truth -> self-check -> arms -> report
    ├── baseline/               # package.json + lockfile pinning opengov-mcp-server@0.1.1
    └── results/                # ground_truth/ snapshots, runs/<id>/ (results, errors, transcripts), results.md
```

**Why one generic adapter instead of an adapter class per city?** The requirement is "adding a city means adding one file." With per-city classes, every new city is new code to review and test. With one adapter driven by a validated config, a new city is data: a schema check and a drift check cover it.

**Why TOML, not YAML?** `tomllib` is in the standard library, so there's no extra dependency. TOML also has no implicit typing, so a raw value like `NO` or `Yes` stays a string. Austin has a `Yes`/`No` column, so this is a real risk, not a hypothetical one.

---

## 2. Normalized schema

```python
Status = Literal["applied", "in_review", "issued", "finaled", "expired", "cancelled", "unknown"]

PermitType = Literal[
    "new_construction",
    "addition_alteration",
    "repair",
    "demolition",
    "electrical",
    "mechanical",
    "plumbing",
    "sign",
    "site_work",
    "other",
    "unknown",
]


class Permit(BaseModel):  # returned by search / stale tools
    permit_id: str
    city: str  # config id, e.g. "chicago"
    address: str | None
    permit_type: PermitType
    permit_type_raw: str | None  # exact source value(s)
    status: Status
    status_raw: str | None  # e.g. "COMPLETE / CANCELLED" when two columns decide it
    applied_date: date | None  # ISO 8601 (YYYY-MM-DD)
    issued_date: date | None
    last_activity_date: date | None
    valuation_usd: float | None
    description: str | None  # truncated to 200 chars in list results
    source_url: str | None


class PermitDetail(Permit):  # returned by get_permit
    raw: dict[str, Any]  # every non-null source column, untouched
```

Rules:
- **A field the city doesn't publish is `null`, never derived or guessed.** For example, `last_activity_date` is null for Chicago and Seattle. I won't fake it from "the latest of the other dates." Placeholder dates (Seattle's `1900-01-01`) also become `null`.
- **`unknown` is a real value, not an error.** An unmapped *non-null* raw value maps to `unknown` and is logged as a WARNING with city, column and value. A null raw value maps to `unknown` without a log, because it's missing data, not drift.

Design notes:
- **Why `date`, not `datetime`?** Socrata returns floating timestamps like `2026-11-20T00:00:00.000`. No source publishes a time of day, so emitting one would claim precision the data doesn't have.
- **Why flat `permit_type` + `permit_type_raw` instead of a nested `{normalized, raw}` object?** Flat keys are cheaper in tokens across 25-record pages, and the filter parameter names match the field names.
- **Why keep `raw` at all?** The normalization is lossy by design. `get_permit` keeps the source values so an agent, or a skeptical user, can always check what the city actually published.
- **The `permit_type` taxonomy is my proposal; your spec didn't define one.** It's the smallest set that covers all three cities. Chicago bundles electrical and HVAC work into "Express" and "Easy" permits, so Chicago can never report `electrical`, `mechanical` or `plumbing`. That limitation goes in Chicago's `known_gaps` rather than being hidden.

---

## 3. City config format (Chicago, drafted from live values)

```toml
id = "chicago"
name = "Chicago, IL"
domain = "data.cityofchicago.org"
dataset_id = "ydr8-5enu"
timezone = "America/Chicago"
known_gaps = [
  "Only issued permits are published; there are no 'applied' or 'in_review' records.",
  "permit_status is blank on ~35% of records (mostly issued before 2024); these are 'unknown'.",
  "No last-activity date is published; last_activity_date is always null.",
  "Electrical/HVAC/plumbing work is filed under Express/Easy permits and normalizes to 'repair'.",
]

[fields]
permit_id     = "permit_"
applied_date  = "application_start_date"
issued_date   = "issue_date"
valuation_usd = "reported_cost"
description   = "work_description"
address       = { concat = ["street_number", "street_direction", "street_name"] }
source_url    = { template = "https://data.cityofchicago.org/resource/ydr8-5enu.json?permit_={permit_id}" }
# last_activity_date: not published → omitted → always null

[status]                       # ordered rules, first match wins; no match → "unknown"
rules = [
  { when = { permit_status    = ["CANCELLED", "REVOKED"] },             then = "cancelled" },
  { when = { permit_milestone = ["CANCELLED", "DENIED"] },              then = "cancelled" },
  { when = { permit_status    = ["EXPIRED"] },                          then = "expired"   },
  { when = { permit_milestone = ["EXPIRED"] },                          then = "expired"   },
  { when = { permit_status    = ["COMPLETE"] },                         then = "finaled"   },
  { when = { permit_status    = ["ACTIVE", "PHASED PERMITTING", "SUSPENDED"] }, then = "issued" },
]

[permit_type]
rules = [
  { when = { permit_type = ["PERMIT - NEW CONSTRUCTION"] },                          then = "new_construction" },
  { when = { permit_type = ["PERMIT - RENOVATION/ALTERATION", "PERMIT - PORCH CONSTRUCTION"] }, then = "addition_alteration" },
  { when = { permit_type = ["PERMIT – EXPRESS PERMIT PROGRAM", "PERMIT - EASY PERMIT PROCESS"] }, then = "repair" },  # note: en dash in EXPRESS
  { when = { permit_type = ["PERMIT - WRECKING/DEMOLITION"] },                       then = "demolition" },
  { when = { permit_type = ["PERMIT - SIGNS"] },                                     then = "sign" },
  { when = { permit_type = ["PERMIT - ELEVATOR EQUIPMENT", "PERMIT - SCAFFOLDING",
                            "PERMIT - REINSTATE REVOKED PMT", "PERMIT - FOR EXTENSION OF PMT"] }, then = "other" },
]
```

**Why ordered rules over columns, not a flat `value → status` dict?** The live data showed that one column is often not enough. In Chicago, `COMPLETE` + milestone `CANCELLED` means cancelled, not finaled (342 rows). Austin's type needs both `permittype` and `work_class`. A flat dict would need a code change for every such city. Ordered rules handle all three cities as config.

**The same rules run in both directions:**
- *Row → status* in Python, when normalizing results.
- *Status → SoQL predicate* when filtering. `status=cancelled` compiles to `(rule1) OR (rule2 AND NOT rule1)`, and `unknown` compiles to `NOT (any rule)`. Negation is **null-safe**: `field IS NULL OR field NOT IN (…)`. Plain SQL-style `NOT IN` silently drops nulls, which would lose Chicago's 303k null-status rows.
- A fixture test asserts both directions agree: rows the API returns for `status=X` must all classify as `X` in Python.

**Why `stats` and `filters` stay correct after normalization:** filtering and grouping happen in SoQL on raw values. Python only re-buckets the few distinct raw groups into normalized ones. No rows are pulled to aggregate.

---

## 4. Tool signatures

Behavior shared by all five tools:
- **Annotations:** `ToolAnnotations(read_only_hint=True, open_world_hint=True)`. Per the SDK docs, destructive and idempotent hints apply only to write tools.
- **`city` is a `Literal` enum built from the loaded config files at startup**, so the valid cities appear in the tool's input schema itself. A `ToolError` path still exists as a safety net.
- **Dates are inclusive ISO dates.** "Today" means today in the city's timezone.
- Tools are registered in a fixed order, so `tools/list` is deterministic, as the spec recommends.
- **Every result envelope has:** `total_count`, `returned`, `next_cursor` (null when done), `hint` (how to narrow, when truncated) and `warnings` (e.g. "12,031 matching permits have status 'unknown' and were excluded").

### 1. `list_cities() -> CitiesResult`
Returns `[{id, name, portal_url, dataset_id, data_last_updated, issued_date_range, statuses_available, permit_types_available, fields_unavailable, known_gaps}]`. `data_last_updated` comes from the view metadata's `rowsUpdatedAt`, which is cached. `statuses_available` and `fields_unavailable` are derived from the config.
> **Use when:** starting out, choosing a city, or checking whether a field or status exists before trusting a null or zero result. **Don't use** for permit data.

**Why expose gaps to the agent?** Questions like "how many Chicago permits are in review?" should get the answer "Chicago doesn't publish that," not a confident `0`. The eval includes questions that test exactly this.

### 2. `search_permits(...) -> SearchResult`
```python
search_permits(
    city: CityId,
    address: str | None = None,              # "121 N LaSalle St"; normalized + substring match
    status: list[Status] | None = None,      # "open" = ["applied", "in_review", "issued"]
    status_raw: list[str] | None = None,     # the city's own status, any case, e.g. ["SUSPENDED"]  ADDED AFTER RUN 1
    permit_type: list[PermitType] | None = None,
    date_field: Literal["issued", "applied"] = "issued",   # PROPOSED ADDITION
    date_from: date | None = None,
    date_to: date | None = None,
    min_valuation_usd: float | None = None,  # PROPOSED ADDITION
    limit: int = 25,                         # 1..50
    cursor: str | None = None,
)
```
Results are ordered by `<date_field> DESC, permit_id ASC`, a total order, so pages never overlap. Records and the `count(*)` query are fetched concurrently.
> **Use when:** you need specific permits or a short list. **Don't use** for counts or totals (use `permit_stats`, which is exact and cheaper), for permits open too long (use `find_stale_permits`), or when you already have an ID (use `get_permit`).

Address input is uppercased, whitespace is collapsed, and directionals and suffixes are standardized (NORTH→N, STREET→ST). It then matches with `LIKE '%…%'` against the city's address expression. When nothing matches and the input had a house number, the hint suggests retrying with only the street name.

### 3. `get_permit(city, permit_id) -> PermitDetail`
Returns the full record plus `raw`.
> **Use when:** you have a `permit_id` from a search result or the user. **Don't use** to discover permits.

Not found → `ToolError: "No permit 'X' in chicago. Chicago IDs look like 'B200520605'. To find it by location, call search_permits with address=…"`.
*Checked 2026-11-23:* permit IDs are unique in all three datasets (a `$group … $having n > 1` over every row found none). `get_permit` still fetches up to 10 matches; if a city ever republishes an ID (e.g. a reinstatement), it returns the most recently issued record and describes the others in `warnings`. The daily drift job re-checks uniqueness and warns.

### 4. `find_stale_permits(...) -> StaleResult`
```python
find_stale_permits(
    city: CityId,
    open_longer_than_days: int,              # >= 1
    permit_type: list[PermitType] | None = None,
    min_valuation_usd: float | None = None,  # PROPOSED ADDITION (needed by your own example question)
    limit: int = 25,
    cursor: str | None = None,
)
```
- **"Open"** means status is `applied`, `in_review` or `issued`.
- **Age** is counted from `applied_date`, or from `issued_date` when there is no applied date. The basis is returned per record (`open_since`, `open_since_basis`, `days_open`).
- Results are sorted oldest first.
- The response includes **`excluded_unknown_status_count`** so the agent can add a caveat. This matters for Chicago, where 35% of statuses are unknown.

> **Use when:** looking for permits that stayed open past a threshold. **Don't use** for closed, expired or cancelled permits (use `search_permits` with `status`).

### 5. `permit_stats(...) -> StatsResult`
```python
permit_stats(
    city: CityId,
    group_by: Literal["status", "permit_type", "month", "year"],
    date_from: date,
    date_to: date,
    date_field: Literal["issued", "applied"] = "issued",   # PROPOSED ADDITION
    status: list[Status] | None = None,
    status_raw: list[str] | None = None,     # ADDED AFTER RUN 1; also group_by="status_raw"
    permit_type: list[PermitType] | None = None,
)
```
Returns `groups: [{key, count, total_valuation_usd}]` plus the overall `total_count`. With `group_by="status_raw"` each group also carries `normalized_status`, the status(es) that raw value maps to (Chicago `COMPLETE` maps to finaled, expired or cancelled depending on the milestone). This is a single SoQL request using `$select=<raw group cols>, count(*), sum(<valuation>)` and `$group`. Month and year grouping use `date_trunc_ym` / `date_trunc_y`. Normalized keys (status, type) are re-bucketed in Python from the few raw groups.
> **Use when:** the question is "how many", "how much" or "which is most common". **Don't use** to list permits.

### Error contract (each error says how to recover)

| Situation | Message pattern |
|---|---|
| Unknown city | `Unknown city 'nyc'. Supported: chicago, seattle, austin. Call list_cities for coverage and gaps.` |
| Bad status or type | Rejected by Pydantic `Literal` validation before the tool runs. The SDK's message lists the valid values. |
| Status or field the city doesn't publish | Not an error: a `warning` plus a correct empty result, e.g. `Chicago does not publish applications under review (see list_cities.known_gaps).` |
| Truncated | `hint`: `Showing 25 of 18,432. Narrow with date_from/date_to, status, permit_type, or address; for counts use permit_stats.` |
| Cursor from a different query | `This cursor belongs to a different query. Repeat the call without cursor to restart, or keep the same filters.` |
| `date_from > date_to`, range in the future | ToolError stating the valid range from `list_cities.issued_date_range`. |
| Socrata down after retries | ToolError: `Chicago's data portal is not responding (HTTP 503 after 4 attempts). Retry in a minute; other cities are unaffected.` |

**Why `ToolError` and not returning error strings?** A returned string is `is_error=False`, so it looks like a successful answer. A `MCPError` is a protocol failure the model never sees. `ToolError` is the only path that both flags the failure and lets the model read the recovery hint.

---

## 5. Request pipeline and reliability

```
tool → Pydantic validation → adapter compiles SoQL (escaped) → cache → single-flight → httpx (retry) → normalize → envelope
```

- **SoQL injection:** every user string is escaped (single quotes doubled), and column names come only from config, never from input.
- **Pagination cursor:** `c1.<base64url(json{tool, filters, offset})>`. The server keeps no state, so any replica can serve page 2. That is the SEP-2567 handle pattern.
  - **Why unsigned?** The cursor grants no capability. Everything in it is something the agent could pass directly, and it's validated like any other input. Signing would add key management with no security benefit for public data.
  - **Why offset, not keyset?** Keyset paging needs a non-null sort key in every city. Seattle's dates are null until issuance. With a total order, short-lived pages and a 50-record cap, offset is correct and simpler. Agents are steered to narrow filters rather than page deeply.
- **Retry:** on 429, 500, 502, 503, 504 and connect/read timeouts. Up to 4 attempts, exponential backoff (0.5s base, ×2, 8s cap) with full jitter, and `Retry-After` is honored when present. Other 4xx responses are not retried. A 400 from Socrata means my SoQL compiler has a bug, so it's logged at ERROR.
- **Cache:** in-process TTL of 10 minutes with an LRU of about 1,024 entries. The key is `(domain, dataset, canonicalized params)`.
  - Single-flight collapses concurrent identical requests, which is common when agents call tools in parallel.
  - Errors are never cached.
  - It is per replica on purpose; a shared Redis is the next step only if traffic warrants it.
- **Socrata app token:** read from `SOCRATA_APP_TOKEN` and sent as `X-App-Token`. It's optional, and the server logs a startup warning when it's missing because anonymous requests are throttled.
- **Timeouts:** 5s connect, 30s read.
- **Why SODA 2.1 `/resource/{id}.json` and not SODA 3?** It works with or without a token, and it's what every Socrata portal exposes today. The HTTP client is one module, so switching later is local.

## 6. Transport and runtime

- `permits-mcp` (stdio) is the default for Claude Code and Claude Desktop. `permits-mcp --transport http --host 0.0.0.0 --port 8000` runs `mcp.run(transport="streamable-http", json_response=True, stateless_http=True)`.
  - **Why `json_response=True`?** No tool streams progress or asks the user for input, so one JSON body per POST is simpler and proxy-friendly.
  - **Why `stateless_http=True` when 2026 traffic is already sessionless?** The setting only affects 2025-era clients, and it means they don't need sticky sessions either.
- **Logging:** JSON lines to **stderr** only, because stdout belongs to the stdio transport. The spec deprecates MCP-level logging, so I'm not adding it.
- **Server `instructions`:** a two-line orientation ("start with list_cities; use permit_stats for counts").

## 7. Tests

- **Unit tests (CI, no network):** `httpx.MockTransport` replays recorded fixtures, and **any unrecorded request fails the test**, so tests can never hit the network by accident.
  - **Why `MockTransport` and not respx or VCR?** It's built into httpx, so there's no extra dependency, and the replay logic is about 30 lines anyone can read.
- **Tool tests:** run through the SDK's in-memory `Client(mcp)`, so they exercise the real MCP path: input schemas, `structured_content`, `is_error`, annotations, and descriptions that contain "Use when" and "Don't use".
- **Config tests:** every `cities/*.toml` file validates, and every rule target is a valid enum value.
- **Schema drift (`pytest -m live`, daily cron):** for each city, it fetches `/api/views/{id}.json`.
  - It **fails** if a mapped column disappears or changes type. A text→number change silently breaks date math and sorting, so type changes count as failures too.
  - It **warns** on new raw status or type values that would map to `unknown` (value drift), listed in the job summary.
  - On failure, the workflow opens or updates a GitHub issue.
  - **Why separate from CI?** Live portals are flaky and change daily. A red PR build should mean my code broke, not that Seattle had a 503.

---

## 8. Eval design (built in Phase 4, designed now)

- **Questions:** 30 total: 9 Chicago, 9 Seattle, 9 Austin and 3 cross-city. Each entry has a natural-language question, a category, an answer type, and its **ground-truth SoQL written against raw columns**, with a note on how the question was interpreted.
  - **Why hand-written SoQL instead of my server's code?** Using my server for ground truth would be circular. Independent ground truth is the only way the comparison means anything.
- **Categories:**
  - date-field semantics (issued vs applied)
  - status semantics ("open", "completed", Austin `VOID`, Chicago milestone overrides)
  - type semantics ("new construction")
  - valuation thresholds
  - stale permits
  - lookups by ID or address
  - aggregations
  - cross-city comparisons
  - **gap awareness**: the correct answer is "not published"
- **Answers must be checkable:** "Which permits over $1M…" becomes "How many…, and list the 5 with the highest valuation." Grading is **deterministic**: exact integers, numbers within ±0.5%, exact ID sets, or an `UNAVAILABLE` token. There's no LLM judge, so results are reproducible and free of judge bias.
- **Data drift:** ground truth is recomputed right before each run and saved with a timestamp. Both arms run back to back. Questions use closed historical windows wherever possible.
- **Harness:**
  - The Anthropic Python SDK runs a **manual tool loop**, with an MCP v2 `Client` connected to each server over stdio. Each server's `tools/list` becomes the Claude tool definitions.
  - Same model (`claude-opus-5-5`, effort held fixed), same system prompt, same turn cap (20 tool calls), no other tools.
  - The final answer comes on a `FINAL ANSWER:` line.
  - **Why not the API's MCP connector?** It needs a public URL, and the baseline is a local stdio process. A local loop also gives exact per-call accounting and full transcripts.
- **Fairness:**
  - The baseline is pinned to an exact npm version.
  - Its system prompt also gets the city→portal-domain mapping, so it isn't penalized for not knowing `data.seattle.gov`. Otherwise the result would measure discovery, not schema understanding.
  - Optional third arm: baseline plus dataset IDs ("strong baseline").
- **Report:** a table of accuracy, mean tool calls, mean tokens (input + output, all turns) and mean latency per arm, with per-category accuracy. Every failure gets one row: question, expected, got, failure class (wrong date field, wrong status mapping, truncation or pagination, gave up, tool error, arithmetic, hallucinated gap) and a transcript link.
- **Threats to validity, stated in the README:**
  - I wrote the questions knowing my tools.
  - Status interpretation is a judgment call, documented per question.
  - Each question runs a single trial unless we choose repeats.
- **Cost:** I'll pilot 3 questions on both arms first and report the measured cost before the full run.

---

## 9. Phases and "done when"

| Phase | Deliverable | Done when |
|---|---|---|
| **2. Chicago end to end** | `chicago.toml`, generic adapter, SoQL builder, Socrata client (retry, cache), the five tools, recorded fixtures, unit and tool tests, `docs/adding-a-city.md` | `uv run pytest` is green with no network; `claude mcp add permits -- uv run permits-mcp` works in Claude Code; a manual walkthrough shows each tool and each error path working |
| **3. Seattle + Austin, CI, deploy** | Two TOML files (zero Python changes, which proves the config claim), `schema-drift.yml`, `ci.yml`, Dockerfile, deployed HTTP endpoint | The diff for adding the two cities touches only `cities/` and fixtures; the drift job passes on schedule; the remote URL works from Claude Code over HTTP |
| **4. Eval** | `questions.toml`, ground-truth snapshot, harness, grader, `results/results.md` | Both arms run with one command; the results table and failure breakdown are generated, not hand-edited |
| **5. README** | Results table at the top, install (stdio + remote), tools, design decisions (condensed from this file), limitations | A stranger can install it and reproduce the eval from the README alone |

---

## 10. Decisions I need from you before Phase 2

1. **Signature additions beyond your spec:**
   - `date_field` on `search_permits` and `permit_stats`. "Issued in March" and "applied in March" are different questions, and guessing the date column is exactly the failure this project exists to fix.
   - `min_valuation_usd` on `search_permits` and `find_stale_permits`. Your own example ("permits over $1M open more than 2 years") can't be answered without paging through every stale permit.
   - `status` and `permit_type` accept lists, so "open" is one call.
   - Approve all, some, or none.
2. **`permit_type` taxonomy** (section 2): OK as proposed?
3. **Chicago status judgment calls:** `SUSPENDED` → `issued` (still open, raw value preserved), `REVOKED` → `cancelled`, milestone `DENIED` → `cancelled` (2 rows).
4. **Stale "age" basis:** days since `applied_date`, falling back to `issued_date`, as proposed. The alternative is "days since issuance", which is the usual meaning in permit-office reports.
5. **Eval trials:** one trial per question per arm (your spec), or three? Three costs 3× but gives error bars on a 30-question set.
6. **Refusal fallback in the eval:** I plan to leave the API's server-side model fallback **off**, so every answer comes from the same model, and record any refusal as a failure. Say if you want it on.
7. **Deploy target (Phase 3):** a container on Fly.io or Google Cloud Run (recommended: always-on process, warm cache, HTTPS) vs. a serverless platform. Also: should the public endpoint require a bearer token? It spends my Socrata quota.
