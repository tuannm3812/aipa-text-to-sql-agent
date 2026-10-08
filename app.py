"""
Streamlit UI for the Text-to-SQL agent (`text_to_sql_agent`).

Run locally:
    streamlit run app.py

Ensure `GEMINI_API_KEY` is set in `.env` in the project root (or in your environment).
"""

from __future__ import annotations

import streamlit as st

import text_to_sql_agent as backend
from ui.chat import render_chat, render_sample_question
from ui.evaluation import render_evaluation
from ui.sidebar import render_sidebar
from ui.styles import CHAT_CSS

backend.load_env()


def main() -> None:
    st.set_page_config(
        page_title="Text-to-SQL",
        page_icon=":material/database:",
        layout="centered",
        initial_sidebar_state="expanded",
    )
    st.markdown(CHAT_CSS, unsafe_allow_html=True)

    if "messages" not in st.session_state:
        st.session_state.messages = []

    settings = render_sidebar()

    st.markdown(
        "<p style='font-size:1.65rem;font-weight:600;margin-bottom:0.15rem;"
        "color:#141413;'>Text-to-SQL</p>"
        "<p style='font-size:0.95rem;color:#6b6b6b;margin-top:0;'>"
        "Ask questions about your connected database.</p>",
        unsafe_allow_html=True,
    )

    render_sample_question(settings)
    render_evaluation()
    render_chat(settings)


if __name__ == "__main__":
    main()
