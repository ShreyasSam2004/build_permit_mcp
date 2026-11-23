"""Run every question's hand-written SoQL against the live portals and save a timestamped snapshot.

    uv run --group eval python -m eval.ground_truth [--questions chi-01,sea-03]

Writes eval/results/ground_truth/<UTC timestamp>.json and a copy at latest.json. Run it right before
each eval run (eval/run.py does) so both arms are graded against the same, current data.

Sanity checks fail the snapshot rather than produce a quietly wrong answer key: a label question whose
top two groups tie, a cross-city winner whose margin is under 10%, an empty id list, a missing value.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .common import CITIES, GROUND_TRUTH_DIR, Query, Question, load_questions, snapshot_path, write_json

CUTOFF = re.compile(r"\{cutoff:(\d+)\}")
ARGMAX_MIN_MARGIN = 1.10  # the winner must beat the runner-up by 10%, so boundary judgment calls can't flip it


class GroundTruthError(Exception):
    pass


def city_today(city: str) -> datetime:
    return datetime.now(ZoneInfo(CITIES[city].timezone))


def render(where: str, city: str) -> str:
    today = city_today(city).date()
    return CUTOFF.sub(lambda m: f"{today - timedelta(days=int(m.group(1)))}T00:00:00", where)


def run_query(client: httpx.Client, query: Query) -> tuple[dict[str, str], list[dict[str, Any]]]:
    city = CITIES[query.city]
    params = {"$select": query.select, "$where": render(query.where, query.city)}
    for key in ("group", "order", "limit"):
        value = getattr(query, key)
        if value:
            params[f"${key}"] = value
    url = f"https://{city.domain}/resource/{city.dataset_id}.json"
    error = "no attempt made"
    for attempt in range(1, 6):
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:
            error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 200:
                return params, response.json()
            if response.status_code not in (429, 500, 502, 503, 504):
                raise GroundTruthError(f"{city.domain} HTTP {response.status_code}: {response.text[:300]}")
            error = f"HTTP {response.status_code}"
        delay = min(30.0, 2.0 * 2**attempt) * random.uniform(0.5, 1.0)
        print(f"  {city.domain}: {error}; retry {attempt} in {delay:.0f}s", file=sys.stderr)
        time.sleep(delay)
    raise GroundTruthError(f"{city.domain} kept failing; last error: {error}")


def _scalar(rows: list[dict[str, Any]], column: str) -> str:
    if len(rows) != 1 or rows[0].get(column) is None:
        raise GroundTruthError(f"expected one row with column '{column}', got {rows[:3]}")
    return str(rows[0][column])


def expected_answer(question: Question, results: list[list[dict[str, Any]]]) -> Any:
    kind, combine = question.answer_type, question.combine
    if combine == "argmax_city":
        counts = sorted(
            ((int(_scalar(r, "n")), q.city) for q, r in zip(question.queries, results, strict=True)), reverse=True
        )
        (top, city), (second, _) = counts[0], counts[1]
        if top < second * ARGMAX_MIN_MARGIN:
            raise GroundTruthError(f"winner margin too small to be robust: {counts}")
        return city
    if kind == "integer":
        return sum(int(_scalar(r, "n")) for r in results)
    if kind == "number":
        return round(sum(float(_scalar(r, "v")) for r in results), 2)
    if kind == "ids":
        ids = sorted({str(row["id"]).strip() for r in results for row in r if row.get("id")})
        if not ids:
            raise GroundTruthError("no ids matched")
        return ids
    if kind == "label":
        (rows,) = results
        if not rows:
            raise GroundTruthError("no groups returned")
        if len(rows) > 1 and rows[0]["n"] == rows[1]["n"]:
            raise GroundTruthError(f"top two groups tie: {rows[:2]}")
        label = str(rows[0]["label"])
        return label[:7] if question.label_format == "month" else label
    raise GroundTruthError(f"unsupported answer_type {kind}")


def compute(questions: list[Question]) -> dict[str, Any]:
    started = datetime.now(UTC)
    entries: dict[str, Any] = {}
    failures: list[str] = []
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        for question in questions:
            entry: dict[str, Any] = {"answer_type": question.answer_type, "category": question.category}
            if question.answer_type == "unavailable":
                entry["expected"] = "UNAVAILABLE"
                entries[question.id] = entry
                print(f"{question.id}: UNAVAILABLE (no query)")
                continue
            try:
                ran = [run_query(client, q) for q in question.queries]
                entry["queries"] = [
                    {"city": q.city, "params": p, "rows": rows[:20]}
                    for q, (p, rows) in zip(question.queries, ran, strict=True)
                ]
                entry["expected"] = expected_answer(question, [rows for _, rows in ran])
            except GroundTruthError as exc:
                failures.append(f"{question.id}: {exc}")
                entry["error"] = str(exc)
                print(f"{question.id}: ERROR {exc}", file=sys.stderr)
            else:
                shown = entry["expected"]
                print(f"{question.id}: {shown if not isinstance(shown, list) else f'{len(shown)} ids'}")
                if shown in (0, 0.0):
                    print(f"  warning: {question.id} expects zero; check the query isn't broken", file=sys.stderr)
            entries[question.id] = entry
    return {
        "computed_at": started.isoformat(timespec="seconds"),
        "city_dates": {cid: city_today(cid).date().isoformat() for cid in CITIES},
        "questions": entries,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", help="comma-separated question ids (default: all)")
    args = parser.parse_args(argv)
    questions = load_questions()
    if args.questions:
        wanted = set(args.questions.split(","))
        unknown = wanted - {q.id for q in questions}
        if unknown:
            parser.error(f"unknown question ids: {sorted(unknown)}")
        questions = [q for q in questions if q.id in wanted]
    snapshot = compute(questions)
    path = snapshot_path(snapshot["computed_at"])
    write_json(path, snapshot)
    write_json(GROUND_TRUTH_DIR / "latest.json", snapshot)
    print(f"\nWrote {path.relative_to(GROUND_TRUTH_DIR.parent.parent)} ({len(snapshot['questions'])} questions)")
    if snapshot["failures"]:
        print("FAILED:\n  " + "\n  ".join(snapshot["failures"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
