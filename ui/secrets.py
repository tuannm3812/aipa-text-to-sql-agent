"""Resolving API keys and model names from Streamlit widgets, env vars and secrets."""

from __future__ import annotations

import os

import streamlit as st

import text_to_sql_agent as backend


def streamlit_secret(name: str) -> str:
    """Read a value from `st.secrets`, tolerating the absence of a secrets file."""
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:
        return ""


def active_gemini_key(typed_key: str) -> str:
    """Resolve the Gemini API key from the sidebar input, env var or Streamlit secrets."""
    return (
        (typed_key or "").strip()
        or (os.environ.get("GEMINI_API_KEY") or "").strip()
        or streamlit_secret("GEMINI_API_KEY").strip()
    )


def model_name_for_provider(provider: str) -> str:
    """Resolve the selected model name for `provider` from sidebar session state."""
    selected = st.session_state.get("sb_model_choice", "")
    if selected == "Custom":
        return (st.session_state.get("sb_custom_model") or "").strip()
    return str(
        selected
        or (backend.DEFAULT_MODEL_NAME if provider == "gemini" else backend.DEFAULT_OLLAMA_MODEL)
    )
