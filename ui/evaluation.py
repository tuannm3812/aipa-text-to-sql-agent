"""The benchmark run and the evaluation summary shown above the chat."""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

import text_to_sql_agent as backend


def evaluate_cases(
    *,
    mode: str,
    provider: str,
    model_name: str,
    use_rag: bool,
    rag_top_k: int,
) -> pd.DataFrame:
    """Run every benchmark case and return one row per case.

    Args:
        mode: `"Gold SQL baseline"` to score the benchmark's own SQL, anything
            else to generate SQL with the selected model.
        provider: The LLM provider to generate with.
        model_name: The provider model identifier to generate with.
        use_rag: Whether to retrieve schema context before prompting.
        rag_top_k: How many table schemas to retrieve.

    Returns:
        A dataframe of per-case safety, execution, match, recall and latency
        columns, empty when the benchmark holds no cases.
    """
    rows: list[dict] = []
    for case in backend.load_cases():
        started = time.perf_counter()
        gold_sql, gold_result = backend.run_gold(case)

        if mode == "Gold SQL baseline":
            generated_sql = gold_sql
            result = gold_result
        else:
            generated_sql, result = backend.ask_database_with_sql(
                case["question"],
                db_path=case["db_path"],
                model_name=model_name,
                provider=provider,
                use_rag=use_rag,
                rag_top_k=rag_top_k,
            )

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        score = backend.score_case(result, gold_result)
        expected_tables = set(case.get("expected_tables", []))
        rag_context = backend.retrieve_schema_context(
            case["db_path"],
            case["question"],
            top_k=rag_top_k,
        )
        retrieved_tables = {chunk.table_name for chunk in rag_context.chunks}
        schema_recall = (
            round(len(expected_tables & retrieved_tables) / len(expected_tables), 3)
            if expected_tables
            else None
        )
        rows.append(
            {
                "case": case["id"],
                "dataset": case["dataset"],
                "difficulty": case["difficulty"],
                "safe_sql": backend.is_safe_query(generated_sql),
                "executed": score.executed,
                "row_match": score.row_match,
                "value_match": score.value_match,
                "exact_match": score.exact_match,
                "schema_recall": schema_recall,
                "prompt_saved_pct": rag_context.prompt_savings_pct,
                "cache_hit": rag_context.cache_hit,
                "expected_tables": ", ".join(sorted(expected_tables)),
                "retrieved_tables": ", ".join(sorted(retrieved_tables)),
                "latency_ms": latency_ms,
                "error": result.error or "",
            }
        )
    return pd.DataFrame(rows)


def render_evaluation() -> None:
    """Render the summary of the last benchmark run, if one has been run."""
    eval_df = st.session_state.get("evaluation_df")
    if isinstance(eval_df, pd.DataFrame) and not eval_df.empty:
        with st.expander(
            f"Evaluation summary - {st.session_state.get('evaluation_mode', 'Benchmark')}",
            expanded=True,
        ):
            total = len(eval_df)
            safe = int(eval_df["safe_sql"].sum())
            executed = int(eval_df["executed"].sum())
            value = int(eval_df["value_match"].sum())
            avg_latency = float(eval_df["latency_ms"].mean())
            avg_recall = float(eval_df["schema_recall"].mean())
            avg_saved = float(eval_df["prompt_saved_pct"].mean())
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Safe SQL", f"{safe}/{total}")
            c2.metric("Executed", f"{executed}/{total}")
            c3.metric("Value match", f"{value}/{total}")
            c4.metric("Schema recall", f"{avg_recall:.2f}")
            c5.metric("Prompt saved", f"{avg_saved:.1f}%")
            st.caption(f"Average latency: {avg_latency:.0f} ms")
            st.dataframe(eval_df, use_container_width=True, hide_index=True)
            recall_df = eval_df[
                [
                    "case",
                    "dataset",
                    "schema_recall",
                    "prompt_saved_pct",
                    "expected_tables",
                    "retrieved_tables",
                ]
            ]
            st.bar_chart(recall_df.set_index("case")["schema_recall"])
            with st.expander("Schema recall details", expanded=False):
                st.dataframe(recall_df, use_container_width=True, hide_index=True)
