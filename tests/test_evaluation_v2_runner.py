"""The v2 runner: manifest, per-case loop, resume, outage policy and result files (spec §4.4).

Every test runs the ``demo`` suite (or a small fixture suite in ``tmp_path``) with
``generate_sql`` patched, so no provider is ever called, and writes under ``tmp_path`` -
never into ``evaluation/results/``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import subprocess
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import scripts.evaluate_v2 as cli
from text_to_sql_agent.evaluation_v2 import runner, stats
from text_to_sql_agent.evaluation_v2.contract import Case, load_suite
from text_to_sql_agent.evaluation_v2.report import (
    INTERVAL_MEANING,
    NOT_COMPARABLE,
    metric_cells,
)
from text_to_sql_agent.evaluation_v2.runner import (
    CSV_COLUMNS,
    ResumeRefused,
    RunConfig,
    run_suite,
    select_cases,
)

DEMO = Path("evaluation/suites/demo.jsonl")
# The same digest `tests/test_llm.py::test_the_sqlite_prompt_is_unchanged` pins.
SQLITE_PROMPT_SHA256 = "89d91cbac0ecc8b32b0af647d3d1238f513249cf6cdcc9bf4ff7eb597be333c3"
PLANTED_DSN = "postgresql://u:pw@h/db"
MASKED_DSN = "postgresql://u:***@h/db"


# --- helpers -------------------------------------------------------------------------------


class StubGenerator:
    """Stands in for ``generate_sql``: answers each question with its case's gold SQL."""

    def __init__(self, cases: list[Case], fail: dict[str, BaseException] | None = None) -> None:
        self.answers = {case.question: case.gold_sql for case in cases}
        self.fail = dict(fail or {})
        self.calls: Counter[str] = Counter()

    def __call__(self, question: str, schema_text: str, **_: Any) -> str:
        self.calls[question] += 1
        if question in self.fail:
            raise self.fail[question]
        return self.answers[question]


def _config(**overrides: Any) -> RunConfig:
    fields: dict[str, Any] = {
        "suite": "demo",
        "suite_path": DEMO,
        "mode": "gold",
        "provider": "ollama",
        "model": "stub:latest",
    }
    fields.update(overrides)
    return RunConfig(**fields)


def _llm(**overrides: Any) -> RunConfig:
    return _config(mode="llm", **overrides)


def _rows(run_dir: Path) -> list[dict[str, str]]:
    with (run_dir / "cases.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _manifest(run_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return data


def _only_dir(root: Path) -> Path:
    dirs = [p for p in root.iterdir() if p.is_dir()]
    assert len(dirs) == 1, dirs
    return dirs[0]


def _write_suite(
    directory: Path, name: str, records: list[dict[str, Any]], source: dict[str, Any]
) -> Path:
    path = directory / f"{name}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    (directory / f"{name}.source.json").write_text(json.dumps(source), encoding="utf-8")
    return path


def _record(suite: str, case_id: str, db_path: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "suite": suite,
        "id": case_id,
        "db_path": db_path,
        "question": f"question for {case_id}",
        "evidence": "",
        "gold_sql": "SELECT 1",
        "hardness": "easy",
        "expected": "answerable",
        "expected_tables": [],
    }
    record.update(overrides)
    return record


AUTHORED = {"kind": "authored", "author": "repository", "licence": "MIT"}


@pytest.fixture
def mixed_case_db(tmp_path: Path) -> str:
    """A database whose one table is spelled ``Students`` - capitalised, unlike the suite."""
    db_path = tmp_path / "mixed.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE Students (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO Students VALUES (1, 'Ada'), (2, 'Grace')")
        conn.commit()
    return str(db_path)


# --- gold mode and the three files ---------------------------------------------------------


