"""The engine seam: what a database must provide to back the agent."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

from ..types import QueryResult, SchemaChunk

# Decision (2026-09-26): schema scope is opt-in, not automatic. Phase 3b Task
# 6 widened DuckDB and PostgreSQL from reading only their own default schema
# (`main`/`public`) to every schema the connecting role can read - which
# means every such schema's table names, columns and DDL are sent to the LLM
# provider. A deployment may have granted its read-only role `USAGE` on a
# staging or PII schema for some unrelated tool, and that must not silently
# become LLM-visible just because this agent happened to widen its own
# catalogue read. `AIPA_EXTRA_SCHEMAS` is the opt-in: a comma-separated list
# of schema names to read *in addition to* the engine's own default schema.
# Unset or empty opts into nothing, which is the safe default - both engines'
# `__init__` read it once via `extra_schemas_from_env()` below, mirroring how
# `ui/secrets.py`'s `active_gemini_key` reads `GEMINI_API_KEY` straight from
# `os.environ` rather than through a threaded setting; `open_engine(dsn)`
# takes only a DSN, and neither `ui/settings.py`'s `Settings` nor the sidebar
# carries any other per-engine configuration today, so adding a second
# constructor parameter (and plumbing it through every `open_engine` call
# site and the DSN string itself) would be new surface for something an
# operator sets once per deployment, not once per question. A DSN-level query
# parameter was ruled out for the same reason `redact_dsn` exists at all: a
# DSN is already sensitive text handled as a single opaque credential-bearing
# string everywhere in this codebase (`ui/uploads.py`, `dsn.py`), and folding
# scope configuration into it would mean redaction, logging and the "Using
# `<dsn>`" sidebar caption all need to start parsing it apart again.
_EXTRA_SCHEMAS_ENV_VAR = "AIPA_EXTRA_SCHEMAS"


def extra_schemas_from_env() -> frozenset[str]:
    """Schema names opted into beyond an engine's own default schema.

    Reads `AIPA_EXTRA_SCHEMAS` fresh on every call (not cached at import
    time) so a test can set it with `monkeypatch.setenv` and a redeployment
    can change it without restarting a long-lived process; each engine reads
    it once, in its own `__init__`, so one engine instance's scope stays
    fixed for its whole lifetime even if the variable changes mid-process.
    Blank entries and surrounding whitespace are dropped. Still gated by
    each engine's own privilege check where one exists - `PostgresEngine`'s
    `_user_schema_names` only ever reads a schema this names *and* the
    connecting role holds `USAGE` on; naming a schema here does not by
    itself grant access to it.

    Returns:
        The opted-in schema names, exactly as spelled in the environment
        variable. Empty when the variable is unset or blank, which is the
        safe default: only the engine's own default schema is read.
    """
    raw = os.environ.get(_EXTRA_SCHEMAS_ENV_VAR, "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


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


class EngineForbiddenError(EngineError):
    """The engine's target is reachable, but refuses to proceed on safety grounds.

    Decision (2026-09-26): distinct from `EngineUnreachableError` - the
    target answered, so "unreachable" would be wrong, and this deliberately
    does **not** also inherit `FileNotFoundError` the way that class does.
    `EngineUnreachableError`'s `FileNotFoundError` inheritance exists so a
    caller that historically checked for a missing file keeps working; there
    is no equivalent legacy contract for "reachable but refused," and
    claiming one would misdescribe the failure to any caller that branches on
    `FileNotFoundError` specifically.

    Raised by `PostgresEngine.check_reachable()` when the connecting role is
    a superuser or holds `pg_read_server_files`, `pg_write_server_files` or
    `pg_execute_server_program` - see that method for why PostgreSQL alone
    needs a role-provisioning check at connect time: its defence against
    reading host files rests entirely on how the role was provisioned,
    unlike DuckDB's `enable_external_access=False`, which the connection
    itself enforces regardless of how the DuckDB file was opened.

    `pipeline.ask_database`/`ask_database_with_sql` call
    `engine.check_reachable()` before their own `try` block (the same place
    `EngineUnreachableError` is raised from), so this propagates to their
    caller the same way - `ui/chat.py`'s `_run_query` and `ui/sidebar.py`'s
    `active_db_path` call already catch bare `Exception` around that call and
    redact the text before it reaches the page, so no new catch site was
    needed for this to surface safely rather than as a raw traceback.
    """


class AmbiguousTableIdentityError(EngineForbiddenError):
    """Two distinct real tables would share one lowercased spelling.

    Decision (2026-09-26, review finding 2/4 on the schema-identity
    boundary). `table_name_spellings`/`table_column_spellings` below
    lowercase every schema and table name before building the spelling a
    query must use - deliberately, so a bare `customers` and a quoted
    `"CUSTOMERS"` both resolve the way every other case-insensitive
    comparison in `safety.py` already does. That lowercasing is safe only
    because, on every engine this project shipped before PostgreSQL, two
    *distinct* real tables could never produce the same lowercased spelling.
    PostgreSQL breaks that assumption two ways:

    * quoted identifiers are case-sensitive, so `public` and `"Public"` are
      two different real schemas that fold to the same spelling
      (`public.customers`) once lowercased - finding 2's live proof;
    * the qualified-name namespace is a flat, unescaped string, so a table
      named `"analytics.thing"` inside the default schema and a table
      `thing` inside a schema named `analytics` both spell as
      `analytics.thing` - finding 4.

    Silently keeping whichever chunk happened to be seen last (the pre-fix
    behaviour) means a query naming the *other* table validates and executes
    against the wrong one - a default-deny gate approving a reference it
    never meant to advertise, which is wrong regardless of whether today's
    two colliding tables happen to both be real (finding 4's write-up:
    "nothing escaped, but... is wrong"). Failing closed here means neither
    colliding spelling is ever advertised or accepted, on the same
    fail-closed precedent as `_refuse_if_role_is_overprivileged` in
    `postgres.py`: an ambiguous config is refused loudly rather than
    resolved by guessing.

    Raised by `table_name_spellings`/`table_column_spellings`, so it
    surfaces through `Engine.table_names()`/`Engine.table_columns()` and
    from there through `safety.is_safe_query` (called from inside
    `_references_unknown_table`/`_references_unresolvable_qualified_column`).
    Every call site of `is_safe_query` in `pipeline.py` sits inside, or one
    frame below, a broad `except Exception` (see `EngineForbiddenError`'s own
    docstring for the equivalent propagation path DuckDB/PostgreSQL's other
    fail-closed errors already take), so this still surfaces as a redacted
    message rather than a raw traceback - it does not need its own catch
    site.
    """

    def __init__(self, spelling: str, first: SchemaChunk, second: SchemaChunk) -> None:
        """Build the message from the colliding spelling and the two chunks it names.

        Args:
            spelling: The lowercased spelling both chunks would share.
            first: The chunk that already owned `spelling`.
            second: The chunk that just collided with it.
        """
        super().__init__(
            f"{spelling!r} would refer ambiguously to both "
            f"{first.qualified_name!r} and {second.qualified_name!r} once "
            "case-folded - refusing to advertise or accept either spelling. "
            "Rename one of the two tables so their spellings no longer collide."
        )
        self.spelling = spelling
        self.first = first
        self.second = second


def _spellings_with_identity(
    chunks: Iterable[SchemaChunk], *, default_schema: str
) -> dict[str, SchemaChunk]:
    """Every valid, lowercased spelling mapped to the one chunk it must mean.

    The shared engine of `table_name_spellings` and `table_column_spellings`
    below - computed once so the two agree by construction rather than by
    each re-deriving the same spellings and hoping they line up, and so the
    collision check below runs exactly once per schema read.

    Raises:
        AmbiguousTableIdentityError: If a spelling this loop is about to
            record already belongs to a *different* chunk (different
            `(schema_name, table_name)`, exact case). Two spellings coming
            from the *same* chunk - a default-schema table's bare and
            qualified forms - are never a collision; they are the documented
            dual-spelling contract `table_name_spellings` implements.
    """
    owners: dict[str, SchemaChunk] = {}
    for chunk in chunks:
        bare = chunk.table_name.lower()
        schema = chunk.schema_name.lower()
        spellings = [f"{schema}.{bare}"] if schema else [bare, f"{default_schema.lower()}.{bare}"]
        for spelling in spellings:
            existing = owners.get(spelling)
            if existing is not None and (
                existing.schema_name,
                existing.table_name,
            ) != (chunk.schema_name, chunk.table_name):
                raise AmbiguousTableIdentityError(spelling, existing, chunk)
            owners[spelling] = chunk
    return owners


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

    Raises:
        AmbiguousTableIdentityError: See `_spellings_with_identity`.
    """
    return frozenset(_spellings_with_identity(chunks, default_schema=default_schema))


