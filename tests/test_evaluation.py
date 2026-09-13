from __future__ import annotations

import json
from pathlib import Path

from text_to_sql_agent import evaluation
from text_to_sql_agent.types import QueryResult


def test_canonical_value_rounds_numbers_to_two_decimals() -> None:
    assert evaluation.canonical_value(10) == "10.0"
    assert evaluation.canonical_value(10.0) == "10.0"
    assert evaluation.canonical_value(10.004) == "10.0"
    assert evaluation.canonical_value("10.004") == "10.0"


def test_canonical_value_lowercases_and_strips_text() -> None:
    assert evaluation.canonical_value("  Business  ") == "business"


def test_rows_match_ignores_row_order() -> None:
    assert evaluation.rows_match([("b", 2), ("a", 1)], [("a", 1), ("b", 2)])


def test_rows_match_ignores_row_order_when_gold_is_unsorted() -> None:
    assert evaluation.rows_match([("a", 1), ("b", 2)], [("b", 2), ("a", 1)])


def test_rows_match_ignores_numeric_formatting() -> None:
    assert evaluation.rows_match([("a", 1)], [("a", 1.0)])
    assert evaluation.rows_match([("a", "1.00")], [("a", 1)])


def test_rows_match_rejects_different_values() -> None:
    assert not evaluation.rows_match([("a", 1)], [("a", 2)])


def test_rows_match_rejects_different_row_counts() -> None:
    assert not evaluation.rows_match([("a", 1)], [("a", 1), ("b", 2)])


def test_rows_match_is_true_for_two_empty_results() -> None:
    assert evaluation.rows_match([], [])


def test_rows_match_rejects_a_duplicated_row_against_a_single_row() -> None:
    assert not evaluation.rows_match([("a", 1), ("a", 1)], [("a", 1)])


def test_rows_match_rejects_a_single_row_against_a_duplicated_row() -> None:
    assert not evaluation.rows_match([("a", 1)], [("a", 1), ("a", 1)])


def test_rows_match_accepts_matching_duplicate_rows_on_both_sides() -> None:
    assert evaluation.rows_match([("a", 1), ("a", 1)], [("a", 1), ("a", 1)])


def test_canonical_value_treats_booleans_as_their_integer_value() -> None:
    assert evaluation.canonical_value(True) == evaluation.canonical_value(1)
    assert evaluation.canonical_value(False) == evaluation.canonical_value(0)


def test_normalise_rows_stringifies_every_cell() -> None:
    assert evaluation.normalise_rows([(1, "a", None)]) == [["1", "a", "None"]]


def test_load_cases_returns_empty_list_when_file_is_missing(tmp_path: Path) -> None:
    assert evaluation.load_cases(tmp_path / "nope.json") == []


def test_load_cases_parses_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{"question": "q", "gold_sql": "SELECT 1"}]), encoding="utf-8")
    assert evaluation.load_cases(path) == [{"question": "q", "gold_sql": "SELECT 1"}]


def test_shipped_cases_file_loads_and_has_the_required_keys() -> None:
    cases = evaluation.load_cases()
    assert cases, "evaluation/cases.json should not be empty"
    for case in cases:
        assert {"question", "gold_sql", "db_path"} <= set(case)


def _ok(columns: list[str], rows: list[tuple[object, ...]]) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, sql="SELECT 1")


def _failed(error: str) -> QueryResult:
    """A query that errored: the backend returns no rows alongside the code."""
    return QueryResult(columns=[], rows=[], sql="SELECT 1", error=error)


def test_score_case_scores_an_ordinary_match() -> None:
    score = evaluation.score_case(_ok(["a"], [("x",)]), _ok(["a"], [("x",)]))
    assert (score.executed, score.row_match, score.value_match, score.exact_match) == (
        True,
        True,
        True,
        True,
    )


def test_score_case_rejects_a_genuine_mismatch() -> None:
    score = evaluation.score_case(_ok(["a"], [("x",)]), _ok(["a"], [("y",)]))
    assert score.executed
    assert not score.row_match
    assert not score.value_match
    assert not score.exact_match


