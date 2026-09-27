"""Live regressions for column types whose conversions or operators run user code.

Codex's 2026-09-27 review (Finding 1): pinning `search_path` to `pg_catalog`
stops a *name* resolving to user code, but not PostgreSQL applying a
user-defined **implicit cast** while coercing an argument for a built-in.
`SELECT upper(v) FROM source`, where `v` is an enum with an `AS IMPLICIT` cast
to `text` backed by a `SECURITY DEFINER` function, validated and returned a
secret the connecting role cannot read - the query names no cast and no
function a structural check could refuse.

The owner's decision ("refuse risky types only", `docs/3_decisions.md`,
2026-09-27): refuse a query that touches a column whose type is not a
`pg_catalog` type and either has a cast backed by a function outside
`pg_catalog` or has operators of its own outside `pg_catalog` - directly, or
through an array, a domain, a composite or a range over such a type. The
second half is what turns `citext`'s silent case-sensitive `=` under the pin
into a loud refusal. A plain enum or a domain over a built-in type has
neither, and stays queryable.

Every test asserts against real execution. Fixtures are created with raw
`psycopg` as the compose file's `postgres` superuser in a uniquely named
schema, and dropped in a `finally` whether or not an assertion failed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import pytest

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")

import text_to_sql_agent as agent  # noqa: E402
from text_to_sql_agent import is_safe_query, safety  # noqa: E402
from text_to_sql_agent.engines import open_engine  # noqa: E402
from text_to_sql_agent.engines.postgres import PostgresEngine  # noqa: E402
from text_to_sql_agent.execution import execute_query  # noqa: E402
from ui.results import describe_error  # noqa: E402

_SECRET = "PROBE_SECRET"
_REFUSAL = "BLOCKED_UNSUPPORTED_COLUMN_TYPE"


def _as_postgres_superuser(dsn: str) -> str:
    parts = urlsplit(dsn)
    netloc = f"postgres:postgres@{parts.hostname}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _admin(dsn: str, *statements: str) -> None:
    with psycopg.connect(_as_postgres_superuser(dsn), connect_timeout=5, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _as_role_unpinned(dsn: str, sql: str) -> list[tuple[Any, ...]]:
    """What PostgreSQL itself answers for `sql`, as `aipa_ro`, on its stock path."""
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.read_only = True
        return [tuple(row) for row in conn.execute(sql).fetchall()]


def _through_the_pipeline(dsn: str, sql: str) -> tuple[str, agent.QueryResult]:
    """`ask_database_with_sql` with the model replaced by `sql` - the app's own path."""
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value=sql):
        return agent.ask_database_with_sql(
            "probe", db_path=dsn, use_rag=False, max_repair_attempts=0
        )


