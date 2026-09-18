"""Engine resolution: map a DSN to an implementation."""

from __future__ import annotations

from .base import (
    Engine,
    EngineError,
    EngineUnavailableError,
    EngineUnreachableError,
)

__all__ = [
    "Engine",
    "EngineError",
    "EngineUnavailableError",
    "EngineUnreachableError",
    "open_engine",
]


def open_engine(dsn: str) -> Engine:
    """Resolve a DSN to an engine.

    A bare filesystem path means SQLite, so every existing caller keeps working
    without knowing engines exist.

    Args:
        dsn: `path.db`, `sqlite://path.db`, `duckdb://path.duckdb`, or a
            `postgresql://` URL.

    Returns:
        The engine for that DSN.

    Raises:
        EngineUnavailableError: If the engine's driver is not installed.
        ValueError: If the scheme is not recognised.
    """
    scheme, _, rest = dsn.partition("://")
    if not rest:
        from .sqlite import SQLiteEngine

        return SQLiteEngine(dsn)
    if scheme == "sqlite":
        from .sqlite import SQLiteEngine

        return SQLiteEngine(rest)
    if scheme == "duckdb":
        try:
            from .duckdb import DuckDBEngine
        except ImportError as exc:
            raise EngineUnavailableError(
                "the 'duckdb' extra is required for duckdb:// databases"
            ) from exc

        return DuckDBEngine(rest)
    raise ValueError(f"unrecognised database scheme: {scheme!r}")
