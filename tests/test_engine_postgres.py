"""PostgreSQL-specific tests: proving the read-only guarantee is two mechanisms.

`PostgresEngine` always connects with both the `aipa_ro` least-privilege role
and a read-only transaction (`conn.read_only = True`). The conformance suite
(`test_engine_conformance.py`) proves that combination refuses a write; it
cannot prove *either one alone* would - that's what these tests are for, per
Phase 3's design (`docs/superpowers/specs/2026-09-14-phase-3-engine-abstraction-design.md`
§4.2): a single point of failure in either mechanism must not be silently
covered for by the other.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")

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
