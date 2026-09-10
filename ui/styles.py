"""The page-level CSS injected once per render by `app.py`."""

from __future__ import annotations

CHAT_CSS = """
<style>
    .stApp {
        background: #faf9f5;
    }
    [data-testid="stSidebar"] {
        background: #f0efe9 !important;
        border-right: 1px solid #e8e6e1;
    }
    [data-testid="stHeader"] {
        background: #faf9f5;
    }
    section[data-testid="stSidebar"] .block-container {
        padding-top: 1.5rem;
    }
    .main .block-container {
        max-width: 52rem;
        padding-top: 1.25rem;
        padding-bottom: 4rem;
    }
    .stMarkdown, [data-testid="stChatMessage"] {
        font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
        color: #1f1f1f;
    }
    [data-testid="stChatInput"] textarea {
        border-radius: 1rem !important;
        border: 1px solid #e3e0d8 !important;
        background: #fff !important;
        padding-left: 1rem !important;
    }

    /* Right-align user messages (content we wrap in .chat-user-wrap) */
    .chat-user-wrap {
        display: flex;
        justify-content: flex-end;
        width: 100%;
    }
    .chat-user-row {
        display: flex;
        justify-content: flex-end;
        align-items: flex-start;
        gap: 0.55rem;
        width: 100%;
    }
    .chat-user-bubble {
        max-width: min(42rem, 85%);
        background: #e9f2ff;
        border: 1px solid #d7e6ff;
        padding: 0.55rem 0.8rem;
        border-radius: 1rem;
        margin-left: auto;
        word-wrap: break-word;
        white-space: pre-wrap;
    }
    .chat-user-avatar {
        width: 2rem;
        height: 2rem;
        border-radius: 999px;
        background: #ff4b4b;
        display: flex;
        align-items: center;
        justify-content: center;
        color: #ffffff;
        font-weight: 700;
        font-size: 0.95rem;
        flex: 0 0 auto;
        border: 1px solid rgba(0,0,0,0.06);
    }
</style>
"""
