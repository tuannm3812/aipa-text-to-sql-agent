from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import text_to_sql_agent as agent
from text_to_sql_agent.engines import EngineUnreachableError, open_engine


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
    from text_to_sql_agent.safety import query_refusal as real_query_refusal

    responses = ["SELECT nope FROM customers", "DROP TABLE customers"]
    safety_check_args: list[str] = []

    def spy_query_refusal(sql: str, **kwargs) -> str | None:
        """Wrap the real query_refusal to record arguments."""
        safety_check_args.append(sql)
        return real_query_refusal(sql, **kwargs)

    with (
        patch("text_to_sql_agent.pipeline.generate_sql", side_effect=responses),
        patch("text_to_sql_agent.pipeline.query_refusal", side_effect=spy_query_refusal),
    ):
        result = agent.ask_database("list customers", db_path=customers_db)

    # Verify the safety check ran twice: once for generated, once for repaired
    assert len(safety_check_args) == 2
    assert safety_check_args[0] == "SELECT nope FROM customers"
    assert safety_check_args[1] == "DROP TABLE customers"
    # Verify the unsafe repaired SQL was blocked, and that the terminal verdict
    # is the refusal of the repair - not the stale error from the first attempt
    # (Codex review, 2026-09-27) and not a "not authorized" from executing it.
    assert not result.ok
    assert result.error == "BLOCKED_UNSAFE_SQL"
    assert result.sql == "DROP TABLE customers"
    # Verify the table still exists as a backstop
    with closing(sqlite3.connect(customers_db)) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert ("customers",) in tables, "the table must still exist"


@pytest.mark.parametrize("entry", ["ask_database", "ask_database_with_sql"])
@pytest.mark.parametrize("code", ["BLOCKED_UNSUPPORTED_COLUMN_TYPE", "BLOCKED_UNSAFE_SQL"])
def test_a_refused_repair_reports_its_refusal_not_the_stale_error(
    customers_db: str, entry: str, code: str
) -> None:
    """When the first attempt genuinely fails and the repair is refused, the
    result must carry the repair's refusal code and the refused SQL.

    Codex's 2026-09-27 review found both entry points used the repair's
    `query_refusal` verdict only as a yes/no gate: the user saw the original,
    repairable-looking SQL error instead of the reason nothing ran, so the
    dedicated `BLOCKED_UNSUPPORTED_COLUMN_TYPE` explanation never reached the
    page on this path. The verdict is injected rather than provoked, because
    SQLite has no risky column types - what is pinned here is propagation.
    """
    first, repair = "SELECT nope FROM customers", "SELECT name FROM customers"
    executed: list[str] = []
    from text_to_sql_agent.execution import execute_query as real_execute

    def spy_execute(db_path: str, sql: str, **kwargs):
        executed.append(sql)
        return real_execute(db_path, sql, **kwargs)

    with (
        patch("text_to_sql_agent.pipeline.generate_sql", side_effect=[first, repair]),
        patch("text_to_sql_agent.pipeline.query_refusal", side_effect=[None, code]),
        patch("text_to_sql_agent.pipeline.execute_query", side_effect=spy_execute),
    ):
        out = getattr(agent, entry)("list customers", db_path=customers_db)

    result = out[1] if isinstance(out, tuple) else out
    assert executed == [first], "the refused repair must never be executed"
    assert result.error == code
    assert result.sql == repair
    assert result.rows == []
    if isinstance(out, tuple):
        assert out[0] == repair, "the SQL shown is the one the verdict is about"


def test_an_ordinary_failed_repair_still_reports_its_own_error(customers_db: str) -> None:
    """An allowed repair that fails at execution keeps reporting that failure."""
    responses = ["SELECT nope FROM customers", "SELECT also_nope FROM customers"]
    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=responses):
        sql, result = agent.ask_database_with_sql("list customers", db_path=customers_db)

    assert not result.ok
    assert "also_nope" in (result.error or "")
    assert sql == "SELECT nope FROM customers"


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
    prompt_dialect_name = "Stand-In"
    prompt_engine_rules_block = (
        "- Do NOT reference any internal Stand-In tables.\n"
        "- Prefer simple SQL compatible with Stand-In.\n"
    )
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


