"""Engine resolution: map a DSN to an implementation."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import cast

from .base import (
    Engine,
    EngineError,
    EngineForbiddenError,
    EngineUnavailableError,
    EngineUnreachableError,
)

__all__ = [
    "Engine",
    "EngineError",
    "EngineForbiddenError",
    "EngineUnavailableError",
    "EngineUnreachableError",
    "open_engine",
]

# scheme -> (module, class name, extra that provides the driver, or None when
# the driver needs no extra - SQLite's stdlib driver ships with Python).
# The one place a scheme maps to an implementation, so a test can enumerate
# every engine rather than hand-maintaining a parallel list that silently
# omits the newest one (Codex's 2026-09-26 review of `tests/test_llm.py`'s
# `_known_engine_classes()` found exactly that drift).
_ENGINES: dict[str, tuple[str, str, str | None]] = {
    "sqlite": (".sqlite", "SQLiteEngine", None),
    "duckdb": (".duckdb", "DuckDBEngine", "duckdb"),
    "postgresql": (".postgres", "PostgresEngine", "postgres"),
    "postgres": (".postgres", "PostgresEngine", "postgres"),
}

# Schemes whose engine needs the DSN whole, scheme included, rather than the
# scheme-stripped remainder the file-path engines (`sqlite`, `duckdb`) take.
# libpq parses its own scheme out of the connection string; PostgreSQL is the
# first engine here that isn't a bare filesystem path.
_NEEDS_FULL_DSN = {"postgresql", "postgres"}


def open_engine(dsn: str) -> Engine:
    """Resolve a DSN to an engine.

    A bare filesystem path means SQLite, so every existing caller keeps working
    without knowing engines exist.

    Args:
        dsn: `path.db`, `sqlite://path.db`, `duckdb://path.duckdb`, or a
            `postgresql://`/`postgres://` URL.

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

    if scheme not in _ENGINES:
        raise ValueError(f"unrecognised database scheme: {scheme!r}")

    module_name, class_name, extra = _ENGINES[scheme]
    try:
        module = importlib.import_module(module_name, __name__)
    except ImportError as exc:
        if extra is None:
            raise
        raise EngineUnavailableError(
            f"the {extra!r} extra is required for {scheme}:// databases"
        ) from exc

    # Protocols get an implicit no-arg `__init__` from `object`, so `type[Engine]`
    # would type-check `engine_cls(arg)` as "too many arguments" - every real
    # implementation's constructor is `(dsn: str) -> Self`, which is what this
    # casts to instead.
    engine_ctor = cast("Callable[[str], Engine]", getattr(module, class_name))
    arg = dsn if scheme in _NEEDS_FULL_DSN else rest
    return engine_ctor(arg)
