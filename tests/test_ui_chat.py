"""The chat path must not let a driver exception reach the page unredacted.

`ui.chat._run_query` is the seam extracted from `render_chat`'s call to
`backend.ask_database_with_sql`: it must catch any exception the backend raises
and hand it to the existing `ui.results.describe_error` -> `redact_dsn` path
instead of letting it escape to Streamlit's default traceback renderer, which
never sees `redact_dsn`.
"""

from __future__ import annotations

from unittest.mock import patch

import ui.chat as chat
from ui.results import describe_error


def test_a_pipeline_exception_is_redacted_before_display() -> None:
    """A driver error echoing its DSN must not put a password on the page."""
    boom = RuntimeError("could not connect to postgresql://u:hunter2@db.example.com:5432/prod")
    with patch("ui.chat.backend.ask_database_with_sql", side_effect=boom):
        sql_text, result = chat._run_query(
            "list customers",
            db_path="postgresql://x",
            model_name="gemini-2.5-flash",
            provider="gemini",
            use_rag=False,
            rag_top_k=3,
        )

    assert sql_text == ""
    assert result.error is not None

    # `describe_error` is exactly what `render_assistant_turn` passes to
    # `st.error`/`st.warning`, so redacting it here proves what actually
    # reaches the page, not just that an exception was caught.
    shown = describe_error(result.error)

    assert "hunter2" not in shown
    assert "***" in shown


def test_a_successful_call_is_returned_unchanged() -> None:
    """The success path must not be altered by the extraction."""
    from text_to_sql_agent import QueryResult

    expected = ("SELECT 1", QueryResult(columns=["1"], rows=[(1,)], sql="SELECT 1", error=None))
    with patch("ui.chat.backend.ask_database_with_sql", return_value=expected):
        actual = chat._run_query(
            "how many rows",
            db_path="some.db",
            model_name="gemini-2.5-flash",
            provider="gemini",
            use_rag=True,
            rag_top_k=5,
        )

    assert actual == expected
