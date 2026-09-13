"""The engine seam: what a database must provide to back the agent."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types import QueryResult, SchemaChunk


class EngineError(Exception):
    """Base class for engine problems the caller is expected to handle."""


class EngineUnavailableError(EngineError):
    """The engine's driver is not installed."""


class EngineUnreachableError(EngineError):
    """The engine's target does not exist or cannot be connected to."""


@runtime_checkable
class Engine(Protocol):
    """One database backend.

    Implementations own their own read-only enforcement. There is no shared
    mechanism, because SQLite's URI flag plus authorizer, DuckDB's read_only
    connect flag and PostgreSQL's read-only transaction have nothing in common
    but the guarantee. `tests/test_engine_conformance.py` is what holds every
    implementation to that guarantee.
    """

    name: str
    sqlglot_dialect: str
    internal_prefixes: tuple[str, ...]
    internal_names: frozenset[str]
    prompt_dialect_section: str

    def check_reachable(self) -> None:
        """Raise `EngineUnreachableError` if the target cannot be opened."""
        ...

    def execute(self, sql: str, *, max_rows: int, work_limit: int) -> QueryResult:
        """Run already-validated read-only SQL."""
        ...

    def raw_schema(self) -> str:
        """Every table's DDL, newline-separated."""
        ...

    def schema_chunks(self) -> list[SchemaChunk]:
        """Table-level chunks for retrieval.

        Must not read row data beyond low-cardinality value hints.
        """
        ...

    def schema_fingerprint(self) -> tuple[object, ...]:
        """A value that changes when the schema changes, for cache keying."""
        ...
