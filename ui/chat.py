"""The chat surface: the demo sample question, the history replay and the input."""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd
import streamlit as st

import text_to_sql_agent as backend
from ui.constants import DEMO_DATABASES
from ui.results import render_assistant_turn, result_to_dataframe
from ui.settings import Settings


def render_sample_question(settings: Settings) -> None:
    """Render the active demo's blurb and its one-click sample question.

    Does nothing unless the sidebar's source is a demo database.

    Args:
        settings: The choices the sidebar collected for this render.
    """
    if settings.source == "Demo database":
        demo_name = settings.demo_name
        demo = DEMO_DATABASES.get(demo_name, DEMO_DATABASES["University"])
        st.markdown(f"**Active demo:** {demo_name} - {demo['description']}")
        sample_prompt = st.selectbox(
            "Try a sample question",
            [""] + demo["questions"],
            key="main_sample_question",
            label_visibility="collapsed",
        )
        if sample_prompt and st.button(
            "Use sample question", type="secondary", disabled=not settings.key_ok
        ):
            st.session_state.messages.append({"role": "user", "content": sample_prompt})
            with st.spinner("Generating SQL and running query..."):
                sql_text, result = backend.ask_database_with_sql(
                    sample_prompt,
                    db_path=str(Path(demo["path"]).resolve()),
                    model_name=settings.model_name,
                    provider=settings.provider,
                    use_rag=settings.use_rag,
                    rag_top_k=settings.rag_top_k,
                )
            df_out = result_to_dataframe(result) if result.columns else None
            rag_report = None
            schema_text = None
            try:
                rag_context = backend.retrieve_schema_context(
                    str(Path(demo["path"]).resolve()),
                    sample_prompt,
                    top_k=settings.rag_top_k,
                )
                rag_report = rag_context.report
                schema_text = rag_context.schema_text
            except Exception:
                pass
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "kind": "result",
                    "error_text": result.error,
                    "blocked_sql": result.sql if result.error else None,
                    "df": df_out,
                    "sql_text": sql_text,
                    "schema_text": schema_text,
                    "rag_report": rag_report,
                }
            )
            st.rerun()


def render_chat(settings: Settings) -> None:
    """Render the conversation so far and handle a newly submitted question.

    Args:
        settings: The choices the sidebar collected for this render.
    """
    if not st.session_state.messages:
        st.session_state.messages.append(
            {
                "role": "assistant",
                "kind": "text",
                "content": (
                    "Choose a database in the **sidebar**, then ask a question below. "
                    "I generate read-only SQL and show results as a table."
                ),
            }
        )

    for msg in st.session_state.messages:
        if msg["role"] == "user":
            safe = html.escape(str(msg.get("content", "")))
            st.markdown(
                "<div class='chat-user-wrap'>"
                "<div class='chat-user-row'>"
                f"<div class='chat-user-bubble'>{safe}</div>"
                "<div class='chat-user-avatar'>You</div>"
                "</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        else:
            render_assistant_turn(msg)

    chat_disabled = settings.db_path is None or not settings.key_ok
    placeholder = (
        "Connect a database in the sidebar..."
        if settings.db_path is None
        else "Ask anything about your data..."
    )
    prompt = st.chat_input(placeholder, disabled=chat_disabled)

    if prompt and settings.db_path and settings.key_ok:
        st.session_state.messages.append({"role": "user", "content": prompt.strip()})

        with st.spinner("Generating SQL and running query..."):
            sql_text, result = backend.ask_database_with_sql(
                prompt.strip(),
                db_path=settings.db_path,
                model_name=(settings.model_name or "").strip()
                or (
                    backend.DEFAULT_MODEL_NAME
                    if settings.provider == "gemini"
                    else backend.DEFAULT_OLLAMA_MODEL
                ),
                provider=settings.provider,
                use_rag=settings.use_rag,
                rag_top_k=settings.rag_top_k,
            )

        error_text = None
        blocked_sql = None
        df_out: pd.DataFrame | None = None

        if result.error:
            error_text = result.error
            blocked_sql = result.sql

        if result.columns:
            df_out = result_to_dataframe(result)

        schema_text: str | None = None
        rag_report: str | None = None
        try:
            if settings.use_rag:
                rag_context = backend.retrieve_schema_context(
                    settings.db_path,
                    prompt.strip(),
                    top_k=settings.rag_top_k,
                )
                schema_text = rag_context.schema_text
                rag_report = rag_context.report
            else:
                schema_text = backend.get_schema(settings.db_path)
        except Exception:
            pass

        st.session_state.messages.append(
            {
                "role": "assistant",
                "kind": "result",
                "error_text": error_text,
                "blocked_sql": blocked_sql,
                "df": df_out,
                "sql_text": sql_text,
                "schema_text": schema_text,
                "rag_report": rag_report,
            }
        )
        st.rerun()