def test_repair_user_prompt_names_the_engines_dialect(customers_db: str) -> None:
    """Phase 3a review (2026-09-21): `_repair_sql` hardcoded "SQLite error"
    and "corrected SQLite SELECT query" into the repair *user* prompt
    regardless of the target engine. The tests above only ever inspect the
    system prompt `_call_provider` receives; this inspects the user prompt,
    which is where those two strings actually lived.
    """
    engine = _StandInEngine()
    user_prompts: list[str] = []

    def fake_call(_prompt: str, user_prompt: str, *_a: object, **_k: object) -> str:
        user_prompts.append(user_prompt)
        if len(user_prompts) == 1:
            return "SELECT nope FROM customers"
        return "SELECT name FROM customers"

    with (
        patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call),
        patch("text_to_sql_agent.pipeline.open_engine", return_value=engine),
    ):
        result = agent.ask_database("list customers", db_path=customers_db)

    assert result.ok, result.error
    assert len(user_prompts) == 2, "one generation plus one repair"
    repair_user_prompt = user_prompts[1]
    assert f"{engine.prompt_dialect_name} error:" in repair_user_prompt
    assert f"corrected {engine.prompt_dialect_name} SELECT query" in repair_user_prompt
    assert "SQLite error:" not in repair_user_prompt
    assert "corrected SQLite SELECT query" not in repair_user_prompt


def test_repair_user_prompt_names_duckdbs_dialect(tmp_path: Path) -> None:
    """Same bug as `test_repair_user_prompt_names_the_engines_dialect` above,
    proven through a real `DuckDBEngine` rather than a stand-in, so the fix is
    checked against the actual engine every DuckDB user hits, not only a
    hand-rolled double.
    """
    duckdb = pytest.importorskip("duckdb", reason="install the duckdb extra")
    db_path = tmp_path / "customers.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name VARCHAR)")
    con.execute("INSERT INTO customers VALUES (1, 'Alice')")
    con.close()

    user_prompts: list[str] = []

    def fake_call(_prompt: str, user_prompt: str, *_a: object, **_k: object) -> str:
        user_prompts.append(user_prompt)
        if len(user_prompts) == 1:
            return "SELECT nope FROM customers"
        return "SELECT name FROM customers"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        result = agent.ask_database("list customers", db_path=f"duckdb://{db_path}")

    assert result.ok, result.error
    assert len(user_prompts) == 2, "one generation plus one repair"
    repair_user_prompt = user_prompts[1]
    assert "DuckDB error:" in repair_user_prompt
    assert "corrected DuckDB SELECT query" in repair_user_prompt
    assert "SQLite error:" not in repair_user_prompt
    assert "corrected SQLite SELECT query" not in repair_user_prompt


def test_repair_user_prompt_names_postgresqls_dialect(postgres_dsn: str) -> None:
    """Same bug as `test_repair_user_prompt_names_the_engines_dialect` above,
    proven through a real `PostgresEngine` rather than a stand-in - see
    `test_repair_user_prompt_names_duckdbs_dialect` for the DuckDB sibling.
    Uses the `customers` table `docker/postgres-init.sql` seeds for every
    PostgreSQL test run, rather than creating its own.
    """
    user_prompts: list[str] = []

    def fake_call(_prompt: str, user_prompt: str, *_a: object, **_k: object) -> str:
        user_prompts.append(user_prompt)
        if len(user_prompts) == 1:
            return "SELECT nope FROM customers"
        return "SELECT name FROM customers"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        result = agent.ask_database("list customers", db_path=postgres_dsn)

    assert result.ok, result.error
    assert len(user_prompts) == 2, "one generation plus one repair"
    repair_user_prompt = user_prompts[1]
    assert "PostgreSQL error:" in repair_user_prompt
    assert "corrected PostgreSQL SELECT query" in repair_user_prompt
    assert "SQLite error:" not in repair_user_prompt
    assert "corrected SQLite SELECT query" not in repair_user_prompt


