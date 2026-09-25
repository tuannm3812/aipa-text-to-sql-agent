"""PostgreSQL-specific tests: proving the read-only guarantee is two mechanisms,
and that the connection cannot reach the host filesystem or run a program.

`PostgresEngine` always connects with both the `aipa_ro` least-privilege role
and a read-only transaction (`conn.read_only = True`). The conformance suite
(`test_engine_conformance.py`) proves that combination refuses a write; it
cannot prove *either one alone* would - that's what the first two tests below
are for, per Phase 3's design
(`docs/superpowers/specs/2026-09-14-phase-3-engine-abstraction-design.md`
§4.2): a single point of failure in either mechanism must not be silently
covered for by the other.

The tests further down are a different question: not "can it write?" but
"can it read the host filesystem or run a program?". DuckDB's `read_only=True`
was assumed to mean exactly that and turned out not to - `read_csv`, a bare
quoted path, `glob` and `COPY ... TO` all reached the filesystem from a
read-only DuckDB connection (see `test_engine_duckdb.py`'s module docstring).
The fix there was a connection-level setting, `enable_external_access=False`,
because a validator can only refuse what it anticipates. PostgreSQL has no
single flag like that; instead, `aipa_ro` is never granted the
`pg_read_server_files`, `pg_write_server_files` or `pg_execute_server_program`
role memberships that every file- or program-reaching built-in requires (see
`docker/postgres-init.sql`). Every probe below calls `engine.execute`
directly, bypassing `is_safe_query` entirely - the point is to prove what the
*connection and role* refuse, not what a validator in front of them catches.
A hole found here would need to be fixed at the connection or role level,
never by adding a validator rule (that is Task 4's separate, second line of
defence).
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")

from text_to_sql_agent import is_safe_query  # noqa: E402
from text_to_sql_agent import safety as _safety  # noqa: E402
from text_to_sql_agent.engines import open_engine  # noqa: E402
from text_to_sql_agent.engines.postgres import PostgresEngine  # noqa: E402


def _as_postgres_superuser(dsn: str) -> str:
    """Swap the DSN's role for the compose file's `postgres` superuser.

    Same substitution `test_engine_conformance.py`'s `_as_postgres_superuser`
    makes, duplicated here rather than imported: this module intentionally
    has no dependency on the conformance suite's internals, and the
    substitution is one line.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    netloc = f"postgres:postgres@{parts.hostname}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def test_the_role_is_load_bearing_without_the_read_only_transaction(postgres_dsn: str) -> None:
    """Removing the read-only transaction flag must still leave the write refused.

    Connects as `aipa_ro` **without** setting `conn.read_only`, then attempts
    an INSERT `aipa_ro` holds no grant for. If a write went through here, the
    role would be decoration and the transaction flag would be the single
    point of failure carrying the whole guarantee - exactly what Phase 3's
    design refused to accept.
    """
    conn = psycopg.connect(postgres_dsn, connect_timeout=5)
    try:
        with pytest.raises(Exception) as caught:
            conn.execute("INSERT INTO customers VALUES (999, 'Mallory')")
        # Observed 2026-09-26: psycopg.errors.InsufficientPrivilege
        # ("permission denied for table customers") - the role's own grants
        # refuse the write before the read-only transaction flag (which is
        # not set on this connection) ever enters into it.
        assert isinstance(caught.value, psycopg.errors.InsufficientPrivilege)
    finally:
        conn.rollback()
        conn.close()


def test_the_transaction_flag_is_load_bearing_for_a_privileged_connection(
    postgres_dsn: str,
) -> None:
    """Removing the least-privilege role must still leave the write refused.

    Connects as the `postgres` superuser - who holds every grant - but with
    `conn.read_only = True` set. If a write went through here, the read-only
    transaction flag would be decoration and the role would be the single
    point of failure - the other half of the same guarantee the test above
    checks.
    """
    conn = psycopg.connect(_as_postgres_superuser(postgres_dsn), connect_timeout=5)
    conn.read_only = True
    try:
        with pytest.raises(Exception) as caught:
            conn.execute("INSERT INTO customers VALUES (999, 'Mallory')")
        # Observed 2026-09-26: psycopg.errors.ReadOnlySqlTransaction
        # ("cannot execute INSERT in a read-only transaction") - a superuser
        # has every grant, so only the read-only transaction flag is left to
        # refuse this.
        assert isinstance(caught.value, psycopg.errors.ReadOnlySqlTransaction)
    finally:
        conn.rollback()
        conn.close()


