"""Live regressions for the pinned `search_path` and engine-owned table qualification.

PostgreSQL resolves functions, **operators** and types through `search_path`,
and an exact argument-type match in any schema on the path beats a built-in
that needs a cast, whatever the path order. `3cf40dc` closed that for function
calls by name; it could not close it for operators, which carry no name the
validator can pin. The owner-approved fix (2026-09-27, `docs/3_decisions.md`)
is to execute every query with `SET LOCAL search_path = pg_catalog` and have
the engine schema-qualify every bare table reference itself, using its own
resolution of each bare name - which also closes Codex's Finding 2, because
the server no longer resolves bare names at all.

Every test here asserts against **real execution**, not only the validator:
where the pin itself closes a shape, `engine.execute` (which bypasses
`is_safe_query`) must return the built-in's answer or refuse; where only the
validator can close a shape (an explicit `OPERATOR(public.op)` or a
`::public.type` cast names its schema, so the pin cannot reach it), the test
proves the payload is armed by executing it directly and then proves the
validator refuses it.

Fixtures are created with raw `psycopg` as the compose file's `postgres`
superuser - `aipa_ro` cannot create anything, which is the honest precondition
recorded in `docs/3_decisions.md` - and dropped in a `finally` whether or not
an assertion failed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")

from text_to_sql_agent import is_safe_query  # noqa: E402
from text_to_sql_agent.engines import open_engine  # noqa: E402
from text_to_sql_agent.execution import execute_query  # noqa: E402

_SECRET_SCHEMA = "pin_secret"
_SECRET = "PROBE_SECRET"


def _as_postgres_superuser(dsn: str) -> str:
    """Swap the DSN's role for the compose file's `postgres` superuser.

    The same one-line substitution the other live test modules make, for the
    same reason: only a superuser can create the hostile objects these tests
    need, while the engine itself keeps connecting as `aipa_ro`.
    """
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
    """What PostgreSQL itself answers for `sql`, as `aipa_ro`, with its stock path.

    Deliberately not through the engine: this is the server's own resolution
    of every bare name, operator and function, the thing the engine's pinned
    execution is compared against.
    """
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.read_only = True
        return [tuple(row) for row in conn.execute(sql).fetchall()]


@pytest.fixture
def secret(postgres_dsn: str) -> Iterator[str]:
    """A table holding `PROBE_SECRET` in a schema `aipa_ro` holds no `USAGE` on.

    Every hostile object below is a `SECURITY DEFINER` function that reads it,
    so a leaked `PROBE_SECRET` in a result is proof that a user-defined object
    ran with its owner's privileges rather than the connecting role's.
    """
    _admin(
        postgres_dsn,
        f"DROP SCHEMA IF EXISTS {_SECRET_SCHEMA} CASCADE",
        f"CREATE SCHEMA {_SECRET_SCHEMA}",
        f"CREATE TABLE {_SECRET_SCHEMA}.secret (marker TEXT)",
        f"INSERT INTO {_SECRET_SCHEMA}.secret VALUES ('{_SECRET}')",
    )
    try:
        with (
            psycopg.connect(postgres_dsn, connect_timeout=5) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(f"SELECT marker FROM {_SECRET_SCHEMA}.secret")
        yield _SECRET
    finally:
        _admin(postgres_dsn, f"DROP SCHEMA IF EXISTS {_SECRET_SCHEMA} CASCADE")


# --- operators: the bypass `3cf40dc` could not see ---------------------------


@pytest.fixture
def evil_concat_operator(postgres_dsn: str, secret: str) -> Iterator[None]:
    """`public.||(text, integer)`, a `SECURITY DEFINER` operator returning the secret."""
    try:
        _admin(
            postgres_dsn,
            "DROP OPERATOR IF EXISTS public.|| (text, integer)",
            "DROP FUNCTION IF EXISTS public.pin_evil_cat(text, integer)",
            "CREATE FUNCTION public.pin_evil_cat(text, integer) RETURNS text "
            f"SECURITY DEFINER LANGUAGE sql AS $$ SELECT marker FROM {_SECRET_SCHEMA}.secret $$",
            "CREATE OPERATOR public.|| "
            "(LEFTARG = text, RIGHTARG = integer, FUNCTION = public.pin_evil_cat)",
        )
        yield
    finally:
        _admin(
            postgres_dsn,
            "DROP OPERATOR IF EXISTS public.|| (text, integer)",
            "DROP FUNCTION IF EXISTS public.pin_evil_cat(text, integer)",
        )


def test_a_user_concat_operator_is_unreachable_from_a_bare_operator(
    postgres_dsn: str, evil_concat_operator: None
) -> None:
    """`name || 1` must reach the built-in `||`, never `public.||(text, integer)`.

    The operator is armed first - PostgreSQL itself, as `aipa_ro` on its stock
    path, dispatches the bare `||` to it and leaks the secret - and then the
    same statement through the engine must return the built-in's answer.
    """
    sql = "SELECT name || 1 FROM customers ORDER BY customer_id"
    assert _as_role_unpinned(postgres_dsn, sql) == [(_SECRET,), (_SECRET,)], "fixture not armed"

    engine = open_engine(postgres_dsn)
    assert is_safe_query(sql, engine=engine)
    result = execute_query(postgres_dsn, sql)
    assert result.error is None
    assert result.rows == [("Alice1",), ("Bob1",)]


@pytest.fixture
def evil_equals_operator(postgres_dsn: str) -> Iterator[None]:
    """`public.=(text, integer)`, always true, so dispatch shows in the row count."""
    try:
        _admin(
            postgres_dsn,
            "DROP OPERATOR IF EXISTS public.= (text, integer)",
            "DROP FUNCTION IF EXISTS public.pin_evil_eq(text, integer)",
            "CREATE FUNCTION public.pin_evil_eq(text, integer) RETURNS boolean "
            "SECURITY DEFINER LANGUAGE sql AS $$ SELECT true $$",
            "CREATE OPERATOR public.= "
            "(LEFTARG = text, RIGHTARG = integer, FUNCTION = public.pin_evil_eq)",
        )
        yield
    finally:
        _admin(
            postgres_dsn,
            "DROP OPERATOR IF EXISTS public.= (text, integer)",
            "DROP FUNCTION IF EXISTS public.pin_evil_eq(text, integer)",
        )


def test_a_user_equals_operator_is_unreachable_from_a_bare_operator(
    postgres_dsn: str, evil_equals_operator: None
) -> None:
    """`name = 1` must not dispatch to `public.=(text, integer)`.

    Armed: on the stock path the user operator answers, so every row matches.
    Pinned: `pg_catalog` has no `text = integer` operator, so PostgreSQL
    refuses the comparison outright instead of calling user code.
    """
    sql = "SELECT name FROM customers WHERE name = 1 ORDER BY customer_id"
    assert _as_role_unpinned(postgres_dsn, sql) == [("Alice",), ("Bob",)], "fixture not armed"

    engine = open_engine(postgres_dsn)
    with pytest.raises(psycopg.errors.UndefinedFunction):
        engine.execute(sql, max_rows=10, work_limit=0)


def test_an_explicit_operator_qualifier_outside_pg_catalog_is_refused(
    postgres_dsn: str, evil_concat_operator: None
) -> None:
    """`OPERATOR(public.||)` names its schema, so the pin cannot reach it.

    That makes the validator the only refusal for this spelling, and this
    proves both halves: executed directly, the payload leaks; validated, it
    is refused. `OPERATOR(pg_catalog.||)` and an unqualified `OPERATOR(||)`
    still validate and reach the built-in.
    """
    engine = open_engine(postgres_dsn)
    payloads = [
        "SELECT name OPERATOR(public.||) 1 FROM customers ORDER BY customer_id",
        'SELECT name OPERATOR("public".||) 1 FROM customers ORDER BY customer_id',
        "SELECT name FROM customers WHERE (name OPERATOR(public.||) 1) IS NOT NULL",
    ]
    armed = engine.execute(payloads[0], max_rows=10, work_limit=0)
    assert armed.rows == [(_SECRET,), (_SECRET,)], "payload not armed"
    for sql in payloads:
        assert not is_safe_query(sql, engine=engine), f"wrongly accepted: {sql!r}"

    for sql in (
        "SELECT name OPERATOR(pg_catalog.||) 1 FROM customers ORDER BY customer_id",
        "SELECT name OPERATOR(||) 1 FROM customers ORDER BY customer_id",
    ):
        assert is_safe_query(sql, engine=engine), f"wrongly rejected: {sql!r}"
        assert execute_query(postgres_dsn, sql).rows == [("Alice1",), ("Bob1",)]


# --- functions: `3cf40dc`'s shape, now also closed at execution -------------


@pytest.fixture
def evil_lower_overload(postgres_dsn: str, secret: str) -> Iterator[None]:
    """`public.lower(integer)`, `SECURITY DEFINER`, returning the secret."""
    try:
        _admin(
            postgres_dsn,
            "DROP FUNCTION IF EXISTS public.lower(integer)",
            "CREATE FUNCTION public.lower(integer) RETURNS text SECURITY DEFINER "
            f"LANGUAGE sql AS $$ SELECT marker FROM {_SECRET_SCHEMA}.secret $$",
        )
        yield
    finally:
        _admin(postgres_dsn, "DROP FUNCTION IF EXISTS public.lower(integer)")


def test_a_user_function_overload_is_unreachable_unqualified_and_refused_qualified(
    postgres_dsn: str, evil_lower_overload: None
) -> None:
    """Both spellings of Codex's Finding 1, against real execution.

    Unqualified, the pin closes it by itself: `pg_catalog.lower(integer)` does
    not exist, so the engine's execution refuses rather than calling user
    code - even with the validator bypassed. Qualified, the call names its
    schema, so `3cf40dc`'s structural rule stays the refusal; the payload is
    proven armed by executing it directly.
    """
    bare = "SELECT lower(customer_id) FROM customers ORDER BY customer_id"
    assert _as_role_unpinned(postgres_dsn, bare) == [(_SECRET,), (_SECRET,)], "not armed"

    engine = open_engine(postgres_dsn)
    assert not is_safe_query(bare, engine=engine)
    with pytest.raises(psycopg.errors.UndefinedFunction):
        engine.execute(bare, max_rows=10, work_limit=0)

    qualified = "SELECT public.lower(customer_id) FROM customers ORDER BY customer_id"
    assert engine.execute(qualified, max_rows=10, work_limit=0).rows == [(_SECRET,), (_SECRET,)]
    assert not is_safe_query(qualified, engine=engine)

    # Scoped, not blanket: a legitimate `lower(text)` still runs the built-in.
    legit = "SELECT lower(name) FROM customers ORDER BY customer_id"
    assert execute_query(postgres_dsn, legit).rows == [("alice",), ("bob",)]


# --- types: a schema-qualified cast names its schema --------------------------


@pytest.fixture
def evil_cast_type(postgres_dsn: str, secret: str) -> Iterator[None]:
    """A composite `public.lower` type whose cast from `text` returns the secret.

    Named `lower` deliberately: the dot-call rule refused `::public.sometype`
    at BASE only by accident, because it read the type name as an
    `(expr).name` function call and `sometype` is not allowlisted. A type
    named after an allowlisted function slipped straight past it.
    """
    cleanup = (
        "DROP CAST IF EXISTS (text AS public.lower)",
        "DROP FUNCTION IF EXISTS public.pin_evil_cast(text)",
        "DROP TYPE IF EXISTS public.lower",
    )
    try:
        _admin(
            postgres_dsn,
            *cleanup,
            "CREATE TYPE public.lower AS (v text)",
            "CREATE FUNCTION public.pin_evil_cast(text) RETURNS public.lower SECURITY DEFINER "
            f"LANGUAGE sql AS $$ SELECT ROW(marker)::public.lower FROM {_SECRET_SCHEMA}.secret $$",
            "CREATE CAST (text AS public.lower) WITH FUNCTION public.pin_evil_cast(text)",
        )
        yield
    finally:
        _admin(postgres_dsn, *cleanup)


def test_a_cast_to_a_schema_qualified_user_type_is_refused(
    postgres_dsn: str, evil_cast_type: None
) -> None:
    """`::public.lower` and `CAST(... AS public.lower)` must be refused.

    Armed first, through the engine with the validator bypassed: the explicit
    schema reaches the user cast even under the pin, so the validator is the
    only refusal - and it must refuse both spellings, and an array of the
    type. A `pg_catalog`-qualified type is not what this rule refuses.
    """
    engine = open_engine(postgres_dsn)
    payloads = [
        "SELECT name::public.lower FROM customers ORDER BY customer_id",
        "SELECT CAST(name AS public.lower) FROM customers ORDER BY customer_id",
        'SELECT name::"public"."lower" FROM customers ORDER BY customer_id',
        "SELECT ARRAY[name]::public.lower[] FROM customers ORDER BY customer_id",
    ]
    armed = engine.execute(payloads[0], max_rows=10, work_limit=0)
    assert all(_SECRET in str(row[0]) for row in armed.rows) and armed.rows, "not armed"
    for sql in payloads:
        assert not is_safe_query(sql, engine=engine), f"wrongly accepted: {sql!r}"

    # An unqualified user type is unreachable under the pin: it resolves in
    # `pg_catalog` or nowhere.
    with pytest.raises(psycopg.errors.UndefinedObject):
        engine.execute("SELECT name::lower FROM customers", max_rows=10, work_limit=0)


# --- Codex's Finding 2: the whole search path, not `current_schema()` --------


@pytest.fixture
def role_named_schema(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A schema named after `aipa_ro`, so the stock `"$user", public` path has two entries.

    It holds only `only_here` at first - Codex's exact reproduction, where
    `current_schema()` becomes `aipa_ro` while `customers` still lives in
    `public`. `AIPA_EXTRA_SCHEMAS` is unset: both schemas are on the path,
    which is PostgreSQL's own meaning of "default".
    """
    monkeypatch.delenv("AIPA_EXTRA_SCHEMAS", raising=False)
    try:
        _admin(
            postgres_dsn,
            "DROP SCHEMA IF EXISTS aipa_ro CASCADE",
            "CREATE SCHEMA aipa_ro",
            "GRANT USAGE ON SCHEMA aipa_ro TO aipa_ro",
            "CREATE TABLE aipa_ro.only_here (a INTEGER)",
            "INSERT INTO aipa_ro.only_here VALUES (7)",
            "GRANT SELECT ON aipa_ro.only_here TO aipa_ro",
        )
        yield
    finally:
        _admin(postgres_dsn, "DROP SCHEMA IF EXISTS aipa_ro CASCADE")