def table_column_spellings(
    chunks: Iterable[SchemaChunk], *, default_schema: str
) -> dict[str, frozenset[str]]:
    """Each queryable table spelling mapped to that table's own columns, lowercased.

    The one implementation of `Engine.table_columns()`'s contract, keyed by
    exactly the spellings `table_name_spellings` above produces (both now
    built from the same `_spellings_with_identity` pass, so the two cannot
    drift), and shared for the same reason: the validator resolves a
    qualified column against *one table's* columns, so "which columns does
    this spelling have" must not drift from "which spellings are real".

    Per-table rather than a flat union deliberately, and that distinction is
    load-bearing security (2026-09-26): a flat union over every readable
    schema re-armed the `alias.name` function-call bypass the moment a column
    anywhere in the database happened to be named after a single-argument
    catalogue function. See
    `safety._references_unresolvable_qualified_column`.

    Args:
        chunks: The engine's schema chunks, each carrying its own
            `schema_name` (empty for the default schema).
        default_schema: The engine's `default_schema`.

    Returns:
        Every spelling in `table_name_spellings(chunks, ...)`, mapped to the
        lowercased column names of the table it spells.

    Raises:
        AmbiguousTableIdentityError: See `_spellings_with_identity`.
    """
    owners = _spellings_with_identity(chunks, default_schema=default_schema)
    return {
        spelling: frozenset(column.lower() for column in chunk.columns)
        for spelling, chunk in owners.items()
    }