@pytest.fixture
def probe_schema(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, uniquely named schema, opted in via `AIPA_EXTRA_SCHEMAS`, dropped after."""
    schema = f"aipa_risky_{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("AIPA_EXTRA_SCHEMAS", schema)
    try:
        _admin(
            postgres_dsn,
            f"CREATE SCHEMA {schema}",
            f"GRANT USAGE ON SCHEMA {schema} TO aipa_ro",
        )
        yield schema
    finally:
        _admin(postgres_dsn, f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture
def implicit_cast_probe(postgres_dsn: str, probe_schema: str) -> str:
    """Codex's reproduction: an enum whose implicit cast to `text` reads a secret.

    `aipa_ro` can read `source` but not `secret`; the `SECURITY DEFINER` cast
    function can. Also adds the derived shapes the rule must follow: a domain
    over the enum, an array of it, and a composite type containing it.
    """
    s = probe_schema
    _admin(
        postgres_dsn,
        f"CREATE TYPE {s}.label AS ENUM ('ordinary')",
        f"CREATE TABLE {s}.source (v {s}.label)",
        f"INSERT INTO {s}.source VALUES ('ordinary')",
        f"CREATE TABLE {s}.secret (v text)",
        f"INSERT INTO {s}.secret VALUES ('{_SECRET}')",
        f"GRANT SELECT ON {s}.source TO aipa_ro",
        f"CREATE FUNCTION {s}.cast_payload({s}.label) RETURNS text LANGUAGE sql "
        f"SECURITY DEFINER AS $$ SELECT v FROM {s}.secret $$",
        f"CREATE CAST ({s}.label AS text) WITH FUNCTION {s}.cast_payload({s}.label) AS IMPLICIT",
        f"CREATE DOMAIN {s}.label_domain AS {s}.label",
        f"CREATE TYPE {s}.label_pair AS (a {s}.label, b integer)",
        f"CREATE TABLE {s}.derived (id integer, d {s}.label_domain, "
        f"arr {s}.label[], pair {s}.label_pair)",
        f"INSERT INTO {s}.derived VALUES (1, 'ordinary', ARRAY['ordinary']::{s}.label[], "
        f"ROW('ordinary', 1)::{s}.label_pair)",
        f"GRANT SELECT ON {s}.derived TO aipa_ro",
    )
    with (
        psycopg.connect(postgres_dsn, connect_timeout=5) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(f"SELECT v FROM {s}.secret")
    return s


def test_an_implicit_user_cast_on_a_column_type_is_refused(
    postgres_dsn: str, implicit_cast_probe: str
) -> None:
    """Codex's P1: `upper(v)` coerces `v` through the user cast, under the pin.

    Armed first through the engine with the validator bypassed - the pinned
    execution itself still runs the cast and leaks the secret, so only a
    refusal before execution closes it. Then the validator must refuse it,
    and the app's own pipeline must return the dedicated code and no rows.
    """
    s = implicit_cast_probe
    sql = f"SELECT upper(v) FROM {s}.source"
    engine = open_engine(postgres_dsn)
    armed = engine.execute(sql, max_rows=10, work_limit=0)
    assert armed.rows == [(_SECRET,)], "fixture not armed"

    assert not is_safe_query(sql, engine=engine), "the implicit cast bypass validated"
    assert safety.query_refusal(sql, engine=engine) == _REFUSAL

    # Both public question entry points, with repair left on: refused before
    # execution, and never handed to a repair (one generation, no execute).
    for entry_point in (agent.ask_database, agent.ask_database_with_sql):
        with (
            patch("text_to_sql_agent.pipeline.generate_sql", return_value=sql) as generate,
            patch.object(PostgresEngine, "execute", autospec=True) as execute,
        ):
            outcome = entry_point("probe", db_path=postgres_dsn, use_rag=False)
        result = outcome[1] if isinstance(outcome, tuple) else outcome
        assert result.error == _REFUSAL, entry_point.__name__
        assert result.rows == []
        assert _SECRET not in repr(outcome)
        assert generate.call_count == 1, entry_point.__name__
        assert execute.call_count == 0, entry_point.__name__
    assert outcome[0] == sql

    message = describe_error(_REFUSAL)
    assert "read-only" not in message and "data type" in message


@pytest.mark.parametrize(
    "template",
    [
        # Explicit references, in every clause.
        "SELECT v FROM {s}.source",
        "SELECT 1 FROM {s}.source WHERE upper(v) <> ''",
        "SELECT upper(src.v) FROM {s}.source src",
        "SELECT count(*) FROM {s}.source GROUP BY v",
        "SELECT 1 FROM {s}.source ORDER BY v",
        # Passed out of a derived table or CTE under another name.
        "SELECT upper(x) FROM (SELECT v AS x FROM {s}.source) q",
        "WITH q AS (SELECT v FROM {s}.source) SELECT upper(q.v) FROM q",
        # Renamed by a table-alias column list: `x` is `v`.
        "SELECT upper(x) FROM {s}.source AS src(x)",
        # Touching columns without naming them.
        "SELECT * FROM {s}.source",
        "SELECT src.* FROM {s}.source src",
        "SELECT upper(q.v) FROM (SELECT * FROM {s}.source) q",
        "SELECT src FROM {s}.source src",
        "SELECT count(src) FROM {s}.source src",
        "SELECT 1 FROM {s}.source a JOIN {s}.source b USING (v)",
        "SELECT 1 FROM {s}.source a NATURAL JOIN {s}.source b",
        # Derived types: a domain over, an array of and a composite holding the enum.
        "SELECT upper(d) FROM {s}.derived",
        "SELECT arr FROM {s}.derived",
        "SELECT pair FROM {s}.derived",
    ],
)
def test_every_way_of_touching_a_risky_column_is_refused(
    postgres_dsn: str, implicit_cast_probe: str, template: str
) -> None:
    sql = template.format(s=implicit_cast_probe)
    engine = open_engine(postgres_dsn)
    assert safety.query_refusal(sql, engine=engine) == _REFUSAL, f"wrongly accepted: {sql!r}"


def test_a_risky_table_is_still_queryable_where_no_risky_column_is_touched(
    postgres_dsn: str, implicit_cast_probe: str
) -> None:
    """The refusal is per touched column, not per table: `id` and `count(*)` stay open."""
    s = implicit_cast_probe
    engine = open_engine(postgres_dsn)
    for sql, expected in (
        (f"SELECT id FROM {s}.derived", [(1,)]),
        (f"SELECT count(*) FROM {s}.source", [(1,)]),
    ):
        assert safety.query_refusal(sql, engine=engine) is None, sql
        assert execute_query(postgres_dsn, sql).rows == expected


def test_the_catalogue_reports_derived_risky_types(
    postgres_dsn: str, implicit_cast_probe: str
) -> None:
    """Arrays, domains and composites over a risky type are risky; `id` is not."""
    s = implicit_cast_probe
    risky = open_engine(postgres_dsn).risky_type_columns()
    assert risky[f"{s}.source"] == frozenset({"v"})
    assert risky[f"{s}.derived"] == frozenset({"d", "arr", "pair"})


# --- citext: the recorded silent cost, now a loud refusal -------------------


@pytest.fixture
def citext_table(postgres_dsn: str) -> Iterator[str]:
    """A `citext` column in `public`, where the extension normally lives.

    Only an extension this fixture created is dropped; a deployment's own is
    never touched.
    """
    table = f"citext_{uuid.uuid4().hex[:10]}"
    admin = _as_postgres_superuser(postgres_dsn)
    with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
        existing = {row[0] for row in conn.execute("SELECT extname FROM pg_extension")}
    created = "citext" not in existing
    try:
        _admin(
            postgres_dsn,
            *(["CREATE EXTENSION citext SCHEMA public"] if created else []),
            f"CREATE TABLE public.{table} (email citext, note text)",
            f"INSERT INTO public.{table} VALUES ('Alice@X.com', 'hello')",
            f"GRANT SELECT ON public.{table} TO aipa_ro",
        )
        yield table
    finally:
        _admin(
            postgres_dsn,
            f"DROP TABLE IF EXISTS public.{table}",
            *(["DROP EXTENSION IF EXISTS citext"] if created else []),
        )


def test_a_citext_comparison_is_refused_rather_than_silently_empty(
    postgres_dsn: str, citext_table: str
) -> None:
    """Under the pin `citext`'s `=` falls back to case-sensitive `text = text`.

    Armed: the server answers the row on its stock path; the pinned engine,
    validator bypassed, silently returns nothing. The validator must refuse
    it with the dedicated code, and the UI message must say why - not that
    the SQL was not read-only.
    """
    sql = f"SELECT email FROM {citext_table} WHERE email = 'alice@x.com'"
    assert _as_role_unpinned(postgres_dsn, sql) == [("Alice@X.com",)]
    engine = open_engine(postgres_dsn)
    assert engine.execute(sql, max_rows=10, work_limit=0).rows == [], "fixture not armed"

    assert not is_safe_query(sql, engine=engine), "the silently-wrong citext query validated"
    assert safety.query_refusal(sql, engine=engine) == _REFUSAL
    _, result = _through_the_pipeline(postgres_dsn, sql)
    assert result.error == _REFUSAL

    # The table's other column is untouched by the rule.
    other = f"SELECT note FROM {citext_table}"
    assert safety.query_refusal(other, engine=engine) is None
    assert execute_query(postgres_dsn, other).rows == [("hello",)]


# --- a whole-row reference: the table's own row type -------------------------


@pytest.fixture
def row_type_cast_probe(postgres_dsn: str, probe_schema: str) -> str:
    """A table whose composite row type has an implicit user cast to `text`.

    A principal owning a table can define a cast from its row type; the
    columns themselves are plain `integer`.
    """
    s = probe_schema
    _admin(
        postgres_dsn,
        f"CREATE TABLE {s}.secret (v text)",
        f"INSERT INTO {s}.secret VALUES ('{_SECRET}')",
        f"CREATE TABLE {s}.rowtab (a integer)",
        f"INSERT INTO {s}.rowtab VALUES (1)",
        f"GRANT SELECT ON {s}.rowtab TO aipa_ro",
        f"CREATE FUNCTION {s}.row_payload({s}.rowtab) RETURNS text LANGUAGE sql "
        f"SECURITY DEFINER AS $$ SELECT v FROM {s}.secret $$",
        f"CREATE CAST ({s}.rowtab AS text) WITH FUNCTION {s}.row_payload({s}.rowtab) AS IMPLICIT",
    )
    return s


def test_a_whole_row_reference_to_a_row_type_with_a_user_cast_is_refused(
    postgres_dsn: str, row_type_cast_probe: str
) -> None:
    s = row_type_cast_probe
    engine = open_engine(postgres_dsn)
    sql = f"SELECT upper(r) FROM {s}.rowtab r"
    armed = engine.execute(sql, max_rows=10, work_limit=0)
    assert armed.rows == [(_SECRET,)], "fixture not armed"

    assert not is_safe_query(sql, engine=engine), "the whole-row cast bypass validated"
    assert safety.query_refusal(sql, engine=engine) == _REFUSAL
    for variant in (
        f"SELECT upper(rowtab) FROM {s}.rowtab",
        f"SELECT upper(r.*) FROM {s}.rowtab r",
        f"SELECT * FROM {s}.rowtab",
    ):
        assert safety.query_refusal(variant, engine=engine) == _REFUSAL, variant

    # Its plain `integer` column is not the row type.
    plain = f"SELECT a FROM {s}.rowtab"
    assert safety.query_refusal(plain, engine=engine) is None
    assert execute_query(postgres_dsn, plain).rows == [(1,)]


# --- what must keep working --------------------------------------------------


@pytest.fixture
def plain_enum_and_domain(postgres_dsn: str, probe_schema: str) -> str:
    """An enum and a domain over `text`, with no user cast or operator of their own."""
    s = probe_schema
    _admin(
        postgres_dsn,
        f"CREATE TYPE {s}.status AS ENUM ('open', 'closed')",
        f"CREATE DOMAIN {s}.short_text AS text CHECK (length(VALUE) < 20)",
        f"CREATE TABLE {s}.tickets (id integer, s {s}.status, d {s}.short_text)",
        f"INSERT INTO {s}.tickets VALUES (1, 'open', 'Alpha'), (2, 'closed', 'Beta')",
        f"GRANT SELECT ON {s}.tickets TO aipa_ro",
    )
    return s


def test_a_plain_enum_and_a_domain_over_text_still_validate_and_execute(
    postgres_dsn: str, plain_enum_and_domain: str
) -> None:
    """Built-in comparison and conversion only: nothing here reaches user code."""
    s = plain_enum_and_domain
    engine = open_engine(postgres_dsn)
    assert f"{s}.tickets" not in engine.risky_type_columns()
    for sql, expected in (
        (f"SELECT id FROM {s}.tickets WHERE s = 'open'", [(1,)]),
        (f"SELECT upper(s::text) FROM {s}.tickets ORDER BY id", [("OPEN",), ("CLOSED",)]),
        (f"SELECT upper(d) FROM {s}.tickets ORDER BY id", [("ALPHA",), ("BETA",)]),
        (f"SELECT * FROM {s}.tickets ORDER BY id", [(1, "open", "Alpha"), (2, "closed", "Beta")]),
    ):
        assert _as_role_unpinned(postgres_dsn, sql) == expected
        assert is_safe_query(sql, engine=engine), f"wrongly refused: {sql!r}"
        result = execute_query(postgres_dsn, sql)
        assert result.error is None, result.error
        assert result.rows == expected
