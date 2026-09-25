"""Schema-qualified table identity, held to the same contract on every engine.

Phase 3b Task 6. `docs/3_decisions.md`'s 2026-09-25 entry scoped DuckDB to its
`main` schema alone because three layers disagreed about the supported
surface, producing two concrete failures: a duplicate table name across
schemas crashed schema building, and a table outside the default schema was
advertised to the model by `raw_schema()` and then rejected by the validator.
This module pins the replacement contract, which must not reintroduce either:

* a table in a **non-default** schema is advertised by `raw_schema()`, appears
  in `table_names()` under its `schema.table` form, passes `is_safe_query`
  when referenced that way, and actually executes;
* its **bare** name is not in `table_names()` and is rejected, because a bare
  name resolves against the engine's own default schema and would fail at
  execution - the 2026-09-25 inversion in mirror image;
* a table in the default schema keeps working under **both** spellings, bare
  and qualified;
* a nonexistent `other.missing` is still rejected, and the engine's internal
  schemas (`information_schema`, `pg_catalog`) still are too, whatever
  user schemas exist.

SQLite is parametrised in for the default-schema half only. A second schema
there means `ATTACH DATABASE`, which this project's DSN model has no shape for
(a DSN is one file path) and which the read-only authorizer denies outright -
see `test_sqlite_has_exactly_one_schema_because_attach_is_denied`. Faking a
second SQLite schema would test a shape the engine does not have.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from text_to_sql_agent import is_safe_query
from text_to_sql_agent.engines import open_engine

OTHER_SCHEMA = "analytics"


@dataclass(frozen=True)
class _Case:
    """One engine under test, plus what a second schema means for it."""

    engine: Any
    # `None` for an engine whose DSN model has exactly one schema (SQLite).
    other_schema: str | None


def _as_postgres_superuser(dsn: str) -> str:
    """Swap the DSN's role for the compose file's `postgres` superuser.

    Same one-line substitution `test_engine_conformance.py` and
    `test_engine_postgres.py` each make for the same reason: only a superuser
    can create the second schema these tests need, while the engine itself
    must keep connecting as the least-privilege `aipa_ro` role.
    """
    parts = urlsplit(dsn)
    netloc = f"postgres:postgres@{parts.hostname}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture(params=["sqlite", "duckdb", "postgres"])
def case(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Case]:
    """A database holding `customers` in the default schema.

    For every engine whose DSN model admits one, it also holds
    `analytics.thing`, a table that exists *only* outside the default schema.
    Schema scope became opt-in after this fixture was written (owner
    decision, 2026-09-26), so `monkeypatch.setenv("AIPA_EXTRA_SCHEMAS", ...)`
    opts `analytics` in before either engine is constructed - without it,
    every test below that relies on `analytics.thing` existing would instead
    be proving the opt-in default (invisible), not the identity contract this
    module exists to pin.

    `thing.lo_get` is named after a single-argument PostgreSQL catalogue
    function deliberately (added 2026-09-26): it is what arms the
    `column_call_lo_get` probe below. While the validator resolved a
    qualified column against a flat union of every advertised column, this
    one column - in a schema only reachable *because* of Task 6's widening -
    was enough to re-arm PostgreSQL's `alias.name` -> `name(alias)` bypass
    for every function scan in the database. Unarmed, that probe passes
    whatever the resolution model does.
    """
    if request.param == "sqlite":
        db = tmp_path / "identity.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO customers VALUES (1, 'Alice')")
        yield _Case(engine=open_engine(str(db)), other_schema=None)
        return

    if request.param == "duckdb":
        duckdb = pytest.importorskip("duckdb", reason="install the duckdb extra")
        db = tmp_path / "identity.duckdb"
        con = duckdb.connect(str(db))
        con.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO customers VALUES (1, 'Alice')")
        con.execute(f"CREATE SCHEMA {OTHER_SCHEMA}")
        con.execute(f"CREATE TABLE {OTHER_SCHEMA}.thing (label VARCHAR, lo_get INTEGER)")
        con.execute(f"INSERT INTO {OTHER_SCHEMA}.thing VALUES ('only-here', 1)")
        con.close()
        monkeypatch.setenv("AIPA_EXTRA_SCHEMAS", OTHER_SCHEMA)
        yield _Case(engine=open_engine(f"duckdb://{db}"), other_schema=OTHER_SCHEMA)
        return

    if request.param == "postgres":
        dsn = request.getfixturevalue("postgres_dsn")
        psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")
        admin = _as_postgres_superuser(dsn)
        # `customers` already exists in `public` from docker/postgres-init.sql.
        with psycopg.connect(admin, connect_timeout=5) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {OTHER_SCHEMA} CASCADE")
            conn.execute(f"CREATE SCHEMA {OTHER_SCHEMA}")
            conn.execute(f"CREATE TABLE {OTHER_SCHEMA}.thing (label TEXT, lo_get INTEGER)")
            conn.execute(f"INSERT INTO {OTHER_SCHEMA}.thing VALUES ('only-here', 1)")
            conn.execute(f"GRANT USAGE ON SCHEMA {OTHER_SCHEMA} TO aipa_ro")
            conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {OTHER_SCHEMA} TO aipa_ro")
        try:
            monkeypatch.setenv("AIPA_EXTRA_SCHEMAS", OTHER_SCHEMA)
            yield _Case(engine=open_engine(dsn), other_schema=OTHER_SCHEMA)
        finally:
            # The container outlives the test, unlike tmp_path, so this schema
            # must not leak into any other test's view of the database.
            with psycopg.connect(admin, connect_timeout=5) as conn:
                conn.execute(f"DROP SCHEMA IF EXISTS {OTHER_SCHEMA} CASCADE")
        return

    raise AssertionError(f"no fixture for engine {request.param!r}")


def _requires_second_schema(case: _Case) -> str:
    if case.other_schema is None:
        pytest.skip(
            f"{case.engine.name} has exactly one schema in this project's DSN model - "
            "see the module docstring and "
            "test_sqlite_has_exactly_one_schema_because_attach_is_denied"
        )
    return case.other_schema


def test_a_default_schema_table_works_bare_and_qualified(case: _Case) -> None:
    """Both spellings resolve, on every engine.

    The bare form is what every pre-Phase-3b query and all twelve gold cases
    use; the qualified form is what a model told about schemas may well write,
    and the engine itself accepts it, so the validator must too.
    """
    engine = case.engine
    qualified = f"{engine.default_schema}.customers"
    names = engine.table_names()
    assert "customers" in names
    assert qualified in names
    assert is_safe_query("SELECT name FROM customers", engine=engine)
    assert is_safe_query(f"SELECT name FROM {qualified}", engine=engine)
    # `("Alice",)` rather than the whole result: PostgreSQL's `customers`
    # comes from `docker/postgres-init.sql` and holds two rows, while the
    # sqlite/duckdb fixtures build one. What matters here is that the
    # qualified spelling reaches the same table, not the row count.
    rows = engine.execute(f"SELECT name FROM {qualified}", max_rows=10, work_limit=0).rows
    assert ("Alice",) in rows


def test_a_non_default_schema_table_is_advertised_and_runs(case: _Case) -> None:
    """The 2026-09-25 inversion, closed from the other side.

    That entry's cost was explicit: "a DuckDB database whose tables all live
    outside `main` now presents an empty schema and answers
    `UNANSWERABLE_WITH_GIVEN_SCHEMA`". This is the test that says it no longer
    does - advertised, validated and executed, all under one spelling.
    """
    other = _requires_second_schema(case)
    engine = case.engine
    assert "thing" in engine.raw_schema()
    assert f"{other}.thing" in engine.table_names()
    sql = f"SELECT label FROM {other}.thing"
    assert is_safe_query(sql, engine=engine)
    assert engine.execute(sql, max_rows=10, work_limit=0).rows == [("only-here",)]


def test_the_bare_name_of_a_non_default_schema_table_is_rejected(case: _Case) -> None:
    """A bare name resolves against the engine's own default schema, so
    accepting one for a table that lives elsewhere would recreate the
    2026-09-25 failure in mirror image: validated here, then failing at
    execution with no table of that name in the default schema.
    """
    _requires_second_schema(case)
    engine = case.engine
    assert "thing" not in engine.table_names()
    assert not is_safe_query("SELECT label FROM thing", engine=engine)
    with pytest.raises(Exception):  # noqa: B017 - engines vary in the exception type
        engine.execute("SELECT label FROM thing", max_rows=10, work_limit=0)


def test_a_nonexistent_table_in_a_real_schema_is_still_rejected(case: _Case) -> None:
    """Widening the universe to every schema must not widen it to every name."""
    other = _requires_second_schema(case)
    engine = case.engine
    assert not is_safe_query(f"SELECT * FROM {other}.missing", engine=engine)
    assert not is_safe_query("SELECT * FROM no_such_schema.thing", engine=engine)


def test_engine_internals_stay_rejected_whatever_schemas_exist(case: _Case) -> None:
    """The internals rules are name rules and do not consult `table_names()`,
    so a database that now has several real schemas must not have gained a
    route into `information_schema` or `pg_catalog`.
    """
    _requires_second_schema(case)
    engine = case.engine
    assert not is_safe_query("SELECT * FROM information_schema.tables", engine=engine)
    assert not is_safe_query("SELECT * FROM pg_catalog.pg_class", engine=engine)


# Widening the table and column universe is the one way this task could have
# broken the default-deny gates: `_references_unknown_table` and
# `_references_unresolvable_qualified_column` both decide by membership, so a
# bigger set is a weaker gate unless the widening is exactly the user's own
# tables. These are the payloads earlier tasks in this phase closed - the
# three PostgreSQL validator bypasses and DuckDB's function/table default-deny
# - re-run against a database that now has a second real schema in it, which
# is the state those tests never cover.
_BYPASS_PROBES_BY_ENGINE: dict[str, list[tuple[str, str]]] = {
    "postgres": [
        ("dot_call", "SELECT ('port').current_setting"),
        ("dot_call_regclass", "SELECT ('customers'::regclass).pg_relation_filepath"),
        ("oid_cast", "SELECT g::regclass AS rel FROM generate_series(16380, 16400) AS g"),
        ("column_call", "SELECT g.pg_relation_filepath FROM generate_series(16384,16400) g"),
        # No `pg_` prefix, so only the default-deny column resolution refuses
        # this one - the check whose universe this task widened.
        ("column_call_lo_get", "SELECT g.lo_get FROM generate_series(16384,16400) g"),
        ("qualified_internal_table", "SELECT * FROM pg_catalog.pg_authid"),
    ],
    "duckdb": [
        ("function_default_deny", "SELECT * FROM read_csv('/etc/passwd')"),
        ("bare_quoted_path", "SELECT * FROM '/etc/passwd'"),
        ("unquoted_path", "SELECT * FROM data.csv"),
        ("query_table_string_argument", "SELECT * FROM query_table('information_schema.tables')"),
        ("administrative_function", "SELECT checkpoint()"),
    ],
}


def test_the_closed_bypasses_stay_closed_with_a_second_schema(case: _Case) -> None:
    """A wider universe must not be a weaker gate."""
    _requires_second_schema(case)
    probes = _BYPASS_PROBES_BY_ENGINE[case.engine.name]
    for label, sql in probes:
        assert not is_safe_query(sql, engine=case.engine), f"{label} should stay rejected: {sql!r}"


def test_chunks_carry_their_schema_and_a_qualified_name(case: _Case) -> None:
    """`SchemaChunk.schema_name` is what carries identity past the engine
    boundary - `rag.py` and the prompt see chunks, not a catalogue.
    """
    other = _requires_second_schema(case)
    chunks = {c.qualified_name: c for c in case.engine.schema_chunks()}
    assert chunks["customers"].schema_name == ""
    assert chunks[f"{other}.thing"].schema_name == other
    assert chunks[f"{other}.thing"].table_name == "thing"


def test_sqlite_has_exactly_one_schema_because_attach_is_denied(tmp_path: Path) -> None:
    """SQLite's only route to a second schema is `ATTACH DATABASE`, and this
    project has no shape for it: a SQLite DSN is one file path, and
    `SQLiteEngine`'s read-only authorizer denies `SQLITE_ATTACH` outright.
    So `main` is not a narrowing of SQLite the way `main` was a narrowing of
    DuckDB - it is the whole engine. This test is the proof, rather than a
    faked second schema that would pin a shape the engine does not have.
    """
    db = tmp_path / "one.db"
    other = tmp_path / "two.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE elsewhere (a INTEGER)")

    engine = open_engine(str(db))
    assert engine.default_schema == "main"
    with pytest.raises(sqlite3.DatabaseError):
        engine.execute(f"ATTACH DATABASE '{other}' AS two", max_rows=10, work_limit=0)
    assert engine.table_names() == frozenset({"customers", "main.customers"})
