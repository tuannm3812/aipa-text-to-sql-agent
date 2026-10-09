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
from text_to_sql_agent.evaluation_v2.identity import (
    IdentityPayload,
    allocate_run_dir,
    identity_hash,
    sanitise,
)
from text_to_sql_agent.evaluation_v2.manifest import Manifest
from text_to_sql_agent.evaluation_v2.runner import (
    CSV_COLUMNS,
    ResumeRefused,
    RunConfig,
    RunResult,
    run_suite,
    select_cases,
)
from text_to_sql_agent.evaluation_v2.scoring import (
    EMPTY_GENERATED_SQL,
    REFUSAL_CODES,
    SCORER_V2_VERSION,
    UNANSWERABLE,
    Outcome,
    score_v2,
)
from text_to_sql_agent.evaluation_v2.stats import (
    Interval,
    bootstrap_ci,
    bootstrap_mean_ci,
    paired_bootstrap_ci,
)

# Later tasks add their own names here.
__all__ = [
    "CSV_COLUMNS",
    "EMPTY_GENERATED_SQL",
    "REFUSAL_CODES",
    "SCORER_V2_VERSION",
    "UNANSWERABLE",
    "Case",
    "Expected",
    "Hardness",
    "IdentityPayload",
    "Interval",
    "Manifest",
    "Outcome",
    "ResumeRefused",
    "RunConfig",
    "RunResult",
    "SuiteError",
    "allocate_run_dir",
    "bootstrap_ci",
    "bootstrap_mean_ci",
    "gold_has_order_by",
    "identity_hash",
    "load_suite",
    "paired_bootstrap_ci",
    "rows_equal_v2",
    "run_suite",
    "sanitise",
    "score_v2",
    "select_cases",
    "suite_sha256",
]