def test_check_reachable_message_never_contains_the_dsn() -> None:
    """A driver error commonly echoes the DSN it failed to reach - the message
    `check_reachable` raises must not repeat that mistake, since the DSN
    carries a password (`docs/0_coding_standards.md` §4's credential rule).
    """
    dsn = "postgresql://aipa_ro:aipa_ro_pw@127.0.0.1:1/does-not-matter"
    engine = PostgresEngine(dsn)

    with pytest.raises(Exception) as caught:
        engine.check_reachable()

    message = str(caught.value)
    assert "aipa_ro_pw" not in message
    assert dsn not in message


# --- Filesystem and program-execution surface (Task 3) ---------------------
#
# Every entry is (label, sql). Probed 2026-09-26 through `engine.execute` as
# `aipa_ro` against the compose container - every one of these returned
# `psycopg.errors.InsufficientPrivilege`, refused by the role's grants, not
# by the read-only transaction flag (`COPY ... TO/FROM` and the file/program
# functions all name a specific missing role membership in their DETAIL text:
# `pg_read_server_files`, `pg_write_server_files` or
# `pg_execute_server_program`). See `docker/postgres-init.sql` - `aipa_ro` is
# never granted any of the three.
_FILESYSTEM_AND_PROGRAM_PROBES: list[tuple[str, str]] = [
    ("pg_read_file_absolute", "SELECT pg_read_file('/etc/passwd')"),
    ("pg_ls_dir_root", "SELECT pg_ls_dir('/')"),
    ("lo_import_absolute", "SELECT lo_import('/etc/passwd')"),
    ("pg_stat_file_absolute", "SELECT * FROM pg_stat_file('/etc/passwd')"),
    ("pg_shadow", "SELECT usename, passwd FROM pg_shadow"),
    ("copy_to_program", "COPY (SELECT 1) TO PROGRAM 'touch /tmp/aipa-task3-pwned'"),
    ("current_setting_data_directory", "SELECT current_setting('data_directory')"),
    # Beyond the brief's own list: pg_ls_waldir and a *relative* path (inside
    # the data directory, not an absolute path like /etc/passwd) for the
    # file-reading functions - a role could plausibly be denied absolute
    # paths yet allowed relative ones, so this checks that directly rather
    # than assuming the absolute-path result generalises.
    ("pg_ls_waldir", "SELECT * FROM pg_ls_waldir()"),
    ("pg_read_binary_file_relative", "SELECT pg_read_binary_file('PG_VERSION')"),
    ("pg_stat_file_relative", "SELECT * FROM pg_stat_file('PG_VERSION')"),
]


@pytest.mark.parametrize(
    "label,sql",
    _FILESYSTEM_AND_PROGRAM_PROBES,
    ids=[label for label, _ in _FILESYSTEM_AND_PROGRAM_PROBES],
)
def test_filesystem_and_program_functions_are_refused(
    postgres_dsn: str, label: str, sql: str
) -> None:
    """A file-reading or program-running built-in must be refused as `aipa_ro`.

    If any of these succeeded, `aipa_ro` could read arbitrary files on the
    PostgreSQL host (`/etc/passwd`, the WAL directory, a relative path inside
    the data directory) or run an OS command, entirely through
    `engine.execute` - the same class of hole DuckDB's `read_only=True` had,
    just reached through PostgreSQL's own built-ins instead of DuckDB's table
    functions. This would be a connection/role hole, not something Task 4's
    validator could be relied on to catch, since a validator only refuses
    what it was told to anticipate.
    """
    engine = open_engine(postgres_dsn)
    with pytest.raises(Exception) as caught:
        engine.execute(sql, max_rows=3, work_limit=5000)
    assert isinstance(caught.value, psycopg.errors.InsufficientPrivilege)


