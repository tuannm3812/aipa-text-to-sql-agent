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

A 2026-09-26 review of this same boundary found one root cause behind four
more findings: the engines' own schema filtering was case-sensitive while
`safety.py`'s rules are case-insensitive, and the qualified-name spelling
built in `engines/base.py` is a flat, unescaped string. The tests below this
point (from `test_an_internal_looking_schema_is_never_advertised_or_readable`
onward) pin that fix, all against live PostgreSQL - the only engine here with
case-sensitive quoted identifiers, so the only one where any of this is
reachable. See `engines/base.py::AmbiguousTableIdentityError` and
`is_internal_schema_name` for the mechanism.
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


# ---------------------------------------------------------------------------
# 2026-09-26 review: the schema-identity boundary, PostgreSQL only.
#
# Every scenario below needs a real, case-sensitive PostgreSQL identifier
# (`"PG_evil"`, `"Public"`, a schema literally named after the connecting
# role, a dotted table name) - none of that is reachable on SQLite or DuckDB,
# whose identifiers fold to one case, so these are not parametrised over
# `case`. Each test creates its own fixture schema(s) as the `postgres`
# superuser (never through `open_engine`, which refuses a superuser DSN by
# design - see `PostgresEngine.check_reachable`) and drops them in a
# `finally`, mirroring the `case` fixture's own `analytics` cleanup.
# ---------------------------------------------------------------------------


