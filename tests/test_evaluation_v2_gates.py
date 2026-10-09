"""The gold gate (spec §4.4): three structural checks, two populations, an exception list.

Every run writes under ``tmp_path``. The non-answerable-with-gold-SQL load failure is pinned
in ``test_evaluation_v2_contract.py`` and is not repeated here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.evaluate_v2 as cli
from text_to_sql_agent.evaluation_v2.contract import load_suite
from text_to_sql_agent.evaluation_v2.gates import gold_gate, load_exceptions
from text_to_sql_agent.evaluation_v2.report import NOT_APPLICABLE, UNDEFINED_EMPTY
from text_to_sql_agent.evaluation_v2.scoring import REFUSAL_CODES, UNANSWERABLE

SUITES = Path("evaluation/suites")
DEMO_DB = "data/university_agent.db"
REFUSED_SQL = "SELECT * FROM sqlite_master"


def _suite(
    directory: Path, name: str, records: list[dict[str, Any]], exceptions: str | None = None
) -> tuple[Path, Path | None]:
    path = directory / f"{name}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    source = {"kind": "authored", "author": "test", "licence": "MIT", "adapter_version": "n/a"}
    (directory / f"{name}.source.json").write_text(json.dumps(source), encoding="utf-8")
    if exceptions is None:
        return path, None
    listed = directory / f"{name}.gold_exceptions.txt"
    listed.write_text(exceptions, encoding="utf-8")
    return path, listed


def _record(case_id: str, gold_sql: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "suite": "gate",
        "id": case_id,
        "db_path": DEMO_DB,
        "question": f"question for {case_id}",
        "evidence": "",
        "gold_sql": gold_sql,
        "hardness": "easy",
        "expected": "answerable",
        "expected_tables": [],
    }
    record.update(overrides)
    return record


def _nine_valid_one_refused() -> list[dict[str, Any]]:
    valid = [_record(f"ok{i}", f"SELECT {i} AS n FROM students LIMIT 1") for i in range(9)]
    return [*valid, _record("bad", REFUSED_SQL)]


# --- the shipped suites --------------------------------------------------------------------


def test_the_gate_passes_on_demo(tmp_path: Path) -> None:
    result = gold_gate(
        SUITES / "demo.jsonl", SUITES / "demo.gold_exceptions.txt", out_root=tmp_path
    )
    assert result.passed, result.failures
    assert result.excepted == ()
    assert result.answerable == 12
    assert "(12/12)" in result.metrics["ex"]


def test_the_gate_passes_on_safety_with_ex_not_applicable_and_no_safety_accuracy(
    tmp_path: Path,
) -> None:
    result = gold_gate(
        SUITES / "safety.jsonl", SUITES / "safety.gold_exceptions.txt", out_root=tmp_path
    )
    assert result.passed, result.failures
    assert result.answerable == 0
    assert result.non_answerable == len(load_suite(SUITES / "safety.jsonl"))
    assert result.metrics["ex"] == NOT_APPLICABLE
    assert result.metrics["ex_valid"] == NOT_APPLICABLE
    assert result.metrics["coverage"] == NOT_APPLICABLE
    assert result.metrics["safety"].startswith("not applicable")
    assert "%" not in result.metrics["safety"]


def test_the_safety_suite_is_well_formed() -> None:
    cases = load_suite(SUITES / "safety.jsonl")
    assert 12 <= len(cases) <= 16
    assert {case.expected for case in cases} == {"expect_refusal", "expect_unanswerable"}
    for case in cases:
        assert (case.suite, case.evidence, case.gold_sql, case.expected_tables) == (
            "safety",
            "",
            "",
            (),
        )
    assert REFUSAL_CODES and UNANSWERABLE


# --- the three-part definition --------------------------------------------------------------


def test_one_unlisted_invalid_reference_fails_the_gate_and_listing_it_passes(
    tmp_path: Path,
) -> None:
    suite, _ = _suite(tmp_path, "gate", _nine_valid_one_refused())

    failing = gold_gate(suite, None, out_root=tmp_path / "out1")
    assert not failing.passed
    assert any(f.startswith("bad:") for f in failing.failures)
    assert "(9/10)" in failing.metrics["ex"]

    _, listed = _suite(tmp_path, "gate", _nine_valid_one_refused(), exceptions="bad\n")
    passing = gold_gate(suite, listed, out_root=tmp_path / "out2")
    assert passing.passed, passing.failures
    assert passing.excepted == ("bad",)
    assert "(9/10)" in passing.metrics["ex"]
    assert "(9/9)" in passing.metrics["ex_valid"]
    assert "(9/10)" in passing.metrics["coverage"]


def test_all_answerable_references_invalid_reports_both_populations(tmp_path: Path) -> None:
    records = [_record(f"bad{i}", REFUSED_SQL) for i in range(10)]
    suite, _ = _suite(tmp_path, "gate", records)

    failing = gold_gate(suite, None, out_root=tmp_path / "out1")
    assert not failing.passed
    assert len(failing.failures) == 10
    assert "(0/10)" in failing.metrics["ex"]
    assert failing.metrics["ex_valid"] == UNDEFINED_EMPTY

    everything = "".join(f"bad{i}\n" for i in range(10))
    _, listed = _suite(tmp_path, "gate", records, exceptions=everything)
    passing = gold_gate(suite, listed, out_root=tmp_path / "out2")
    assert passing.passed, passing.failures
    assert len(passing.excepted) == 10
    assert "(0/10)" in passing.metrics["ex"]
    assert passing.metrics["ex_valid"] == UNDEFINED_EMPTY

    _, nine = _suite(tmp_path, "gate", records, exceptions=everything.replace("bad9\n", ""))
    assert not gold_gate(suite, nine, out_root=tmp_path / "out3").passed


def test_a_mixed_suite_reports_each_population(tmp_path: Path) -> None:
    records = [
        _record("a1", "SELECT 1 AS n FROM students LIMIT 1"),
        _record("a2", "SELECT 2 AS n FROM students LIMIT 1"),
        _record("r1", "", expected="expect_refusal"),
        _record("u1", "", expected="expect_unanswerable"),
    ]
    suite, _ = _suite(tmp_path, "gate", records)
    result = gold_gate(suite, None, out_root=tmp_path / "out")
    assert result.passed, result.failures
    assert (result.answerable, result.non_answerable) == (2, 2)
    assert "(2/2)" in result.metrics["ex"]
    assert "(2/2)" in result.metrics["coverage"]
    assert result.metrics["safety"].startswith("not applicable")


def test_stale_exceptions_are_reported_but_do_not_fail_the_gate(tmp_path: Path) -> None:
    suite, listed = _suite(
        tmp_path, "gate", [_record("ok", "SELECT 1 AS n FROM students LIMIT 1")], exceptions="ok\n"
    )
    result = gold_gate(suite, listed, out_root=tmp_path / "out")
    assert result.passed
    assert result.stale_exceptions == ("ok",)


def test_a_missing_exception_file_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    suite, _ = _suite(tmp_path, "gate", [_record("ok", "SELECT 1 AS n FROM students LIMIT 1")])
    with pytest.raises(OSError):
        gold_gate(suite, tmp_path / "nope.txt", out_root=tmp_path / "out")


# --- the exception list ----------------------------------------------------------------------


def test_the_loader_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.gold_exceptions.txt"
    path.write_text(
        "# a header comment\n\n701   # ~14.6 billion VM steps\n  1131\t# indented, tabbed\n"
        "   \n#only a comment\nabc_def\n",
        encoding="utf-8",
    )
    assert load_exceptions(path) == {"701", "1131", "abc_def"}
    assert load_exceptions(None) == frozenset()


def test_the_committed_exception_lists() -> None:
    for suite in ("demo", "safety", "spider_dev"):
        assert load_exceptions(SUITES / f"{suite}.gold_exceptions.txt") == frozenset()
    assert load_exceptions(SUITES / "bird_dev.gold_exceptions.txt") == {"701", "1131"}


# --- the command line --------------------------------------------------------------------------


def test_the_cli_gate_exit_codes_and_nothing_written_to_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sorted(p.name for p in Path("evaluation/results").iterdir())
    assert cli.main(["--gate", "gold", "--suite", "demo", "safety"]) == 0
    out = capsys.readouterr().out
    assert "demo: gold gate PASS" in out and "safety: gold gate PASS" in out
    assert sorted(p.name for p in Path("evaluation/results").iterdir()) == before

    suite, _ = _suite(tmp_path, "gate", _nine_valid_one_refused(), exceptions="")
    argv = ["--gate", "gold", "--suite", "gate", "--suites-dir", str(tmp_path)]
    assert cli.main(argv) == 1
    assert "FAILURE bad:" in capsys.readouterr().out
    (tmp_path / "gate.gold_exceptions.txt").write_text("bad  # refused by design\n")
    assert cli.main(argv) == 0
    (tmp_path / "gate.gold_exceptions.txt").unlink()
    assert cli.main(argv) == 2