def test_ask_database_with_sql_shows_the_sql_postgresql_actually_ran(postgres_dsn: str) -> None:
    """Shown-SQL decision (2026-09-27): on PostgreSQL the engine schema-qualifies
    each bare table before running it under the pinned search path, and the UI
    shows the SQL that produced the rows - the qualified text, not the model's
    bare draft. SQLite's and DuckDB's shown SQL is unchanged, since they return
    exactly what they were given (`test_ask_database_with_sql_returns_generated_sql`).
    """
    with patch(
        "text_to_sql_agent.pipeline.generate_sql",
        return_value="SELECT name FROM customers ORDER BY customer_id",
    ):
        sql, result = agent.ask_database_with_sql("list customers", db_path=postgres_dsn)

    assert result.ok, result.error
    assert sql == 'SELECT name FROM "public".customers ORDER BY customer_id'
    assert result.sql == sql
    assert result.rows == [("Alice",), ("Bob",)]


# --- per-call execution budget (Evaluation Contract v2: the runner sets it per run) ---------


def _runaway_db(tmp_path: Path) -> str:
    db_path = tmp_path / "big.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE n (i INTEGER)")
        conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(60_000)])
        conn.commit()
    return str(db_path)


@pytest.mark.parametrize("entry", ["ask_database", "ask_database_with_sql"])
def test_the_budget_reaches_execute_query(entry: str, customers_db: str) -> None:
    from text_to_sql_agent.execution import execute_query as real_execute

    with (
        patch("text_to_sql_agent.pipeline.generate_sql", return_value="SELECT 1"),
        patch("text_to_sql_agent.pipeline.execute_query", side_effect=real_execute) as spy,
    ):
        getattr(agent, entry)("q", db_path=customers_db, work_limit=123_456, max_rows=7)
        getattr(agent, entry)("q", db_path=customers_db)

    explicit, default = spy.call_args_list
    assert explicit.kwargs == {"max_rows": 7, "max_vm_steps": 123_456}
    # No budget given: exactly what the app always ran with.
    assert default.kwargs == {"max_rows": agent.DEFAULT_MAX_ROWS, "max_vm_steps": None}


def test_a_disabled_guard_lets_the_runaway_query_finish(tmp_path: Path) -> None:
    db_path = _runaway_db(tmp_path)
    runaway = "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i"
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value=runaway):
        _, default = agent.ask_database_with_sql("count pairs", db_path=db_path)
        _, unlimited = agent.ask_database_with_sql("count pairs", db_path=db_path, work_limit=0)
    assert default.error == "QUERY_ABORTED_AFTER_100000_VM_STEPS"
    assert unlimited.ok and unlimited.rows == [(60_000,)]


def test_a_failed_repair_call_is_reported_to_the_callback_and_still_swallowed(
    customers_db: str,
) -> None:
    """The callback observes the repair failure; the returned result is what it always was."""
    boom = RuntimeError("429 Too Many Requests")

    def generate(question: str, schema_text: str, **_: object) -> str:
        if question.startswith("Repair"):
            raise boom
        return "SELECT no_such_column FROM customers"

    seen: list[Exception] = []
    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=generate):
        without = agent.ask_database_with_sql("q", db_path=customers_db)
        with_callback = agent.ask_database_with_sql(
            "q", db_path=customers_db, on_repair_error=seen.append
        )
    assert seen == [boom]
    assert with_callback == without
    assert without[1].error is not None and "no_such_column" in without[1].error


BLOCKED_SENTINEL_SQL = "SELECT 'BLOCKED_UNSAFE_SQL' AS error;"
UNANSWERABLE_SENTINEL_SQL = "SELECT 'UNANSWERABLE_WITH_GIVEN_SCHEMA' AS error;"


@pytest.mark.parametrize(
    ("sentinel_sql", "code"),
    [
        (BLOCKED_SENTINEL_SQL, "BLOCKED_UNSAFE_SQL"),
        (UNANSWERABLE_SENTINEL_SQL, "UNANSWERABLE_WITH_GIVEN_SCHEMA"),
    ],
)
def test_ask_database_reports_a_sentinel_without_executing_it(
    customers_db: str, sentinel_sql: str, code: str
) -> None:
    with (
        patch("text_to_sql_agent.pipeline.generate_sql", return_value=sentinel_sql),
        patch("text_to_sql_agent.pipeline.execute_query") as spy,
    ):
        result = agent.ask_database("delete everything", db_path=customers_db)

    spy.assert_not_called()
    assert (result.error, result.columns, result.rows, result.sql) == (code, [], [], sentinel_sql)


