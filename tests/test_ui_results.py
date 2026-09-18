from __future__ import annotations

import pytest

from ui import results

KNOWN_CODES = [
    "BLOCKED_UNSAFE_SQL",
    "UNANSWERABLE_WITH_GIVEN_SCHEMA",
    "RESULT_TRUNCATED_TO_1000_ROWS",
    "QUERY_ABORTED_AFTER_100000_VM_STEPS",
    "QUERY_ABORTED_AFTER_5000_MS",
]


@pytest.mark.parametrize("code", KNOWN_CODES)
def test_describe_error_returns_a_human_message(code: str) -> None:
    message = results.describe_error(code)
    assert message != code, f"{code} is still shown raw to the user"
    assert message[0].isupper()
    assert len(message) > 20


def test_describe_error_mentions_read_only_for_blocked_sql() -> None:
    assert "read-only" in results.describe_error("BLOCKED_UNSAFE_SQL")


def test_describe_error_mentions_schema_for_unanswerable() -> None:
    assert "schema" in results.describe_error("UNANSWERABLE_WITH_GIVEN_SCHEMA")


def test_describe_error_includes_the_row_cap_in_the_truncation_message() -> None:
    assert "1000" in results.describe_error("RESULT_TRUNCATED_TO_1000_ROWS")


def test_describe_error_includes_the_step_budget_in_the_abort_message() -> None:
    assert "100000" in results.describe_error("QUERY_ABORTED_AFTER_100000_VM_STEPS")


def test_describe_error_passes_through_an_unrecognised_message() -> None:
    assert results.describe_error("RuntimeError: boom") == "RuntimeError: boom"


def test_describe_error_handles_none() -> None:
    assert results.describe_error(None) == ""


def test_describe_error_reports_duckdb_abort_in_ms_not_steps() -> None:
    message = results.describe_error("QUERY_ABORTED_AFTER_5000_MS")
    assert "5000 ms" in message
    assert "steps" not in message


def test_describe_error_redacts_a_dsn_password_in_raw_exception_text() -> None:
    raw = "could not connect to postgresql://u:hunter2@host/db"
    assert "hunter2" not in results.describe_error(raw)