def test_copy_table_to_a_server_side_file_is_refused(postgres_dsn: str) -> None:
    """`COPY customers TO '<server path>'` must be refused before it writes.

    This is the exfiltration direction: a working query could copy real row
    data from a table `aipa_ro` can legitimately `SELECT` out to a file on
    the PostgreSQL *server's* filesystem (inside its container, not this test
    process's), entirely outside anything the app ever reads back - a leak
    the read-only transaction flag does not address, because `COPY TO` a file
    is a server-side write PostgreSQL treats as separate from writing to a
    table. `InsufficientPrivilege` is raised before the write is attempted,
    which is what proves nothing reached disk; there is no local path this
    test can check, since the file would land inside the server's container.
    """
    engine = open_engine(postgres_dsn)
    target = "/tmp/aipa-task3-probe-out.csv"
    with pytest.raises(Exception) as caught:
        engine.execute(f"COPY customers TO '{target}'", max_rows=3, work_limit=5000)
    assert isinstance(caught.value, psycopg.errors.InsufficientPrivilege)


def test_copy_table_from_a_server_side_file_is_refused(postgres_dsn: str) -> None:
    """`COPY customers FROM '<server path>'` must be refused.

    If this succeeded, `aipa_ro` - a role with no `INSERT` grant at all -
    could still load arbitrary file content (here, `/etc/passwd`) into a real
    table via the file-based `COPY` path, sidestepping the role's own
    `GRANT`s the same way an `INSERT` statement is refused by
    `test_the_role_is_load_bearing_without_the_read_only_transaction` above.
    """
    engine = open_engine(postgres_dsn)
    with pytest.raises(Exception) as caught:
        engine.execute("COPY customers FROM '/etc/passwd'", max_rows=3, work_limit=5000)
    assert isinstance(caught.value, psycopg.errors.InsufficientPrivilege)


@pytest.mark.parametrize("extension_name", ["dblink", "postgres_fdw"])
def test_create_extension_is_refused_through_the_engine(
    postgres_dsn: str, extension_name: str
) -> None:
    """`CREATE EXTENSION dblink`/`postgres_fdw` must be refused end-to-end.

    Either extension, once installed, lets a connection reach an arbitrary
    other network host as its own PostgreSQL server. If installation
    succeeded here it would change the entire threat model this engine
    relies on - an available `dblink`/`postgres_fdw` turns a single
    read-only, single-database role into a pivot onto anything else the
    Postgres host can reach.
    """
    engine = open_engine(postgres_dsn)
    with pytest.raises(Exception) as caught:
        engine.execute(f"CREATE EXTENSION {extension_name}", max_rows=3, work_limit=5000)
    # Refused twice over: the read-only transaction flag refuses CREATE
    # EXTENSION outright, so this alone would not prove the *role* lacks the
    # privilege - see the test below, which isolates that half.
    assert isinstance(caught.value, psycopg.errors.ReadOnlySqlTransaction)


@pytest.mark.parametrize("extension_name", ["dblink", "postgres_fdw"])
def test_create_extension_is_refused_by_privilege_alone(
    postgres_dsn: str, extension_name: str
) -> None:
    """`CREATE EXTENSION` must be refused by `aipa_ro`'s own grants, not only
    by the read-only transaction flag the test above goes through.

    Connects as `aipa_ro` **without** setting `conn.read_only`, the same
    isolation `test_the_role_is_load_bearing_without_the_read_only_transaction`
    uses above. `dblink` and `postgres_fdw` are both untrusted extensions
    PostgreSQL restricts to superusers regardless of schema-level `CREATE`
    grants; if this passed for `aipa_ro`, the "must be a superuser to install
    this" restriction this task's guarantee assumes would not actually apply
    to the role the agent connects as, and the read-only transaction flag
    tested above would be the *only* thing standing between the agent and a
    network pivot.
    """
    conn = psycopg.connect(postgres_dsn, connect_timeout=5)
    try:
        with pytest.raises(Exception) as caught:
            conn.execute(f"CREATE EXTENSION {extension_name}")
        assert isinstance(caught.value, psycopg.errors.InsufficientPrivilege)
    finally:
        conn.rollback()
        conn.close()