@pytest.mark.parametrize(
    ("sentinel_sql", "code"),
    [
        (BLOCKED_SENTINEL_SQL, "BLOCKED_UNSAFE_SQL"),
        (UNANSWERABLE_SENTINEL_SQL, "UNANSWERABLE_WITH_GIVEN_SCHEMA"),
    ],
)
def test_ask_database_with_sql_reports_a_sentinel_without_executing_it(
    customers_db: str, sentinel_sql: str, code: str
) -> None:
    with (
        patch("text_to_sql_agent.pipeline.generate_sql", return_value=sentinel_sql),
        patch("text_to_sql_agent.pipeline.execute_query") as spy,
    ):
        sql, result = agent.ask_database_with_sql("delete everything", db_path=customers_db)

    spy.assert_not_called()
    assert sql == sentinel_sql
    assert (result.error, result.columns, result.rows, result.sql) == (code, [], [], sentinel_sql)


def test_a_repair_that_returns_the_blocked_sentinel_is_reported_not_executed(
    customers_db: str,
) -> None:
    answers = iter(["SELECT nope FROM customers", BLOCKED_SENTINEL_SQL])
    with patch(
        "text_to_sql_agent.pipeline.generate_sql", side_effect=lambda *_a, **_k: next(answers)
    ):
        first = agent.ask_database("list", db_path=customers_db)
    answers = iter(["SELECT nope FROM customers", BLOCKED_SENTINEL_SQL])
    with patch(
        "text_to_sql_agent.pipeline.generate_sql", side_effect=lambda *_a, **_k: next(answers)
    ):
        sql, second = agent.ask_database_with_sql("list", db_path=customers_db)

    for result in (first, second):
        assert (result.error, result.rows, result.sql) == (
            "BLOCKED_UNSAFE_SQL",
            [],
            BLOCKED_SENTINEL_SQL,
        )
    assert sql == BLOCKED_SENTINEL_SQL


def test_a_usage_scope_around_ask_database_with_sql_sums_generation_and_repair(
    customers_db: str,
) -> None:
    from text_to_sql_agent.llm import _record_usage, usage_scope

    answers = iter(["SELECT nope FROM customers", "SELECT 1"])

    def fake_generate(*_a: object, **_k: object) -> str:
        _record_usage(100, 10)
        return next(answers)

    with (
        patch("text_to_sql_agent.pipeline.generate_sql", side_effect=fake_generate),
        usage_scope() as usage,
    ):
        sql, result = agent.ask_database_with_sql("list", db_path=customers_db)
    assert (sql, result.error) == ("SELECT 1", None)
    assert (usage.prompt_tokens, usage.completion_tokens) == (200, 20)


@pytest.fixture
def events_db(tmp_path: Path) -> str:
    """`events(id, status)` whose rows hold the control codes as ordinary data."""
    db_path = tmp_path / "events.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, status TEXT)")
        conn.executemany(
            "INSERT INTO events VALUES (?, ?)",
            [(1, "BLOCKED_UNSAFE_SQL"), (2, "ok"), (3, "UNANSWERABLE_WITH_GIVEN_SCHEMA")],
        )
        conn.commit()
    return str(db_path)


# Ordinary reads that merely contain a control code: they must run and return rows.
READS_CONTAINING_A_CODE = [
    ("SELECT id FROM events /* BLOCKED_UNSAFE_SQL */", [(1,), (2,), (3,)]),
    ("SELECT id FROM events WHERE status = 'BLOCKED_UNSAFE_SQL'", [(1,)]),
    ("SELECT id FROM events WHERE status <> 'UNANSWERABLE_WITH_GIVEN_SCHEMA'", [(1,), (2,)]),
]


@pytest.mark.parametrize(("read_sql", "rows"), READS_CONTAINING_A_CODE)
def test_ask_database_runs_a_read_that_merely_contains_a_code(
    events_db: str, read_sql: str, rows: list[tuple[int]]
) -> None:
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value=read_sql):
        result = agent.ask_database("ids", db_path=events_db)

    assert result.error is None
    assert [tuple(r) for r in result.rows] == rows


