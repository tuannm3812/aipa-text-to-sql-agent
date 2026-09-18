"""Guarantees every engine must satisfy, whatever its mechanism.

Read-only is enforced differently by every engine — SQLite by a URI flag plus an
authorizer, DuckDB by a connect flag, PostgreSQL by a read-only transaction.
These tests deliberately call `engine.execute` **directly**, bypassing
`is_safe_query`, because that is what distinguishes real enforcement from a
single validation gate in front of a database that would happily write.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from text_to_sql_agent.engines import open_engine

pytestmark = pytest.mark.conformance

WRITE_STATEMENTS = [
    "INSERT INTO customers VALUES (99, 'Mallory')",
    "UPDATE customers SET name = 'x'",
    "DELETE FROM customers",
    "DROP TABLE customers",
    "CREATE TABLE evil (a INTEGER)",
]


@pytest.fixture(params=["sqlite", "duckdb"])
def engine(request, tmp_path: Path):
    """One populated database per engine under test."""
    if request.param == "sqlite":
        db = tmp_path / "c.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute(
                "CREATE TABLE sales (sale_id INTEGER PRIMARY KEY, customer_id INTEGER, "
                "FOREIGN KEY(customer_id) REFERENCES customers(customer_id))"
            )
            conn.executemany("INSERT INTO customers VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
        return open_engine(str(db))
    if request.param == "duckdb":
        duckdb = pytest.importorskip("duckdb", reason="install the duckdb extra")
        db = tmp_path / "c.duckdb"
        con = duckdb.connect(str(db))
        con.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        con.execute(
            "CREATE TABLE sales (sale_id INTEGER PRIMARY KEY, "
            "customer_id INTEGER REFERENCES customers(customer_id))"
        )
        con.execute("INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob')")
        con.close()
        return open_engine(f"duckdb://{db}")
    raise AssertionError(f"no fixture for engine {request.param!r}")


def _add_table(engine, name: str) -> None:
    """Mutate the schema underneath `engine` via a separate writable connection.

    `engine`'s own connection is read-only by design, so it cannot be the one
    that runs the DDL. Dispatches on `engine.name`, exactly like the `engine`
    fixture dispatches on `request.param` — extending either to a new engine
    means adding one branch here and one there.
    """
    if engine.name == "sqlite":
        with sqlite3.connect(engine.dsn) as conn:
            conn.execute(f"CREATE TABLE {name} (a INTEGER)")
        return
    if engine.name == "duckdb":
        import duckdb as duckdb_module

        con = duckdb_module.connect(engine.dsn)
        try:
            con.execute(f"CREATE TABLE {name} (a INTEGER)")
        finally:
            con.close()
        return
    raise AssertionError(f"no _add_table branch for engine {engine.name!r}")


def test_a_select_returns_rows_and_columns(engine) -> None:
    result = engine.execute(
        "SELECT name FROM customers ORDER BY customer_id", max_rows=10, work_limit=0
    )
    assert result.ok, result.error
    assert result.columns == ["name"]
    assert result.rows == [("Alice",), ("Bob",)]


@pytest.mark.parametrize("statement", WRITE_STATEMENTS)
def test_writes_are_refused_by_the_connection_itself(engine, statement: str) -> None:
    """Bypasses is_safe_query entirely: this is the defence-in-depth check."""
    with pytest.raises(Exception) as caught:
        engine.execute(statement, max_rows=10, work_limit=0)
    assert not isinstance(caught.value, AssertionError)

    surviving = engine.execute("SELECT COUNT(*) FROM customers", max_rows=10, work_limit=0)
    assert surviving.ok, surviving.error
    assert surviving.rows == [(2,)], "the write must not have taken effect"


def test_the_row_cap_truncates_and_says_so(engine) -> None:
    result = engine.execute("SELECT name FROM customers", max_rows=1, work_limit=0)
    assert len(result.rows) == 1
    assert result.error == "RESULT_TRUNCATED_TO_1_ROWS"


def test_a_missing_table_raises_rather_than_returning(engine) -> None:
    """pipeline.py depends on the exception to trigger its repair attempt."""
    with pytest.raises(Exception):  # noqa: B017 - engines vary in the exception type raised
        engine.execute("SELECT * FROM no_such_table", max_rows=10, work_limit=0)


def test_schema_chunks_expose_tables_columns_and_foreign_keys(engine) -> None:
    chunks = {c.table_name: c for c in engine.schema_chunks()}
    assert {"customers", "sales"} <= set(chunks)
    assert "name" in chunks["customers"].columns
    assert "customers" in chunks["sales"].foreign_tables


def test_raw_schema_mentions_every_table(engine) -> None:
    schema = engine.raw_schema()
    assert "customers" in schema
    assert "sales" in schema


def test_the_fingerprint_is_stable_when_nothing_changes(engine) -> None:
    before = engine.schema_fingerprint()
    assert engine.schema_fingerprint() == before, "must be stable when nothing changes"


def test_the_fingerprint_changes_when_the_schema_changes(engine) -> None:
    before = engine.schema_fingerprint()
    _add_table(engine, "brand_new_table")
    assert engine.schema_fingerprint() != before, "must change when the schema changes"