def test_a_bare_name_falls_through_to_a_later_search_path_schema(
    postgres_dsn: str, role_named_schema: None
) -> None:
    """Finding 2: the first path schema exists but lacks `customers`.

    PostgreSQL resolves bare `customers` by walking the whole path, so it
    falls through to `public.customers`. At BASE the validator modelled only
    `current_schema()` and refused a query the server would have answered;
    the engine must now advertise, accept and execute it, with the same rows
    the server returns on its own.
    """
    sql = "SELECT name FROM customers ORDER BY customer_id"
    server = _as_role_unpinned(postgres_dsn, sql)
    assert server == [("Alice",), ("Bob",)]

    engine = open_engine(postgres_dsn)
    names = engine.table_names()
    assert "customers" in names
    assert "only_here" in names
    assert is_safe_query(sql, engine=engine)
    assert execute_query(postgres_dsn, sql).rows == server
    assert is_safe_query("SELECT a FROM only_here", engine=engine)
    assert execute_query(postgres_dsn, "SELECT a FROM only_here").rows == [(7,)]


def test_shadowing_precedence_matches_the_server(
    postgres_dsn: str, role_named_schema: None
) -> None:
    """When two path schemas both hold a relation, the earlier one wins - as on the server.

    `aipa_ro.customers` shadows `public.customers` for the bare name; the
    shadowed table stays reachable under its qualified spelling only. A
    shadowing relation the role cannot read still shadows (PostgreSQL does
    not skip it), so the bare name `sales` then means nothing the role can
    use and is not advertised, while `public.sales` still is.
    """
    _admin(
        postgres_dsn,
        "CREATE TABLE aipa_ro.customers (customer_id INTEGER, name TEXT)",
        "INSERT INTO aipa_ro.customers VALUES (1, 'SHADOW')",
        "GRANT SELECT ON aipa_ro.customers TO aipa_ro",
        "CREATE TABLE aipa_ro.sales (sale_id INTEGER)",  # deliberately no GRANT
    )
    bare = "SELECT name FROM customers ORDER BY customer_id"
    server = _as_role_unpinned(postgres_dsn, bare)
    assert server == [("SHADOW",)]

    engine = open_engine(postgres_dsn)
    names = engine.table_names()
    assert {"customers", "aipa_ro.customers", "public.customers"} <= names
    assert is_safe_query(bare, engine=engine)
    assert execute_query(postgres_dsn, bare).rows == server

    qualified = "SELECT name FROM public.customers ORDER BY customer_id"
    assert is_safe_query(qualified, engine=engine)
    assert execute_query(postgres_dsn, qualified).rows == [("Alice",), ("Bob",)]

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _as_role_unpinned(postgres_dsn, "SELECT sale_id FROM sales")
    assert "sales" not in names
    assert "public.sales" in names
    assert not is_safe_query("SELECT sale_id FROM sales", engine=engine)
    assert is_safe_query("SELECT sale_id FROM public.sales", engine=engine)


