"""The evaluation script must not write a DSN password to its results CSV.

`evaluate_case` puts `result.error` straight into the row dict `main` writes
with `csv.DictWriter`, and `--out-dir` defaults to `evaluation/results`, a
git-tracked directory. A PostgreSQL driver error commonly echoes the DSN it
failed to reach, including its password (Phase 3b), so that value must
already be redacted by the time it lands in the row - there is no further
transform between here and the file. Every test here points `--out-dir` at a
`tmp_path`; none writes to `evaluation/results/`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import scripts.evaluate_text_to_sql as script
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


def test_evaluate_case_redacts_a_dsn_password_in_the_error_field() -> None:
    case = _case()
    gold_result = QueryResult(
        columns=[],
        rows=[],
        sql=None,
        error="could not connect to postgresql://u:hunter2@db.example.com:5432/prod: timed out",
    )
    with (
        patch(
            "scripts.evaluate_text_to_sql.agent.run_gold", return_value=("SELECT 1", gold_result)
        ),
        patch(
            "scripts.evaluate_text_to_sql.agent.score_case",
            return_value=CaseScore(
                executed=False, row_match=False, value_match=False, exact_match=False
            ),
        ),
        patch("scripts.evaluate_text_to_sql.agent.open_engine", return_value=object()),
        patch("scripts.evaluate_text_to_sql.agent.is_safe_query", return_value=True),
        patch(
            "scripts.evaluate_text_to_sql.agent.retrieve_schema_context",
            return_value=SchemaRetrievalResult(
                chunks=[], query_tokens=[], expanded_tokens=[], top_k=3
            ),
        ),
    ):
        row = script.evaluate_case(
            case,
            mode="gold",
            provider="gemini",
            model_name="gemini-2.5-flash",
            use_rag=False,
            rag_top_k=3,
        )

    assert "hunter2" not in row["error"]
    assert "***" in row["error"]


def test_main_does_not_write_a_dsn_password_to_the_results_csv(tmp_path: Path) -> None:
    case = _case()
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([case]), encoding="utf-8")
    out_dir = tmp_path / "results"

    gold_result = QueryResult(
        columns=[],
        rows=[],
        sql=None,
        error="could not connect to postgresql://u:hunter2@db.example.com:5432/prod: timed out",
    )
    argv = [
        "evaluate_text_to_sql.py",
        "--cases",
        str(cases_path),
        "--mode",
        "gold",
        "--out-dir",
        str(out_dir),
    ]
    with (
        patch.object(sys, "argv", argv),
        patch(
            "scripts.evaluate_text_to_sql.agent.run_gold", return_value=("SELECT 1", gold_result)
        ),
        patch(
            "scripts.evaluate_text_to_sql.agent.score_case",
            return_value=CaseScore(
                executed=False, row_match=False, value_match=False, exact_match=False
            ),
        ),
        patch("scripts.evaluate_text_to_sql.agent.open_engine", return_value=object()),
        patch("scripts.evaluate_text_to_sql.agent.is_safe_query", return_value=True),
        patch(
            "scripts.evaluate_text_to_sql.agent.retrieve_schema_context",
            return_value=SchemaRetrievalResult(
                chunks=[], query_tokens=[], expanded_tokens=[], top_k=3
            ),
        ),
    ):
        script.main()

    csv_path = out_dir / "evaluation_gold.csv"
    assert csv_path.exists()
    csv_text = csv_path.read_text(encoding="utf-8")
    assert "hunter2" not in csv_text
    assert "***" in csv_text

    md_text = (out_dir / "evaluation_gold.md").read_text(encoding="utf-8")
    assert "hunter2" not in md_text