@pytest.mark.parametrize(("read_sql", "rows"), READS_CONTAINING_A_CODE)
def test_ask_database_with_sql_runs_a_read_that_merely_contains_a_code(
    events_db: str, read_sql: str, rows: list[tuple[int]]
) -> None:
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value=read_sql):
        sql, result = agent.ask_database_with_sql("ids", db_path=events_db)

    assert sql == read_sql
    assert result.error is None
    assert [tuple(r) for r in result.rows] == rows


@pytest.mark.parametrize(("read_sql", "rows"), READS_CONTAINING_A_CODE)
@pytest.mark.parametrize("entry", ["ask_database", "ask_database_with_sql"])
def test_a_repair_that_merely_contains_a_code_is_executed(
    events_db: str, entry: str, read_sql: str, rows: list[tuple[int]]
) -> None:
    answers = iter(["SELECT nope FROM events", read_sql])
    with patch(
        "text_to_sql_agent.pipeline.generate_sql", side_effect=lambda *_a, **_k: next(answers)
    ):
        out = getattr(agent, entry)("ids", db_path=events_db)

    result = out if entry == "ask_database" else out[1]
    assert result.error is None
    assert [tuple(r) for r in result.rows] == rows


CONTROL_VARIANTS = [
    (BLOCKED_SENTINEL_SQL, "BLOCKED_UNSAFE_SQL"),
    (UNANSWERABLE_SENTINEL_SQL, "UNANSWERABLE_WITH_GIVEN_SCHEMA"),
    ("select 'BLOCKED_UNSAFE_SQL' as error;", "BLOCKED_UNSAFE_SQL"),
    ("SELECT 'BLOCKED_UNSAFE_SQL';", "BLOCKED_UNSAFE_SQL"),
    ("SELECT 'BLOCKED_UNSAFE_SQL' AS \"error\";", "BLOCKED_UNSAFE_SQL"),
    ("SELECT 'UNANSWERABLE_WITH_GIVEN_SCHEMA' AS error", "UNANSWERABLE_WITH_GIVEN_SCHEMA"),
    ("  \n SELECT   'BLOCKED_UNSAFE_SQL'\n  AS   error \n ;\n\n", "BLOCKED_UNSAFE_SQL"),
    ("select\n'UNANSWERABLE_WITH_GIVEN_SCHEMA'\nas \"error\"\n", "UNANSWERABLE_WITH_GIVEN_SCHEMA"),
]


@pytest.mark.parametrize(("sentinel_sql", "code"), CONTROL_VARIANTS)
def test_control_statement_variants_are_recognised_and_never_executed(
    customers_db: str, sentinel_sql: str, code: str
) -> None:
    with (
        patch("text_to_sql_agent.pipeline.generate_sql", return_value=sentinel_sql),
        patch("text_to_sql_agent.pipeline.execute_query") as spy,
    ):
        result = agent.ask_database("x", db_path=customers_db)
        _, with_sql = agent.ask_database_with_sql("x", db_path=customers_db)

    spy.assert_not_called()
    for r in (result, with_sql):
        assert (r.error, r.columns, r.rows) == (code, [], [])


NEAR_MISSES = [
    "SELECT 'BLOCKED_UNSAFE_SQL' AS error FROM customers",
    "SELECT 'BLOCKED_UNSAFE_SQL', 1",
    "SELECT 'BLOCKED_UNSAFE_SQL' AS error UNION SELECT 'x'",
    "SELECT 'BLOCKED_UNSAFE_SQL' AS error; SELECT 'UNANSWERABLE_WITH_GIVEN_SCHEMA' AS error",
    "SELECT 'BLOCKED_UNSAFE_SQL' AS error WHERE 1 = 1",
    "SELECT DISTINCT 'BLOCKED_UNSAFE_SQL' AS error",
    "WITH c AS (SELECT 1) SELECT 'BLOCKED_UNSAFE_SQL' AS error",
    "SELECT 'BLOCKED_UNSAFE_SQL' || 'x' AS error",
    "SELECT 'not a code' AS error",
    "SELECT 'BLOCKED_UNSAFE_SQL' AS error; DROP TABLE customers",
]


