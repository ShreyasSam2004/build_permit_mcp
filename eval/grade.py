"""Grade a run deterministically and write its results.md. No LLM judge.

    uv run --group eval python -m eval.grade <run id>          # writes runs/<id>/results.md and results/results.md
    uv run --group eval python -m eval.grade --selfcheck        # oracle, null and near-miss answers through the grader

Rules: integers exact; dollar amounts within 0.5%; id lists as exact sets; months as YYYY-MM; cities by name;
UNAVAILABLE as a token. Trials whose response hit max_tokens are shown but kept out of the accuracy
denominator; refusals, missing answers and the tool-call cap count as failures and are reported as their own
classes. Attempts in errors.jsonl (infrastructure failures) are never scored.

Optional eval/results/failure_notes.toml adds a human failure class to a graded failure, keyed "<question>/<arm>":
    ["chi-08/baseline"]
    class = "address spelling"
    note = "searched LASALLE; the data spells LA SALLE"
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import CITIES, RESULTS_DIR, RUNS_DIR, UNAVAILABLE, Question, latest_ground_truth, load_questions, read_json

NUMBER_TOLERANCE = 0.005
NOTES_FILE = RESULTS_DIR / "failure_notes.toml"
_NUM = re.compile(r"(?<![\w.])[-+]?\$?\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|million|thousand|bn|[bmk])?\b", re.IGNORECASE)
_SCALE = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name} | {
    name.lower(): i for i, name in enumerate(calendar.month_abbr) if name
}


@dataclass(frozen=True)
class Grade:
    correct: bool
    failure_class: str | None = None
    detail: str = ""


def parse_number(text: str) -> float | None:
    match = _NUM.search(text.replace("\u00a0", " "))
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    if match.group(2):
        value *= _SCALE[match.group(2).lower()]
    return -value if text.lstrip().startswith("-") else value


def parse_ids(text: str) -> set[str]:
    parts = re.split(r"[,;\n]|\band\b", text)
    return {p.strip().strip("`'\".*[]() ").upper() for p in parts if p.strip().strip("`'\".*[]() ")}


def parse_month(text: str) -> str | None:
    if m := re.search(r"\b(\d{4})-(\d{1,2})\b", text):
        year, month = int(m.group(1)), int(m.group(2))
        return f"{year:04d}-{month:02d}" if 1 <= month <= 12 else None
    if m := re.search(r"\b([A-Za-z]{3,9})\.?,?\s+(\d{4})\b", text):
        month = _MONTHS.get(m.group(1).lower())
        return f"{int(m.group(2)):04d}-{month:02d}" if month else None
    return None


def parse_city(text: str) -> str | None:
    words = set(re.findall(r"[a-z]+", text.lower()))
    named = [cid for cid, city in CITIES.items() if city.name.lower() in words]
    return named[0] if len(named) == 1 else None


def is_unavailable(text: str) -> bool:
    return text.strip().strip("`*.").upper().startswith(UNAVAILABLE)


def grade_answer(answer_type: str, expected: Any, answer: str | None, label_format: str | None = None) -> Grade:
    if answer is None or not answer.strip():
        return Grade(False, "no answer")
    claimed_gap = is_unavailable(answer)
    if answer_type == "unavailable":
        return Grade(True) if claimed_gap else Grade(False, "missed gap", f"answered {answer!r}")
    if claimed_gap:
        return Grade(False, "hallucinated gap")
    if answer_type in ("integer", "number"):
        got = parse_number(answer)
        if got is None:
            return Grade(False, "unparseable answer", answer)
        exp = float(expected)
        tolerance = 0.0 if answer_type == "integer" else NUMBER_TOLERANCE * max(abs(exp), 1e-9)
        if abs(got - exp) <= tolerance:
            return Grade(True)
        pct = f" ({(got - exp) / exp:+.1%})" if exp else ""
        return Grade(False, "wrong value", f"off by {got - exp:+,.2f}{pct}".replace(".00 ", " "))
    if answer_type == "ids":
        got_ids, exp_ids = parse_ids(answer), {str(e).upper() for e in expected}
        if got_ids == exp_ids:
            return Grade(True)
        missing, extra = sorted(exp_ids - got_ids), sorted(got_ids - exp_ids)
        return Grade(False, "wrong value", f"missing {missing or 'none'}, extra {extra or 'none'}")
    if answer_type == "label":
        got_label = parse_month(answer) if label_format == "month" else parse_city(answer)
        if got_label is None:
            return Grade(False, "unparseable answer", answer)
        return Grade(True) if got_label == expected else Grade(False, "wrong value", f"got {got_label}")
    raise ValueError(f"unknown answer_type {answer_type}")


def render_expected(answer_type: str, expected: Any, label_format: str | None = None) -> str:
    if answer_type == "ids":
        return ", ".join(expected)
    if answer_type == "number":
        return f"{float(expected):.2f}"
    if answer_type == "label" and label_format == "city":
        return CITIES[expected].name
    return str(expected)


def near_miss(answer_type: str, expected: Any, label_format: str | None = None) -> str:
    """A plausible wrong answer the grader must reject."""
    if answer_type == "integer":
        return str(int(expected) + 1)
    if answer_type == "number":
        return f"{float(expected) * 1.01:.2f}"
    if answer_type == "ids":
        return ", ".join(expected[1:]) if len(expected) > 1 else f"{expected[0]}, X0"
    if answer_type == "label" and label_format == "month":
        year, month = map(int, str(expected).split("-"))
        return f"{year}-{month % 12 + 1:02d}"
    if answer_type == "label":
        return next(c.name for cid, c in CITIES.items() if cid != expected)
    return "0"


def selfcheck(questions: list[Question], snapshot: dict[str, Any]) -> list[str]:
    problems = []
    for q in questions:
        entry = snapshot["questions"].get(q.id, {})
        if "expected" not in entry:
            problems.append(f"{q.id}: no expected answer in the snapshot")
            continue
        exp = entry["expected"]
        if not grade_answer(
            q.answer_type, exp, render_expected(q.answer_type, exp, q.label_format), q.label_format
        ).correct:
            problems.append(f"{q.id}: the reference answer does not pass")
        for wrong in ("", "I could not determine this.", near_miss(q.answer_type, exp, q.label_format)):
            if grade_answer(q.answer_type, exp, wrong, q.label_format).correct:
                problems.append(f"{q.id}: wrong answer {wrong!r} passes")
    return problems


# ---- report --------------------------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _short(value: Any, limit: int = 60) -> str:
    text = ", ".join(value) if isinstance(value, list) else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


STATUS_CLASS = {"no_answer": "gave up (no FINAL ANSWER)", "turn_cap": "tool-call cap reached", "refusal": "refusal"}


def grade_rows(
    rows: list[dict[str, Any]], questions: dict[str, Question], snapshot: dict[str, Any], notes: dict[str, Any]
) -> list[dict[str, Any]]:
    graded = []
    for row in rows:
        q = questions.get(row["question_id"])
        if q is None:
            continue
        expected = snapshot["questions"][q.id]["expected"]
        status = row["status"]
        if status == "answered":
            g = grade_answer(q.answer_type, expected, row.get("final_answer"), q.label_format)
        elif status == "truncated":
            g = Grade(False, "truncated (max_tokens)")
        else:
            g = Grade(False, STATUS_CLASS.get(status, status))
        note = notes.get(f"{q.id}/{row['arm']}", {})
        graded.append(
            {
                **row,
                "category": q.category,
                "expected": expected,
                "correct": g.correct,
                "failure_class": note.get("class", g.failure_class),
                "detail": note.get("note", g.detail),
                "scored": status != "truncated",
            }
        )
    return graded


def build_report(
    run_dir: Path,
    meta: dict[str, Any],
    graded: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    questions: dict[str, Question],
    snapshot: dict[str, Any],
) -> str:
    arms = meta["arms"]
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in graded:
        by_arm[g["arm"]].append(g)
    lines = [
        f"# Eval results: run `{meta['run_id']}`" + (" (pilot)" if meta.get("pilot") else ""),
        "",
        f"Generated by `eval/grade.py` from `{run_dir.name}/results.jsonl`; do not edit by hand.",
        "",
        f"- **Model:** `{meta['model']}`, effort `{meta['effort']}`, adaptive thinking, "
        f"cap {meta['limits']['max_tool_calls']} tool calls, same system prompt for every arm "
        "(the baseline arms also get each city's portal hostname)",
        f"- **Ground truth:** `{meta['ground_truth']['file']}` (computed {meta['ground_truth']['computed_at']}), "
        "hand-written SoQL against raw columns",
        f"- **Baseline:** `{meta['baseline']}`. **Code:** `{meta['git']['commit'][:10]}`"
        + (" (uncommitted changes)" if meta["git"]["dirty"] else ""),
        f"- **Trials:** {len(meta['question_ids'])} questions × {len(arms)} arms × {meta['reps']} rep(s); "
        f"{len(graded)} graded, {len(errors)} unscored infrastructure errors",
        "",
        "## Summary",
        "",
        "| Arm | Accuracy | 95% CI | Correct / scored | Mean tool calls | Mean input tokens | Mean output tokens "
        "| Mean cost | Mean latency | Truncated | Errors |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in arms:
        rows = by_arm.get(arm, [])
        scored = [r for r in rows if r["scored"]]
        k = sum(r["correct"] for r in scored)
        lo, hi = _wilson(k, len(scored))
        inputs = [r["usage"]["input"] + r["usage"]["cache_write"] + r["usage"]["cache_read"] for r in rows]
        lines.append(
            f"| {arm} | {k / len(scored):.0%} | {lo:.0%}–{hi:.0%} | {k} / {len(scored)} | "
            f"{_mean([r['tool_calls'] for r in rows]):.1f} | {_mean(inputs):,.0f} | "
            f"{_mean([r['usage']['output'] for r in rows]):,.0f} | ${_mean([r['cost_usd'] for r in rows]):.3f} | "
            f"{_mean([r['wall_s'] for r in rows]):.0f}s | {len(rows) - len(scored)} | "
            f"{sum(e['arm'] == arm for e in errors)} |"
            if scored
            else f"| {arm} | n/a | | 0 / 0 | | | | | | {len(rows)} | {sum(e['arm'] == arm for e in errors)} |"
        )
    lines += [
        "",
        "Input tokens include cached reads and writes. Latency is wall-clock per question, including tool calls.",
        "",
        "## Accuracy by category",
        "",
        "| Category | Questions | " + " | ".join(arms) + " |",
        "|---|---|" + "---|" * len(arms),
    ]
    categories = sorted({g["category"] for g in graded})
    for cat in categories:
        n_q = len({g["question_id"] for g in graded if g["category"] == cat})
        cells = []
        for arm in arms:
            rows = [r for r in by_arm.get(arm, []) if r["category"] == cat and r["scored"]]
            cells.append(f"{sum(r['correct'] for r in rows)}/{len(rows)}" if rows else "–")
        lines.append(f"| {cat} | {n_q} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Per question",
        "",
        "| Question | Expected | " + " | ".join(arms) + " |",
        "|---|---|" + "---|" * len(arms),
    ]
    for qid in meta["question_ids"]:
        if qid not in snapshot["questions"]:
            continue
        cells = []
        for arm in arms:
            rows = [r for r in by_arm.get(arm, []) if r["question_id"] == qid]
            cells.append(
                " ".join(
                    ("✅ " if r["correct"] else "❌ ") + _cell(_short(r.get("final_answer") or r["status"], 30))
                    for r in rows
                )
                or "–"
            )
        lines.append(
            f"| `{qid}` {_cell(_short(questions[qid].question, 70))} | "
            f"{_cell(_short(snapshot['questions'][qid]['expected'], 30))} | " + " | ".join(cells) + " |"
        )

    failures = [g for g in graded if not g["correct"]]
    lines += ["", "## Failures", ""]
    if failures:
        lines += ["| Question | Arm | Expected | Got | Class | Detail | Transcript |", "|---|---|---|---|---|---|---|"]
        for f in sorted(failures, key=lambda r: (r["question_id"], r["arm"], r["rep"])):
            lines.append(
                f"| `{f['question_id']}` | {f['arm']} | {_cell(_short(f['expected'], 40))} | "
                f"{_cell(_short(f.get('final_answer') or '—', 40))} | {_cell(f['failure_class'])} | "
                f"{_cell(_short(f['detail'], 60))} | [{f['key']}]({f['transcript']}) |"
            )
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for f in failures:
            counts[(f["arm"], f["failure_class"])] += 1
        lines += ["", "Failure classes: " + "; ".join(f"{arm} {cls} ×{n}" for (arm, cls), n in sorted(counts.items()))]
    else:
        lines.append("None.")

    differ = []
    for qid in meta["question_ids"]:
        results = {arm: [r["correct"] for r in by_arm.get(arm, []) if r["question_id"] == qid] for arm in arms}
        if len({tuple(v) for v in results.values()}) > 1:
            differ.append(f"`{qid}` (" + ", ".join(f"{a}: {sum(v)}/{len(v)}" for a, v in results.items()) + ")")
    lines += ["", "Questions where the arms disagree: " + (", ".join(differ) if differ else "none.")]

    if errors:
        lines += ["", "## Unscored attempts (infrastructure errors)", "", "| Trial | Class | Detail |", "|---|---|---|"]
        lines += [f"| {e['key']} | {e['error_class']} | {_cell(_short(e['detail'], 100))} |" for e in errors]

    all_rows = [*graded, *errors]
    total = sum(r["cost_usd"] for r in all_rows)
    per_trial = {arm: _mean([r["cost_usd"] for r in by_arm.get(arm, [])]) for arm in arms}
    full_questions = len(questions)
    projected = sum(per_trial.values()) * full_questions * meta["reps"]
    lines += [
        "",
        "## Cost",
        "",
        f"This run cost **${total:.2f}** ({len(all_rows)} attempts, including unscored ones). Mean per trial: "
        + ", ".join(f"{arm} ${c:.3f}" for arm, c in per_trial.items())
        + f". At those rates, all {full_questions} questions × {len(arms)} arms × {meta['reps']} rep(s) "
        f"would cost about **${projected:.2f}**.",
        "",
        "## Reading these numbers",
        "",
        f"- With {len(meta['question_ids'])} questions and {meta['reps']} rep(s) per arm, the 95% interval on "
        "accuracy is wide; differences of a few questions are within noise.",
        "- The questions were written by the author of permits-mcp, knowing its tools. Each question's "
        "interpretation is recorded in `eval/questions.toml`.",
        "- Status and type semantics are judgment calls; the interpretation notes say which call each "
        "ground-truth query makes.",
        "- Ground truth is recomputed from the live portals right before each run, and the arms run back to back.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", nargs="?", help="run to grade (default: the most recent)")
    parser.add_argument("--selfcheck", action="store_true", help="grade oracle, null and near-miss answers")
    parser.add_argument("--ground-truth", type=Path, help="override the snapshot recorded in the run")
    args = parser.parse_args(argv)
    questions = {q.id: q for q in load_questions()}

    if args.selfcheck:
        snapshot = read_json(args.ground_truth or latest_ground_truth())
        problems = selfcheck(list(questions.values()), snapshot)
        print(
            "\n".join(problems)
            or f"Self-check passed: all {len(questions)} reference answers pass; "
            "empty, non-answers and near misses all fail."
        )
        return 1 if problems else 0

    runs = sorted(p for p in RUNS_DIR.glob("*") if (p / "meta.json").exists()) if RUNS_DIR.exists() else []
    if args.run_id:
        run_dir = RUNS_DIR / args.run_id
    elif runs:
        run_dir = runs[-1]
    else:
        raise SystemExit("No runs yet. Run: uv run --group eval python -m eval.run --pilot")
    meta = read_json(run_dir / "meta.json")
    gt_path = args.ground_truth or (RESULTS_DIR / "ground_truth" / meta["ground_truth"]["file"])
    snapshot = read_json(gt_path)
    notes = tomllib.loads(NOTES_FILE.read_text(encoding="utf-8")) if NOTES_FILE.exists() else {}
    graded = grade_rows(_read_jsonl(run_dir / "results.jsonl"), questions, snapshot, notes)
    errors = _read_jsonl(run_dir / "errors.jsonl")
    report = build_report(run_dir, meta, graded, errors, questions, snapshot)
    (run_dir / "results.md").write_text(report, encoding="utf-8")
    if not meta.get("pilot"):
        latest = report.replace("](transcripts/", f"](runs/{run_dir.name}/transcripts/")
        (RESULTS_DIR / "results.md").write_text(latest, encoding="utf-8")
    (run_dir / "graded.jsonl").write_text("".join(json.dumps(g, default=str) + "\n" for g in graded), encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
