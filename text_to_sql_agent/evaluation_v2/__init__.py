"""Evaluation contract v2: typed cases, scoring and run manifests.

Sibling of the legacy ``text_to_sql_agent.evaluation`` module, which is left unchanged.
"""

from text_to_sql_agent.evaluation_v2.comparator import gold_has_order_by, rows_equal_v2
from text_to_sql_agent.evaluation_v2.contract import (
    Case,
    Expected,
    Hardness,
    SuiteError,
    load_suite,
    suite_sha256,
)
from text_to_sql_agent.evaluation_v2.scoring import (
    REFUSAL_CODES,
    SCORER_V2_VERSION,
    UNANSWERABLE,
    Outcome,
    score_v2,
)

# Later tasks add their own names here.
__all__ = [
    "REFUSAL_CODES",
    "SCORER_V2_VERSION",
    "UNANSWERABLE",
    "Case",
    "Expected",
    "Hardness",
    "Outcome",
    "SuiteError",
    "gold_has_order_by",
    "load_suite",
    "rows_equal_v2",
    "score_v2",
    "suite_sha256",
]
