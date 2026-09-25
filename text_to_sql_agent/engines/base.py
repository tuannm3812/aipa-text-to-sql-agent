"""The engine seam: what a database must provide to back the agent."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..types import QueryResult, SchemaChunk


def table_name_spellings(chunks: Iterable[SchemaChunk], *, default_schema: str) -> frozenset[str]:
    """Every spelling of every chunk's table that is valid to query, lowercased.

    The one implementation of `Engine.table_names()`'s contract - see that
    method's docstring for the rule and why the bare/qualified asymmetry is
    what it is. Shared rather than repeated per engine deliberately: three
    layers quietly disagreeing about which table names are real is the exact
    defect `docs/3_decisions.md`'s 2026-09-25 entry recorded, so the rule
    lives in one place that every engine calls, unlike read-only enforcement
    (see `Engine`'s own docstring), where the three mechanisms genuinely have
    nothing in common to factor out.

    Args:
        chunks: The engine's schema chunks, each carrying its own
            `schema_name` (empty for the default schema).
        default_schema: The engine's `default_schema`.

    Returns:
        Bare *and* `default_schema`-qualified spellings for a default-schema
        table; the qualified spelling alone for every other table.
    """
    names: set[str] = set()
    for chunk in chunks:
        bare = chunk.table_name.lower()
        schema = chunk.schema_name.lower()
        if schema:
            names.add(f"{schema}.{bare}")
        else:
            names.add(bare)
            names.add(f"{default_schema.lower()}.{bare}")
    return frozenset(names)


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
    # Phase 3b Task 6 made this load-bearing: it is what `table_names()` uses
    # to decide which tables may also be spelled bare, what `SchemaChunk.
    # schema_name` is resolved against before a chunk records it, and - via
    # `table_names()` rather than a hardcoded string - what `safety.py`'s
    # schema-qualifier check now accepts.
    default_schema: str
    # The `work_limit` `execute()` receives when a caller does not pass one
    # explicitly - see `execution.execute_query`. Each engine's `work_limit`
    # is in its own unit (SQLite counts VM steps, DuckDB and PostgreSQL count
    # milliseconds), so this must be the right number *in that engine's own
    # unit*, not a single value shared across engines - passing SQLite's
    # 100_000-VM-step figure straight through as milliseconds to a
    # millisecond engine is a ~100-second timeout, not the ~5-second one the
    # design intends. SQLite's own value and error code
    # (`QUERY_ABORTED_AFTER_100000_VM_STEPS`) are pinned by
    # `tests/test_execution.py` and must not change.
    default_work_limit: int
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

        Every schema the engine can actually query is in scope, not just
        `default_schema` - a chunk for a table outside it sets
        `SchemaChunk.schema_name`, so `chunk.qualified_name` is the spelling
        a query must use and the spelling `table_names()` advertises. What
        `raw_schema()` shows the model and what `table_names()` accepts must
        agree table for table: `docs/3_decisions.md`'s 2026-09-25 entry is
        what happens when they do not.
        """
        ...

    def schema_fingerprint(self) -> tuple[object, ...]:
        """A value that changes when the schema changes, for cache keying."""
        ...

    def table_names(self) -> frozenset[str]:
        """Every spelling of every user table that is valid to query, lowercased.

        Used by `safety.is_safe_query`'s default-deny table check
        (`_references_unknown_table`) - only called for an engine whose
        `allowed_functions` is not `None`. Implementations should reuse
        `schema.py`'s fingerprint-keyed schema-chunk cache rather than
        issuing a fresh catalogue query on every call.

        Two spellings, and the asymmetry between them is the contract
        (Phase 3b Task 6):

        * a table in `default_schema` appears **twice** - bare (`customers`)
          and qualified (`public.customers`) - because the engine itself
          resolves both to that table;
        * a table outside it appears **only** qualified (`analytics.thing`).
          Its bare name is deliberately absent: a bare name resolves against
          the engine's default schema, so accepting `thing` would approve a
          query that then fails at execution. That is the 2026-09-25 failure
          (`docs/3_decisions.md`) in mirror image, and this set is where it
          is refused.

        A bare name is therefore never ambiguous, however many schemas share
        it: it means the default schema's table or nothing, which is what the
        engine's own search path does. `analytics.shared` and `main.shared`
        coexist here as two distinct entries.
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
