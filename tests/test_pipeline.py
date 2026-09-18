from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

import text_to_sql_agent as agent
from text_to_sql_agent.engines import EngineUnreachableError


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
    # Verify the first call is the original question
    first_call_question = gen.call_args_list[0][0][0]
    assert first_call_question == "list customers"
    # Verify the second call's question starts with "Repair the SQL"
    second_call_question = gen.call_args_list[1][0][0]
    assert "Repair the SQL" in second_call_question
    assert result.ok, result.error
    assert result.rows == [("Alice",)]


def test_repaired_sql_is_rechecked_for_safety(customers_db: str) -> None:
    """A repair that returns unsafe SQL must not be executed."""
    from text_to_sql_agent.safety import is_safe_query as real_is_safe_query

    responses = ["SELECT nope FROM customers", "DROP TABLE customers"]
    safety_check_args: list[str] = []

    def spy_is_safe_query(sql: str, **kwargs) -> bool:
        """Wrap the real is_safe_query to record arguments."""
        safety_check_args.append(sql)
        return real_is_safe_query(sql, **kwargs)

    with (
        patch("text_to_sql_agent.pipeline.generate_sql", side_effect=responses),
        patch("text_to_sql_agent.pipeline.is_safe_query", side_effect=spy_is_safe_query),
    ):
        result = agent.ask_database("list customers", db_path=customers_db)

    # Verify is_safe_query was called twice: once for generated, once for repaired
    assert len(safety_check_args) == 2
    assert safety_check_args[0] == "SELECT nope FROM customers"
    assert safety_check_args[1] == "DROP TABLE customers"
    # Verify the unsafe repaired SQL was blocked
    assert not result.ok
    # Verify the error is the original execution error, not "not authorized"
    assert "OperationalError" in (result.error or "")
    # Verify the table still exists as a backstop
    with closing(sqlite3.connect(customers_db)) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert ("customers",) in tables, "the table must still exist"


def test_ask_database_raises_when_the_database_is_unreachable(tmp_path: Path) -> None:
    """Both entry points must reach through the engine, not `os.path.exists`."""
    missing_db = str(tmp_path / "does-not-exist.db")

    with pytest.raises(FileNotFoundError, match="input database not found"):
        agent.ask_database("list customers", db_path=missing_db)


def test_ask_database_with_sql_raises_when_the_database_is_unreachable(tmp_path: Path) -> None:
    missing_db = str(tmp_path / "does-not-exist.db")

    with pytest.raises(EngineUnreachableError, match="input database not found"):
        agent.ask_database_with_sql("list customers", db_path=missing_db)


def test_generation_receives_the_engine_dialect_section(customers_db: str) -> None:
    from text_to_sql_agent.engines import open_engine

    engine = open_engine(customers_db)
    seen: dict[str, str] = {}

    def fake_call(prompt: str, *_a: object, **_k: object) -> str:
        seen["prompt"] = prompt
        return "SELECT name FROM customers"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        agent.ask_database("list customers", db_path=customers_db)

    assert engine.prompt_dialect_section in seen["prompt"]


def test_repair_receives_the_engine_dialect_section(customers_db: str) -> None:
    """A repair instructed in the wrong dialect is the failure this prevents."""
    from text_to_sql_agent.engines import open_engine

    engine = open_engine(customers_db)
    prompts: list[str] = []

    def fake_call(prompt: str, *_a: object, **_k: object) -> str:
        prompts.append(prompt)
        return "SELECT nope FROM customers" if len(prompts) == 1 else "SELECT name FROM customers"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        agent.ask_database("list customers", db_path=customers_db)

    assert len(prompts) == 2, "one generation plus one repair"
    assert all(engine.prompt_dialect_section in p for p in prompts)


class _StandInEngine:
    """A non-SQLite dialect section, to prove the repair path is load-bearing.

    `test_repair_receives_the_engine_dialect_section` above uses a real
    `SQLiteEngine`, whose `prompt_dialect_section` is byte-identical to the
    default `generate_sql` falls back to when no engine is passed at all - so
    it cannot tell a forwarded engine apart from a silently dropped one. This
    stand-in's section differs from SQLite's, so a dropped engine changes what
    the repair prompt contains instead of leaving it identical. DuckDB does not
    exist until Task 6, so this is the only way to get a second, differing
    dialect section today.
    """

    sqlglot_dialect = "sqlite"
    internal_prefixes: tuple[str, ...] = ("sqlite_", "pragma_")
    internal_names: frozenset[str] = frozenset({"dbstat"})
    allowed_functions: frozenset[str] | None = None
    prompt_dialect_section = "STAND-IN DIALECT (must follow):\n- Definitely not SQLite.\n"
    schema_header = "Stand-in schema (DDL)"

    def check_reachable(self) -> None:
        return None


def test_repair_receives_a_non_sqlite_engines_dialect_section(customers_db: str) -> None:
    """Load-bearing: fails if `_repair_sql` stops forwarding its engine.

    Verified by temporarily dropping `engine=engine` from `_repair_sql`'s call
    to `generate_sql` in `pipeline.py`: with the engine no longer forwarded,
    `generate_sql` falls back to the SQLite default, this stand-in's section
    is absent from the second prompt, and this test fails as expected.
    """
    engine = _StandInEngine()
    prompts: list[str] = []

    def fake_call(prompt: str, *_a: object, **_k: object) -> str:
        prompts.append(prompt)
        return "SELECT nope FROM customers" if len(prompts) == 1 else "SELECT name FROM customers"

    with (
        patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call),
        patch("text_to_sql_agent.pipeline.open_engine", return_value=engine),
    ):
        result = agent.ask_database("list customers", db_path=customers_db)

    assert result.ok, result.error
    assert len(prompts) == 2, "one generation plus one repair"
    assert all(engine.prompt_dialect_section in p for p in prompts)