# --- the recorded cost: extension operators installed in `public` ------------


def test_the_pin_changes_what_extension_operators_in_public_mean(postgres_dsn: str) -> None:
    """Records a cost of the pin, measured rather than assumed (2026-09-27).

    Extensions such as `citext` and `pg_trgm` install their operators into a
    schema on the path - normally `public` - exactly where a hostile operator
    would live, and the pin cannot tell them apart. Measured here:

    * `citext`'s `=` is replaced by `pg_catalog`'s `text = text` through
      citext's implicit cast, so a comparison that is case-insensitive on
      the server becomes case-sensitive **silently** - fewer rows, no error;
    * `pg_trgm`'s `%` has no `pg_catalog` counterpart, so the query fails
      loudly (`UndefinedFunction`).

    If this test starts failing, the cost paragraph of `docs/3_decisions.md`'s
    2026-09-27 entry is out of date. Only extensions this test created are
    dropped; a deployment's own are never touched.
    """
    admin = _as_postgres_superuser(postgres_dsn)
    with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
        existing = {row[0] for row in conn.execute("SELECT extname FROM pg_extension")}
    created = [name for name in ("citext", "pg_trgm") if name not in existing]
    try:
        _admin(
            postgres_dsn,
            *(f"CREATE EXTENSION {name} SCHEMA public" for name in created),
            "DROP TABLE IF EXISTS public.pin_extension_cost",
            "CREATE TABLE public.pin_extension_cost (email citext, note text)",
            "INSERT INTO public.pin_extension_cost VALUES ('Alice@X.com', 'widget blue')",
        )
        engine = open_engine(postgres_dsn)

        citext_eq = "SELECT email FROM pin_extension_cost WHERE email = 'alice@x.com'"
        assert _as_role_unpinned(postgres_dsn, citext_eq) == [("Alice@X.com",)]
        assert is_safe_query(citext_eq, engine=engine)
        assert engine.execute(citext_eq, max_rows=10, work_limit=0).rows == []

        trigram = "SELECT note FROM pin_extension_cost WHERE note % 'widgit blue'"
        assert _as_role_unpinned(postgres_dsn, trigram) == [("widget blue",)]
        with pytest.raises(psycopg.errors.UndefinedFunction):
            engine.execute(trigram, max_rows=10, work_limit=0)
    finally:
        _admin(
            postgres_dsn,
            "DROP TABLE IF EXISTS public.pin_extension_cost",
            *(f"DROP EXTENSION IF EXISTS {name}" for name in reversed(created)),
        )