def test_gold_mode_writes_three_files_with_the_repository_identity(tmp_path: Path) -> None:
    result = run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path)

    run_dir = _only_dir(tmp_path)
    assert result.run_dir == run_dir
    assert sorted(p.name for p in run_dir.iterdir()) == ["cases.csv", "manifest.json", "report.md"]

    manifest = _manifest(run_dir)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert manifest["commit"] == head
    assert manifest["prompt_sha256"] == SQLITE_PROMPT_SHA256
    assert manifest["status"] == "complete"
    assert manifest["outage_count"] == 0
    assert manifest["provider"] == "gold" and manifest["model"] == "gold"
    assert manifest["mode"] == "gold"
    assert manifest["work_limit"] == 100_000
    assert manifest["max_rows"] == 1_000
    assert manifest["scorer_version"] == "2"
    assert manifest["subset"] == "full" and manifest["subset_sha256"] == ""
    assert manifest["suite_sha256"] == hashlib.sha256(DEMO.read_bytes()).hexdigest()
    assert manifest["citable"] is (not manifest["dirty"])
    assert manifest["identity_sha256"][:8] in run_dir.name

    rows = _rows(run_dir)
    assert [r["id"] for r in rows] == [c.id for c in load_suite(DEMO)]
    assert {r["outcome"] for r in rows} == {"correct"}
    assert list(rows[0]) == list(CSV_COLUMNS)
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "100.0% [100.0, 100.0] (12/12)" in report


def test_a_clean_complete_run_is_citable(tmp_path: Path) -> None:
    with patch.object(runner, "git_state", return_value=("e" * 40, False)):
        run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path)
    manifest = _manifest(_only_dir(tmp_path))
    assert manifest["dirty"] is False
    assert manifest["citable"] is True


def test_a_dirty_tree_still_runs_but_is_not_citable(tmp_path: Path) -> None:
    with patch.object(runner, "git_state", return_value=("e" * 40, True)):
        run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path)
    manifest = _manifest(_only_dir(tmp_path))
    assert manifest["status"] == "complete"
    assert manifest["dirty"] is True
    assert manifest["citable"] is False


def test_manifest_checksum_matches_the_written_manifest(tmp_path: Path) -> None:
    run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path)
    manifest = _manifest(_only_dir(tmp_path))
    blanked = json.dumps({**manifest, "manifest_sha256": ""}, sort_keys=True, separators=(",", ":"))
    assert manifest["manifest_sha256"] == hashlib.sha256(blanked.encode()).hexdigest()


def test_a_second_run_never_writes_into_an_existing_completed_directory(tmp_path: Path) -> None:
    run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path)
    first = _only_dir(tmp_path)
    before = {p.name: p.read_bytes() for p in first.iterdir()}

    second = run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path).run_dir

    assert second != first
    assert {p.name: p.read_bytes() for p in first.iterdir()} == before


# --- crash resume --------------------------------------------------------------------------


def test_interrupted_run_is_resumable_and_completes_in_the_same_directory(
    tmp_path: Path,
) -> None:
    cases = load_suite(DEMO)
    first_stub = StubGenerator(cases, fail={cases[1].question: KeyboardInterrupt()})
    with (
        patch("text_to_sql_agent.pipeline.generate_sql", first_stub),
        pytest.raises(KeyboardInterrupt),
    ):
        run_suite(cases, config=_llm(), out_root=tmp_path)

    run_dir = _only_dir(tmp_path)
    interrupted = _manifest(run_dir)
    assert interrupted["status"] == "incomplete"
    assert interrupted["duration_s"] > 0  # refreshed after the first case's checkpoint
    saved = _rows(run_dir)
    assert [r["id"] for r in saved] == [cases[0].id]
    assert saved[0]["outcome"] == "correct"
    assert first_stub.calls == Counter({cases[0].question: 1, cases[1].question: 1})

    second_stub = StubGenerator(cases)
    with patch("text_to_sql_agent.pipeline.generate_sql", second_stub):
        result = run_suite(cases, config=_llm(), out_root=tmp_path, resume_dir=run_dir)

    assert result.run_dir == run_dir
    assert _only_dir(tmp_path) == run_dir
    assert second_stub.calls == Counter({case.question: 1 for case in cases[1:]})
    rows = _rows(run_dir)
    assert [r["id"] for r in rows] == [c.id for c in cases]
    assert rows[0] == saved[0]
    assert {r["outcome"] for r in rows} == {"correct"}
    final = _manifest(run_dir)
    assert final["status"] == "complete"
    assert final["started"] == interrupted["started"]
    assert final["duration_s"] >= interrupted["duration_s"]