def test_aipa_ro_holds_none_of_the_file_or_program_roles(postgres_dsn: str) -> None:
    """`aipa_ro` must not be a member of `pg_read_server_files`,
    `pg_write_server_files` or `pg_execute_server_program`.

    `docker/postgres-init.sql`'s comment asserts this in prose but Task 1
    never verified it against the catalogue. If any membership were present,
    every probe above would be trusting a claim the init script did not
    actually enforce - the guarantee this whole module pins rests entirely
    on `aipa_ro` holding none of these three role memberships, so this is the
    one test that checks the premise the others assume.
    """
    conn = psycopg.connect(postgres_dsn, connect_timeout=5)
    try:
        rows = conn.execute(
            "SELECT rolname, pg_has_role('aipa_ro', rolname, 'MEMBER') "
            "FROM pg_roles "
            "WHERE rolname IN "
            "('pg_read_server_files', 'pg_write_server_files', 'pg_execute_server_program') "
            "ORDER BY rolname"
        ).fetchall()
        assert rows == [
            ("pg_execute_server_program", False),
            ("pg_read_server_files", False),
            ("pg_write_server_files", False),
        ]
    finally:
        conn.close()


# --- Task 4: default-deny function and table validation ---------------------
#
# `PostgresEngine.allowed_functions` (see that attribute's own long comment
# for how it was built, verified against `pg_proc`, and what was deliberately
# left off) switches `safety.is_safe_query` from SQLite's blocklist-only
# behaviour to DuckDB-style default-deny: every function call anywhere in the
# query - not just in a table-source position - must resolve to a name in
# that set, and every `FROM`/`JOIN` target must be a real table or a CTE.
# Everything below proves that allowlist means what a reviewer reads it to
# mean, the same three things `test_engine_duckdb.py` proves for DuckDB's:
# the round trip (every name really resolves to itself through sqlglot), the
# rejections (the functions Task 3 found the *connection* does not refuse),
# and the acceptances (a realistic corpus that both validates and executes).


@pytest.fixture(scope="module")
def engine_with_table_t(postgres_dsn: str) -> PostgresEngine:
    """A real table `t` (one dummy integer column, no rows), for tests that
    need `is_safe_query`'s default-deny table check (`_references_unknown_
    table`) to have a real table to say yes to - mirrors `test_engine_duckdb.
    py`'s fixture of the same name and purpose.

    `is_safe_query` never checks column existence, only table and function
    names, so `t`'s single dummy column is enough regardless of which column
    name a given round-trip snippet happens to reference.

    Created (and dropped) through the `postgres` superuser connection, since
    `aipa_ro` holds no DDL grant - the same substitution `_as_postgres_
    superuser` above provides for the rest of this module. `aipa_ro` can
    `SELECT` from `t` with no separate `GRANT`: `docker/postgres-init.sql`'s
    `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO
    aipa_ro` already covers every table the `postgres` role creates
    afterwards, the same way it already covers `customers`/`sales`.

    Module-scoped: nothing in this file mutates `t` (only `is_safe_query`,
    read-only validation, ever looks at it), so one table safely backs every
    round-trip check in this module.
    """
    with psycopg.connect(_as_postgres_superuser(postgres_dsn), connect_timeout=5) as conn:
        conn.execute("DROP TABLE IF EXISTS t")
        conn.execute("CREATE TABLE t (a INTEGER)")
    engine = open_engine(postgres_dsn)
    yield engine
    with psycopg.connect(_as_postgres_superuser(postgres_dsn), connect_timeout=5) as conn:
        conn.execute("DROP TABLE IF EXISTS t")


