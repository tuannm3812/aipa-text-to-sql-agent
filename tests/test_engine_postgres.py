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
    if name == "collate":
        return 'a COLLATE "C"'
    return f"{name}{_SCALAR_FUNCTION_ARGS[name]}"


@pytest.mark.parametrize("name", sorted(PostgresEngine.allowed_functions))
def test_allowed_function_round_trip(name, engine_with_table_t):
    """The task brief's Step 3, proven directly: for every one of the 74
    names in `PostgresEngine.allowed_functions`, a realistic call using that
    name parses under the `postgres` dialect, and `safety._resolve_function_
    name` resolves at least one parsed node in the statement back to `name`
    itself - not necessarily every node the statement contains
    (`case`/`if`'s snippet also produces a sibling `exp.If`/`exp.Case` node
    for the other of the pair, since every `CASE ... WHEN` branch parses to
    its own child `exp.If` node - see `_FUNCTION_NAME_OVERRIDES`'s module
    comment), but `name` itself must be among the resolved set - not merely
    *some* allowed name (Task 4, 2026-09-26: the original assertion checked
    `resolved_names & engine.allowed_functions`, which is satisfied by any
    other allowed name appearing anywhere in the statement and would stay
    green even if `name`'s own snippet resolved to nothing in
    `allowed_functions` at all, as long as some other node did - it asserts
    non-emptiness of an intersection, not that the name under test actually
    round-trips). `is_safe_query` itself is asserted too, on the full
    statement, so this is the real path the validator runs, not just the
    resolver in isolation.

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
    assert name in resolved_names, (
        f"{name!r} (SQL: {sql!r}) resolved to {resolved_names}, which does not "
        f"contain {name!r} itself"
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


# Task 4 (2026-09-26 review round), Bypass 1: PostgreSQL's `(expr).name` is
# grammar sugar for `name(expr)` - a single-argument function call - for any
# `name` `expr`'s type has no field called that. sqlglot parses this into
# `exp.Dot(this=<expr>, expression=exp.Identifier(name))`, never an
# `exp.Func`, so the pre-fix `_references_disallowed_function`'s
# `find_all(exp.Func)` walk never saw it. Reproduced live against
# `postgresql://aipa_ro:...@127.0.0.1:55432/aipa` (2026-09-26):
# `SELECT ('port').current_setting` returned `['5432']`,
# `SELECT ('/etc/passwd').pg_read_file` raised `InsufficientPrivilege` (the
# connection's own defence, not the validator's - `is_safe_query` still
# wrongly said yes), and `SELECT ('customers'::regclass).pg_relation_
# filepath` returned `['base/16384/16387']`. Every variant below reproduces
# a distinct shape the review confirmed working: doubled parens, a quoted
# right-hand identifier, inside `WHERE`, inside a scalar subquery, inside
# `CAST`, inside another function call, and chained.
_DOT_CALL_BYPASS_PROBES: list[tuple[str, str]] = [
    ("simple", "SELECT ('port').current_setting"),
    ("pg_read_file", "SELECT ('/etc/passwd').pg_read_file"),
    ("pg_relation_filepath", "SELECT ('customers'::regclass).pg_relation_filepath"),
    ("doubled_parens", "SELECT (('port')).current_setting"),
    ("quoted_identifier", "SELECT ('port').\"current_setting\""),
    ("in_where", "SELECT * FROM sales WHERE ('port').current_setting = '5432'"),
    ("scalar_subquery", "SELECT (SELECT ('port').current_setting)"),
    ("inside_cast", "SELECT CAST(('port').current_setting AS text)"),
    ("inside_another_function", "SELECT upper(('port').current_setting)"),
    ("chained", "SELECT (('port').current_setting).upper"),
]


@pytest.mark.parametrize(
    "label,sql", _DOT_CALL_BYPASS_PROBES, ids=[label for label, _ in _DOT_CALL_BYPASS_PROBES]
)
def test_dot_call_bypass_is_rejected(engine_with_table_t, label: str, sql: str) -> None:
    """Every variant of Bypass 1 must be refused post-fix - each one passed
    `is_safe_query` at BASE (commit 20dd3e9), before `safety._references_
    disallowed_dot_call` existed.
    """
    assert not is_safe_query(sql, engine=engine_with_table_t), f"should have rejected: {sql!r}"


def test_dot_call_with_its_own_arguments_was_already_covered(engine_with_table_t) -> None:
    """Not a new gap: `(expr).name(more, args)` - as opposed to the bare
    `(expr).name` form above - parses `.expression` as `exp.Anonymous`
    (itself an `exp.Func` subclass), which `_references_disallowed_function`
    already walks via `find_all(exp.Func)`. Pinned here so a future change
    cannot silently narrow that coverage without a test noticing: a
    disallowed name called this way must still be rejected, and an allowed
    one must still validate.
    """
    assert not is_safe_query("SELECT ('/etc/passwd').pg_read_file()", engine=engine_with_table_t)
    assert is_safe_query("SELECT ('a,b,c').split_part(',', 1)", engine=engine_with_table_t)


# Task 4, Bypass 2: a cast to a PostgreSQL object-identifier ("OID") type -
# `regclass`, `regrole`, `regproc`, `regnamespace`, `regtype`, `regoper`,
# `regoperator`, `regconfig`, `regdictionary`, `regcollation`,
# `regprocedure` - resolves a string or integer against exactly the
# catalogue (`pg_class`, `pg_authid`, `pg_proc`, `pg_namespace`, ...) the
# `pg_` internals rule exists to block, with no function call and no table
# reference for the pre-fix validator to see. Reproduced live (2026-09-26):
# `SELECT g::regclass AS rel FROM generate_series(16380, 16400) AS g`
# executed and returned real relation names - `is_safe_query` said yes.
# Every one of the eleven type names is probed under both the `::` and
# `CAST(... AS ...)` spellings.
_OID_CAST_TYPES: tuple[str, ...] = (
    "regclass",
    "regrole",
    "regproc",
    "regnamespace",
    "regtype",
    "regoper",
    "regoperator",
    "regconfig",
    "regdictionary",
    "regcollation",
    "regprocedure",
)
_OID_CAST_BYPASS_PROBES: list[tuple[str, str]] = (
    [(f"{type_name}_coloncolon", f"SELECT 'x'::{type_name}") for type_name in _OID_CAST_TYPES]
    + [(f"{type_name}_cast", f"SELECT CAST('x' AS {type_name})") for type_name in _OID_CAST_TYPES]
    + [
        (
            "regclass_generate_series_loop",
            "SELECT g::regclass AS rel FROM generate_series(16380, 16400) AS g",
        ),
        (
            "regrole_generate_series_loop",
            "SELECT CAST(g AS regrole) AS rel FROM generate_series(1, 20) AS g",
        ),
    ]
)


@pytest.mark.parametrize(
    "label,sql", _OID_CAST_BYPASS_PROBES, ids=[label for label, _ in _OID_CAST_BYPASS_PROBES]
)
def test_oid_cast_bypass_is_rejected(engine_with_table_t, label: str, sql: str) -> None:
    """Every variant of Bypass 2 must be refused post-fix - each one passed
    `is_safe_query` at BASE (commit 20dd3e9), before `safety._casts_to_
    object_identifier_type` existed.
    """
    assert not is_safe_query(sql, engine=engine_with_table_t), f"should have rejected: {sql!r}"


def test_ordinary_casts_still_validate(engine_with_table_t) -> None:
    """The Bypass 2 fix must not reject an everyday cast to a real SQL type -
    only `exp.ObjectIdentifier` targets (PostgreSQL's OID types) are
    checked; `exp.DataType` targets (`text`, `integer`, ...) are untouched.
    """
    assert is_safe_query("SELECT CAST(a AS TEXT) FROM t", engine=engine_with_table_t)
    assert is_safe_query("SELECT a::TEXT FROM t", engine=engine_with_table_t)


# Task 4, Bypass 3: PostgreSQL's function-call sugar does not need
# parentheses at all. `alias.name`, where `name` is not a column of `alias`,
# resolves as `name(alias)` - and sqlglot parses that into
# `exp.Column(this=Identifier(name), table=Identifier(alias))`: not an
# `exp.Func`, not an `exp.Dot`, not an `exp.Table`, so it was invisible to
# every gate in `safety.py`, including the `pg_` internals rule. Reproduced
# live as `aipa_ro` (2026-09-26), `is_safe_query` returning True for each:
#
#   SELECT g.pg_relation_filepath FROM generate_series(16384,16400) g
#       -> 'base/16384/16387'
#   SELECT g.pg_get_indexdef FROM generate_series(16384,16500) g
#       -> 'CREATE UNIQUE INDEX customers_pkey ON public...'
#   SELECT c.pg_column_size FROM customers c   -> 34, 32
#   SELECT g.pg_sleep FROM generate_series(1,2) g -> executed, 3.0s elapsed
#
# and `SELECT g.pg_terminate_backend FROM generate_series(<pid>,<pid>) g`,
# which the reviewer proved killed a live backend. That last one is
# deliberately *not* in the list below and deliberately never re-run: it is
# proven and disruptive. Its shape is covered by `pg_sleep`, which reaches
# the same `integer`-argument surface.
#
# The five positional variants after the four proven cases are the ones the
# reviewer confirmed also work today: select list, `WHERE`, `ORDER BY`,
# inside a CTE, and across a `UNION ALL`.
_COLUMN_CALL_BYPASS_PROBES: list[tuple[str, str]] = [
    # The four reproduced cases.
    (
        "pg_relation_filepath",
        "SELECT g.pg_relation_filepath FROM generate_series(16384,16400) g",
    ),
    ("pg_get_indexdef", "SELECT g.pg_get_indexdef FROM generate_series(16384,16500) g"),
    ("pg_column_size", "SELECT c.pg_column_size FROM customers c"),
    ("pg_sleep", "SELECT g.pg_sleep FROM generate_series(1,2) g"),
    # The five positional variants.
    ("select_list", "SELECT c.customer_id, c.pg_column_size FROM customers c"),
    ("in_where", "SELECT * FROM customers c WHERE c.pg_column_size > 0"),
    ("in_order_by", "SELECT c.customer_id FROM customers c ORDER BY c.pg_column_size"),
    (
        "inside_cte",
        "WITH x AS (SELECT c.pg_column_size AS v FROM customers c) SELECT * FROM x",
    ),
    (
        "across_union_all",
        "SELECT c.customer_id FROM customers c "
        "UNION ALL SELECT g.pg_column_size FROM generate_series(1,2) g",
    ),
    # Part 2's own reach, beyond what the `pg_` name rule covers: `lo_get`
    # reads a large object by OID and carries no `pg_` prefix, so only the
    # default-deny column resolution refuses it.
    ("lo_get_no_pg_prefix", "SELECT g.lo_get FROM generate_series(16384,16400) g"),
    ("quoted_identifier", 'SELECT c."pg_column_size" FROM customers c'),
]


@pytest.mark.parametrize(
    "label,sql", _COLUMN_CALL_BYPASS_PROBES, ids=[label for label, _ in _COLUMN_CALL_BYPASS_PROBES]
)
def test_column_call_bypass_is_rejected(engine_with_table_t, label: str, sql: str) -> None:
    """Every variant of Bypass 3 must be refused post-fix - each one passed
    `is_safe_query` at BASE (commit 30baa70), before `safety._references_
    internal_column_name` and `safety._references_unresolvable_qualified_
    column` existed.
    """
    assert not is_safe_query(sql, engine=engine_with_table_t), f"should have rejected: {sql!r}"


# The Bypass 3 fix is default-deny over *qualified* column references, so the
# thing it must not do is reject ordinary analytics SQL that qualifies its
# columns - which is most real SQL. Beyond `ANALYTICS_CORPUS` below (32
# queries, all of which both validate and execute), these are the shapes
# where a qualifier resolves to something other than a plain base table:
# derived tables, explicit CTE column alias lists, `LATERAL`, self-joins,
# qualified stars, and a function scan's own output column - each checked
# because each is a place a naive implementation of this rule would break.
_LEGITIMATE_QUALIFIED_COLUMN_QUERIES: list[tuple[str, str]] = [
    ("base_table", "SELECT c.name FROM customers c"),
    ("derived_table_alias", "SELECT t.total FROM (SELECT SUM(amount) AS total FROM sales) t"),
    (
        "derived_table_in_where",
        "SELECT t.n FROM (SELECT COUNT(*) AS n FROM sales) AS t WHERE t.n > 0",
    ),
    ("qualified_star", "SELECT d.* FROM (SELECT * FROM sales) d"),
    (
        "cte_column_alias_list",
        "WITH t(x, y) AS (SELECT category, SUM(amount) FROM sales GROUP BY category) "
        "SELECT t.x, t.y FROM t",
    ),
    ("function_scan_alias_list", "SELECT g.n FROM generate_series(1,5) AS g(n)"),
    ("function_scan_default_column", "SELECT g.generate_series FROM generate_series(1,5) g"),
    (
        "self_join",
        "SELECT a.name, b.name FROM customers a JOIN customers b ON a.customer_id <> b.customer_id",
    ),
    (
        "uncorrelated_in_subquery",
        "SELECT s.amount FROM sales s WHERE s.customer_id IN "
        "(SELECT c.customer_id FROM customers c)",
    ),
    (
        "correlated_scalar_subquery",
        "SELECT s.amount, (SELECT c.name FROM customers c WHERE c.customer_id = s.customer_id) "
        "AS who FROM sales s",
    ),
    (
        "lateral",
        "SELECT c.name FROM customers AS c JOIN LATERAL "
        "(SELECT s.amount FROM sales s WHERE s.customer_id = c.customer_id) l ON TRUE",
    ),
]
# Two shapes deliberately left out of the list above, because both are
# already refused at BASE (commit 30baa70) for reasons that have nothing to
# do with Bypass 3, and listing them here would misattribute a pre-existing
# limitation to this fix:
#   - `... WHERE EXISTS (SELECT 1 FROM customers c ...)` - `exp.Exists` is an
#     `exp.Func` subclass and `"exists"` is not in
#     `PostgresEngine.allowed_functions`.
#   - `SELECT public.customers.name FROM public.customers` -
#     `_references_unknown_table` still hardcodes `"main"` as the only
#     acceptable schema qualifier; PostgreSQL's is `"public"`
#     (`PostgresEngine.default_schema`). Phase 3b Task 6 is where
#     `default_schema` gets wired through that check.
# Both were re-confirmed False at BASE on 2026-09-26 before being excluded.


@pytest.mark.parametrize(
    "label,sql",
    _LEGITIMATE_QUALIFIED_COLUMN_QUERIES,
    ids=[label for label, _ in _LEGITIMATE_QUALIFIED_COLUMN_QUERIES],
)
def test_legitimate_qualified_columns_still_validate(
    postgres_dsn: str, label: str, sql: str
) -> None:
    """The other half of Bypass 3's fix: a false rejection costs a user an
    unanswerable question, so every one of these must still validate. Uses
    the real `customers`/`sales` tables rather than the `t` fixture so the
    column names being resolved are the ones a real question would use.
    """
    engine = open_engine(postgres_dsn)
    assert is_safe_query(sql, engine=engine), f"wrongly rejected: {sql!r}"


def test_dot_call_dialects_pins_the_postgres_engines_own_dialect() -> None:
    """`safety._DOT_CALL_DIALECTS` gates all three of PostgreSQL's dotted-
    notation rules (`_references_disallowed_dot_call`,
    `_references_internal_column_name`,
    `_references_unresolvable_qualified_column`). Nothing else connects that
    hardcoded string to the engine, so renaming `PostgresEngine.
    sqlglot_dialect` would turn all three into silent no-ops with every
    other test in this file still green - the rules would simply never run.
    This is the test that fails instead.
    """
    assert PostgresEngine.sqlglot_dialect in _safety._DOT_CALL_DIALECTS


def test_postgres_column_names_reports_the_real_columns(postgres_dsn: str) -> None:
    """`Engine.column_names()` is what `_references_unresolvable_qualified_
    column` resolves against, so an empty or wrong answer here would turn
    Part 2 of the Bypass 3 fix into either a no-op or a blanket refusal
    without any other test necessarily noticing.
    """
    engine = open_engine(postgres_dsn)
    columns = engine.column_names()
    assert {"customer_id", "name", "amount", "sale_date", "category"} <= columns
    assert "pg_column_size" not in columns


def test_collate_is_allowed(postgres_dsn: str) -> None:
    """`COLLATE` is ordinary SQL grammar, not a `pg_proc` call, but sqlglot's
    `exp.Collate` is an `exp.Func` subclass, so it went through default-deny
    the same as a real function name and was wrongly rejected before
    `"collate"` was added to `PostgresEngine.allowed_functions`. Validated
    and executed against the real `customers` table, matching the exact
    query the task brief flagged.
    """
    engine = open_engine(postgres_dsn)
    sql = 'SELECT name COLLATE "C" FROM customers'
    assert is_safe_query(sql, engine=engine), f"wrongly rejected: {sql!r}"
    result = engine.execute(sql, max_rows=10, work_limit=0)
    assert result.ok, f"{sql!r} failed to execute: {result.error}"


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
