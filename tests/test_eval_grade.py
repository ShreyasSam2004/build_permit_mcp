"""The eval's grader: deterministic parsing, tolerance, and that wrong answers can't pass."""

from __future__ import annotations

import pytest

from eval.common import load_questions
from eval.grade import grade_answer, near_miss, parse_month, parse_number, render_expected, selfcheck


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1234", 1234),
        ("1,234 permits", 1234),
        ("$1,323,118,235.74", 1323118235.74),
        ("1.32 billion", 1.32e9),
        ("about 5.2M", 5.2e6),
        ("Austin had 17104", 17104),
    ],
)
def test_parse_number(text: str, value: float) -> None:
    assert parse_number(text) == pytest.approx(value)


@pytest.mark.parametrize(
    ("text", "month"),
    [
        ("2024-10", "2024-10"),
        ("October 2024", "2024-10"),
        ("Apr 2024", "2024-04"),
        ("2024-4-01", "2024-04"),
        ("the busiest", None),
    ],
)
def test_parse_month(text: str, month: str | None) -> None:
    assert parse_month(text) == month


def test_integer_is_exact_and_number_has_half_percent_tolerance() -> None:
    assert grade_answer("integer", 5924, "5924").correct
    assert not grade_answer("integer", 5924, "5925").correct
    assert grade_answer("number", 1_000_000.0, "1004000").correct
    assert not grade_answer("number", 1_000_000.0, "1006000").correct


def test_ids_are_a_set_and_gaps_are_classified() -> None:
    assert grade_answer("ids", ["101", "102"], "102, 101").correct
    assert grade_answer("ids", ["101", "102"], "101").failure_class == "wrong value"
    assert grade_answer("integer", 5, "UNAVAILABLE").failure_class == "hallucinated gap"
    assert grade_answer("unavailable", "UNAVAILABLE", "0").failure_class == "missed gap"
    assert grade_answer("unavailable", "UNAVAILABLE", "UNAVAILABLE").correct
    assert grade_answer("integer", 5, None).failure_class == "no answer"


def test_city_label_must_name_exactly_one_city() -> None:
    assert grade_answer("label", "austin", "Austin, TX", "city").correct
    assert not grade_answer("label", "austin", "Austin or Chicago", "city").correct


def test_final_answer_line_is_parsed_from_the_last_occurrence() -> None:
    pytest.importorskip("anthropic", reason="install the eval group: uv sync --group eval")
    from eval.harness import parse_final_answer

    text = "Draft: FINAL ANSWER: 3\nChecked again.\n**FINAL ANSWER:** 4"
    assert parse_final_answer(text) == "4"
    assert parse_final_answer("no answer here") is None


def test_selfcheck_on_every_question_shape() -> None:
    # Synthetic expected answers of each question's type: the reference must pass, null and near misses must fail.
    questions = load_questions()
    fake = {"integer": 42, "number": 1234.5, "ids": ["A1", "B2"], "unavailable": "UNAVAILABLE"}
    snapshot = {"questions": {}}
    for q in questions:
        expected = fake.get(q.answer_type) or ("2024-12" if q.label_format == "month" else "seattle")
        snapshot["questions"][q.id] = {"expected": expected}
        assert render_expected(q.answer_type, expected, q.label_format) != near_miss(
            q.answer_type, expected, q.label_format
        )
    assert selfcheck(questions, snapshot) == []
