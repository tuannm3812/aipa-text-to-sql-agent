from __future__ import annotations

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


def test_an_unrecognised_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="unrecognised database scheme"):
        open_engine("mysql://localhost/x")


def test_a_missing_driver_names_the_extra_to_install() -> None:
    """A bare ImportError tells the user nothing actionable."""
    try:
        import duckdb  # noqa: F401
    except ImportError:
        with pytest.raises(EngineUnavailableError, match="duckdb"):
            open_engine("duckdb:///tmp/x.duckdb")
    else:
        pytest.skip("duckdb is installed, so the unavailable path cannot be exercised")