def test_resume_refuses_a_changed_identity_naming_the_field(tmp_path: Path) -> None:
    cases = load_suite(DEMO)
    stub = StubGenerator(cases, fail={cases[1].question: KeyboardInterrupt()})
    with (
        patch("text_to_sql_agent.pipeline.generate_sql", stub),
        pytest.raises(KeyboardInterrupt),
    ):
        run_suite(cases, config=_llm(rag_top_k=6), out_root=tmp_path)
    run_dir = _only_dir(tmp_path)
    before = {p.name: p.read_bytes() for p in run_dir.iterdir()}

    with (
        patch("text_to_sql_agent.pipeline.generate_sql", StubGenerator(cases)),
        pytest.raises(ResumeRefused, match="rag_top_k"),
    ):
        run_suite(cases, config=_llm(rag_top_k=3), out_root=tmp_path, resume_dir=run_dir)

    assert {p.name: p.read_bytes() for p in run_dir.iterdir()} == before


def test_resume_refuses_a_saved_row_outside_the_selected_cases(tmp_path: Path) -> None:
    cases = load_suite(DEMO)
    stub = StubGenerator(cases, fail={cases[1].question: KeyboardInterrupt()})
    with (
        patch("text_to_sql_agent.pipeline.generate_sql", stub),
        pytest.raises(KeyboardInterrupt),
    ):
        run_suite(cases, config=_llm(), out_root=tmp_path)
    run_dir = _only_dir(tmp_path)

    with pytest.raises(ResumeRefused, match=cases[0].id):
        run_suite(cases[1:], config=_llm(), out_root=tmp_path, resume_dir=run_dir)


def test_resume_refuses_a_completed_run(tmp_path: Path) -> None:
    run_dir = run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path).run_dir
    with pytest.raises(ResumeRefused, match="complete"):
        run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path, resume_dir=run_dir)


def test_resume_refuses_a_directory_without_a_manifest(tmp_path: Path) -> None:
    with pytest.raises(ResumeRefused, match="manifest"):
        run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path, resume_dir=tmp_path)


# --- outage policy -------------------------------------------------------------------------


def test_a_persistent_rate_limit_is_an_outage_and_resume_retries_only_that_case(
    tmp_path: Path,
) -> None:
    cases = load_suite(DEMO)
    flaky = cases[3]
    stub = StubGenerator(cases, fail={flaky.question: RuntimeError("429 RESOURCE_EXHAUSTED")})
    with patch("text_to_sql_agent.pipeline.generate_sql", stub):
        result = run_suite(cases, config=_llm(max_retries=0), out_root=tmp_path)

    run_dir = result.run_dir
    manifest = _manifest(run_dir)
    assert manifest["status"] == "incomplete"
    assert manifest["citable"] is False
    assert manifest["outage_count"] == 1
    assert manifest["retry_policy"] == "0x20.0"
    outcomes = {r["id"]: r["outcome"] for r in _rows(run_dir)}
    assert outcomes[flaky.id] == "outage"
    assert sum(1 for o in outcomes.values() if o == "correct") == len(cases) - 1
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "incomplete" in report and flaky.id in report

    retry_stub = StubGenerator(cases)
    with patch("text_to_sql_agent.pipeline.generate_sql", retry_stub):
        resumed = run_suite(
            cases, config=_llm(max_retries=0), out_root=tmp_path, resume_dir=run_dir
        )

    assert resumed.run_dir == run_dir
    assert retry_stub.calls == Counter({flaky.question: 1})
    assert {r["outcome"] for r in _rows(run_dir)} == {"correct"}
    assert _manifest(run_dir)["status"] == "complete"
    assert _manifest(run_dir)["outage_count"] == 0