# Window functions need an `OVER (...)` clause to parse at all; a handful
# also need specific non-empty arguments sqlglot's `postgres` dialect
# requires - the same subset DuckDB's own round-trip test carries, since
# these are standard SQL window functions with the same signatures in both
# engines.
_WINDOW_FUNCTION_ARGS = {
    "ntile": "(4)",
    "lag": "(a)",
    "lead": "(a)",
    "nth_value": "(a, 2)",
    "first_value": "(a)",
    "last_value": "(a)",
}
_WINDOW_FUNCTIONS = frozenset(
    {
        "row_number",
        "rank",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
    }
)
# Table functions are called in a `FROM` position with their own argument
# shape, not a scalar `SELECT` list. `generate_series` is
# `PostgresEngine.allowed_functions`'s only table-function entry - see that
# attribute's comment for why `unnest` was left off.
_TABLE_FUNCTION_ARGS = {
    "generate_series": "(1, 10)",
}
# A plausible, parseable argument list for every remaining scalar/aggregate
# entry in `PostgresEngine.allowed_functions` - one call shape each function
# is known to accept, used only to prove the round trip below, not to
# exercise every overload or to type-check against `t.a`'s actual INTEGER
# type (`is_safe_query` never executes the SQL, so a string literal passed
# where a real query would pass a text column is fine here). `cast`,
# `extract`, `case`, `if`, `current_date`, `current_timestamp` and `mode`
# have their own irregular syntax and are special-cased in `_round_trip_
# snippet` instead of listed here, matching `test_engine_duckdb.py`'s own
# pattern for `current_date`/`now`/`extract`/`case`/`if`.
_SCALAR_FUNCTION_ARGS = {
    "abs": "(a)",
    "age": "(a, a)",
    "array_agg": "(a)",
    "avg": "(a)",
    "bool_and": "(a)",
    "bool_or": "(a)",
    "cbrt": "(a)",
    "ceil": "(a)",
    "coalesce": "(a, a)",
    "concat": "(a, a)",
    "concat_ws": "(',', a, a)",
    "corr": "(a, a)",
    "count": "(a)",
    "covar_pop": "(a, a)",
    "covar_samp": "(a, a)",
    "date_trunc": "('month', a)",
    "exp": "(a)",
    "floor": "(a)",
    "greatest": "(a, a)",
    "initcap": "(a)",
    "least": "(a, a)",
    "left": "(a, 3)",
    "length": "(a)",
    "ln": "(a)",
    "log": "(a)",
    "lower": "(a)",
    "lpad": "(a, 5, '0')",
    "make_date": "(2024, 1, 1)",
    "max": "(a)",
    "min": "(a)",
    "nullif": "(a, a)",
    "position": "('x' in a)",
    "power": "(a, 2)",
    "regexp_replace": "(a, 'x', 'y')",
    "regexp_matches": "(a, 'x')",
    "repeat": "(a, 3)",
    "replace": "(a, 'x', 'y')",
    "reverse": "(a)",
    "right": "(a, 3)",
    "round": "(a, 2)",
    "sign": "(a)",
    "split_part": "(a, ',', 1)",
    "sqrt": "(a)",
    "starts_with": "(a, 'x')",
    "stddev": "(a)",
    "stddev_pop": "(a)",
    "stddev_samp": "(a)",
    "string_agg": "(a, ',')",
    "substring": "(a from 1 for 3)",
    "sum": "(a)",
    "trim": "(a)",
    "upper": "(a)",
    "var_pop": "(a)",
    "variance": "(a)",
}


def _round_trip_snippet(name: str) -> str:
    """A single, plausible SQL fragment calling `name`, for the round-trip test."""
    if name in _WINDOW_FUNCTIONS:
        args = _WINDOW_FUNCTION_ARGS.get(name, "()")
        return f"{name}{args} OVER (ORDER BY a)"
    if name in _TABLE_FUNCTION_ARGS:
        return f"{name}{_TABLE_FUNCTION_ARGS[name]}"
    if name == "cast":
        return "cast(a AS INTEGER)"
    if name == "extract":
        return "extract(year FROM a)"
    if name == "case":
        return "case when a > 1 then 'x' else 'y' end"
    if name == "if":
        return "if(a > 1, 'x', 'y')"
    if name == "current_date":
        return "current_date"
    if name == "current_timestamp":
        return "current_timestamp"
    if name == "mode":
        return "mode() within group (order by a)"
    return f"{name}{_SCALAR_FUNCTION_ARGS[name]}"


