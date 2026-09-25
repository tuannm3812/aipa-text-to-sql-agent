"""The engine seam: what a database must provide to back the agent."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types import QueryResult, SchemaChunk


class EngineError(Exception):
    """Base class for engine problems the caller is expected to handle."""


class EngineUnavailableError(EngineError):
    """The engine's driver is not installed."""


class EngineUnreachableError(EngineError, FileNotFoundError):
    """The engine's target does not exist or cannot be connected to.

    Also inherits `FileNotFoundError` so pipeline callers that historically
    raised `FileNotFoundError("input database not found")` keep that
    documented contract for any caller that still catches it specifically.
    """


@runtime_checkable
class Engine(Protocol):
    """One database backend.

    Implementations own their own read-only enforcement. There is no shared
    mechanism, because SQLite's URI flag plus authorizer, DuckDB's read_only
    connect flag and PostgreSQL's read-only transaction have nothing in common
    but the guarantee. `tests/test_engine_conformance.py` is what holds every
    implementation to that guarantee.

    `allowed_functions` picks which of two validation strategies
    `safety.is_safe_query` applies to this engine's queries. `None` keeps the
    original blocklist behaviour (name-based internals checks only, in a
    table-source position) - SQLite stays here, since its function surface is
    small and that list has held. A `frozenset[str]` switches to default-deny:
    every function call anywhere in the query - scalar, aggregate, window or
    table - must resolve to a name in the set, or the query is rejected, and
    every `FROM`/`JOIN` target must name a real table (from `table_names()`)
    or a CTE defined in the same statement. See `DuckDBEngine.allowed_functions`
    for why DuckDB needed the stronger guarantee,
    `text_to_sql_agent/safety.py`'s `_resolve_function_name` for how a parsed
    call is resolved to a name to check, and `_references_unknown_table` for
    the table rule.

    `prompt_dialect_section`, `prompt_dialect_name` and
    `prompt_engine_rules_block` are the three engine-owned fragments
    `llm.py`'s `_assemble_prompt` substitutes into the shared prompt body, so
    every dialect-dependent instruction in the finished system prompt (not
    just the labelled dialect section) names the right engine. `pipeline.py`'s
    `_repair_sql` also reads `prompt_dialect_name` for the same reason in the
    repair user prompt. See `SQLiteEngine`'s copies for why each one is
    pinned to its exact legacy wording.
    """

    name: str
    sqlglot_dialect: str
    # The schema/catalogue a bare, unqualified table name resolves against for
    # this engine - SQLite's and DuckDB's is "main", PostgreSQL's is "public".
    # Not yet consumed here (Phase 3b Task 6 wires it into `table_names()`'s
    # qualified identity and `safety.py`'s schema-qualifier check); declared
    # on the protocol now so every implementation states its own value rather
    # than one being added later with no engine actually holding it.
    default_schema: str
    internal_prefixes: tuple[str, ...]
    internal_names: frozenset[str]
    allowed_functions: frozenset[str] | None
    prompt_dialect_section: str
    prompt_dialect_name: str
    prompt_engine_rules_block: str
    schema_header: str

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

    def table_names(self) -> frozenset[str]:
        """Every user table's name, lowercased.

        Used by `safety.is_safe_query`'s default-deny table check
        (`_references_unknown_table`) - only called for an engine whose
        `allowed_functions` is not `None`. Implementations should reuse
        `schema.py`'s fingerprint-keyed schema-chunk cache rather than
        issuing a fresh catalogue query on every call.
        """
        ...

    def column_names(self) -> frozenset[str]:
        """Every user table's column names, lowercased, unioned across tables.

        Used by `safety.is_safe_query`'s default-deny *column* check
        (`_references_unresolvable_qualified_column`), which extends
        default-deny from functions and tables to the qualified-column
        position PostgreSQL also reads as a function call - see that
        function's docstring for the bypass it closes and why the union
        across tables (rather than a per-table resolution) is the right
        granularity here.

        Only called for an engine whose `allowed_functions` is not `None`
        *and* whose `sqlglot_dialect` is in `safety._DOT_CALL_DIALECTS`, so
        SQLite and DuckDB never reach it in practice. Implementations should
        reuse `schema.py`'s fingerprint-keyed schema-chunk cache the same way
        `table_names()` does - the two read the same chunks.
        """
        ...