def is_internal_schema_name(
    name: str, *, internal_prefixes: tuple[str, ...], internal_names: frozenset[str]
) -> bool:
    """True if `name` is one of the engine's own internal schemas, case-insensitively.

    Decision (2026-09-26, review finding 1). `safety._references_internals`
    lowercases a query's schema qualifier before comparing it against an
    engine's `internal_prefixes`/`internal_names` - deliberately, so
    `"PG_evil".t` is refused exactly as `pg_evil.t` would be. Before this
    function existed, nothing on the *engine* side made the same comparison
    when deciding which schemas to read: `PostgresEngine._user_schema_names`
    and `DuckDBEngine._allowed_schemas` matched only the exact literal
    strings `"information_schema"`/`"pg_catalog"` (or, for PostgreSQL,
    nothing at all beyond the operator-supplied candidate list). A schema
    genuinely named `PG_evil` or `Information_Schema` - whether it exists in
    the target database or was merely opted into via `AIPA_EXTRA_SCHEMAS` -
    was therefore read into `raw_schema()` and `table_names()` and then
    permanently rejected by `safety.py`'s case-insensitive check: advertised
    to the model, refused by the validator, the exact layer-disagreement
    `docs/3_decisions.md`'s 2026-09-25 entry closed in the opposite
    direction. Both sides must apply the identical comparison - this
    function *is* that comparison, called from each engine's own schema-scope
    filtering (`PostgresEngine._user_schema_names`,
    `DuckDBEngine._allowed_schemas`) so a schema `safety.py` would refuse to
    let a query reference is never read in the first place.

    Args:
        name: A candidate schema name, in whatever case it was spelled -
            by an operator in `AIPA_EXTRA_SCHEMAS`, or by the database's own
            catalogue.
        internal_prefixes: The engine's `Engine.internal_prefixes`.
        internal_names: The engine's `Engine.internal_names`.

    Returns:
        True if `name`, lowercased, is in `internal_names` or starts with
        one of `internal_prefixes` - the same test `_references_internals`
        applies to a query's schema qualifier.
    """
    lowered = name.lower()
    return lowered in internal_names or lowered.startswith(internal_prefixes)


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

        Scope is `default_schema` plus whatever `AIPA_EXTRA_SCHEMAS` opts
        into (`extra_schemas_from_env()` above), and nothing else - an
        engine must not read a schema simply because it is able to. On a
        deployment that sets nothing, that is one schema. A chunk for a
        table outside `default_schema` sets `SchemaChunk.schema_name`, so
        `chunk.qualified_name` is the spelling a query must use and the
        spelling `table_names()` advertises. What `raw_schema()` shows the
        model, what `schema_chunks()` yields and what `table_names()` accepts
        must all resolve the opted-in set the same way and agree table for
        table: `docs/3_decisions.md`'s 2026-09-25 entry is what happens when
        they do not.
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

        Raises:
            AmbiguousTableIdentityError: If two distinct real tables would
                share one lowercased spelling once case-folded - only
                reachable on an engine with case-sensitive quoted
                identifiers (PostgreSQL). See that error's own docstring.
        """
        ...

    def table_columns(self) -> Mapping[str, frozenset[str]]:
        """Each spelling `table_names()` advertises, mapped to that table's columns.

        Used by `safety.is_safe_query`'s default-deny *column* check
        (`_references_unresolvable_qualified_column`), which extends
        default-deny from functions and tables to the qualified-column
        position PostgreSQL also reads as a function call.

        Per table, not a union across tables. It was a union
        (`column_names()`) until 2026-09-26: Task 6 widened every engine from
        one schema to every schema the role can read, which silently widened
        that union to every column in the database and re-armed the bypass -
        a table named after a single-argument catalogue function in any
        readable schema was enough. (That widening was itself narrowed to the
        `AIPA_EXTRA_SCHEMAS` opt-in later the same day, but the per-table
        keying stands on its own: an opted-in schema can carry exactly the
        same hostile column name.) The validator now resolves each qualifier
        against the one table it names, so a function scan (which has no table
        columns at all) can no longer borrow another table's column name. See
        `table_column_spellings`, which every engine implements this with.

        Only called for an engine whose `allowed_functions` is not `None`
        *and* whose `sqlglot_dialect` is in `safety._DOT_CALL_DIALECTS`, so
        SQLite and DuckDB never reach it in practice. Implementations should
        reuse `schema.py`'s fingerprint-keyed schema-chunk cache the same way
        `table_names()` does - the two read the same chunks.

        Raises:
            AmbiguousTableIdentityError: See `table_names()`.
        """
        ...

    def shadowed_function_names(self) -> frozenset[str]:
        """Allowlisted names that resolve to more than the audited built-in.

        Decision (2026-09-26, Codex review of the Phase 3b closeout, Finding
        1). `allowed_functions` pins a *spelling* - `_references_disallowed_
        function` accepts a call once its resolved name is in that set - but
        nothing constrained *which* `pg_proc` row the connecting role's
        catalogue lookup would actually dispatch to. A user-defined
        `public.lower(integer)` overload sharing an allowlisted name is
        reachable both schema-qualified and bare (an exact argument-type
        match beats the built-in's implicit cast regardless of `search_path`
        order - reordering it to `pg_catalog, public` does not help, verified
        live), and a `SECURITY DEFINER` one then reads whatever its owner can
        read, not what `aipa_ro` can - a read-only transaction does not stop
        a read a `SECURITY DEFINER` function makes on the connecting role's
        behalf. This is what closes that gap: `safety.is_safe_query` refuses
        any query that *uses* a name this reports, rather than trusting the
        name alone to mean the audited `pg_catalog` entry.

        SQLite and DuckDB have no notion of a same-named catalogue overload
        shadowing a built-in the way PostgreSQL's schema search path does -
        neither engine's function surface is pluggable by an unprivileged
        role the way `CREATE FUNCTION` in a schema on `search_path` is - so
        both return the empty set unconditionally and this changes nothing
        about their validation. `PostgresEngine` is the only implementation
        that ever returns a non-empty set.

        Only called for an engine whose `allowed_functions` is not `None`,
        the same gate `table_names()`/`table_columns()` are already read
        behind - an engine that opts out of default-deny is not required to
        implement anything beyond returning the empty set (see those two
        methods' own docstrings for the same reasoning applied to them).

        Implementations should cache this rather than querying `pg_proc` on
        every `is_safe_query` call - see `PostgresEngine.shadowed_function_
        names` for the caching decision this project made and what it costs
        if an overload is created mid-session.

        Returns:
            The lowercased subset of `allowed_functions` that currently has
            an executable overload outside `pg_catalog` - empty when nothing
            is shadowed, which is the common case.
        """
        ...
