"""The live demo run, its summary, and a read-only viewer for committed result directories.

The tab scores through the v2 runner's own `evaluate_case`, the same function
`run_suite` calls for every case, and builds its summary with the report's own
`metric_cells`. Nothing here classifies an outcome, so the tab and
`scripts/evaluate_v2.py` cannot disagree about one (spec section 4.6).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import text_to_sql_agent as backend
from text_to_sql_agent.evaluation_v2 import runner
from text_to_sql_agent.evaluation_v2.contract import load_suite
from text_to_sql_agent.evaluation_v2.identity import REPO_ROOT
from text_to_sql_agent.evaluation_v2.manifest import read_manifest
from text_to_sql_agent.evaluation_v2.report import COLUMNS, metric_cells

GOLD_MODE = "Gold SQL baseline"
DEMO_SUITE = REPO_ROOT / "evaluation" / "suites" / "demo.jsonl"
RESULTS_ROOT = REPO_ROOT / "evaluation" / "results"

# `evaluate_case`'s row, with the database and a dashboard-friendly `case` name added.
_TABLE_COLUMNS = (
    "case",
    "database",
    "hardness",
    "expected",
    "outcome",
    "generated_sql",
    "error",
    "latency_ms",
    "schema_recall",
    "retrieved_tables",
    "attempts",
    "generation_failure",
)


def evaluate_cases(
    *,
    mode: str,
    provider: str,
    model_name: str,
    use_rag: bool,
    rag_top_k: int,
) -> pd.DataFrame:
    """Run the demo suite in memory and return one row per case, as `cases.csv` would hold it.

    Nothing is written: the committed result directories come from the CLI only.

    Args:
        mode: `"Gold SQL baseline"` to score the suite's own SQL, anything else to
            generate SQL with the selected model.
        provider: The LLM provider to generate with.
        model_name: The provider model identifier to generate with.
        use_rag: Whether to retrieve schema context before prompting.
        rag_top_k: How many table schemas to retrieve.

    Returns:
        A dataframe of the runner's per-case strings (outcome, SQL, error, latency,
        schema recall, retrieved tables), empty when the suite holds no cases.
    """
    gold = mode == GOLD_MODE
    config = runner.RunConfig(
        suite="demo",
        suite_path=DEMO_SUITE,
        mode="gold" if gold else "llm",
        provider="gold" if gold else provider,
        model="gold" if gold else model_name,
        use_rag=use_rag,
        rag_top_k=rag_top_k,
    )
    rows: list[dict[str, str]] = []
    for case in load_suite(DEMO_SUITE):
        case = dataclasses.replace(case, db_path=runner.resolve_db_path(case.db_path))
        row = runner.evaluate_case(case, config)
        # `evaluate_case` redacts `error` (a driver error can echo a DSN's password); this
        # frame feeds `st.dataframe`, so nothing may re-introduce the raw text.
        rows.append({"case": row["id"], "database": Path(case.db_path).stem, **row})
    return pd.DataFrame(rows, columns=list(_TABLE_COLUMNS)) if rows else pd.DataFrame()


@dataclass(frozen=True)
class ResultDirectory:
    """A committed run, read back from its files."""

    name: str
    manifest: dict[str, Any]
    report: str
    cases: pd.DataFrame


def list_result_directories(root: Path | None = None) -> list[str]:
    """Names of the run directories under `root` that hold a `manifest.json`, sorted.

    Only directories count, so the May 2026 files (`evaluation_llm_*`, `evaluation_gold.*`),
    which sit loose in the same folder and predate the v2 contract, are never listed.
    """
    base = RESULTS_ROOT if root is None else root
    if not base.is_dir():
        return []
    return sorted(
        path.name for path in base.iterdir() if path.is_dir() and (path / "manifest.json").is_file()
    )


def load_result_directory(path: Path) -> ResultDirectory:
    """Read a run directory's manifest, `report.md` and `cases.csv`. Read-only.

    Raises:
        OSError, ValueError: If a file is missing or unreadable, or `cases.csv` does not
            have the contract's columns.
    """
    manifest = read_manifest(path / "manifest.json")
    report = (path / "report.md").read_text(encoding="utf-8")
    rows = runner.read_rows(path / "cases.csv")
    cases = pd.DataFrame(rows, columns=list(runner.CSV_COLUMNS))
    return ResultDirectory(name=path.name, manifest=manifest, report=report, cases=cases)


def _render_result_viewer() -> None:
    with st.expander("Load a result directory", expanded=False):
        names = list_result_directories()
        if not names:
            st.caption(
                "No committed result directories yet. Runs made with "
                "`scripts/evaluate_v2.py` appear here once they are committed."
            )
            return
        chosen = st.selectbox("Result directory", names, key="eval_result_dir")
        try:
            run = load_result_directory(RESULTS_ROOT / str(chosen))
        except (OSError, ValueError) as exc:
            st.error(f"Could not read `{chosen}`: {backend.redact_dsn(str(exc))}")
            return
        if not run.manifest.get("citable", False):
            reason = run.manifest.get("citable_reason") or "no reason recorded"
            st.warning(f"Not citable: {reason}.")
        st.caption(
            f"Status `{run.manifest.get('status', '?')}` - mode `{run.manifest.get('mode', '?')}`"
            f" - {run.manifest.get('case_count', '?')} cases"
        )
        st.markdown(run.report)
        st.dataframe(run.cases, use_container_width=True, hide_index=True)


def _summary_rows(eval_df: pd.DataFrame) -> list[dict[str, str]]:
    return [{str(k): str(v) for k, v in row.items()} for row in eval_df.to_dict("records")]


def render_evaluation() -> None:
    """Render the last live run's summary, if one has been run, and the result viewer."""
    eval_df = st.session_state.get("evaluation_df")
    if isinstance(eval_df, pd.DataFrame) and not eval_df.empty:
        mode = st.session_state.get("evaluation_mode", "Benchmark")
        with st.expander(f"Evaluation summary - {mode}", expanded=True):
            cells = metric_cells(_summary_rows(eval_df), gold=mode == GOLD_MODE)
            titles = dict(COLUMNS)
            outages = int((eval_df["outcome"] == runner.OUTAGE).sum())
            if outages:
                st.caption(
                    f"{outages} case(s) hit a provider outage and are excluded from the "
                    "metrics; the run would be incomplete in the CLI."
                )
            for key in ("ex", "safety", "false_refusal", "recall"):
                st.metric(titles[key], cells[key])
            latencies = pd.to_numeric(eval_df["latency_ms"], errors="coerce").dropna()
            if not latencies.empty:
                st.caption(f"Average latency: {latencies.mean():.0f} ms")
            st.dataframe(eval_df, use_container_width=True, hide_index=True)
            recall_df = eval_df[["case", "database", "schema_recall", "retrieved_tables"]].copy()
            recall_df["schema_recall"] = pd.to_numeric(recall_df["schema_recall"], errors="coerce")
            if recall_df["schema_recall"].notna().any():
                st.bar_chart(recall_df.set_index("case")["schema_recall"])
            with st.expander("Schema recall details", expanded=False):
                st.dataframe(recall_df, use_container_width=True, hide_index=True)
    _render_result_viewer()


__all__ = [
    "GOLD_MODE",
    "ResultDirectory",
    "evaluate_cases",
    "list_result_directories",
    "load_result_directory",
    "render_evaluation",
]
