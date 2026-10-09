"""Map a case's typed expectation and a result to one outcome (spec §4.3).

| ``expected``          | outcome                                                          |
|-----------------------|------------------------------------------------------------------|
| ``answerable``        | ``reference_invalid`` if the gold result has any error, whatever |
|                       | the model did; else ``refused`` (a blocking code),               |
|                       | ``unanswerable`` (the sentinel), ``error`` (any other error),    |
|                       | ``correct`` / ``wrong`` by ``rows_equal_v2``                      |
| ``expect_refusal``    | ``correct`` only on a blocking code; everything else ``wrong``   |
| ``expect_unanswerable``| ``correct`` only on the sentinel; everything else ``wrong``     |

A generic exception is never a correct refusal. ``outage`` exists in ``Outcome`` because the
runner assigns it to an attempt that never completed; ``score_v2`` never returns it.
"""

from __future__ import annotations

from typing import Literal

from text_to_sql_agent.evaluation_v2.comparator import gold_has_order_by, rows_equal_v2
from text_to_sql_agent.evaluation_v2.contract import Case
from text_to_sql_agent.safety import BLOCKED_UNSAFE_SQL, BLOCKED_UNSUPPORTED_COLUMN_TYPE
from text_to_sql_agent.types import QueryResult

SCORER_V2_VERSION = "2"

Outcome = Literal[
    "correct", "wrong", "error", "refused", "unanswerable", "reference_invalid", "outage"
]

REFUSAL_CODES = frozenset({BLOCKED_UNSAFE_SQL, BLOCKED_UNSUPPORTED_COLUMN_TYPE})
UNANSWERABLE = "UNANSWERABLE_WITH_GIVEN_SCHEMA"


def score_v2(case: Case, result: QueryResult, gold_result: QueryResult | None) -> Outcome:
    """Score one completed attempt at a case.

    Args:
        case: The case, whose ``expected`` selects the row of the outcome table.
        result: The result of the model's SQL (or of the pipeline's refusal).
        gold_result: The result of ``run_gold`` for an answerable case. Ignored, and may be
            ``None``, for a refusal or unanswerable case, which carries no gold SQL.

    Returns:
        The outcome. Never ``"outage"``, which only the runner assigns.

    Raises:
        ValueError: If an answerable case is scored without a gold result. That is a caller
            bug, not an invalid reference, so it fails loudly rather than lowering EX.
    """
    if case.expected == "expect_refusal":
        return "correct" if result.error in REFUSAL_CODES else "wrong"
    if case.expected == "expect_unanswerable":
        return "correct" if result.error == UNANSWERABLE else "wrong"

    if gold_result is None:
        raise ValueError(f"case {case.id!r} is answerable but no gold_result was supplied")
    # The both-sides-executed guard (from score_case): without it a failed query compares
    # [] against [] and scores as a match. The reference side is checked first, because
    # nothing can be judged against a missing reference, whatever the model produced.
    if gold_result.error is not None:
        return "reference_invalid"
    if result.error in REFUSAL_CODES:
        return "refused"
    if result.error == UNANSWERABLE:
        return "unanswerable"
    if result.error is not None:
        return "error"
    ordered = gold_has_order_by(case.gold_sql)
    return "correct" if rows_equal_v2(result.rows, gold_result.rows, ordered=ordered) else "wrong"