@pytest.mark.parametrize("name", sorted(PostgresEngine.allowed_functions))
def test_allowed_function_round_trip(name, engine_with_table_t):
    """The task brief's Step 3, proven directly: for every one of the 73
    names in `PostgresEngine.allowed_functions`, a realistic call using that
    name parses under the `postgres` dialect, and `safety._resolve_function_
    name` resolves the parsed node back to a name that is itself in
    `allowed_functions` - not necessarily the same literal spelling
    (`generate_series(1, 10)` in a `FROM` clause parses to `exp.
    ExplodingGenerateSeries`, resolved via the override added in `safety.py`
    back to `"generate_series"` - see `_FUNCTION_NAME_OVERRIDES`), but always
    a member of the set, which is what makes `is_safe_query` accept it.
    `is_safe_query` itself is asserted too, on the full statement, so this is
    the real path the validator runs, not just the resolver in isolation.

    A mismatch here fails in one of two directions: a legitimate function
    wrongly rejected (caught by the `is_safe_query` assertion below), or a
    resolver that maps a dangerous function onto an allowed name (which
    `test_dangerous_functions_are_rejected` below checks independently, for
    the specific names the task brief calls out, though not as an exhaustive
    sweep of PostgreSQL's ~3,300-function catalogue the way DuckDB's
    `test_every_unlisted_duckdb_function_is_rejected_by_default_deny` sweeps
    DuckDB's).
    """
    engine = engine_with_table_t
    snippet = _round_trip_snippet(name)
    is_table_function = name in _TABLE_FUNCTION_ARGS
    sql = f"SELECT * FROM {snippet}" if is_table_function else f"SELECT {snippet} FROM t"

    parsed = sqlglot.parse_one(sql, read="postgres")
    funcs = list(parsed.find_all(exp.Func))
    assert funcs, f"{sql!r} produced no exp.Func node to resolve"
    resolved_names = {_safety._resolve_function_name(f) for f in funcs}
    assert resolved_names & engine.allowed_functions, (
        f"{name!r} (SQL: {sql!r}) resolved to {resolved_names}, none of which "
        "are in PostgresEngine.allowed_functions"
    )
    assert is_safe_query(sql, engine=engine), f"{sql!r} should have been allowed"


# The task brief's "at minimum" rejection list, plus the specific gap Task 3
# deferred to this task: `current_setting` and every route it or an
# equivalent built-in offers to reading server configuration or the
# `pg_settings` catalogue view - proven both as a function call (any
# position) and as a table source, per the brief's Step 4/Step 5. None of
# these are refused by the connection or the `aipa_ro` role (Task 3 proved
# `SELECT * FROM pg_settings` executes successfully as `aipa_ro` - privileges
# do not block it); `is_safe_query` is the only thing standing between an LLM
# and any of them.
_DANGEROUS_FUNCTION_PROBES: list[tuple[str, str]] = [
    ("pg_read_file", "SELECT pg_read_file('/etc/passwd')"),
    ("pg_sleep", "SELECT pg_sleep(5)"),
    ("dblink", "SELECT dblink('host=evil', 'select 1')"),
    ("query_to_xml", "SELECT query_to_xml('select 1', false, false, '')"),
    ("lo_import", "SELECT lo_import('/etc/passwd')"),
    ("current_setting_scalar", "SELECT current_setting('data_directory')"),
    ("current_setting_missing_ok", "SELECT current_setting('data_directory', true)"),
    ("current_setting_in_where", "SELECT * FROM sales WHERE current_setting('port') = '5432'"),
]


@pytest.mark.parametrize(
    "label,sql", _DANGEROUS_FUNCTION_PROBES, ids=[label for label, _ in _DANGEROUS_FUNCTION_PROBES]
)
def test_dangerous_functions_are_rejected(engine_with_table_t, label: str, sql: str) -> None:
    """`is_safe_query` must refuse every one of these regardless of where in
    the query it appears - `current_setting_in_where` in particular proves
    the scalar/value-position case, not just a bare `SELECT current_setting(
    ...)`, since a `WHERE` clause is exactly the kind of place a function
    check restricted to table-source position (like `internal_prefixes`/
    `internal_names`) could never reach.
    """
    assert not is_safe_query(sql, engine=engine_with_table_t), f"should have rejected: {sql!r}"