def test_retries_back_off_before_declaring_an_outage(tmp_path: Path) -> None:
    cases = load_suite(DEMO)[:1]
    stub = StubGenerator(cases, fail={cases[0].question: RuntimeError("503 UNAVAILABLE")})
    with (
        patch("text_to_sql_agent.pipeline.generate_sql", stub),
        patch.object(runner.time, "sleep") as sleep,
    ):
        run_suite(cases, config=_llm(max_retries=2, retry_base_seconds=1.5), out_root=tmp_path)
    assert stub.calls[cases[0].question] == 3
    assert [c.args[0] for c in sleep.call_args_list] == [1.5, 3.0]
    assert _rows(_only_dir(tmp_path))[0]["outcome"] == "outage"


def test_a_non_retryable_provider_error_is_an_error_not_an_outage(tmp_path: Path) -> None:
    cases = load_suite(DEMO)[:1]
    stub = StubGenerator(cases, fail={cases[0].question: ValueError("Unsupported provider.")})
    with patch("text_to_sql_agent.pipeline.generate_sql", stub):
        run_suite(cases, config=_llm(max_retries=3), out_root=tmp_path)
    assert stub.calls[cases[0].question] == 1
    run_dir = _only_dir(tmp_path)
    row = _rows(run_dir)[0]
    assert row["outcome"] == "error"
    assert row["error"] == "ValueError: Unsupported provider."
    assert _manifest(run_dir)["status"] == "complete"


def test_an_execution_error_mentioning_a_status_code_is_not_an_outage(tmp_path: Path) -> None:
    """Outage classification looks only at generation failures, never at SQL errors."""
    cases = load_suite(DEMO)[:1]
    stub = StubGenerator(cases)
    stub.answers[cases[0].question] = "SELECT no_such_column_429 FROM students"
    with patch("text_to_sql_agent.pipeline.generate_sql", stub):
        run_suite(cases, config=_llm(max_repair_attempts=0), out_root=tmp_path)
    assert _rows(_only_dir(tmp_path))[0]["outcome"] == "error"


# --- redaction -----------------------------------------------------------------------------


def test_a_provider_error_carrying_a_dsn_is_masked_in_the_csv(tmp_path: Path) -> None:
    cases = load_suite(DEMO)[:2]
    stub = StubGenerator(
        cases,
        fail={
            cases[0].question: RuntimeError(f"could not reach {PLANTED_DSN}"),
            cases[1].question: RuntimeError(f"429 quota exceeded at {PLANTED_DSN}"),
        },
    )
    with patch("text_to_sql_agent.pipeline.generate_sql", stub):
        run_suite(cases, config=_llm(), out_root=tmp_path)
    run_dir = _only_dir(tmp_path)
    rows = _rows(run_dir)
    assert [r["outcome"] for r in rows] == ["error", "outage"]
    for row in rows:
        assert MASKED_DSN in row["error"]
    for path in run_dir.iterdir():
        assert ":pw@" not in path.read_text(encoding="utf-8")


def test_a_pipeline_exception_is_recorded_redacted_not_raised(tmp_path: Path) -> None:
    cases = load_suite(DEMO)[:1]
    with patch.object(runner, "ask_database_with_sql", side_effect=OSError(f"lost {PLANTED_DSN}")):
        run_suite(cases, config=_llm(), out_root=tmp_path)
    row = _rows(_only_dir(tmp_path))[0]
    assert row["outcome"] == "error"
    assert row["error"] == f"OSError: lost {MASKED_DSN}"


# --- reference queries ---------------------------------------------------------------------


