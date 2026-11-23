# Adding a city

Adding a city means adding one file, `src/permits_mcp/cities/<id>.toml`. There is no Python
to write: one generic adapter reads the config, and tests plus the schema-drift check cover
the new file automatically.

## 1. Find the dataset and verify its columns against the live API

Don't copy column names or values from the portal's web page or docs. Query the API.

```bash
# Column names and types
curl -s "https://<domain>/api/views/<dataset-id>.json" | python -c "
import json,sys
for c in json.load(sys.stdin)['columns']: print(c['fieldName'], c['dataTypeName'])"

# Every raw status value, with counts (repeat for the type column(s))
curl -s -G "https://<domain>/resource/<dataset-id>.json" \
  --data-urlencode '$select=<status_col>, count(*) as n' \
  --data-urlencode '$group=<status_col>' --data-urlencode '$order=n DESC'

# Is the ID column unique? Are there placeholder dates (e.g. 1900-01-01)?
curl -s -G "https://<domain>/resource/<dataset-id>.json" \
  --data-urlencode '$select=count(*), min(<applied_col>), min(<issued_col>)'
```

Copy raw values **from that output**, character for character. Chicago's
`PERMIT – EXPRESS PERMIT PROGRAM` uses an en dash while its other types use a hyphen. A
retyped value silently sends every matching permit to `unknown`.

## 2. Write the config

Start from `cities/chicago.toml`. The fields:

| Key | Meaning |
|---|---|
| `id` | Lowercase id. Must match the file name. |
| `domain`, `dataset_id` | e.g. `data.cityofchicago.org`, `ydr8-5enu` |
| `timezone` | IANA zone. Used for "today" when computing permit age. |
| `permit_id_example` | A real ID, shown to agents in "not found" errors. |
| `known_gaps` | Plain-English limits of this city's data. Agents read these in `list_cities`. |
| `date_null_before` | Optional. Earlier dates are placeholders and normalize to null. |
| `[fields]` | Raw column for each normalized field. **Omit a field the city doesn't publish**; it is then always null and listed in `fields_unavailable`. `address` can be `{ concat = [...] }`. `source_url` can be a column or `{ template = "...{permit_id}..." }`. |
| `[status].rules` / `[permit_type].rules` | Ordered rules; the first match wins. `when` maps one or more columns to accepted values (all must match). `then` is the normalized value. |

Rules over several columns handle cases a flat lookup can't. For example, Chicago's status
`COMPLETE` combined with milestone `CANCELLED` is a cancellation. Put the more specific rules
first.

A non-blank raw value that matches no rule becomes `unknown` and is logged as a WARNING
(`"unmapped raw value -> unknown"`). It is never dropped.

## 3. Record fixtures and test

Add tool tests for the new city in `tests/test_tools.py`; the Chicago tests are the template.
Then record their responses once, live, and replay them offline after that:

```bash
PERMITS_RECORD=1 uv run pytest   # hits the live portal, writes tests/fixtures/<domain>/
uv run pytest                    # offline; any unrecorded request fails the test
```

`tests/test_soql.py` picks up the new config automatically. It proves, in SQLite, that the
SoQL filter for every status and type selects exactly the rows the Python classifier assigns
to it, including rows with null columns.

Then run the live checks once, the same ones the daily workflow runs:

```bash
uv run pytest -m live   # mapped columns exist with the right types; no unmapped raw values
```

The value check groups the **whole** dataset by your status and type columns. It lists any
raw value no rule covers, with its row count. Fix those before shipping. If a value's meaning
genuinely conflicts with the record's dates, map it to `unknown` explicitly and add a line to
`known_gaps`.

## 4. Check the agent-facing result

Connect the server to Claude Code (see the README), then ask it to call `list_cities`. Confirm `statuses_available`, `fields_unavailable` and `known_gaps`
describe the city honestly.