# The task brief's Step 5: PostgreSQL's internals surface (`pg_*`,
# `information_schema`) must be refused whether it is named as a bare table
# source or as a schema qualifier in front of an otherwise-innocent-looking
# table name (`pg_catalog.pg_tables`, `information_schema.tables`) - both
# forms are checked for both surfaces, per the brief.
_INTERNAL_TABLE_PROBES: list[tuple[str, str]] = [
    ("pg_settings_bare", "SELECT * FROM pg_settings"),
    ("pg_settings_schema_qualified", "SELECT * FROM pg_catalog.pg_settings"),
    ("pg_tables_bare", "SELECT * FROM pg_tables"),
    ("pg_tables_schema_qualified", "SELECT * FROM pg_catalog.pg_tables"),
    ("information_schema_tables", "SELECT * FROM information_schema.tables"),
    ("information_schema_columns", "SELECT * FROM information_schema.columns"),
]


@pytest.mark.parametrize(
    "label,sql", _INTERNAL_TABLE_PROBES, ids=[label for label, _ in _INTERNAL_TABLE_PROBES]
)
def test_internal_catalogue_tables_are_rejected(engine_with_table_t, label: str, sql: str) -> None:
    """`pg_catalog.pg_tables` and `information_schema.tables` must be
    refused both as a bare `pg_`-prefixed/`information_schema`-named table
    source and as a schema-qualified reference - Task 3 proved `SELECT *
    FROM pg_settings` executes successfully as `aipa_ro` (privileges do not
    block it), so this validator-level check is the only refusal for any of
    these.
    """
    assert not is_safe_query(sql, engine=engine_with_table_t), f"should have rejected: {sql!r}"


