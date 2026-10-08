"""Evaluation contract v2: typed cases, scoring and run manifests.

Sibling of the legacy ``text_to_sql_agent.evaluation`` module, which is left unchanged.
"""

from text_to_sql_agent.evaluation_v2.contract import (
    Case,
    Expected,
    Hardness,
    SuiteError,
    load_suite,
    suite_sha256,
)

# Later tasks add their own names here (Outcome, score_v2, rows_equal_v2, ...).
__all__ = ["Case", "Expected", "Hardness", "SuiteError", "load_suite", "suite_sha256"]
