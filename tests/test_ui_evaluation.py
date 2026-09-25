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
from text_to_sql_agent import CaseScore, QueryResult, SchemaChunk, SchemaRetrievalResult


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


def test_schema_recall_keys_retrieved_tables_by_qualified_name() -> None:
    """Minor review finding (2026-09-26): `retrieved_tables` used to be
    `{chunk.table_name for chunk in rag_context.chunks}` - the bare name,
    with no schema. On a multi-schema database, a retrieved `analytics.
    customers` chunk and an expected bare `customers` table would spuriously
    match (both key to `"customers"`), and two retrieved chunks that are
    genuinely different tables in different schemas (`public.customers` and
    `analytics.customers`) would collapse into one entry, understating how
    many distinct tables were actually retrieved.

    `qualified_name` fixes this and is a no-op for every case in
    `evaluation/cases.json` today - all single-schema - which this test
    proves in the other direction: a chunk with a non-default `schema_name`
    must be counted under its qualified spelling, not merged with a
    same-named default-schema table.
    """
    case = {
        "id": "case-1",
        "dataset": "multi_schema_demo",
        "difficulty": "easy",
        "question": "how many customers per schema?",
        "db_path": "some.db",
        "expected_tables": ["customers", "analytics.customers"],
    }
    gold_result = QueryResult(columns=[], rows=[], sql="SELECT 1", error=None)
    default_schema_chunk = SchemaChunk(
        table_name="customers",
        ddl="CREATE TABLE customers (id INTEGER);",
        columns=["id"],
        foreign_tables=[],
        search_text="customers",
    )
    other_schema_chunk = SchemaChunk(
        table_name="customers",
        ddl="CREATE TABLE analytics.customers (id INTEGER);",
        columns=["id"],
        foreign_tables=[],
        search_text="analytics.customers",
        schema_name="analytics",
    )
    with (
        patch("ui.evaluation.backend.load_cases", return_value=[case]),
        patch("ui.evaluation.backend.run_gold", return_value=("SELECT 1", gold_result)),
        patch(
            "ui.evaluation.backend.score_case",
            return_value=CaseScore(
                executed=True, row_match=True, value_match=True, exact_match=True
            ),
        ),
        patch("ui.evaluation.backend.open_engine", return_value=object()),
        patch("ui.evaluation.backend.is_safe_query", return_value=True),
        patch("ui.evaluation.backend.redact_dsn", side_effect=lambda s: s),
        patch(
            "ui.evaluation.backend.retrieve_schema_context",
            return_value=SchemaRetrievalResult(
                chunks=[default_schema_chunk, other_schema_chunk],
                query_tokens=[],
                expanded_tokens=[],
                top_k=3,
            ),
        ),
    ):
        df = evaluation.evaluate_cases(
            mode="Gold SQL baseline",
            provider="gemini",
            model_name="gemini-2.5-flash",
            use_rag=True,
            rag_top_k=3,
        )

    retrieved = df.loc[0, "retrieved_tables"]
    assert retrieved == "analytics.customers, customers"
    # Both expected tables were actually retrieved, distinctly.
    assert df.loc[0, "schema_recall"] == 1.0
