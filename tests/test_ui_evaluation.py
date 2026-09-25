"""The evaluation summary must not leak a DSN password into the rendered table.

`evaluate_cases` puts `result.error` straight into a DataFrame row that
`render_evaluation` hands to `st.dataframe`. A PostgreSQL driver error commonly
echoes the DSN it failed to reach, including its password (Phase 3b), so that
value must already be redacted by the time it reaches the DataFrame - there is
no further transform between here and the page.
"""

from __future__ import annotations

from unittest.mock import patch

import ui.evaluation as evaluation
from text_to_sql_agent import CaseScore, QueryResult, SchemaRetrievalResult


def _case() -> dict:
    return {
        "id": "case-1",
        "dataset": "postgres_demo",
        "difficulty": "easy",
        "question": "how many customers?",
        "db_path": "postgresql://u:hunter2@db.example.com:5432/prod",
        "expected_tables": ["customers"],
    }


def test_evaluate_cases_redacts_a_dsn_password_in_the_error_column() -> None:
    case = _case()
    gold_result = QueryResult(
        columns=[],
        rows=[],
        sql=None,
        error="could not connect to postgresql://u:hunter2@db.example.com:5432/prod: timed out",
    )
    with (
        patch("ui.evaluation.backend.load_cases", return_value=[case]),
        patch("ui.evaluation.backend.run_gold", return_value=("SELECT 1", gold_result)),
        patch(
            "ui.evaluation.backend.score_case",
            return_value=CaseScore(
                executed=False, row_match=False, value_match=False, exact_match=False
            ),
        ),
        patch("ui.evaluation.backend.open_engine", return_value=object()),
        patch("ui.evaluation.backend.is_safe_query", return_value=True),
        patch(
            "ui.evaluation.backend.retrieve_schema_context",
            return_value=SchemaRetrievalResult(
                chunks=[], query_tokens=[], expanded_tokens=[], top_k=3
            ),
        ),
    ):
        df = evaluation.evaluate_cases(
            mode="Gold SQL baseline",
            provider="gemini",
            model_name="gemini-2.5-flash",
            use_rag=False,
            rag_top_k=3,
        )

    error_value = df.loc[0, "error"]
    assert "hunter2" not in error_value
    assert "***" in error_value