@pytest.mark.parametrize("near_miss", NEAR_MISSES)
def test_near_misses_are_not_sentinels_and_go_to_the_validator(
    customers_db: str, near_miss: str
) -> None:
    from text_to_sql_agent.pipeline import _sentinel_code
    from text_to_sql_agent.safety import query_refusal

    engine = open_engine(customers_db)
    assert _sentinel_code(near_miss, engine=engine) is None

    with patch("text_to_sql_agent.pipeline.generate_sql", return_value=near_miss):
        result = agent.ask_database("x", db_path=customers_db)

    # Whatever the validator decides is the verdict; the pipeline adds nothing of its own.
    expected = query_refusal(near_miss, engine=engine)
    if expected is not None:
        assert (result.error, result.rows) == (expected, [])
    else:
        assert result.error != "UNANSWERABLE_WITH_GIVEN_SCHEMA"


# --- a control statement followed by prose (extractor) -------------------------------------


def _model_says(raw: str) -> object:
    """Patch the Ollama SDK so the real extractor sees `raw` as the model's whole reply."""

    class _FakeChat:
        def __init__(self, **_: object) -> None:
            pass

        def invoke(self, _messages: object) -> SimpleNamespace:
            return SimpleNamespace(content=raw, usage_metadata=None)

    return patch(
        "text_to_sql_agent.llm._load_ollama_sdk",
        return_value=(_FakeChat, lambda content: content, lambda content: content),
    )


@pytest.mark.parametrize(
    ("sentinel_sql", "code"),
    [
        (BLOCKED_SENTINEL_SQL, "BLOCKED_UNSAFE_SQL"),
        (UNANSWERABLE_SENTINEL_SQL, "UNANSWERABLE_WITH_GIVEN_SCHEMA"),
    ],
)
@pytest.mark.parametrize(
    "prose",
    [
        " This request changes data, so I refused it.",
        "\n\nNote: the schema has no such table.",
        " I can't answer that.",
    ],
)
def test_a_control_statement_followed_by_prose_keeps_its_code(
    customers_db: str, sentinel_sql: str, code: str, prose: str
) -> None:
    with (
        _model_says(sentinel_sql + prose),
        patch("text_to_sql_agent.pipeline.execute_query") as execute,
    ):
        sql, result = agent.ask_database_with_sql(
            "q", db_path=customers_db, provider="ollama", max_repair_attempts=0
        )
    assert sql == sentinel_sql
    assert (result.error, result.rows) == (code, [])
    execute.assert_not_called()


@pytest.mark.parametrize(
    "stacked", ["DROP TABLE customers", "DROP TABLE", "SELECT name FROM customers", "-- x\nDELETE"]
)
def test_a_control_statement_followed_by_a_statement_still_reaches_the_validator(
    customers_db: str, stacked: str
) -> None:
    from text_to_sql_agent.llm import _extract_sql_from_text

    raw = f"{BLOCKED_SENTINEL_SQL} {stacked}"
    assert _extract_sql_from_text(raw) == raw
    with _model_says(raw), patch("text_to_sql_agent.pipeline.execute_query") as execute:
        sql, result = agent.ask_database_with_sql(
            "q", db_path=customers_db, provider="ollama", max_repair_attempts=0
        )
    assert sql == raw
    assert result.error == "BLOCKED_UNSAFE_SQL" and result.rows == []
    execute.assert_not_called()


def test_extraction_is_unchanged_for_fenced_prose_before_and_ordinary_queries() -> None:
    from text_to_sql_agent.llm import _extract_sql_from_text as extract

    fenced = f"```sql\n{BLOCKED_SENTINEL_SQL} trailing words\n```"
    assert extract(fenced) == f"{BLOCKED_SENTINEL_SQL} trailing words"
    assert extract(f"Sure, here you go.\n{BLOCKED_SENTINEL_SQL}") == BLOCKED_SENTINEL_SQL
    assert extract(f"Sure.\n{BLOCKED_SENTINEL_SQL}\n\nNote: unsafe.") == BLOCKED_SENTINEL_SQL
    ordinary = "SELECT name FROM customers; This lists every customer."
    assert extract(ordinary) == ordinary
    assert extract("SELECT 1 FROM t;") == "SELECT 1 FROM t;"
    assert extract("SELECT 'a;b' AS x; Done.") == "SELECT 'a;b' AS x; Done."
