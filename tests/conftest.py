"""Shared fixtures for the text-to-sql agent test suite.

PostgreSQL tests need a real server. They read `TEXT_TO_SQL_TEST_POSTGRES_DSN` and skip
with an explicit reason when it is unset or unreachable, so a developer without
Docker can still run the suite. CI sets the variable and asserts nothing skipped,
so a silent skip cannot hide a broken engine.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

POSTGRES_DSN_ENV = "TEXT_TO_SQL_TEST_POSTGRES_DSN"

_SKIP_REASON = (
    f"set {POSTGRES_DSN_ENV} to a reachable PostgreSQL DSN to run these "
    "(docker compose -f docker/postgres.yml up -d)"
)


@pytest.fixture
def customers_db(tmp_path: Path) -> str:
    """A database with one `customers` table holding a single row."""
    db_path = tmp_path / "customers.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO customers VALUES (1, 'Alice')")
        conn.commit()
    return str(db_path)


@pytest.fixture
def customers_courses_db(tmp_path: Path) -> str:
    """`customers` (one row) plus an unrelated `courses` table.

    Used where a test needs one clearly relevant table and one clearly
    irrelevant one, to assert that retrieval excludes the latter.
    """
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE courses (course_id INTEGER PRIMARY KEY, course_name TEXT)")
        conn.execute("INSERT INTO customers VALUES (1, 'Alice')")
        conn.commit()
    return str(db_path)


@pytest.fixture
def customers_sales_courses_db(tmp_path: Path) -> str:
    """Three tables where `sales` has a foreign key to `customers`.

    `courses` is deliberately unrelated, so retrieval tests can assert it is
    *not* selected.
    """
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute(
            "CREATE TABLE sales (sale_id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, "
            "FOREIGN KEY(customer_id) REFERENCES customers(customer_id))"
        )
        conn.execute("CREATE TABLE courses (course_id INTEGER PRIMARY KEY, course_name TEXT)")
        conn.commit()
    return str(db_path)


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """A DSN for a reachable PostgreSQL, or skip with an explicit reason."""
    dsn = os.environ.get(POSTGRES_DSN_ENV)
    if not dsn:
        pytest.skip(_SKIP_REASON)
    psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        pytest.skip(f"{POSTGRES_DSN_ENV} is set but unreachable: {type(exc).__name__}")
    return dsn


@dataclass
class OllamaCall:
    """One `ChatOllama(...)` construction and the user prompt its `invoke` received."""

    options: dict[str, Any]
    user_prompt: str


@dataclass
class RecordingOllama:
    """A stand-in for `llm._load_ollama_sdk` that records every Ollama call.

    Each `ChatOllama(**options)` is recorded with the user prompt its `invoke` received, and
    answers with the next of `replies` - an `AIMessage`, or a string used as its content -
    or with `SELECT 1` once they run out. No server is contacted.
    """

    replies: list[Any] = field(default_factory=list)
    calls: list[OllamaCall] = field(default_factory=list)

    @property
    def reasoning(self) -> list[Any]:
        """Each construction's `reasoning` argument, `"absent"` where none was passed."""
        return [call.options.get("reasoning", "absent") for call in self.calls]

    def sdk(self) -> tuple[Any, Any, Any]:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        recorder = self

        class ChatOllama:
            def __init__(self, **options: Any) -> None:
                self.options = options

            def invoke(self, messages: list[Any]) -> AIMessage:
                recorder.calls.append(OllamaCall(self.options, str(messages[-1].content)))
                reply = recorder.replies.pop(0) if recorder.replies else "SELECT 1"
                return reply if isinstance(reply, AIMessage) else AIMessage(content=reply)

        return ChatOllama, HumanMessage, SystemMessage


@pytest.fixture
def recording_ollama() -> Iterator[RecordingOllama]:
    """Patch the Ollama SDK with a `RecordingOllama` for the test's duration."""
    recorder = RecordingOllama()
    with patch("text_to_sql_agent.llm._load_ollama_sdk", side_effect=recorder.sdk):
        yield recorder
