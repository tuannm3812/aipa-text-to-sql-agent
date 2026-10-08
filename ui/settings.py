"""The settings the sidebar collects and the rest of the UI consumes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """One render's worth of user choices, passed explicitly rather than via session state.

    The sidebar previously wrote these into `st.session_state` under string keys
    that the body read back, which made the coupling invisible once the code was
    split across modules. `st.session_state` is still used for what it is for:
    values that must survive a rerun, such as chat history.
    """

    provider: str
    model_name: str
    use_rag: bool
    rag_top_k: int
    db_path: str | None
    gemini_key: str
    key_ok: bool
    source: str
    demo_name: str