def test_score_case_does_not_credit_an_aborted_query() -> None:
    """Two failed queries both return no rows; `[] == []` must not be a match.

    This is the regression that mattered: without the both-succeeded guard an
    aborted query scored as a perfect match and inflated reported accuracy.
    """
    aborted = _failed("QUERY_ABORTED_AFTER_100000_VM_STEPS")
    score = evaluation.score_case(aborted, aborted)

    assert not score.executed
    assert not score.row_match
    assert not score.value_match
    assert not score.exact_match


def test_score_case_does_not_credit_a_blocked_query() -> None:
    score = evaluation.score_case(_failed("BLOCKED_UNSAFE_SQL"), _ok(["a"], []))
    assert not score.executed
    assert not score.row_match
    assert not score.value_match


def test_score_case_does_not_credit_a_failed_gold_side() -> None:
    """A succeeding generated query cannot match a gold query that failed."""
    score = evaluation.score_case(_ok(["a"], []), _failed("QUERY_ABORTED_AFTER_100000_VM_STEPS"))
    assert score.executed, "the generated side did execute"
    assert not score.row_match
    assert not score.value_match


def test_score_case_treats_a_truncated_result_as_not_executed() -> None:
    """Truncation sets `error` while returning rows; both harnesses have always
    reported that as not-executed, and the match fields follow the same guard."""
    truncated = QueryResult(
        columns=["a"], rows=[("x",)], sql="SELECT 1", error="RESULT_TRUNCATED_TO_1_ROWS"
    )
    score = evaluation.score_case(truncated, _ok(["a"], [("x",)]))
    assert not score.executed
    assert not score.row_match
    assert not score.value_match


def test_score_case_matches_two_successful_empty_results() -> None:
    """Empty is a legitimate answer when both sides genuinely executed."""
    score = evaluation.score_case(_ok(["a"], []), _ok(["a"], []))
    assert score.executed
    assert score.row_match
    assert score.value_match
    assert score.exact_match


def test_score_case_requires_matching_columns_for_an_exact_match() -> None:
    score = evaluation.score_case(_ok(["a"], [("x",)]), _ok(["b"], [("x",)]))
    assert score.row_match
    assert score.value_match
    assert not score.exact_match


def test_both_harnesses_score_through_the_shared_function() -> None:
    """Guards the parity this module exists to provide.

    Either harness recomputing a match field locally is how the UI and the CLI
    drifted apart in the first place: the UI compared rows with no error guard
    and credited aborted queries that the CLI correctly rejected.
    """
    for path in (Path("ui/evaluation.py"), Path("scripts/evaluate_text_to_sql.py")):
        source = path.read_text(encoding="utf-8")
        assert "score_case(" in source, f"{path} must score through score_case"
        assert "rows_match(" not in source, f"{path} must not compare rows itself"


def test_run_gold_refuses_unsafe_reference_sql() -> None:
    """The UI executed unsafe gold SQL while the CLI refused it; now neither does."""
    sql, result = evaluation.run_gold(
        {"gold_sql": "SELECT * FROM sqlite_master", "db_path": "data/university_agent.db"}
    )
    assert sql == "SELECT * FROM sqlite_master"
    assert result.error == "GOLD_SQL_UNSAFE"
    assert result.rows == []


def test_run_gold_executes_safe_reference_sql() -> None:
    _, result = evaluation.run_gold(
        {"gold_sql": "SELECT major FROM students LIMIT 2", "db_path": "data/university_agent.db"}
    )
    assert result.error is None
    assert len(result.rows) == 2


def test_both_harnesses_run_gold_through_the_shared_function() -> None:
    """Neither harness may execute reference SQL itself.

    The UI previously called `execute_query` on `gold_sql` directly, skipping the
    safety check the CLI applied, so an unsafe reference query ran in one
    harness and was refused in the other.
    """
    for path in (Path("ui/evaluation.py"), Path("scripts/evaluate_text_to_sql.py")):
        source = path.read_text(encoding="utf-8")
        assert "run_gold(" in source, f"{path} must run reference SQL through run_gold"
        assert "execute_query(" not in source, f"{path} must not execute SQL itself"
