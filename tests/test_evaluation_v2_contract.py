from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from text_to_sql_agent.evaluation_v2 import Case, SuiteError, load_suite, suite_sha256

REPO_ROOT = Path(__file__).resolve().parent.parent

VALID: dict[str, Any] = {
    "suite": "demo",
    "id": "c1",
    "db_path": "data/university_agent.db",
    "question": "How many students are there?",
    "evidence": "",
    "gold_sql": "SELECT COUNT(*) FROM students;",
    "hardness": "easy",
    "expected": "answerable",
    "expected_tables": ["students"],
}


def write(tmp_path: Path, *records: dict[str, Any]) -> Path:
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_load_suite_accepts_a_valid_record(tmp_path: Path) -> None:
    [case] = load_suite(write(tmp_path, VALID))
    assert isinstance(case, Case)
    assert case.id == "c1" and case.expected == "answerable"
    assert case.expected_tables == ("students",)


@pytest.mark.parametrize("field", list(VALID))
def test_load_suite_names_a_missing_field(tmp_path: Path, field: str) -> None:
    record = {k: v for k, v in VALID.items() if k != field}
    # Anchor on the message, not a bare field name: pytest's tmp_path contains
    # this test's name, which contains "suite", so match="suite" would match
    # the path and pass even if the loader named the wrong field.
    with pytest.raises(SuiteError, match=rf"field '{field}' is missing"):
        load_suite(write(tmp_path, record))


def test_error_names_the_one_based_line(tmp_path: Path) -> None:
    bad = {k: v for k, v in VALID.items() if k != "question"}
    with pytest.raises(SuiteError, match=r"s\.jsonl:2: field 'question'"):
        load_suite(write(tmp_path, VALID, bad))


def test_a_refusal_case_may_not_carry_gold_sql(tmp_path: Path) -> None:
    record = {**VALID, "expected": "expect_refusal"}
    with pytest.raises(SuiteError, match="gold_sql"):
        load_suite(write(tmp_path, record))


def test_a_refusal_case_without_gold_sql_and_tables_is_valid(tmp_path: Path) -> None:
    record = {**VALID, "expected": "expect_refusal", "gold_sql": "", "expected_tables": []}
    [case] = load_suite(write(tmp_path, record))
    assert case.expected_tables == ()


def test_an_answerable_case_needs_gold_sql(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="gold_sql"):
        load_suite(write(tmp_path, {**VALID, "gold_sql": ""}))


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="duplicates 'c1'"):
        load_suite(write(tmp_path, VALID, VALID))


def test_unknown_expected_value_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="expected"):
        load_suite(write(tmp_path, {**VALID, "expected": "maybe"}))


def test_unknown_hardness_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="hardness"):
        load_suite(write(tmp_path, {**VALID, "hardness": "simple"}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("question", 3),
        ("evidence", None),
        ("expected_tables", "students"),
        ("expected_tables", [1]),
        ("id", ""),
    ],
)
def test_ill_typed_fields_are_rejected(tmp_path: Path, field: str, value: Any) -> None:
    with pytest.raises(SuiteError, match=rf"field '{field}' "):
        load_suite(write(tmp_path, {**VALID, field: value}))


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match=r"field 'difficulty' "):
        load_suite(write(tmp_path, {**VALID, "difficulty": "easy"}))


def test_a_unicode_line_separator_inside_a_string_does_not_split_the_record(
    tmp_path: Path,
) -> None:
    """U+2028 is legal inside a JSON string and json.dumps emits it verbatim;
    str.splitlines() would cut the record in two (review finding, 2026-10-09).
    """
    record = {**VALID, "question": "first part\u2028second part"}
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    [case] = load_suite(path)
    assert case.question == "first part\u2028second part"


def test_a_whitespace_gold_sql_is_rejected_for_a_refusal_case(tmp_path: Path) -> None:
    record = {**VALID, "expected": "expect_refusal", "gold_sql": "  "}
    with pytest.raises(SuiteError, match=r"field 'gold_sql' must be empty"):
        load_suite(write(tmp_path, record))


def test_records_with_different_suite_names_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(
        json.dumps(VALID) + "\n" + json.dumps({**VALID, "id": "c2", "suite": "other"}) + "\n"
    )
    with pytest.raises(SuiteError, match=r":2: field 'suite' is 'other'"):
        load_suite(path)


def test_invalid_json_and_empty_suite_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text("{not json\n")
    with pytest.raises(SuiteError, match=r":1: invalid JSON"):
        load_suite(path)
    path.write_text("\n")
    with pytest.raises(SuiteError, match="no records"):
        load_suite(path)


def test_a_missing_database_file_does_not_fail_loading(tmp_path: Path) -> None:
    [case] = load_suite(write(tmp_path, {**VALID, "db_path": "data/benchmarks/absent.sqlite"}))
    assert case.db_path == "data/benchmarks/absent.sqlite"


def test_suite_sha256_changes_when_a_record_changes(tmp_path: Path) -> None:
    path = write(tmp_path, VALID)
    before = suite_sha256(path)
    assert before == suite_sha256(path)
    write(tmp_path, {**VALID, "question": "Something else?"})
    assert suite_sha256(path) != before


def test_the_demo_suite_loads_and_matches_cases_json() -> None:
    v2 = {c.id: c for c in load_suite(REPO_ROOT / "evaluation/suites/demo.jsonl")}
    v1 = {c["id"]: c for c in json.loads((REPO_ROOT / "evaluation/cases.json").read_text())}
    assert len(v2) == 12
    assert v2.keys() == v1.keys()
    for cid, c in v1.items():
        assert v2[cid].suite == "demo"
        assert v2[cid].gold_sql == c["gold_sql"]
        assert v2[cid].hardness == c["difficulty"]
        assert v2[cid].db_path == c["db_path"]
        assert v2[cid].question == c["question"]
        assert v2[cid].expected_tables == tuple(c["expected_tables"])
        assert v2[cid].expected == "answerable" and v2[cid].evidence == ""


def test_the_demo_source_block_is_authored() -> None:
    source = json.loads((REPO_ROOT / "evaluation/suites/demo.source.json").read_text())
    assert source["kind"] == "authored"
    assert source == {
        "kind": "authored",
        "author": "repository",
        "licence": "MIT",
        "adapter_version": "n/a",
    }
