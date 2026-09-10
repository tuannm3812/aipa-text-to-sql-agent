from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

import text_to_sql_agent as agent


def test_ask_database_uses_retrieved_schema_by_default(customers_courses_db: str) -> None:
    captured_schema: dict[str, str] = {}

    def fake_generate_sql(_question: str, schema_text: str, **_kwargs: object) -> str:
        captured_schema["text"] = schema_text
        return "SELECT name FROM customers"

    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=fake_generate_sql):
        result = agent.ask_database(
            "list customer names", db_path=customers_courses_db, rag_top_k=1
        )

    assert result.ok
    assert "CREATE TABLE customers" in captured_schema["text"]
    assert "CREATE TABLE courses" not in captured_schema["text"]


def test_ask_database_blocks_unsafe_generated_sql(customers_db: str) -> None:
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value="DROP TABLE customers"):
        result = agent.ask_database("remove customers", db_path=customers_db)

    assert not result.ok
    assert result.error == "BLOCKED_UNSAFE_SQL"
    assert result.sql == "DROP TABLE customers"


def test_ask_database_executes_safe_generated_sql(customers_db: str) -> None:
    with patch(
        "text_to_sql_agent.pipeline.generate_sql", return_value="SELECT name FROM customers"
    ):
        result = agent.ask_database("list customers", db_path=customers_db)

    assert result.ok
    assert result.columns == ["name"]
    assert result.rows == [("Alice",)]


def test_ask_database_does_not_repair_an_aborted_query(tmp_path: Path) -> None:
    """An abort is a resource limit, not bad SQL - repairing it wastes an LLM call."""
    db_path = tmp_path / "big.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE n (i INTEGER)")
        conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(60_000)])
        conn.commit()

    runaway = "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i"
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value=runaway) as gen:
        result = agent.ask_database("count pairs", db_path=str(db_path))

    assert gen.call_count == 1, "the repair path must not fire for an aborted query"
    assert result.error == "QUERY_ABORTED_AFTER_100000_VM_STEPS"


def test_ask_database_with_sql_does_not_repair_an_aborted_query(tmp_path: Path) -> None:
    """`ask_database_with_sql` shares the same abort path and must not repair it either."""
    db_path = tmp_path / "big.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE n (i INTEGER)")
        conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(60_000)])
        conn.commit()

    runaway = "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i"
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value=runaway) as gen:
        sql, result = agent.ask_database_with_sql("count pairs", db_path=str(db_path))

    assert gen.call_count == 1, "the repair path must not fire for an aborted query"
    assert sql == runaway
    assert result.error == "QUERY_ABORTED_AFTER_100000_VM_STEPS"


def test_ask_database_with_sql_returns_generated_sql(customers_db: str) -> None:
    with patch(
        "text_to_sql_agent.pipeline.generate_sql", return_value="SELECT name FROM customers"
    ):
        sql, result = agent.ask_database_with_sql("list customers", db_path=customers_db)

    assert sql == "SELECT name FROM customers"
    assert result.ok
    assert result.rows == [("Alice",)]


def test_ask_database_with_sql_returns_empty_sql_when_generation_fails(
    customers_db: str,
) -> None:
    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=RuntimeError("no key")):
        sql, result = agent.ask_database_with_sql("list customers", db_path=customers_db)

    assert sql == ""
    assert not result.ok
    assert "RuntimeError" in (result.error or "")


def test_ask_database_with_sql_blocks_unsafe_sql(customers_db: str) -> None:
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value="DROP TABLE customers"):
        sql, result = agent.ask_database_with_sql("remove customers", db_path=customers_db)

    assert sql == "DROP TABLE customers"
    assert result.error == "BLOCKED_UNSAFE_SQL"


def test_ask_from_files_routes_a_db_path(customers_db: str) -> None:
    with patch(
        "text_to_sql_agent.pipeline.generate_sql", return_value="SELECT name FROM customers"
    ):
        result = agent.ask_from_files("list customers", [customers_db])

    assert result.ok
    assert result.rows == [("Alice",)]


def test_ask_from_files_ingests_csvs_then_queries(tmp_path: Path) -> None:
    csv_path = tmp_path / "people.csv"
    csv_path.write_text("name,age\nAlice,30\nBob,41\n", encoding="utf-8")
    out_db = tmp_path / "ingested.db"

    with patch("text_to_sql_agent.pipeline.generate_sql", return_value="SELECT name FROM people"):
        result = agent.ask_from_files("list people", [str(csv_path)], output_db_path=str(out_db))

    assert result.ok, result.error
    assert result.rows == [("Alice",), ("Bob",)]


def test_ask_from_files_rejects_mixed_extensions(tmp_path: Path) -> None:
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("a\n1\n", encoding="utf-8")
    db_path = tmp_path / "b.db"
    db_path.touch()

    with pytest.raises(ValueError, match="mixed"):
        agent.ask_from_files("q", [str(csv_path), str(db_path)])


def test_ask_from_files_rejects_an_empty_file_list() -> None:
    with pytest.raises(ValueError, match="at least one"):
        agent.ask_from_files("q", [])


def test_repair_is_attempted_once_when_execution_fails(customers_db: str) -> None:
    """A genuine SQL error should trigger exactly one repair attempt."""
    responses = ["SELECT nope FROM customers", "SELECT name FROM customers"]
    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=responses) as gen:
        result = agent.ask_database("list customers", db_path=customers_db)

    assert gen.call_count == 2, "one generation plus one repair"
    assert result.ok, result.error
    assert result.rows == [("Alice",)]


def test_repaired_sql_is_rechecked_for_safety(customers_db: str) -> None:
    """A repair that returns unsafe SQL must not be executed."""
    responses = ["SELECT nope FROM customers", "DROP TABLE customers"]
    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=responses):
        result = agent.ask_database("list customers", db_path=customers_db)

    assert not result.ok
    with closing(sqlite3.connect(customers_db)) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert ("customers",) in tables, "the table must still exist"
