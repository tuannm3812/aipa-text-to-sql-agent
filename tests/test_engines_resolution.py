from __future__ import annotations

import sys

import pytest

from text_to_sql_agent.engines import EngineUnavailableError, open_engine


@pytest.mark.parametrize(
    "dsn",
    ["data/university_agent.db", "sqlite://data/university_agent.db"],
)
def test_a_bare_path_and_a_sqlite_scheme_both_resolve_to_sqlite(dsn: str) -> None:
    engine = open_engine(dsn)
    assert engine.name == "sqlite"
    assert engine.sqlglot_dialect == "sqlite"


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user:pw@localhost:5432/db",
        "postgres://user:pw@localhost:5432/db",
    ],
)
def test_both_postgres_spellings_resolve_and_keep_the_full_dsn(dsn: str) -> None:
    """Unlike the file-path engines, PostgreSQL needs the whole URL: libpq
    parses its own scheme out of the connection string, so `open_engine`
    must not strip it the way it strips `sqlite://`/`duckdb://`.
    """
    engine = open_engine(dsn)
    assert engine.name == "postgres"
    assert engine.sqlglot_dialect == "postgres"
    assert engine.dsn == dsn


def test_an_unrecognised_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="unrecognised database scheme"):
        open_engine("mysql://localhost/x")


def test_a_missing_driver_names_the_extra_to_install(monkeypatch) -> None:
    """A bare ImportError tells the user nothing actionable.

    CI now always installs the `engines` extra (Task 6, Fix 1), so a version
    of this test that only ran when `duckdb` happened to be absent -
    `pytest.skip`-ing otherwise - would never exercise its real assertion
    again, and a regression in the `except ImportError -> EngineUnavailableError`
    wrapping in `engines/__init__.py` could pass CI silently. This simulates
    the driver being absent instead, regardless of what is actually
    installed: setting `sys.modules["duckdb"] = None` makes `import duckdb`
    raise `ImportError` (the standard library's own convention for a known-
    unimportable module), and dropping `engines.duckdb` from `sys.modules`
    forces it to be re-imported - and hit that failure - the next time
    `open_engine` reaches its `from .duckdb import DuckDBEngine` line, rather
    than reusing an already-imported (and already-succeeded) module object.
    """
    monkeypatch.setitem(sys.modules, "duckdb", None)
    monkeypatch.delitem(sys.modules, "text_to_sql_agent.engines.duckdb", raising=False)

    with pytest.raises(EngineUnavailableError, match="duckdb"):
        open_engine("duckdb:///tmp/x.duckdb")


def test_a_missing_postgres_driver_names_the_extra_to_install(monkeypatch) -> None:
    """Same proof as `test_a_missing_driver_names_the_extra_to_install`, for the
    registry's newest entry - the `_ENGINES`/`importlib` refactor must not have
    quietly dropped this per-engine guarantee for the engine it added.
    """
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.delitem(sys.modules, "text_to_sql_agent.engines.postgres", raising=False)

    with pytest.raises(EngineUnavailableError, match="postgres"):
        open_engine("postgresql://user:pw@localhost:5432/db")
