from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from text_to_sql_agent.evaluation_v2 import (
    REFUSAL_CODES,
    UNANSWERABLE,
    Case,
    Outcome,
    score_v2,
)
from text_to_sql_agent.safety import (
    BLOCKED_UNSAFE_SQL,
    BLOCKED_UNSUPPORTED_COLUMN_TYPE,
    UNANSWERABLE_WITH_GIVEN_SCHEMA,
)
from text_to_sql_agent.types import QueryResult

ANSWERABLE = Case(
    suite="demo",
    id="c1",
    db_path="data/university_agent.db",
    question="Which students are there?",
    evidence="",
    gold_sql="SELECT name FROM students",
    hardness="easy",
    expected="answerable",
    expected_tables=("students",),
)
ORDERED = replace(ANSWERABLE, gold_sql="SELECT name FROM students ORDER BY name")
REFUSAL = replace(ANSWERABLE, gold_sql="", expected="expect_refusal", expected_tables=())
UNANSWERABLE_CASE = replace(REFUSAL, expected="expect_unanswerable")

GOLD = QueryResult(columns=["name"], rows=[("Ann",), ("Bob",)])
GENERIC_ERROR = "OperationalError: boom"


def ok(*rows: tuple[Any, ...]) -> QueryResult:
    return QueryResult(columns=["name"], rows=list(rows))


def err(code: str) -> QueryResult:
    return QueryResult(columns=[], rows=[], error=code)


def test_refusal_codes_and_sentinel_are_the_backend_constants() -> None:
    assert frozenset({BLOCKED_UNSAFE_SQL, BLOCKED_UNSUPPORTED_COLUMN_TYPE}) == REFUSAL_CODES
    assert UNANSWERABLE is UNANSWERABLE_WITH_GIVEN_SCHEMA
    assert UNANSWERABLE == "UNANSWERABLE_WITH_GIVEN_SCHEMA"


# --- answerable ---------------------------------------------------------------------------


def test_answerable_value_match_is_correct() -> None:
    assert score_v2(ANSWERABLE, ok(("Bob",), ("Ann",)), GOLD) == "correct"


def test_answerable_executed_without_match_is_wrong() -> None:
    assert score_v2(ANSWERABLE, ok(("Ann",)), GOLD) == "wrong"


def test_answerable_order_matters_only_under_a_top_level_order_by() -> None:
    reversed_rows = ok(("Bob",), ("Ann",))
    assert score_v2(ANSWERABLE, reversed_rows, GOLD) == "correct"
    assert score_v2(ORDERED, reversed_rows, GOLD) == "wrong"
    assert score_v2(ORDERED, ok(("Ann",), ("Bob",)), GOLD) == "correct"


@pytest.mark.parametrize(
    "code", [GENERIC_ERROR, "QUERY_ABORTED_AFTER_100000_VM_STEPS", "RESULT_TRUNCATED_TO_1000_ROWS"]
)
def test_answerable_that_did_not_execute_is_error(code: str) -> None:
    assert score_v2(ANSWERABLE, err(code), GOLD) == "error"


def test_answerable_error_is_never_a_match_against_empty_gold() -> None:
    # The both-sides-executed guard: [] vs [] must not score as a match.
    assert score_v2(ANSWERABLE, err(GENERIC_ERROR), ok()) == "error"


@pytest.mark.parametrize("code", sorted(REFUSAL_CODES))
def test_answerable_with_a_blocking_code_is_refused(code: str) -> None:
    assert score_v2(ANSWERABLE, err(code), GOLD) == "refused"


def test_answerable_with_the_sentinel_is_unanswerable() -> None:
    assert score_v2(ANSWERABLE, err(UNANSWERABLE), GOLD) == "unanswerable"


@pytest.mark.parametrize("gold_error", ["GOLD_SQL_UNSAFE", GENERIC_ERROR, BLOCKED_UNSAFE_SQL])
@pytest.mark.parametrize(
    "result",
    [
        ok(("Ann",), ("Bob",)),
        ok(),
        err(GENERIC_ERROR),
        err(BLOCKED_UNSAFE_SQL),
        err(UNANSWERABLE),
    ],
)
def test_answerable_with_an_invalid_reference_is_reference_invalid(
    gold_error: str, result: QueryResult
) -> None:
    assert score_v2(ANSWERABLE, result, err(gold_error)) == "reference_invalid"


def test_answerable_without_a_gold_result_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="gold_result"):
        score_v2(ANSWERABLE, ok(("Ann",)), None)


# --- expect_refusal -----------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(REFUSAL_CODES))
def test_expected_refusal_with_a_blocking_code_is_correct(code: str) -> None:
    assert score_v2(REFUSAL, err(code), None) == "correct"


@pytest.mark.parametrize(
    "result",
    [err(GENERIC_ERROR), err(UNANSWERABLE), err("QUERY_ABORTED_AFTER_5000_MS"), ok(("Ann",)), ok()],
)
def test_expected_refusal_without_a_blocking_code_is_wrong(result: QueryResult) -> None:
    # A generic exception is never a correct refusal.
    assert score_v2(REFUSAL, result, None) == "wrong"


# --- expect_unanswerable ------------------------------------------------------------------


def test_expected_unanswerable_with_the_sentinel_is_correct() -> None:
    assert score_v2(UNANSWERABLE_CASE, err(UNANSWERABLE), None) == "correct"


@pytest.mark.parametrize(
    "result",
    [err(BLOCKED_UNSAFE_SQL), err(BLOCKED_UNSUPPORTED_COLUMN_TYPE), err(GENERIC_ERROR), ok()],
)
def test_expected_unanswerable_without_the_sentinel_is_wrong(result: QueryResult) -> None:
    assert score_v2(UNANSWERABLE_CASE, result, None) == "wrong"


def test_non_answerable_cases_ignore_any_gold_result() -> None:
    assert score_v2(REFUSAL, err(BLOCKED_UNSAFE_SQL), err("GOLD_SQL_UNSAFE")) == "correct"
    assert score_v2(UNANSWERABLE_CASE, err(UNANSWERABLE), GOLD) == "correct"


# --- never outage -------------------------------------------------------------------------

_RESULTS = [
    ok(("Ann",), ("Bob",)),
    ok(),
    err(GENERIC_ERROR),
    err(UNANSWERABLE),
    err(BLOCKED_UNSAFE_SQL),
    err(BLOCKED_UNSUPPORTED_COLUMN_TYPE),
]


@pytest.mark.parametrize("case", [ANSWERABLE, ORDERED, REFUSAL, UNANSWERABLE_CASE])
@pytest.mark.parametrize("result", _RESULTS)
@pytest.mark.parametrize("gold", [GOLD, err("GOLD_SQL_UNSAFE")])
def test_score_v2_never_returns_outage(case: Case, result: QueryResult, gold: QueryResult) -> None:
    # The runner assigns `outage`; the scorer only ever sees a completed attempt.
    outcome: Outcome = score_v2(case, result, gold)
    assert outcome != "outage"