# Step 4's acceptance corpus: at least 20 analytics queries a user would
# plausibly ask of `customers`/`sales` - aggregates, GROUP BY, JOIN, CASE,
# date functions, window functions, CTEs, and ILIKE text matching - each
# proven to both pass `is_safe_query` and actually execute against the live
# database. `sales` carries `sale_date`/`amount`/`category`/`status`/`region`
# for exactly this purpose - see `docker/postgres-init.sql`'s comment on why
# those columns live on `sales` rather than `customers`.
ANALYTICS_CORPUS: list[str] = [
    # Aggregates
    "SELECT COUNT(*) FROM sales",
    "SELECT SUM(amount) FROM sales WHERE status = 'completed'",
    "SELECT AVG(amount) FROM sales WHERE amount IS NOT NULL",
    "SELECT MIN(amount), MAX(amount) FROM sales WHERE amount IS NOT NULL",
    # GROUP BY
    "SELECT category, SUM(amount) AS total FROM sales GROUP BY category ORDER BY total DESC",
    "SELECT region, COUNT(*) AS n FROM sales GROUP BY region",
    (
        "SELECT status, MODE() WITHIN GROUP (ORDER BY status) AS most_common "
        "FROM sales GROUP BY status"
    ),
    # JOIN
    (
        "SELECT c.name, SUM(s.amount) AS total FROM sales s "
        "JOIN customers c ON c.customer_id = s.customer_id "
        "GROUP BY c.name ORDER BY total DESC"
    ),
    # CASE
    (
        "SELECT sale_id, "
        "CASE WHEN amount IS NULL THEN 'unknown' "
        "WHEN amount > 200 THEN 'large' ELSE 'small' END AS size_bucket FROM sales"
    ),
    (
        "SELECT category, SUM(CASE WHEN status = 'completed' THEN amount ELSE 0 END) "
        "AS completed_total FROM sales GROUP BY category"
    ),
    # Date functions
    (
        "SELECT date_trunc('month', sale_date) AS month, SUM(amount) FROM sales "
        "GROUP BY month ORDER BY month"
    ),
    "SELECT EXTRACT(year FROM sale_date) AS yr, COUNT(*) FROM sales GROUP BY yr",
    "SELECT EXTRACT(quarter FROM sale_date) AS q, SUM(amount) FROM sales GROUP BY q",
    "SELECT sale_id, AGE(CURRENT_DATE, sale_date) AS days_since FROM sales",
    "SELECT make_date(2024, 1, 1) AS d",
    # Window functions
    (
        "SELECT sale_id, amount, "
        "ROW_NUMBER() OVER (PARTITION BY category ORDER BY amount DESC) AS rnk FROM sales"
    ),
    (
        "SELECT sale_id, sale_date, amount, "
        "SUM(amount) OVER (ORDER BY sale_date) AS running_total FROM sales"
    ),
    (
        "SELECT sale_id, customer_id, sale_date, "
        "LAG(sale_date) OVER (PARTITION BY customer_id ORDER BY sale_date) AS prev_sale FROM sales"
    ),
    (
        "SELECT sale_id, amount, NTILE(4) OVER (ORDER BY amount) AS quartile "
        "FROM sales WHERE amount IS NOT NULL"
    ),
    # CTEs
    (
        "WITH totals AS (SELECT category, SUM(amount) AS total FROM sales GROUP BY category) "
        "SELECT * FROM totals WHERE total > 100"
    ),
    (
        "WITH ranked AS ("
        "SELECT sale_id, category, amount, "
        "ROW_NUMBER() OVER (PARTITION BY category ORDER BY amount DESC) AS rnk FROM sales"
        ") SELECT * FROM ranked WHERE rnk = 1"
    ),
    # ILIKE text matching
    "SELECT * FROM customers WHERE name ILIKE '%al%'",
    "SELECT * FROM sales WHERE category ILIKE 'widget%'",
    # String cleaning / numeric helpers
    "SELECT UPPER(TRIM(name)) AS clean_name FROM customers",
    "SELECT customer_id, LENGTH(TRIM(name)) AS name_length FROM customers",
    "SELECT initcap(lower(name)) AS pretty_name FROM customers",
    "SELECT sale_id, COALESCE(amount, 0) AS amount_or_zero FROM sales",
    "SELECT sale_id, GREATEST(amount, 100) AS floor_amount FROM sales WHERE amount IS NOT NULL",
    "SELECT sale_id, ROUND(amount, 0) AS rounded FROM sales WHERE amount IS NOT NULL",
    "SELECT customer_id, NULLIF(region, 'south') AS not_south FROM sales",
    # Table function
    "SELECT * FROM generate_series(1, 5)",
    # Combined, closer to a realistic multi-clause business question
    (
        "SELECT s.region, date_trunc('month', s.sale_date) AS month, "
        "COUNT(*) AS n_sales, SUM(s.amount) AS total, AVG(s.amount) AS avg_amount "
        "FROM sales s JOIN customers c ON c.customer_id = s.customer_id "
        "WHERE s.status = 'completed' "
        "GROUP BY s.region, month ORDER BY month, s.region"
    ),
]


def test_analytics_corpus_size_is_at_least_twenty() -> None:
    """Guards the corpus itself, not just what it proves - a corpus that
    silently shrank below the brief's stated floor would make every other
    assertion about it in the task report false.
    """
    assert len(ANALYTICS_CORPUS) >= 20


@pytest.mark.parametrize("sql", ANALYTICS_CORPUS)
def test_analytics_corpus_passes_validation_and_executes(postgres_dsn: str, sql: str) -> None:
    """Step 4: every query in `ANALYTICS_CORPUS` must both pass
    `is_safe_query` and actually execute against the live PostgreSQL
    database - the two are checked separately so a failure names which side
    broke. A query that only passed the first half would mean the allowlist
    describes a PostgreSQL that does not exist.
    """
    engine = open_engine(postgres_dsn)
    assert is_safe_query(sql, engine=engine), f"wrongly rejected: {sql!r}"
    result = engine.execute(sql, max_rows=1000, work_limit=0)
    assert result.ok, f"{sql!r} failed to execute: {result.error}"
