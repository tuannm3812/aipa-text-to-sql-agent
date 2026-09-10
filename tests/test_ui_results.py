from __future__ import annotations

import pytest

from ui import results

KNOWN_CODES = [
    "BLOCKED_UNSAFE_SQL",
    "UNANSWERABLE_WITH_GIVEN_SCHEMA",
    "RESULT_TRUNCATED_TO_1000_ROWS",
    "QUERY_ABORTED_AFTER_100000_VM_STEPS",
]


@pytest.mark.parametrize("code", KNOWN_CODES)
def test_describe_error_returns_a_human_message(code: str) -> None:
    message = results.describe_error(code)
    assert message != code, f"{code} is still shown raw to the user"
    assert message[0].isupper()
    assert len(message) > 20


def test_describe_error_includes_the_row_cap_in_the_truncation_message() -> None:
    assert "1000" in results.describe_error("RESULT_TRUNCATED_TO_1000_ROWS")


def test_describe_error_includes_the_step_budget_in_the_abort_message() -> None:
    assert "100000" in results.describe_error("QUERY_ABORTED_AFTER_100000_VM_STEPS")


def test_describe_error_passes_through_an_unrecognised_message() -> None:
    assert results.describe_error("RuntimeError: boom") == "RuntimeError: boom"


def test_describe_error_handles_none() -> None:
    assert results.describe_error(None) == ""
