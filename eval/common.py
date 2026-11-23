"""Shared pieces of the eval: paths, the city map and question loading.

Deliberately independent of ``permits_mcp``: ground truth is written against raw Socrata
columns, so nothing here may reuse the server's mappings.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

EVAL_DIR = Path(__file__).resolve().parent
REPO_DIR = EVAL_DIR.parent
QUESTIONS_FILE = EVAL_DIR / "questions.toml"
RESULTS_DIR = EVAL_DIR / "results"
GROUND_TRUTH_DIR = RESULTS_DIR / "ground_truth"
RUNS_DIR = RESULTS_DIR / "runs"

AnswerType = Literal["integer", "number", "ids", "label", "unavailable"]
ANSWER_TYPES: tuple[str, ...] = ("integer", "number", "ids", "label", "unavailable")
UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class City:
    id: str
    name: str
    domain: str
    dataset_id: str
    timezone: str


# Written out here rather than read from src/permits_mcp/cities/*.toml, to keep the ground truth independent.
CITIES: dict[str, City] = {
    "chicago": City("chicago", "Chicago", "data.cityofchicago.org", "ydr8-5enu", "America/Chicago"),
    "seattle": City("seattle", "Seattle", "data.seattle.gov", "76t5-zqzr", "America/Los_Angeles"),
    "austin": City("austin", "Austin", "data.austintexas.gov", "3syk-w9eu", "America/Chicago"),
}


@dataclass(frozen=True)
class Query:
    city: str
    select: str
    where: str
    group: str | None = None
    order: str | None = None
    limit: str | None = None


@dataclass(frozen=True)
class Question:
    id: str
    cities: tuple[str, ...]
    category: str
    answer_type: AnswerType
    question: str
    interpretation: str
    queries: tuple[Query, ...] = ()
    combine: str = "single"
    label_format: str | None = None


def load_questions(path: Path = QUESTIONS_FILE) -> list[Question]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    questions: list[Question] = []
    seen: set[str] = set()
    for raw in data["question"]:
        qid = raw["id"]
        if qid in seen:
            raise ValueError(f"duplicate question id {qid}")
        seen.add(qid)
        answer_type = raw["answer_type"]
        if answer_type not in ANSWER_TYPES:
            raise ValueError(f"{qid}: unknown answer_type {answer_type!r}")
        queries = tuple(Query(**q) for q in raw.get("query", []))
        for q in queries:
            if q.city not in CITIES:
                raise ValueError(f"{qid}: unknown city {q.city!r}")
        if (answer_type == "unavailable") != (not queries):
            raise ValueError(f"{qid}: 'unavailable' questions have no query; every other type needs one")
        combine = raw.get("combine", "single")
        if combine not in ("single", "sum", "argmax_city"):
            raise ValueError(f"{qid}: unknown combine {combine!r}")
        if combine == "single" and len(queries) > 1:
            raise ValueError(f"{qid}: several queries need combine = 'sum' or 'argmax_city'")
        if answer_type == "label" and raw.get("label_format") not in ("month", "city"):
            raise ValueError(f"{qid}: label questions need label_format 'month' or 'city'")
        questions.append(
            Question(
                id=qid,
                cities=tuple(raw["cities"]),
                category=raw["category"],
                answer_type=answer_type,
                question=raw["question"].strip(),
                interpretation=raw["interpretation"].strip(),
                queries=queries,
                combine=combine,
                label_format=raw.get("label_format"),
            )
        )
    return questions


def snapshot_path(computed_at: str) -> Path:
    """The immutable file for a snapshot ('2026-11-23T10:46:11+00:00' -> 2026-11-23T104611Z.json)."""
    return GROUND_TRUTH_DIR / f"{computed_at.replace(':', '').replace('+0000', 'Z')}.json"


def latest_ground_truth() -> Path:
    path = GROUND_TRUTH_DIR / "latest.json"
    if not path.exists():
        raise SystemExit("No ground truth snapshot yet. Run: uv run --group eval python -m eval.ground_truth")
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
