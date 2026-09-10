from __future__ import annotations

import json
from pathlib import Path

from text_to_sql_agent import evaluation


def test_canonical_value_rounds_numbers_to_two_decimals() -> None:
    assert evaluation.canonical_value(10) == "10.0"
    assert evaluation.canonical_value(10.0) == "10.0"
    assert evaluation.canonical_value(10.004) == "10.0"
    assert evaluation.canonical_value("10.004") == "10.0"


def test_canonical_value_lowercases_and_strips_text() -> None:
    assert evaluation.canonical_value("  Business  ") == "business"


def test_rows_match_ignores_row_order() -> None:
    assert evaluation.rows_match([("b", 2), ("a", 1)], [("a", 1), ("b", 2)])


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