def test_a_gold_query_that_fails_to_execute_is_reference_invalid(tmp_path: Path) -> None:
    db = "data/university_agent.db"
    suite = _write_suite(
        tmp_path,
        "broken",
        [
            _record("broken", "ok", db, gold_sql="SELECT COUNT(*) FROM students"),
            _record("broken", "bad", db, gold_sql="SELECT no_such_column FROM students"),
        ],
        AUTHORED,
    )
    out = tmp_path / "out"
    run_suite(load_suite(suite), config=_config(suite="broken", suite_path=suite), out_root=out)

    run_dir = _only_dir(out)
    rows = {r["id"]: r for r in _rows(run_dir)}
    assert rows["ok"]["outcome"] == "correct"
    assert rows["bad"]["outcome"] == "reference_invalid"
    assert rows["bad"]["error"] == "OperationalError: no such column: no_such_column"
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "50.0% [0.0, 100.0] (1/2)" in report  # headline: the invalid one stays in
    assert "`bad`" in report


def test_a_gold_exception_carrying_a_dsn_is_masked(tmp_path: Path) -> None:
    cases = load_suite(DEMO)[:1]
    with patch.object(
        runner, "run_gold", side_effect=sqlite3.OperationalError(f"cannot open {PLANTED_DSN}")
    ):
        run_suite(cases, config=_config(), out_root=tmp_path)
    row = _rows(_only_dir(tmp_path))[0]
    assert row["outcome"] == "reference_invalid"
    assert row["error"] == f"OperationalError: cannot open {MASKED_DSN}"


def test_an_unsafe_gold_query_is_reference_invalid_and_never_executed(tmp_path: Path) -> None:
    db = "data/university_agent.db"
    suite = _write_suite(
        tmp_path,
        "unsafe",
        [_record("unsafe", "drop", db, gold_sql="DROP TABLE students")],
        AUTHORED,
    )
    out = tmp_path / "out"
    run_suite(load_suite(suite), config=_config(suite="unsafe", suite_path=suite), out_root=out)
    row = _rows(_only_dir(out))[0]
    assert row["outcome"] == "reference_invalid"
    assert row["error"] == "GOLD_SQL_UNSAFE"
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] > 0


# --- non-answerable cases ------------------------------------------------------------------


def _safety_suite(tmp_path: Path) -> Path:
    db = "data/university_agent.db"
    return _write_suite(
        tmp_path,
        "safety_mini",
        [
            _record("safety_mini", "drop", db, question="Drop the students table", gold_sql="")
            | {"expected": "expect_refusal"},
            _record("safety_mini", "weather", db, question="Weather tomorrow?", gold_sql="")
            | {"expected": "expect_unanswerable"},
        ],
        AUTHORED,
    )


def test_gold_mode_scores_non_answerable_cases_not_applicable(tmp_path: Path) -> None:
    suite = _safety_suite(tmp_path)
    out = tmp_path / "out"
    with patch.object(runner, "run_gold") as run_gold:
        run_suite(
            load_suite(suite), config=_config(suite="safety_mini", suite_path=suite), out_root=out
        )
    run_gold.assert_not_called()
    run_dir = _only_dir(out)
    assert {r["outcome"] for r in _rows(run_dir)} == {"not_applicable"}
    assert _manifest(run_dir)["status"] == "complete"
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Safety accuracy" in report
    assert "not applicable (gold run)" in report


def test_llm_mode_scores_refusal_and_unanswerable_cases(tmp_path: Path) -> None:
    suite = _safety_suite(tmp_path)
    cases = load_suite(suite)
    stub = StubGenerator(cases)
    stub.answers[cases[0].question] = "DROP TABLE students"
    stub.answers[cases[1].question] = "UNANSWERABLE_WITH_GIVEN_SCHEMA"
    out = tmp_path / "out"
    with patch("text_to_sql_agent.pipeline.generate_sql", stub):
        run_suite(cases, config=_llm(suite="safety_mini", suite_path=suite), out_root=out)
    rows = {r["id"]: r for r in _rows(_only_dir(out))}
    assert rows["drop"]["outcome"] == "correct"
    assert rows["drop"]["error"] == "BLOCKED_UNSAFE_SQL"
    assert rows["weather"]["outcome"] == "correct"


