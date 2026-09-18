"""Turning a `QueryResult` into a dataframe, a chart and a rendered chat turn."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import text_to_sql_agent as backend
from ui.uploads import redact_dsn


def result_to_dataframe(result: backend.QueryResult) -> pd.DataFrame | None:
    """Convert a `QueryResult` to a dataframe, or None when it has no columns."""
    if not result.columns:
        return None
    return pd.DataFrame(result.rows, columns=result.columns)


def chartable_columns(df: pd.DataFrame) -> tuple[str, str] | None:
    """Return (category_col, value_col) if `df` is a two-column GROUP BY-shaped result."""
    if len(df.columns) != 2 or len(df) < 2:
        return None
    first, second = df.columns[0], df.columns[1]
    first_numeric = pd.api.types.is_numeric_dtype(df[first])
    second_numeric = pd.api.types.is_numeric_dtype(df[second])
    if first_numeric and not second_numeric:
        return second, first
    if second_numeric and not first_numeric:
        return first, second
    return None


_ERROR_MESSAGES = {
    "BLOCKED_UNSAFE_SQL": (
        "The generated SQL was blocked because it was not a read-only query. "
        "The SQL is shown below so you can see what was rejected."
    ),
    "UNANSWERABLE_WITH_GIVEN_SCHEMA": (
        "The question could not be answered from this database's schema. "
        "Try rephrasing it, or pick a database that holds the relevant tables."
    ),
}

_TRUNCATED_PREFIX = "RESULT_TRUNCATED_TO_"
_ABORTED_PREFIX = "QUERY_ABORTED_AFTER_"


def describe_error(code: str | None) -> str:
    """Turn a `QueryResult.error` code into a message for a non-technical reader.

    Anything unrecognised is passed through, since the backend also puts raw
    exception text in this field - with any DSN password masked by
    `redact_dsn`, because a driver error can echo the connection string.

    Args:
        code: The `QueryResult.error` value, or None.

    Returns:
        A human-readable message, or `""` when `code` is None.
    """
    if not code:
        return ""
    if code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[code]
    if code.startswith(_TRUNCATED_PREFIX):
        limit = code[len(_TRUNCATED_PREFIX) :].removesuffix("_ROWS")
        return (
            f"Showing the first {limit} rows. The query matched more than that, "
            "so add a filter or an aggregate to narrow it down."
        )
    if code.startswith(_ABORTED_PREFIX) and code.endswith("_VM_STEPS"):
        budget = code[len(_ABORTED_PREFIX) :].removesuffix("_VM_STEPS")
        return (
            f"The query was stopped after {budget} database steps to keep the demo "
            "responsive. It was probably joining tables without a matching condition."
        )
    if code.startswith(_ABORTED_PREFIX) and code.endswith("_MS"):
        budget = code[len(_ABORTED_PREFIX) :].removesuffix("_MS")
        return (
            f"The query was stopped after {budget} ms to keep the demo "
            "responsive. It was probably joining tables without a matching condition."
        )
    # Raw exception text: a driver may echo the DSN it failed to reach.
    return redact_dsn(code)


def render_assistant_turn(msg: dict) -> None:
    """Render one assistant chat message: plain text, or a query result."""
    with st.chat_message("assistant"):
        if msg.get("kind") == "text":
            st.markdown(msg.get("content", ""))
            return
        if msg.get("kind") == "result":
            if msg.get("error_text"):
                if msg.get("df") is not None:
                    st.warning(describe_error(msg["error_text"]))
                else:
                    st.error(describe_error(msg["error_text"]))
                if msg.get("blocked_sql"):
                    st.code(msg["blocked_sql"], language="sql")
            if msg.get("df") is not None:
                df = msg["df"]
                if not df.empty:
                    st.dataframe(df, use_container_width=True, hide_index=True)
                    chart_cols = chartable_columns(df)
                    if chart_cols:
                        category_col, value_col = chart_cols
                        # Preserve the query's row order (e.g. ORDER BY ... DESC) instead of
                        # letting the chart library re-sort categories alphabetically.
                        labels = df[category_col].astype(str)
                        unique_order = list(dict.fromkeys(labels))
                        ordered = pd.Categorical(labels, categories=unique_order, ordered=True)
                        chart_df = df.assign(**{category_col: ordered}).set_index(category_col)
                        st.bar_chart(chart_df[value_col])
                else:
                    st.caption("No rows returned.")
            if msg.get("sql_text"):
                with st.expander("Generated SQL", expanded=False):
                    st.code(msg["sql_text"], language="sql")
            if msg.get("schema_text"):
                with st.expander("Schema (DDL)", expanded=False):
                    st.code(msg["schema_text"], language="sql")
            if msg.get("rag_report"):
                with st.expander("Schema RAG retrieval report", expanded=False):
                    st.text(msg["rag_report"])
