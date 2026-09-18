"""End-to-end coverage: both public entry points, driven through every engine.

`tests/test_engine_conformance.py` calls `engine.execute` directly, which
cannot catch a failure that happens *before* the engine is reached - and that
is exactly where this phase found bugs: both `ask_database` and
`ask_database_with_sql` used to reject any non-file DSN with `os.path.exists`
before an engine was ever opened. These tests drive `ask_database` and
`ask_database_with_sql` the whole way through - schema retrieval, generation
(stubbed), safety validation, execution - for every engine `open_engine`
resolves, with RAG on and off.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import text_to_sql_agent as agent
from text_to_sql_agent.engines import EngineUnreachableError


@pytest.fixture(params=["sqlite", "duckdb"])
def engine_dsn(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """A populated database's DSN, one per engine under test.

    Mirrors `tests/test_engine_conformance.py`'s `engine` fixture, but returns
    the DSN string a pipeline entry point accepts rather than an opened
    `Engine`, since these tests exercise `open_engine` resolution too.
    """
    if request.param == "sqlite":
        db = tmp_path / "e2e.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob')")
        return str(db)
    if request.param == "duckdb":
        duckdb = pytest.importorskip("duckdb", reason="install the duckdb extra")
        db = tmp_path / "e2e.duckdb"
        con = duckdb.connect(str(db))
        con.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob')")
        con.close()
        return f"duckdb://{db}"
    raise AssertionError(f"no fixture for engine {request.param!r}")


@pytest.mark.parametrize("use_rag", [True, False])
@pytest.mark.parametrize("entry", ["ask_database", "ask_database_with_sql"])
def test_a_question_reaches_the_engine(engine_dsn: str, entry: str, use_rag: bool) -> None:
    """Both entry points, RAG on and off, reach execution against every engine.

    `generate_sql` is stubbed so the test is independent of any LLM provider,
    but the stub still asserts the schema text it was handed came from the
    real engine behind `engine_dsn` - proving retrieval (RAG or full-schema)
    dispatched through `open_engine` rather than assuming SQLite.
    """
    seen: dict[str, str] = {}

    def fake(_question: str, schema_text: str, **_k: object) -> str:
        seen["schema"] = schema_text
        return "SELECT name FROM customers"

    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=fake):
        if entry == "ask_database":
            result = agent.ask_database("list customers", db_path=engine_dsn, use_rag=use_rag)
        else:
            _sql, result = agent.ask_database_with_sql(
                "list customers", db_path=engine_dsn, use_rag=use_rag
            )

    assert result.ok, result.error
    assert result.columns == ["name"]
    assert result.rows == [("Alice",), ("Bob",)]
    assert "customers" in seen["schema"], "the schema must come from this engine"


def test_an_unreachable_dsn_raises_engine_unreachable_and_is_also_file_not_found() -> None:
    """Reachability is checked before the pipeline's own try block, so it raises.

    Verified directly: `ask_database('q', db_path='duckdb:///nonexistent/path.duckdb')`
    raises `EngineUnreachableError` with message "input database not found",
    which is deliberately also a `FileNotFoundError` so callers that
    historically caught that exception keep working. Both halves of that
    dual contract are pinned here.
    """
    with pytest.raises(EngineUnreachableError) as excinfo:
        agent.ask_database("q", db_path="duckdb:///nonexistent/path.duckdb")

    assert isinstance(excinfo.value, FileNotFoundError)
    assert "not found" in str(excinfo.value)