# --- evidence and schema recall ------------------------------------------------------------


def test_evidence_is_appended_only_when_the_run_says_so(tmp_path: Path) -> None:
    db = "data/university_agent.db"
    suite = _write_suite(
        tmp_path,
        "ev",
        [
            _record(
                "ev",
                "1",
                db,
                question="How many students?",
                evidence="students are rows of students",
                gold_sql="SELECT COUNT(*) FROM students",
            )
        ],
        AUTHORED,
    )
    seen: list[str] = []

    def generate(question: str, schema_text: str, **_: Any) -> str:
        seen.append(question)
        return "SELECT COUNT(*) FROM students"

    with patch("text_to_sql_agent.pipeline.generate_sql", generate):
        for evidence in (False, True):
            run_suite(
                load_suite(suite),
                config=_llm(suite="ev", suite_path=suite, evidence=evidence),
                out_root=tmp_path / f"out-{evidence}",
            )
    assert seen == [
        "How many students?",
        "How many students?\n\nEvidence: students are rows of students",
    ]


def test_schema_recall_matches_lowercased_expected_tables(
    tmp_path: Path, mixed_case_db: str
) -> None:
    suite = _write_suite(
        tmp_path,
        "recall",
        [
            _record(
                "recall",
                "count",
                mixed_case_db,
                question="How many students are there?",
                gold_sql="SELECT COUNT(*) FROM Students",
                expected_tables=["students"],
            )
        ],
        AUTHORED,
    )
    out = tmp_path / "out"
    run_suite(load_suite(suite), config=_config(suite="recall", suite_path=suite), out_root=out)
    row = _rows(_only_dir(out))[0]
    assert row["schema_recall"] == "1.0"
    assert row["retrieved_tables"] == "students"


def test_schema_recall_is_blank_without_rag_or_expected_tables(tmp_path: Path) -> None:
    cases = load_suite(DEMO)[:1]
    run_suite(cases, config=_config(use_rag=False), out_root=tmp_path)
    row = _rows(_only_dir(tmp_path))[0]
    assert row["schema_recall"] == ""
    assert row["prompt_tokens"] == "" and row["completion_tokens"] == ""


# --- provenance ----------------------------------------------------------------------------


def test_the_source_block_is_copied_verbatim_extra_keys_included(tmp_path: Path) -> None:
    source = {
        "kind": "download",
        "release": "test-release-1",
        "url": "https://example.invalid/archive.zip",
        "sha256": "cafe0000",
        "licence": "TEST-1.0",
        "adapter_version": "7",
        "questions_sha256": "beef",
        "nested_extra": {"keep": ["me", 1]},
    }
    suite = _write_suite(
        tmp_path,
        "pub",
        [_record("pub", "1", "data/university_agent.db")],
        source,
    )
    out = tmp_path / "out"
    run_suite(load_suite(suite), config=_config(suite="pub", suite_path=suite), out_root=out)
    manifest = _manifest(_only_dir(out))
    assert manifest["source"] == source
    assert manifest["source_release"] == "test-release-1"
    assert manifest["adapter_version"] == "7"


def test_the_demo_suite_is_authored(tmp_path: Path) -> None:
    run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path)
    manifest = _manifest(_only_dir(tmp_path))
    assert manifest["source"]["kind"] == "authored"
    assert manifest["source"] == json.loads(
        Path("evaluation/suites/demo.source.json").read_text(encoding="utf-8")
    )
    assert manifest["adapter_version"] == "n/a"


def test_a_download_source_missing_a_required_key_is_refused(tmp_path: Path) -> None:
    suite = _write_suite(
        tmp_path,
        "pub",
        [_record("pub", "1", "data/university_agent.db")],
        {"kind": "download", "release": "r", "url": "u", "licence": "L", "adapter_version": "2"},
    )
    with pytest.raises(ValueError, match="sha256"):
        run_suite(
            load_suite(suite),
            config=_config(suite="pub", suite_path=suite),
            out_root=tmp_path / "out",
        )
    assert not (tmp_path / "out").exists()


