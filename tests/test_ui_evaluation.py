"""The Streamlit evaluation tab: the live demo run and the result-directory viewer.

The tab scores through the v2 runner's own `evaluate_case`, so these tests check the two
properties that matter: it cannot disagree with `run_suite` about an outcome, and what it
puts on the page is safe (a DSN password never reaches the table). Nothing here writes under
`evaluation/results/`; every fixture lives in `tmp_path`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

import ui.evaluation as evaluation
from text_to_sql_agent import QueryResult, SchemaChunk, SchemaRetrievalResult
from text_to_sql_agent.evaluation_v2 import runner
from text_to_sql_agent.evaluation_v2.contract import Case, load_suite
from text_to_sql_agent.evaluation_v2.runner import RunConfig, run_suite, select_cases

APP = str(Path(__file__).resolve().parents[1] / "app.py")
DEMO = evaluation.DEMO_SUITE
DSN = "postgresql://u:hunter2@db.example.com:5432/prod"


def _case(**overrides: Any) -> Case:
    fields: dict[str, Any] = {
        "suite": "demo",
        "id": "case-1",
        "db_path": DSN,
        "question": "how many customers?",
        "evidence": "",
        "gold_sql": "SELECT 1",
        "hardness": "easy",
        "expected": "answerable",
        "expected_tables": ("customers",),
    }
    fields.update(overrides)
    return Case(**fields)


def _empty_retrieval(chunks: list[SchemaChunk] | None = None) -> SchemaRetrievalResult:
    return SchemaRetrievalResult(chunks=chunks or [], query_tokens=[], expanded_tokens=[], top_k=3)


def _live(mode: str) -> Any:
    return evaluation.evaluate_cases(
        mode=mode, provider="ollama", model_name="stub:latest", use_rag=True, rag_top_k=3
    )


def test_rows_carry_an_outcome_and_none_of_the_v1_match_columns() -> None:
    df = _live(evaluation.GOLD_MODE)
    assert len(df) == 12
    assert "outcome" in df.columns
    for gone in ("value_match", "row_match", "exact_match", "dataset"):
        assert gone not in df.columns
    assert set(df["database"]) == {"university_agent", "retail_analytics", "healthcare_analytics"}


def test_evaluate_cases_redacts_a_dsn_password_in_the_error_column() -> None:
    gold_result = QueryResult(
        columns=[], rows=[], sql=None, error=f"could not connect to {DSN}: timed out"
    )
    with (
        patch("ui.evaluation.load_suite", return_value=[_case()]),
        patch("text_to_sql_agent.evaluation_v2.runner.run_gold", return_value=("S", gold_result)),
        patch(
            "text_to_sql_agent.evaluation_v2.runner.retrieve_schema_context",
            return_value=_empty_retrieval(),
        ),
    ):
        df = _live(evaluation.GOLD_MODE)

    assert df.loc[0, "outcome"] == "reference_invalid"
    assert "hunter2" not in df.loc[0, "error"]
    assert "hunter2" not in df.to_csv()


def test_schema_recall_keys_retrieved_tables_by_qualified_name() -> None:
    """A retrieved `analytics.customers` must not merge with a bare `customers`."""
    case = _case(db_path="some.db", expected_tables=("customers", "analytics.customers"))
    ok = QueryResult(columns=["x"], rows=[(1,)], sql="SELECT 1", error=None)
    default_chunk = SchemaChunk(
        table_name="customers",
        ddl="CREATE TABLE customers (id INTEGER);",
        columns=["id"],
        foreign_tables=[],
        search_text="customers",
    )
    other_chunk = SchemaChunk(
        table_name="customers",
        ddl="CREATE TABLE analytics.customers (id INTEGER);",
        columns=["id"],
        foreign_tables=[],
        search_text="analytics.customers",
        schema_name="analytics",
    )
    with (
        patch("ui.evaluation.load_suite", return_value=[case]),
        patch("text_to_sql_agent.evaluation_v2.runner.run_gold", return_value=("S", ok)),
        patch(
            "text_to_sql_agent.evaluation_v2.runner.retrieve_schema_context",
            return_value=_empty_retrieval([default_chunk, other_chunk]),
        ),
    ):
        df = _live(evaluation.GOLD_MODE)

    assert df.loc[0, "retrieved_tables"] == "analytics.customers, customers"
    assert df.loc[0, "schema_recall"] == "1.0"


def test_a_patched_live_run_has_the_same_outcome_per_case_as_run_suite(tmp_path: Path) -> None:
    """The no-disagreement proof: the tab and the CLI runner classify identically.

    The stub answers the first case correctly, the second with wrong SQL, the third with
    nothing, the fourth with a destructive statement and raises a provider outage for the
    fifth, so several distinct outcomes (and `outage`/`error`) are compared, not just `correct`.
    """
    cases = load_suite(DEMO)
    answers: dict[str, str | BaseException] = {c.question: c.gold_sql for c in cases}
    answers[cases[1].question] = "SELECT 1"
    answers[cases[2].question] = ""
    answers[cases[3].question] = "DROP TABLE students"
    answers[cases[4].question] = RuntimeError("429 quota exceeded")

    def stub(question: str, schema_text: str, **_: Any) -> str:
        answer = answers[question]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=stub):
        live = _live("Selected LLM")
        config = RunConfig(
            suite="demo",
            suite_path=DEMO,
            mode="llm",
            provider="ollama",
            model="stub:latest",
            rag_top_k=3,
        )
        result = run_suite(select_cases(config), config=config, out_root=tmp_path)

    by_tab = dict(zip(live["case"], live["outcome"], strict=True))
    by_cli = {row["id"]: row["outcome"] for row in result.rows}
    assert by_tab == by_cli
    assert len(set(by_cli.values())) >= 3, by_cli
    assert "outage" in by_cli.values()
    # Every column the two produce for a case is the same string, not just the outcome.
    for row in result.rows:
        tab_row = live[live["case"] == row["id"]].iloc[0]
        for column in ("outcome", "generated_sql", "error", "schema_recall", "retrieved_tables"):
            assert tab_row[column] == row[column], (row["id"], column)


def test_the_tab_classifies_only_through_the_runner() -> None:
    """The tab must not score, compare rows or execute SQL itself (spec section 4.6)."""
    source = Path("ui/evaluation.py").read_text(encoding="utf-8")
    assert "runner.evaluate_case(" in source
    for forbidden in (
        "score_v2(",
        "score_case(",
        "rows_equal_v2(",
        "rows_match(",
        "execute_query(",
    ):
        assert forbidden not in source, forbidden


def test_the_live_run_writes_nothing_under_the_results_directory() -> None:
    before = sorted(p.name for p in evaluation.RESULTS_ROOT.iterdir())
    _live(evaluation.GOLD_MODE)
    assert sorted(p.name for p in evaluation.RESULTS_ROOT.iterdir()) == before


# --- the result-directory viewer ------------------------------------------------------------


def _make_run(root: Path, name: str, *, citable: bool = True, reason: str = "") -> Path:
    run = root / name
    run.mkdir(parents=True)
    manifest = {
        "run_id": name,
        "status": "complete" if citable else "incomplete",
        "mode": "llm",
        "case_count": 1,
        "citable": citable,
        "citable_reason": reason,
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run / "report.md").write_text(f"# Evaluation v2: fixture {name}\n", encoding="utf-8")
    header = ",".join(runner.CSV_COLUMNS)
    values = {c: "" for c in runner.CSV_COLUMNS}
    values.update(suite="demo", id="fixture-case", outcome="correct", expected="answerable")
    (run / "cases.csv").write_text(
        header + "\n" + ",".join(values[c] for c in runner.CSV_COLUMNS) + "\n", encoding="utf-8"
    )
    return run


def test_load_result_directory_reads_the_report_and_the_case_table(tmp_path: Path) -> None:
    run = _make_run(tmp_path, "run-a")
    loaded = evaluation.load_result_directory(run)
    assert "fixture run-a" in loaded.report
    assert list(loaded.cases["id"]) == ["fixture-case"]
    assert list(loaded.cases.columns) == list(runner.CSV_COLUMNS)
    assert loaded.manifest["citable"] is True


def test_listing_skips_loose_files_and_directories_without_a_manifest(tmp_path: Path) -> None:
    _make_run(tmp_path, "run-b")
    _make_run(tmp_path, "run-a")
    (tmp_path / "evaluation_llm_gemini.csv").write_text("x", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    assert evaluation.list_result_directories(tmp_path) == ["run-a", "run-b"]
    assert evaluation.list_result_directories(tmp_path / "missing") == []


def test_the_real_results_folder_lists_no_may_files_as_runs() -> None:
    names = evaluation.list_result_directories()
    assert not [n for n in names if n.startswith(("evaluation_llm_", "evaluation_gold"))]


def test_loading_never_modifies_the_directory(tmp_path: Path) -> None:
    run = _make_run(tmp_path, "run-a")
    before = {p.name: p.read_bytes() for p in run.iterdir()}
    evaluation.load_result_directory(run)
    assert {p.name: p.read_bytes() for p in run.iterdir()} == before


# --- AppTest smoke --------------------------------------------------------------------------


def _app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=120)


def test_app_cold_start_renders_the_empty_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evaluation, "RESULTS_ROOT", tmp_path)
    at = _app().run()
    assert not at.exception
    assert any("No committed result directories" in c.value for c in at.caption)


def test_app_runs_the_demo_evaluation_in_gold_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evaluation, "RESULTS_ROOT", tmp_path)
    at = _app().run()
    at.selectbox(key="sb_eval_mode").select("Gold SQL baseline")
    next(b for b in at.button if b.label == "Run benchmark").click()
    at.run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert labels == [
        "EX (headline)",
        "Safety accuracy",
        "False-refusal rate",
        "Schema recall (mean of per-case fractions)",
    ]
    by_label = {m.label: m.value for m in at.metric}
    assert by_label["Safety accuracy"] == "not applicable (gold run)"
    assert by_label["False-refusal rate"] == "not applicable (gold run)"
    assert len(at.session_state["evaluation_df"]) == 12


def test_app_shows_a_chosen_result_directory_and_labels_a_non_citable_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_run(tmp_path, "a-run", citable=False, reason="the working tree was dirty")
    _make_run(tmp_path, "b-run")
    monkeypatch.setattr(evaluation, "RESULTS_ROOT", tmp_path)
    at = _app().run()
    assert not at.exception
    assert any("Not citable: the working tree was dirty" in w.value for w in at.warning)
    at.selectbox(key="eval_result_dir").select("b-run").run()
    assert not at.exception
    assert not any("Not citable" in w.value for w in at.warning)
    assert any("fixture b-run" in m.value for m in at.markdown)