def test_an_internal_looking_schema_is_never_advertised_or_readable(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1: schema exclusion must be case-insensitive, matching safety.py.

    At BASE, `PG_evil`/`Information_Schema` were read into `raw_schema()`/
    `table_names()` - nothing on the engine side compared candidate schema
    names case-insensitively - and then permanently rejected by
    `safety._references_internals`, which does: advertised, then refused,
    the same layer-disagreement shape `docs/3_decisions.md`'s 2026-09-25
    entry closed in the opposite direction. The fix never advertises them at
    all.
    """
    psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")
    admin = _as_postgres_superuser(postgres_dsn)

    def _cleanup(conn: Any) -> None:
        conn.execute('DROP SCHEMA IF EXISTS "PG_evil" CASCADE')
        conn.execute('DROP SCHEMA IF EXISTS "Information_Schema" CASCADE')

    with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
        _cleanup(conn)
        conn.execute('CREATE SCHEMA "PG_evil"')
        conn.execute('CREATE TABLE "PG_evil".t (id INTEGER)')
        conn.execute('GRANT USAGE ON SCHEMA "PG_evil" TO aipa_ro')
        conn.execute('GRANT SELECT ON ALL TABLES IN SCHEMA "PG_evil" TO aipa_ro')
        conn.execute('CREATE SCHEMA "Information_Schema"')
        conn.execute('CREATE TABLE "Information_Schema".t (id INTEGER)')
        conn.execute('GRANT USAGE ON SCHEMA "Information_Schema" TO aipa_ro')
        conn.execute('GRANT SELECT ON ALL TABLES IN SCHEMA "Information_Schema" TO aipa_ro')
    try:
        monkeypatch.setenv("AIPA_EXTRA_SCHEMAS", "PG_evil,Information_Schema")
        engine = open_engine(postgres_dsn)

        raw = engine.raw_schema().lower()
        assert "pg_evil" not in raw
        assert "information_schema" not in raw

        names = engine.table_names()
        assert not {n for n in names if "pg_evil" in n or "information_schema" in n}

        assert not is_safe_query('SELECT * FROM "PG_evil".t', engine=engine)
        assert not is_safe_query('SELECT * FROM "Information_Schema".t', engine=engine)
    finally:
        with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
            _cleanup(conn)


def test_a_case_variant_schema_collision_fails_closed(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: PostgreSQL's quoted identifiers are case-sensitive, so
    `public` and `"Public"` are two different real schemas that fold to the
    same lowercased spelling (`public.customers`). At BASE, `table_names()`
    silently kept whichever chunk was built last under that one spelling, so
    a reference to `"Public".customers` validated against - and, on
    execution, actually reached - a genuinely different table than the one
    every other query's `public.customers` means. The fix refuses to
    advertise or accept either spelling once a real collision like this
    exists, rather than picking a winner.
    """
    from text_to_sql_agent.engines.base import AmbiguousTableIdentityError

    psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")
    admin = _as_postgres_superuser(postgres_dsn)

    def _cleanup(conn: Any) -> None:
        conn.execute('DROP SCHEMA IF EXISTS "Public" CASCADE')

    with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
        _cleanup(conn)
        # `customers` already exists in `public` (docker/postgres-init.sql) -
        # this is the colliding second table, in a schema differing only by
        # case.
        conn.execute('CREATE SCHEMA "Public"')
        conn.execute('CREATE TABLE "Public".customers (id INTEGER, tag TEXT)')
        conn.execute("INSERT INTO \"Public\".customers VALUES (1, 'UPPER-SCHEMA-ROW')")
        conn.execute('GRANT USAGE ON SCHEMA "Public" TO aipa_ro')
        conn.execute('GRANT SELECT ON ALL TABLES IN SCHEMA "Public" TO aipa_ro')
    try:
        monkeypatch.setenv("AIPA_EXTRA_SCHEMAS", "Public")
        engine = open_engine(postgres_dsn)

        with pytest.raises(AmbiguousTableIdentityError):
            engine.table_names()
        with pytest.raises(AmbiguousTableIdentityError):
            is_safe_query('SELECT * FROM "Public".customers', engine=engine)
    finally:
        with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
            _cleanup(conn)


def test_default_schema_is_the_servers_answer_not_a_hardcoded_constant(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3: `search_path`'s stock value is `"$user", public` - a schema
    named after the connecting role shadows `public` for a bare reference.
    At BASE, `PostgresEngine.default_schema` was the constant `"public"`
    regardless of the server's own answer, so `is_safe_query` approved a
    bare `customers` reference against `public.customers`'s identity while
    PostgreSQL itself resolved the same bare name to the role-named schema's
    own `customers` - a query approved under one identity that then executed
    against a different, real table.
    """
    psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")
    admin = _as_postgres_superuser(postgres_dsn)

    def _cleanup(conn: Any) -> None:
        conn.execute("DROP SCHEMA IF EXISTS aipa_ro CASCADE")

    with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
        _cleanup(conn)
        conn.execute("CREATE SCHEMA aipa_ro")
        conn.execute("CREATE TABLE aipa_ro.customers (id INTEGER, tag TEXT)")
        conn.execute("INSERT INTO aipa_ro.customers VALUES (1, 'SHADOWED-SECRET')")
        conn.execute("GRANT USAGE ON SCHEMA aipa_ro TO aipa_ro")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA aipa_ro TO aipa_ro")
    try:
        # `public` must be opted in explicitly here: now that `default_schema`
        # correctly resolves to `aipa_ro`, `public` is no longer read for
        # free as "the default schema" the way it was at BASE - this test
        # still wants it reachable, under its own qualified spelling, to
        # prove the fix changes what the *bare* name means without taking
        # `public.customers` away.
        monkeypatch.setenv("AIPA_EXTRA_SCHEMAS", "public")
        with psycopg.connect(postgres_dsn, connect_timeout=5) as roconn:
            row = roconn.execute("SELECT current_schema()").fetchone()
        assert row is not None
        server_default = row[0]
        assert server_default == "aipa_ro", "fixture assumption: role and schema share a name"

        engine = open_engine(postgres_dsn)
        assert engine.default_schema == server_default

        assert is_safe_query("SELECT * FROM customers", engine=engine)
        result = engine.execute("SELECT * FROM customers", max_rows=10, work_limit=0)
        assert result.rows == [(1, "SHADOWED-SECRET")]

        # `public.customers` is still its own, separately reachable table -
        # the fix changes what the *bare* name means, not whether the other
        # table is still there under its own qualified spelling.
        assert is_safe_query("SELECT * FROM public.customers", engine=engine)
        public_rows = engine.execute(
            "SELECT * FROM public.customers", max_rows=10, work_limit=0
        ).rows
        assert all(row[1] != "SHADOWED-SECRET" for row in public_rows)
    finally:
        with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
            _cleanup(conn)


def test_a_dotted_table_name_colliding_with_a_qualified_spelling_fails_closed(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 4: the qualified-name namespace is a flat, unescaped string, so
    a table literally named `"analytics.thing"` in the default schema and a
    table `thing` in a schema named `analytics` both spell as
    `analytics.thing`. Both are real tables - nothing escaped in the
    reviewer's probe - but a default-deny gate approving a reference it
    never meant to advertise is still wrong, so this fails closed the same
    way finding 2's case collision does (both go through the same
    `AmbiguousTableIdentityError` check in `engines/base.py`).
    """
    from text_to_sql_agent.engines.base import AmbiguousTableIdentityError

    psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")
    admin = _as_postgres_superuser(postgres_dsn)

    def _cleanup(conn: Any) -> None:
        conn.execute('DROP TABLE IF EXISTS public."analytics.thing"')
        conn.execute("DROP SCHEMA IF EXISTS analytics CASCADE")

    with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
        _cleanup(conn)
        conn.execute('CREATE TABLE public."analytics.thing" (id INTEGER)')
        conn.execute('GRANT SELECT ON public."analytics.thing" TO aipa_ro')
        conn.execute("CREATE SCHEMA analytics")
        conn.execute("CREATE TABLE analytics.thing (id INTEGER)")
        conn.execute("GRANT USAGE ON SCHEMA analytics TO aipa_ro")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO aipa_ro")
    try:
        monkeypatch.setenv("AIPA_EXTRA_SCHEMAS", "analytics")
        engine = open_engine(postgres_dsn)
        with pytest.raises(AmbiguousTableIdentityError):
            engine.table_names()
    finally:
        with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
            _cleanup(conn)


def test_layer_agreement_over_a_hostile_multi_schema_database(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that actually matters, checked directly rather than by
    construction: every real table is advertised, validates and executes to
    the row that identifies it (not some other table's), and a schema that
    must never be advertised (finding 1) is absent everywhere at once. This
    is the check that would have caught all four findings in one run rather
    than one at a time - a mixed-case extra schema, a `PG_`-prefixed schema,
    and a dotted table name, none of which collide with each other, so the
    ordinary multi-schema case still works after the fail-closed fix
    (collisions themselves are pinned separately, above, where the assertion
    is "refuses", not "agrees").
    """
    psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")
    admin = _as_postgres_superuser(postgres_dsn)

    def _cleanup(conn: Any) -> None:
        conn.execute('DROP SCHEMA IF EXISTS "Sales" CASCADE')
        conn.execute('DROP SCHEMA IF EXISTS "PG_reports" CASCADE')
        conn.execute('DROP TABLE IF EXISTS public."weird.dotted"')

    with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
        _cleanup(conn)
        conn.execute('CREATE SCHEMA "Sales"')
        conn.execute('CREATE TABLE "Sales".orders (id INTEGER, tag TEXT)')
        conn.execute("INSERT INTO \"Sales\".orders VALUES (1, 'sales-orders-row')")
        conn.execute('GRANT USAGE ON SCHEMA "Sales" TO aipa_ro')
        conn.execute('GRANT SELECT ON ALL TABLES IN SCHEMA "Sales" TO aipa_ro')

        conn.execute('CREATE SCHEMA "PG_reports"')
        conn.execute('CREATE TABLE "PG_reports".secret (id INTEGER, tag TEXT)')
        conn.execute("INSERT INTO \"PG_reports\".secret VALUES (1, 'should-never-be-visible')")
        conn.execute('GRANT USAGE ON SCHEMA "PG_reports" TO aipa_ro')
        conn.execute('GRANT SELECT ON ALL TABLES IN SCHEMA "PG_reports" TO aipa_ro')

        conn.execute('CREATE TABLE public."weird.dotted" (id INTEGER, tag TEXT)')
        conn.execute("INSERT INTO public.\"weird.dotted\" VALUES (1, 'dotted-row')")
        conn.execute('GRANT SELECT ON public."weird.dotted" TO aipa_ro')
    try:
        monkeypatch.setenv("AIPA_EXTRA_SCHEMAS", "Sales,PG_reports")
        engine = open_engine(postgres_dsn)

        # No ambiguity in this fixture - the collision check must not fire.
        names = engine.table_names()
        # Lowercased for comparison against `names`/`spelling` below, which
        # are always lowercase - `SchemaChunk.qualified_name` itself is
        # case-preserving (`"Sales".orders` spells as `"Sales.orders"`), and
        # this fixture has no collision that lowercasing it here could cause.
        chunks = {c.qualified_name.lower(): c for c in engine.schema_chunks()}
        raw = engine.raw_schema().lower()

        # Finding 1: the PG_-prefixed schema is invisible end to end, not
        # merely rejected at the validator.
        assert not any("pg_reports" in n for n in names)
        assert "pg_reports" not in raw
        assert not is_safe_query('SELECT * FROM "PG_reports".secret', engine=engine)

        # Every other real table: advertised, in schema_chunks(), validates,
        # and executes to the one sentinel row that identifies it.
        expectations = {
            '"Sales".orders': ("sales.orders", "sales-orders-row"),
            'public."weird.dotted"': ("weird.dotted", "dotted-row"),
        }
        for sql_ref, (spelling, sentinel) in expectations.items():
            assert spelling in names, spelling
            assert spelling in chunks, spelling
            sql = f"SELECT tag FROM {sql_ref}"
            assert is_safe_query(sql, engine=engine), sql
            rows = engine.execute(sql, max_rows=10, work_limit=0).rows
            assert rows == [(sentinel,)], (sql_ref, rows)

        # The reverse direction: every advertised spelling maps back to a
        # real, distinct chunk (trivially true once the two loops above
        # agree, but checked directly rather than assumed).
        assert len({id(c) for c in chunks.values()}) == len(chunks)
    finally:
        with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
            _cleanup(conn)