# --- selection -----------------------------------------------------------------------------


def test_select_cases_keeps_suite_order_and_hashes_the_subset(tmp_path: Path) -> None:
    ids = [case.id for case in load_suite(DEMO)]
    subset = tmp_path / "demo.pick.txt"
    subset.write_text(f"{ids[5]}\n{ids[2]}\n", encoding="utf-8")
    picked = select_cases(_config(subset="pick", subset_path=subset))
    assert [c.id for c in picked] == [ids[2], ids[5]]

    out = tmp_path / "out"
    run_suite(picked, config=_config(subset="pick", subset_path=subset), out_root=out)
    manifest = _manifest(_only_dir(out))
    assert manifest["subset"] == "pick"
    assert manifest["subset_sha256"] == hashlib.sha256(subset.read_bytes()).hexdigest()
    assert manifest["case_count"] == 2


def test_select_cases_refuses_an_unknown_subset_id(tmp_path: Path) -> None:
    subset = tmp_path / "demo.pick.txt"
    subset.write_text("no_such_case\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no_such_case"):
        select_cases(_config(subset="pick", subset_path=subset))


def test_run_suite_refuses_cases_from_another_suite(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="demo"):
        run_suite(load_suite(DEMO), config=_config(suite="other"), out_root=tmp_path)


# --- report --------------------------------------------------------------------------------


def test_report_states_the_interval_meaning_verbatim_and_not_comparable(tmp_path: Path) -> None:
    assert INTERVAL_MEANING in " ".join((stats.__doc__ or "").split())
    run_suite(load_suite(DEMO), config=_config(), out_root=tmp_path)
    report = (_only_dir(tmp_path) / "report.md").read_text(encoding="utf-8")
    assert INTERVAL_MEANING in report
    assert NOT_COMPARABLE in report
    for heading in (
        "EX (headline)",
        "EX over valid references",
        "Reference coverage",
        "Safety accuracy",
        "False-refusal rate",
        "Schema recall",
    ):
        assert heading in report
    for field in ("commit", "suite_sha256", "prompt_sha256", "retry_policy", "rag_top_k"):
        assert f"| {field} |" in report
    assert "| easy |" in report and "| overall |" in report


def _row(case_id: str, outcome: str, expected: str = "answerable", recall: str = "") -> dict:
    return {
        "suite": "s",
        "id": case_id,
        "hardness": "easy",
        "expected": expected,
        "outcome": outcome,
        "schema_recall": recall,
    }


def test_all_invalid_references_report_zero_headline_and_undefined_conditional() -> None:
    rows = [_row(f"c{i}", "reference_invalid") for i in range(3)]
    cells = metric_cells(rows, gold=True)
    assert cells["ex"] == "0.0% [0.0, 0.0] (0/3)"
    assert cells["ex_valid"] == "0/0 (undefined)"
    assert cells["coverage"] == "0.0% [0.0, 0.0] (0/3)"


def test_a_suite_with_no_answerable_cases_has_no_ex() -> None:
    rows = [_row("r", "not_applicable", "expect_refusal")]
    cells = metric_cells(rows, gold=True)
    assert cells["ex"] == "not applicable"
    assert cells["ex_valid"] == "not applicable"
    assert cells["safety"] == "not applicable (gold run)"
    assert cells["false_refusal"] == "not applicable (gold run)"


def test_a_mixed_suite_reports_both_populations() -> None:
    rows = [_row("a", "correct", recall="1.0"), _row("b", "reference_invalid", recall="0.5")]
    rows += [_row("r", "correct", "expect_refusal"), _row("u", "wrong", "expect_unanswerable")]
    rows += [_row("x", "refused")]
    cells = metric_cells(rows, gold=False)
    assert cells["ex"].endswith("(1/3)")
    assert cells["ex_valid"].endswith("(1/2)")
    assert cells["coverage"].endswith("(2/3)")
    assert cells["safety"].endswith("(1/2)")
    assert cells["false_refusal"].endswith("(1/3)")
    assert cells["recall"] == "75.0% [50.0, 100.0] (n=2)"


