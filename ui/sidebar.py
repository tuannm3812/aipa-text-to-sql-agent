"""The sidebar: provider, model, key, RAG, database source, demo data, evaluation."""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

import text_to_sql_agent as backend
from ui.constants import DEMO_DATABASES, GEMINI_MODELS, OLLAMA_MODELS
from ui.evaluation import evaluate_cases
from ui.secrets import active_gemini_key, model_name_for_provider
from ui.settings import Settings
from ui.uploads import active_db_path


def render_sidebar() -> Settings:
    """Render the whole sidebar and return the choices the body needs.

    Returns:
        The `Settings` for this render, including the database path resolved
        from whichever source the sidebar's radio selected.
    """
    with st.sidebar:
        st.markdown("### Text-to-SQL")
        st.caption("Local SQLite | read-only SQL | Gemini or Ollama")

        provider = st.selectbox(
            "LLM provider",
            ["gemini", "ollama"],
            key="sb_provider",
            help="Use Gemini API or a local Ollama model.",
        )

        model_options = GEMINI_MODELS if provider == "gemini" else OLLAMA_MODELS
        default_model = (
            backend.DEFAULT_MODEL_NAME if provider == "gemini" else backend.DEFAULT_OLLAMA_MODEL
        )
        default_index = model_options.index(default_model) if default_model in model_options else 0
        st.selectbox(
            "Model",
            model_options,
            index=default_index,
            key="sb_model_choice",
            help="Choose a known model or select Custom to type another model name.",
        )
        if st.session_state.get("sb_model_choice") == "Custom":
            st.text_input(
                "Custom model name",
                value=default_model,
                key="sb_custom_model",
                help="Use the exact provider model identifier.",
            )
        model_name = model_name_for_provider(provider)

        gemini_key = ""
        if provider == "gemini":
            with st.expander("Gemini API key", expanded=False):
                typed_key = st.text_input(
                    "API key",
                    type="password",
                    key="sb_gemini_key",
                    placeholder="Leave blank to use Streamlit secrets or local .env",
                    help="Session-only input. Do not commit API keys to GitHub.",
                )
                gemini_key = active_gemini_key(typed_key)
                if gemini_key:
                    os.environ["GEMINI_API_KEY"] = gemini_key

        key_ok = provider == "ollama" or bool(gemini_key)
        if provider == "gemini" and key_ok:
            st.success("Gemini API key loaded", icon=":material/check_circle:")
        elif provider == "gemini":
            st.warning(
                "Add a key above, in `.env`, or in Streamlit secrets.", icon=":material/warning:"
            )
        else:
            st.info("Using local Ollama. Make sure Ollama is running.")

        use_rag = st.toggle(
            "Use schema RAG",
            value=True,
            key="sb_use_rag",
            help="Retrieve only the most relevant table schemas before prompting.",
        )
        rag_top_k = st.slider(
            "Schema tables to retrieve",
            min_value=1,
            max_value=12,
            value=backend.DEFAULT_RAG_TOP_K,
            key="sb_rag_top_k",
            disabled=not use_rag,
        )

        st.divider()
        st.markdown("**Database**")

        st.radio(
            "Source",
            ["Demo database", "Path on disk", "Upload `.db`", "Upload CSV(s)"],
            key="sb_source",
            label_visibility="collapsed",
        )

        if st.session_state.get("sb_source") == "Demo database":
            demo_name = st.selectbox(
                "Demo database",
                list(DEMO_DATABASES),
                key="sb_demo_db",
            )
            demo = DEMO_DATABASES[demo_name]
            st.caption(demo["description"])
            st.caption(f"Using `{Path(demo['path']).name}`")

        elif st.session_state.get("sb_source") == "Path on disk":
            st.text_input(
                "Path to `.db`",
                value="data/university_agent.db",
                key="sb_path_db",
            )
            p = Path(st.session_state.get("sb_path_db", "")).expanduser()
            if st.session_state.get("sb_path_db") and p.is_file():
                st.caption(f"Using `{p.name}`")
            elif st.session_state.get("sb_path_db"):
                st.caption("File not found - adjust path or create demo below.")

        elif st.session_state.get("sb_source") == "Upload `.db`":
            st.file_uploader("SQLite file", type=["db"], key="sb_upload_db")
            uf = st.session_state.get("sb_upload_db")
            if uf is not None:
                st.caption(f"Uploaded `{uf.name}`")

        else:
            st.file_uploader(
                "CSV files",
                type=["csv"],
                accept_multiple_files=True,
                key="sb_upload_csv",
            )
            if st.session_state.get("sb_upload_csv"):
                st.caption("Tables use the CSV file names (without `.csv`).")

        with st.expander("Demo data"):
            st.text_input(
                "Write demo university DB to",
                value="data/university_agent.db",
                key="sb_demo_path",
            )
            if st.button("Create demo `.db`", use_container_width=True):
                demo_out = st.session_state.get("sb_demo_path", "data/university_agent.db")
                Path(demo_out).parent.mkdir(parents=True, exist_ok=True)
                try:
                    backend.write_university_db(demo_out)
                    st.success("Saved.")
                except Exception as e:
                    st.error(str(e))

        with st.expander("Evaluation"):
            eval_mode = st.selectbox(
                "Mode",
                ["Gold SQL baseline", "Selected LLM"],
                key="sb_eval_mode",
                help=(
                    "Gold SQL validates the benchmark. Selected LLM compares model "
                    "output with gold query results."
                ),
            )
            if st.button("Run benchmark", use_container_width=True):
                if eval_mode == "Selected LLM" and not key_ok:
                    st.error("Add an API key or choose Ollama before running LLM evaluation.")
                else:
                    with st.spinner("Evaluating demo databases..."):
                        st.session_state["evaluation_df"] = evaluate_cases(
                            mode=eval_mode,
                            provider=provider,
                            model_name=model_name,
                            use_rag=use_rag,
                            rag_top_k=rag_top_k,
                        )
                        st.session_state["evaluation_mode"] = eval_mode

        st.divider()
        if st.button("Clear conversation", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

    db_path_to_query: str | None = None
    try:
        db_path_to_query = active_db_path()
    except Exception as e:
        st.sidebar.error(f"Ingestion failed: {e}")

    return Settings(
        provider=provider,
        model_name=model_name,
        use_rag=use_rag,
        rag_top_k=rag_top_k,
        db_path=db_path_to_query,
        gemini_key=gemini_key,
        key_ok=key_ok,
        source=str(st.session_state.get("sb_source", "Demo database")),
        demo_name=str(st.session_state.get("sb_demo_db", "University")),
    )
