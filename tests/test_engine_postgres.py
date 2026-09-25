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

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")

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
