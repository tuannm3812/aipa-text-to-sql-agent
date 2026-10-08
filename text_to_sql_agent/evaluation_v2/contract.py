"""The v2 case record and its JSON Lines loader.

Every case, from every source, is one ``Case``. Records are validated on load so a broken
conversion fails before a run instead of scoring zero.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

Expected = Literal["answerable", "expect_refusal", "expect_unanswerable"]
Hardness = Literal["easy", "medium", "hard", "extra"]

_EXPECTED_VALUES: tuple[str, ...] = get_args(Expected)
_HARDNESS_VALUES: tuple[str, ...] = get_args(Hardness)

_STRING_FIELDS: tuple[str, ...] = (
    "suite",
    "id",
    "db_path",
    "question",
    "evidence",
    "gold_sql",
    "hardness",
    "expected",
)
_FIELDS: tuple[str, ...] = (*_STRING_FIELDS, "expected_tables")
_NON_EMPTY: frozenset[str] = frozenset({"suite", "id", "db_path", "question"})


@dataclass(frozen=True)
class Case:
    """One evaluation case, identical in shape for every suite."""

    suite: str
    id: str
    db_path: str
    question: str
    evidence: str
    gold_sql: str
    hardness: Hardness
    expected: Expected
    expected_tables: tuple[str, ...]


class SuiteError(ValueError):
    """A suite file is unreadable or a record in it breaks the case contract."""


def _fail(path: Path, line_no: int, field: str, problem: str) -> SuiteError:
    return SuiteError(f"{path}:{line_no}: field '{field}' {problem}")


def _parse_record(path: Path, line_no: int, record: Any) -> Case:
    if not isinstance(record, dict):
        raise SuiteError(f"{path}:{line_no}: record must be a JSON object")
    for field in _FIELDS:
        if field not in record:
            raise _fail(path, line_no, field, "is missing")
    for field in record:
        if field not in _FIELDS:
            raise _fail(path, line_no, field, "is not a known field")
    for field in _STRING_FIELDS:
        value = record[field]
        if not isinstance(value, str):
            raise _fail(path, line_no, field, "must be a string")
        if field in _NON_EMPTY and not value.strip():
            raise _fail(path, line_no, field, "must not be empty")
    tables = record["expected_tables"]
    if not isinstance(tables, list) or not all(isinstance(t, str) and t for t in tables):
        raise _fail(path, line_no, "expected_tables", "must be a list of non-empty strings")
    if record["expected"] not in _EXPECTED_VALUES:
        raise _fail(path, line_no, "expected", f"must be one of {list(_EXPECTED_VALUES)}")
    if record["hardness"] not in _HARDNESS_VALUES:
        raise _fail(path, line_no, "hardness", f"must be one of {list(_HARDNESS_VALUES)}")
    if record["expected"] == "answerable" and not record["gold_sql"].strip():
        raise _fail(path, line_no, "gold_sql", "must not be empty for an answerable case")
    # Exactly "", not merely blank: the spec says a non-answerable case carries
    # no gold SQL, and a whitespace string stored as "gold" is a conversion bug.
    if record["expected"] != "answerable" and record["gold_sql"] != "":
        raise _fail(path, line_no, "gold_sql", "must be empty unless expected is 'answerable'")
    return Case(
        suite=record["suite"],
        id=record["id"],
        db_path=record["db_path"],
        question=record["question"],
        evidence=record["evidence"],
        gold_sql=record["gold_sql"],
        hardness=record["hardness"],
        expected=record["expected"],
        expected_tables=tuple(tables),
    )


def load_suite(path: str | Path) -> list[Case]:
    """Read and validate a JSON Lines suite. Line numbers in errors are 1-based."""
    suite_path = Path(path)
    try:
        text = suite_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SuiteError(f"{suite_path}: cannot read suite: {exc}") from exc
    cases: list[Case] = []
    first_seen: dict[str, int] = {}
    suite_name: str | None = None
    # Split on "\n" only. str.splitlines() also breaks on U+2028, U+0085, \x0b,
    # \x0c and \x1c-\x1e, which are legal *inside* a JSON string and which
    # json.dumps(ensure_ascii=False) emits verbatim - a benchmark question
    # containing one would be cut in two and reported as "invalid JSON", and
    # every later line number would drift (review finding, 2026-10-09).
    for line_no, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SuiteError(f"{suite_path}:{line_no}: invalid JSON: {exc.msg}") from exc
        case = _parse_record(suite_path, line_no, record)
        if suite_name is None:
            suite_name = case.suite
        elif case.suite != suite_name:
            raise _fail(
                suite_path,
                line_no,
                "suite",
                f"is '{case.suite}' but this file's suite is '{suite_name}'",
            )
        if case.id in first_seen:
            raise _fail(
                suite_path,
                line_no,
                "id",
                f"duplicates '{case.id}' first seen on line {first_seen[case.id]}",
            )
        first_seen[case.id] = line_no
        cases.append(case)
    if not cases:
        raise SuiteError(f"{suite_path}: suite contains no records")
    return cases


def suite_sha256(path: str | Path) -> str:
    """SHA-256 of the suite file's bytes, as lowercase hex."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