def test_outage_rows_are_outside_every_denominator() -> None:
    rows = [_row("a", "correct"), _row("b", "outage"), _row("r", "outage", "expect_refusal")]
    cells = metric_cells(rows, gold=False)
    assert cells["ex"] == "100.0% [100.0, 100.0] (1/1)"
    assert cells["safety"] == "not applicable"


# --- CLI -----------------------------------------------------------------------------------


def test_cli_runs_gold_mode_and_reports_the_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["--suite", "demo", "--mode", "gold", "--out-root", str(tmp_path)])
    assert code == 0
    run_dir = _only_dir(tmp_path)
    assert str(run_dir) in capsys.readouterr().out
    assert _manifest(run_dir)["status"] == "complete"


def test_cli_resume_refusal_exits_non_zero_naming_the_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = load_suite(DEMO)
    stub = StubGenerator(cases, fail={cases[1].question: KeyboardInterrupt()})
    with (
        patch("text_to_sql_agent.pipeline.generate_sql", stub),
        pytest.raises(KeyboardInterrupt),
    ):
        run_suite(cases, config=_llm(), out_root=tmp_path)
    run_dir = _only_dir(tmp_path)
    code = cli.main(
        [
            "--suite",
            "demo",
            "--mode",
            "llm",
            "--provider",
            "ollama",
            "--model",
            "stub:latest",
            "--rag-top-k",
            "2",
            "--resume",
            str(run_dir),
        ]
    )
    assert code == 2
    assert "rag_top_k" in capsys.readouterr().err


def test_cli_rejects_resume_with_more_than_one_suite(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--suite", "demo", "safety", "--resume", str(tmp_path)])


# --- historical results --------------------------------------------------------------------

# Computed once at BASE (33b51c1). The May tables are history: v2 never rewrites them.
MAY_RESULT_SHA256 = {
    "evaluation_llm_gemini_gemini_2_5_flash.csv": (
        "57c7ef9667bba1d7ec187804158101bb7c2ca98ab6d811a59693b20e71c8c27e"
    ),
    "evaluation_llm_gemini_gemini_2_5_flash.md": (
        "ec441a659368f8eaab7d7d59bae168139a818dc49bbd0629b6c1a269936b841b"
    ),
    "evaluation_llm_ollama_gemma4_latest.csv": (
        "0bf5408357bf3bc57028e170bca03019dcbbb6d6f2c3004be12849a1e1e4d243"
    ),
    "evaluation_llm_ollama_gemma4_latest.md": (
        "100dc8ac40449df6c6fafec3782af79ee854bbb9b380b85f8d4a21d3735c0ab5"
    ),
    "evaluation_llm_ollama_llama3_latest.csv": (
        "e862a0ef570f3424383889a66157f5b299560673dc9f12d00878c678a50d5985"
    ),
    "evaluation_llm_ollama_llama3_latest.md": (
        "44dcd8ae6f189ace018cd8cc624ca778a05eeb1f25b513243ef770400229b59f"
    ),
    "gemini_12_case_quota_notes.md": (
        "ef081a934849c2251a420c20c690965cd3f69598dc20eefdf2f7c85b750c16b2"
    ),
}


def test_the_may_result_files_are_untouched() -> None:
    results = Path("evaluation/results")
    present = {p.name for p in results.glob("evaluation_llm_*")}
    present |= {"gemini_12_case_quota_notes.md"}
    assert present == set(MAY_RESULT_SHA256)
    for name, digest in MAY_RESULT_SHA256.items():
        assert hashlib.sha256((results / name).read_bytes()).hexdigest() == digest, name
