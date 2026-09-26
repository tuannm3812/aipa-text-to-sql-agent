"""PostgreSQL, proving read-only with a least-privilege role and a read-only transaction.

Neither alone is enough. The transaction flag is the per-statement guarantee and
survives a role that was granted too much; the role is what survives a driver or
pooler that resets session state between statements. `tests/test_engine_postgres.py`
proves each is load-bearing by removing it.

Verified against psycopg 3.3.6 (2026-09-26): `Connection.read_only` exists,
and `psycopg.errors.QueryCanceled`, `ReadOnlySqlTransaction` and
`InsufficientPrivilege` are all real exception classes matching the plan's
expectations exactly - see `docs/superpowers/sdd/task-2-report.md` for the
probe output. `conn.read_only = True` set before the first statement, plus a
read-only role holding no write grant, means a write is refused twice over:
`ReadOnlySqlTransaction` when the transaction flag alone would have refused
it, `InsufficientPrivilege` when only the role's grants would have.

`prompt_dialect_section`, `prompt_dialect_name` and `prompt_engine_rules_block`
are PostgreSQL's own prompt fragments (Task 7), substituted into the shared
prompt body by `llm.py`'s `_assemble_prompt`. `allowed_functions` (Task 4,
2026-09-26) is PostgreSQL's own default-deny allowlist, matching
`DuckDBEngine`'s Task 6b work - see that attribute's own comment for how it
was built and verified.

Task 5 (2026-09-26) hardened the schema-extraction methods below without
widening what they expose - every catalogue query filtered to `public` alone,
because `safety.py` accepted no other qualifier and advertising a table the
validator would reject is exactly the inversion `docs/3_decisions.md`'s
2026-09-25 entry closed for DuckDB. Task 6 removed that constraint at its
source: the validator now takes its schema rule from `table_names()`, so
`_user_schema_names` below could read every schema this role can actually
use (never `pg_catalog`, `information_schema` or any other `pg_*`), and each
table keeps its schema through `SchemaChunk.schema_name` all the way to the
validator. Task 5's keying is what made that a widening rather than a
rewrite: `_fetch_columns`/`_fetch_primary_keys`/`_fetch_foreign_keys` were
already keyed by `(schema_name, table_name)`, never bare `table_name` - what
a bare-name key does the moment two schemas share a table name is the live
`BinderException` the 2026-09-25 DuckDB review found - and already took a
list of schema names, so Task 6 widened that list and touched nothing else in
them. The value-hint query has always qualified its table reference
(`_quote_qualified`), so it resolves to the table asked for rather than to
whatever `search_path` finds first.

A 2026-09-26 owner decision narrowed Task 6's "every schema this role can
read" to an opt-in: `_user_schema_names` now reads `default_schema` plus only
what `engines/base.py`'s `extra_schemas_from_env()` (`AIPA_EXTRA_SCHEMAS`)
names, still gated by `has_schema_privilege` - see that function's own
docstring. The same owner decision added `_refuse_if_role_is_overprivileged`,
called from `check_reachable()`: PostgreSQL's defence against reading host
files (see the module's own first paragraph) is entirely a matter of how the
connecting role was provisioned, unlike DuckDB's engine-enforced
`enable_external_access=False`, so this engine now checks that at connect
time and refuses with `EngineForbiddenError` rather than trusting every
deployment to have provisioned its role correctly.

A 2026-09-26 review of the schema-identity boundary found three further gaps,
all one root cause: this engine's own schema filtering was case-sensitive
while `safety.py`'s rules are case-insensitive. `_user_schema_names` now runs
every candidate through `engines/base.py`'s `is_internal_schema_name` - the
same comparison `safety._references_internals` applies - before it is ever
queried, so a schema spelled `PG_evil` or `Information_Schema` is refused at
the source instead of being advertised and then rejected (finding 1).
`default_schema` is no longer the constant `_PUBLIC_SCHEMA`: it is now a
cached property that asks the server's own `current_schema()`, because
`search_path`'s stock value is `"$user", public` and a schema named after the
connecting role resolves a bare table name differently from what the constant
asserted (finding 3) - see that property's own docstring. `engines/base.py`'s
`table_name_spellings`/`table_column_spellings` now refuse (rather than
silently merge) two distinct real tables that would share one lowercased
spelling - PostgreSQL's quoted identifiers are case-sensitive, so `public`
and `"Public"` are two different schemas that fold together only once
lowercased (finding 2), and the flat, unescaped qualified-name string has the
same shape of collision between a dotted table name and a qualified one
(finding 4). See `AmbiguousTableIdentityError`.

A 2026-09-26 Codex review of that Phase 3b closeout found a further, critical
gap: `allowed_functions` pinned a call's *spelling*, never its resolved
identity. `public.lower(integer)` - a user-defined overload sharing an
allowlisted name, reachable both schema-qualified and bare - let a
`SECURITY DEFINER` function run under an audited name and read data `aipa_ro`
has no direct grant on. `shadowed_function_names()` below closes it: a
one-time (per instance) `pg_proc`/`pg_namespace` query reports which
allowlisted names currently have an executable overload outside
`pg_catalog`, and `safety.is_safe_query` refuses any query that *uses* one of
those names - both an explicit non-`pg_catalog` schema qualifier and a name
this reports as shadowed, regardless of qualification. See
`docs/3_decisions.md`'s 2026-09-26 entry for why reordering `search_path` and
rewriting calls to `pg_catalog.`-qualified were both ruled out, and for the
honest precondition this fix rests on: `aipa_ro` itself cannot `CREATE` in
any schema it does not own (verified live), so the overload that arms this
bypass can only come from a different, more privileged principal sharing the
database - a DBA, or another application's role.

2026-09-27 (owner-approved): that fix could not cover **operators** - a
`public.||(text, integer)` operator captures `name || 1` with no function
name for the allowlist to pin - nor Codex's Finding 2, where
`current_schema()` modelled only the first entry of the search path. Both
are closed by one design. Every connection this engine opens is pinned with
`SET LOCAL search_path = pg_catalog` (`_pin_search_path`), so unqualified
functions, operators and types resolve to built-ins or not at all. Under the
pin the server no longer resolves bare table names either, so the engine
does: `resolve_bare_relation_names` walks the role's effective path
(`current_schemas(false)`, read before pinning) exactly as the server would,
and is the single answer used both to decide which chunks are spelled bare
(so what `table_names()` advertises and the validator accepts) and to
schema-qualify each bare table in the SQL `execute()` runs
(`safety.qualify_bare_table_references`). Scope widened with it: every
schema on the path is read (`_scope_schema_names`, which replaced
`_user_schema_names`), and `AIPA_EXTRA_SCHEMAS` now means schemas outside
the path. `shadowed_function_names()` is kept as defence in depth. See
`docs/3_decisions.md`'s 2026-09-27 entries.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import errors as psycopg_errors

from ..config import (
    DEFAULT_VALUE_HINT_LIMIT,
    DEFAULT_VALUE_HINT_MAX_CARDINALITY,
    DEFAULT_WORK_LIMIT_MS,
)
from ..safety import qualify_bare_table_references
from ..types import QueryResult, SchemaChunk
from .base import (
    EngineForbiddenError,
    EngineUnreachableError,
    extra_schemas_from_env,
    is_internal_schema_name,
    table_column_spellings,
    table_name_spellings,
)

_CONNECT_TIMEOUT_SECONDS = 5

# See `PostgresEngine.default_schema`: its fallback when the role's effective
# search path is empty. It no longer decides which tables are advertised bare
# - `resolve_bare_relation_names` does - nor which schemas are read - see
# `_scope_schema_names`.
_PUBLIC_SCHEMA = "public"


def _connect_read_only(dsn: str) -> psycopg.Connection[tuple[Any, ...]]:
    """Open a PostgreSQL connection with the read-only transaction flag set.

    `read_only = True` must be assigned before the first statement of a
    transaction runs - psycopg sends it as part of that transaction's opening
    `SET TRANSACTION` rather than as a live per-statement toggle, so setting
    it right after `connect()` and before any `execute()` is what makes every
    statement on this connection read-only, not just ones after some later
    point.
    """
    conn: psycopg.Connection[tuple[Any, ...]] = psycopg.connect(
        dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS
    )
    conn.read_only = True
    return conn


# Decision (2026-09-26): PostgreSQL fails closed on an over-privileged role.
# DuckDB's defence against reading host files is `enable_external_access=
# False`, a connection setting the engine itself sets - see `duckdb.py`'s
# module docstring. PostgreSQL has no equivalent flag: `pg_read_file`,
# `COPY ... TO/FROM PROGRAM` and every other host-filesystem or
# program-execution built-in are refused only because `aipa_ro` was never
# granted membership in `pg_read_server_files`, `pg_write_server_files` or
# `pg_execute_server_program` (`docker/postgres-init.sql`, and
# `test_engine_postgres.py`'s Task 3 probes, which proved every one of those
# built-ins refused for exactly that reason). That defence lives entirely in
# how a deployment provisions its role - nothing here enforces it - so a
# misconfigured deployment (or a plain `postgres` superuser DSN pasted into
# the "Connection string" sidebar field) would otherwise connect successfully
# and get no filesystem protection at all, with no error naming why. Checked
# once, at `check_reachable()`, rather than on every `execute()`: the role
# does not change between queries on the same DSN, and repeating a role-
# introspection query per query would cost real latency for a property that
# is fixed for the life of a connection string.
_OVERPRIVILEGED_ROLE_MEMBERSHIPS: tuple[str, ...] = (
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_execute_server_program",
)


def _refuse_if_role_is_overprivileged(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    """Refuse to proceed if the connecting role could reach the host filesystem.

    Args:
        conn: An open connection - any role. Reads `pg_roles` only, which
            every role may read about itself (`current_user`'s own row).

    Raises:
        EngineForbiddenError: Naming which condition(s) held (superuser
            status and/or the specific role membership) and what the role
            should look like instead. Never includes the DSN
            (`docs/0_coding_standards.md` §4's credential rule) - only
            `current_user`'s name, which carries no password - so this is
            safe to surface directly to a user, matching `check_reachable`'s
            own existing redaction discipline.
    """
    row = conn.execute(
        "SELECT rolname, rolsuper, "
        "pg_has_role(rolname, 'pg_read_server_files', 'MEMBER'), "
        "pg_has_role(rolname, 'pg_write_server_files', 'MEMBER'), "
        "pg_has_role(rolname, 'pg_execute_server_program', 'MEMBER') "
        "FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if row is None:
        return
    rolname, is_superuser, *memberships = row

    reasons: list[str] = []
    if is_superuser:
        reasons.append("is a superuser")
    for name, held in zip(_OVERPRIVILEGED_ROLE_MEMBERSHIPS, memberships, strict=True):
        if held:
            reasons.append(f"holds {name}")
    if not reasons:
        return

    raise EngineForbiddenError(
        f"PostgreSQL role {rolname!r} {', '.join(reasons)} - refusing to "
        "connect, because that role could read or write files on the "
        "PostgreSQL server regardless of what this agent's own SQL "
        "validator refuses. The connecting role must be a non-superuser "
        "holding none of pg_read_server_files, pg_write_server_files or "
        "pg_execute_server_program - see docker/postgres-init.sql's "
        "aipa_ro role for one provisioned correctly."
    )


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_qualified(schema_name: str, table_name: str) -> str:
    return _quote_identifier(schema_name) + "." + _quote_identifier(table_name)


def _pin_search_path(conn: psycopg.Connection[tuple[Any, ...]]) -> list[str]:
    """Read the role's effective search path, then pin this transaction's to `pg_catalog`.

    Decision (2026-09-27, owner-approved). PostgreSQL resolves functions,
    operators and types through `search_path`, and an exact argument-type
    match in *any* schema on it beats a built-in needing a cast, whatever the
    order - so anyone able to create objects in a schema on the path can
    shadow a built-in. `SET LOCAL search_path = pg_catalog` removes every
    other schema from that lookup for the rest of the transaction (verified
    live: a `public.||(text, integer)` operator and a `public.lower(integer)`
    overload both become unreachable to an unqualified spelling). Every
    connection this engine opens calls this first, not only `execute()`: the
    catalogue reads below use unqualified `unnest`, `array_agg`,
    `array_position`, `has_schema_privilege` and `=`, and would otherwise be
    exposed to the same shadowing on the role's own path.

    The path is read *before* the pin, through an explicitly
    `pg_catalog`-qualified call with an exact-type argument (`false` is a
    `boolean`), so no user overload can answer it. `current_schemas(false)`
    is PostgreSQL's own model of the path (verified live 2026-09-27 on
    PostgreSQL 16): ordered, `"$user"` expanded, schemas that do not exist or
    that the role holds no `USAGE` on omitted, and the *implicitly* searched
    `pg_catalog` omitted (an explicitly listed one is kept, in its position).
    Omitting the implicit `pg_catalog` cannot change a resolution this agent
    accepts: every relation in `pg_catalog` is named `pg_*` (verified live -
    none is not), and `safety._references_internals` refuses those names.

    `SET LOCAL` needs an open transaction; `_connect_read_only`'s connections
    are not autocommit, so the `SELECT` above has already opened one, and the
    setting ends with it - it cannot leak into a pooled or reused connection.

    Returns:
        The role's effective search path, in resolution order.
    """
    row = conn.execute("SELECT pg_catalog.current_schemas(false)").fetchone()
    path = [str(name) for name in row[0]] if row and row[0] else []
    conn.execute("SET LOCAL search_path = pg_catalog")
    return path


def resolve_bare_relation_names(
    conn: psycopg.Connection[tuple[Any, ...]],
    names: Iterable[str],
    *,
    search_path: list[str],
) -> dict[str, str]:
    """The schema each bare relation name resolves to, exactly as the server would.

    **The single source of truth for "what does this bare name mean"**
    (2026-09-27). It decides which chunks `schema_chunks()` spells bare (and
    therefore what `table_names()` advertises and `safety.is_safe_query`
    accepts bare), and it is the `resolve` callback `execute()` hands to
    `safety.qualify_bare_table_references`, which writes the answer into the
    SQL before it runs under the pinned path. One function, so the validator
    and the executor cannot disagree - two implementations of this question
    are how Phase 3b kept producing advertised-then-rejected inversions, and
    Codex's Finding 2 was one: `current_schema()` modelled only the first
    path entry, while the server walks all of them.

    Walks `search_path` in order and returns, per name, the first schema
    holding *any* `pg_class` entry of that name - table, view, sequence,
    index or composite type alike, readable or not - because that is what
    the server's own `RangeVarGetRelid` does: an unreadable relation earlier
    on the path still shadows a readable one later (the query then fails on
    privileges), so it must shadow here too. Internal schemas on the path
    keep their position for the same reason; `_scope_schema_names` is what
    keeps them from being advertised.

    Must run on a pinned connection (`_pin_search_path`): the query's own
    `unnest` and `=` then resolve in `pg_catalog`, so a user overload cannot
    answer the question that decides what every bare name means.

    Args:
        conn: A connection already pinned by `_pin_search_path`.
        names: Server-folded relation names (quoted identifiers exact,
            unquoted ones lowercased, as PostgreSQL folds them).
        search_path: `_pin_search_path`'s return value for this connection.

    Returns:
        `{name: schema}` for each name that resolves; a name found in no
        path schema is absent, exactly as the server would fail to find it.
    """
    wanted = sorted(set(names))
    if not wanted or not search_path:
        return {}
    rows = conn.execute(
        "SELECT DISTINCT ON (c.relname) c.relname, n.nspname "
        "FROM unnest(%s::text[]) WITH ORDINALITY AS p(nspname, ord) "
        "JOIN pg_namespace n ON n.nspname = p.nspname "
        "JOIN pg_class c ON c.relnamespace = n.oid "
        "WHERE c.relname = ANY(%s::text[]) "
        "ORDER BY c.relname, p.ord",
        (search_path, wanted),
    ).fetchall()
    return {str(relname): str(nspname) for relname, nspname in rows}


def _scope_schema_names(
    conn: psycopg.Connection[tuple[Any, ...]],
    *,
    search_path: list[str],
    extra_schemas: frozenset[str],
    internal_prefixes: tuple[str, ...],
    internal_names: frozenset[str],
) -> list[str]:
    """The schemas whose tables are read and advertised: the search path, plus opted-in extras.

    Decision (2026-09-27), refining the 2026-09-26 opt-in decision. That
    decision read only `default_schema` (`current_schema()`, the *first*
    path entry) plus `AIPA_EXTRA_SCHEMAS`. But the search path *is*
    PostgreSQL's meaning of "default": every schema on it can supply a bare
    table name, and Codex's Finding 2 showed the server answering a bare
    `customers` from `public` while this engine, reading only `aipa_ro`,
    refused it. So every schema on the role's effective path is now in
    scope, and `AIPA_EXTRA_SCHEMAS` keeps its meaning for schemas **outside**
    the path. What stays true of the 2026-09-26 decision: a schema the role
    can merely *read* is still never advertised unless it is on the path or
    opted in - a deployment's staging or PII schema granted for some other
    tool is still invisible. See `docs/3_decisions.md`.

    Path schemas need no privilege check: `current_schemas(false)` already
    omits any schema the role holds no `USAGE` on. Opted-in extras still
    require `has_schema_privilege` - naming a schema does not grant it.
    Internal schemas (`pg_catalog`, `information_schema`, anything `pg_*`,
    case-insensitively - `engines/base.py::is_internal_schema_name`, review
    finding 1 of 2026-09-26) are never in scope, whether they reached here
    through the path or the opt-in.

    Args:
        conn: A connection already pinned by `_pin_search_path`.
        search_path: That connection's effective path, in order.
        extra_schemas: The engine's `AIPA_EXTRA_SCHEMAS` opt-in set.
        internal_prefixes: `Engine.internal_prefixes`.
        internal_names: `Engine.internal_names`.

    Returns:
        The in-scope path schemas in path order, then the privileged extras
        in name order.
    """

    def _internal(name: str) -> bool:
        return is_internal_schema_name(
            name, internal_prefixes=internal_prefixes, internal_names=internal_names
        )

    in_path = [name for name in search_path if not _internal(name)]
    candidates = sorted(
        name for name in extra_schemas if name not in search_path and not _internal(name)
    )
    if not candidates:
        return in_path
    rows = conn.execute(
        "SELECT nspname FROM pg_namespace "
        "WHERE nspname = ANY(%s) AND has_schema_privilege(nspname, 'USAGE') "
        "ORDER BY nspname",
        (candidates,),
    ).fetchall()
    return [*in_path, *(str(name) for (name,) in rows)]


def _fetch_shadowed_function_names(
    conn: psycopg.Connection[tuple[Any, ...]], *, allowed_functions: frozenset[str]
) -> frozenset[str]:
    """Which of `allowed_functions` has an executable overload outside `pg_catalog`.

    Finding 1 fix (Codex review of the Phase 3b closeout, 2026-09-26) - see
    `Engine.shadowed_function_names` for the vulnerability this closes.
    `pg_proc`/`pg_namespace` are plain catalog tables, world-readable the
    same way `allowed_functions`'s own comment already relies on for
    `pg_constraint` (`_fetch_primary_keys`) - no privilege beyond an
    ordinary connection is needed to read them.

    `has_function_privilege(oid, 'EXECUTE')` is what makes this a real
    identity check rather than a name check one level down: PostgreSQL
    grants `EXECUTE` to `PUBLIC` by default on every function a role
    creates, which is exactly how the review's reproduction worked without
    any explicit `GRANT` at all - a `proname` match with no privilege check
    would both over- and under-report (an overload the role could not
    actually invoke is not a threat; one it can is, regardless of who owns
    it).

    Deliberately not scoped to `default_schema`/`extra_schemas`: an overload
    is reachable by an explicit schema-qualified call (`other_schema.
    lower(...)`) regardless of whether `other_schema` is one this engine
    advertises tables from, so restricting the search to opted-in schemas
    would miss exactly the shape the review used (a schema `aipa_ro` was
    never granted `USAGE` on at all).

    Args:
        conn: An open connection - any role.
        allowed_functions: The engine's own `allowed_functions` - only these
            names are worth asking about, since only these can pass the
            other, name-based half of `_references_disallowed_function`
            first.

    Returns:
        The lowercased subset of `allowed_functions` with at least one
        `pg_proc` row outside the `pg_catalog` namespace that
        `has_function_privilege` says the connecting role may execute.
        Empty when `allowed_functions` is empty (never true for
        `PostgresEngine` itself, but keeps this total for any future
        caller) or when nothing is shadowed.
    """
    if not allowed_functions:
        return frozenset()
    rows = conn.execute(
        "SELECT DISTINCT p.proname "
        "FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname <> 'pg_catalog' "
        "AND p.proname = ANY(%s) "
        "AND has_function_privilege(p.oid, 'EXECUTE')",
        (sorted(allowed_functions),),
    ).fetchall()
    return frozenset(name for (name,) in rows)


def _fetch_columns(
    conn: psycopg.Connection[tuple[Any, ...]], schema_names: list[str]
) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """Every table's `(column_name, data_type, is_nullable)`, keyed by `(schema, table)`.

    Keyed by the pair, never by bare `table_name` - the 2026-09-25 DuckDB fix
    (see `duckdb.py`'s module docstring) is what a bare-name key does the
    moment a query result spans two schemas holding a same-named table:
    their columns land in the same list, silently merged. Every caller in
    this module now passes every search-path schema plus every opted-in
    `AIPA_EXTRA_SCHEMAS` entry (`_scope_schema_names`), so `schema_names` is
    routinely more than one element - the keying holds regardless of how
    many schemas are asked for, proven directly, with more than one, by
    `tests/test_engine_postgres.py::
    test_fetch_columns_keys_by_schema_and_table_not_bare_name`.
    """
    if not schema_names:
        return {}
    rows = conn.execute(
        "SELECT table_schema, table_name, column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = ANY(%s) "
        "ORDER BY table_schema, table_name, ordinal_position",
        (schema_names,),
    ).fetchall()
    columns_by_table: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for schema_name, table_name, column_name, data_type, is_nullable in rows:
        columns_by_table.setdefault((schema_name, table_name), []).append(
            (column_name, data_type, is_nullable)
        )
    return columns_by_table


def _fetch_primary_keys(
    conn: psycopg.Connection[tuple[Any, ...]], schema_names: list[str]
) -> dict[tuple[str, str], list[str]]:
    """Every table's primary-key column names, in key order, keyed by `(schema, table)`.

    `information_schema.table_constraints`/`key_column_usage` only show what
    `aipa_ro` owns, which is nothing - probed 2026-09-26 for `_fetch_
    foreign_keys` below, same result here. `pg_constraint` plus `pg_class`/
    `pg_namespace`/`pg_attribute` are plain catalog tables, world-readable
    the way `pg_proc` already is (see `allowed_functions`'s comment), so
    this reads those directly instead.
    """
    if not schema_names:
        return {}
    rows = conn.execute(
        "SELECT nsp.nspname, cls.relname, att.attname, "
        "array_position(con.conkey, att.attnum) AS ordinal "
        "FROM pg_constraint con "
        "JOIN pg_class cls ON cls.oid = con.conrelid "
        "JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace "
        "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey) "
        "WHERE con.contype = 'p' AND nsp.nspname = ANY(%s) "
        "ORDER BY nsp.nspname, cls.relname, ordinal",
        (schema_names,),
    ).fetchall()
    pk_by_table: dict[tuple[str, str], list[str]] = {}
    for schema_name, table_name, column_name, _ordinal in rows:
        pk_by_table.setdefault((schema_name, table_name), []).append(column_name)
    return pk_by_table


@dataclass(frozen=True)
class _ForeignKey:
    """One `FOREIGN KEY` constraint, with both sides' schema-qualified identity."""

    local_columns: tuple[str, ...]
    ref_schema: str
    ref_table: str
    ref_columns: tuple[str, ...]


def _fetch_foreign_keys(
    conn: psycopg.Connection[tuple[Any, ...]], schema_names: list[str]
) -> dict[tuple[str, str], list[_ForeignKey]]:
    """Every table's foreign-key constraints, keyed by `(schema, table)`.

    Deliberately not `con.conrelid::regclass::text` for the referencing or
    referenced table name (Task 2's original approach): `::regclass::text`
    renders bare or schema-qualified depending on the connection's
    `search_path`, which is exactly the ambiguity `_quote_qualified`'s
    module-level comment warns a value-hint query about - this joins
    `pg_class`/`pg_namespace` directly instead, so both sides' schema come
    from the catalogue, never from search-path-dependent formatting.
    `unnest(con.conkey, con.confkey) WITH ORDINALITY` zips the local and
    referenced column-number arrays element-wise (verified live 2026-09-26)
    so a multi-column foreign key's columns pair up correctly rather than
    being cross-joined. `con.oid` (via `con.conname`, unique per table) is
    the true `GROUP BY` key - two separate foreign keys from the same table
    to the same referenced table would otherwise have their column arrays
    merged by `array_agg` if grouped on the table pair alone.
    """
    if not schema_names:
        return {}
    rows = conn.execute(
        "SELECT nsp.nspname, cls.relname, con.conname, "
        "array_agg(att.attname ORDER BY k.ord) AS local_columns, "
        "fnsp.nspname, fcls.relname, "
        "array_agg(fatt.attname ORDER BY k.ord) AS ref_columns "
        "FROM pg_constraint con "
        "JOIN pg_class cls ON cls.oid = con.conrelid "
        "JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace "
        "JOIN pg_class fcls ON fcls.oid = con.confrelid "
        "JOIN pg_namespace fnsp ON fnsp.oid = fcls.relnamespace "
        "JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY "
        "AS k(local_attnum, ref_attnum, ord) ON true "
        "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.local_attnum "
        "JOIN pg_attribute fatt ON fatt.attrelid = con.confrelid AND fatt.attnum = k.ref_attnum "
        "WHERE con.contype = 'f' AND nsp.nspname = ANY(%s) "
        "GROUP BY con.oid, nsp.nspname, cls.relname, con.conname, fnsp.nspname, fcls.relname "
        "ORDER BY nsp.nspname, cls.relname, con.conname",
        (schema_names,),
    ).fetchall()
    fk_by_table: dict[tuple[str, str], list[_ForeignKey]] = {}
    for schema_name, table_name, _conname, local_cols, ref_schema, ref_table, ref_cols in rows:
        fk_by_table.setdefault((schema_name, table_name), []).append(
            _ForeignKey(
                local_columns=tuple(local_cols),
                ref_schema=ref_schema,
                ref_table=ref_table,
                ref_columns=tuple(ref_cols),
            )
        )
    return fk_by_table


def _is_bare(schema_name: str, table_name: str, bare_home: Mapping[str, str]) -> bool:
    """Whether `schema_name.table_name` is what its bare name resolves to.

    `bare_home` is `resolve_bare_relation_names`'s answer, so this is the
    server's own rule: a table is spelled bare exactly when the bare name
    reaches it, which also means a table shadowed by an earlier search-path
    schema - or one outside the path entirely - is spelled qualified.
    """
    return bare_home.get(table_name) == schema_name


def _plain_table(schema_name: str, table_name: str, bare_home: Mapping[str, str]) -> str:
    """The bare name when it resolves to this table, `schema.table` otherwise, unquoted.

    The same rule `SchemaChunk.qualified_name` applies, for the two places a
    chunk cannot apply it for itself: the names it lists in `foreign_tables`
    (which must match how the referenced table's own chunk spells itself, or
    `rag.py`'s neighbour graph cannot join the two) and the leading term of
    its `search_text`. `_display_table` below is the quoted form of the same
    rule, for DDL text.
    """
    if _is_bare(schema_name, table_name, bare_home):
        return table_name
    return f"{schema_name}.{table_name}"


def _display_table(schema_name: str, table_name: str, bare_home: Mapping[str, str]) -> str:
    """Bare-quoted when the bare name resolves to this table, schema-qualified otherwise.

    Keeps `raw_schema()`/`schema_chunks()`'s output byte-for-byte the same as
    before for the common single-schema (`public`-only) case, while still
    disambiguating a table the bare name would not reach.
    """
    if _is_bare(schema_name, table_name, bare_home):
        return _quote_identifier(table_name)
    return _quote_qualified(schema_name, table_name)


def _table_ddl(
    schema_name: str,
    table_name: str,
    typed_columns: list[tuple[str, str, str]],
    pk_columns: list[str],
    foreign_keys: list[_ForeignKey],
    *,
    bare_home: Mapping[str, str],
) -> str:
    """Synthesise a `CREATE TABLE` statement from catalogue columns, PK and FKs.

    Not `pg_dump` fidelity - no defaults, indexes or check constraints - but
    enough to read like the DDL `SQLiteEngine.raw_schema()` returns: typed
    columns, a `PRIMARY KEY` clause, and one `FOREIGN KEY ... REFERENCES`
    clause per constraint, columns included on both sides.
    """
    lines = []
    for column_name, data_type, is_nullable in typed_columns:
        not_null = "" if is_nullable == "YES" else " NOT NULL"
        lines.append(f"{_quote_identifier(column_name)} {data_type}{not_null}")
    if pk_columns:
        pk_list = ", ".join(_quote_identifier(c) for c in pk_columns)
        lines.append(f"PRIMARY KEY ({pk_list})")
    for fk in foreign_keys:
        local_list = ", ".join(_quote_identifier(c) for c in fk.local_columns)
        ref_list = ", ".join(_quote_identifier(c) for c in fk.ref_columns)
        ref_table = _display_table(fk.ref_schema, fk.ref_table, bare_home)
        lines.append(f"FOREIGN KEY ({local_list}) REFERENCES {ref_table} ({ref_list})")
    body = ",\n  ".join(lines)
    table_ref = _display_table(schema_name, table_name, bare_home)
    return f"CREATE TABLE {table_ref} (\n  {body}\n);"


def _value_hints_for_table(
    conn: psycopg.Connection[tuple[Any, ...]],
    schema_name: str,
    table_name: str,
    columns: list[tuple[str, str]],
    *,
    max_cardinality: int = DEFAULT_VALUE_HINT_MAX_CARDINALITY,
    limit: int = DEFAULT_VALUE_HINT_LIMIT,
) -> dict[str, list[str]]:
    hints: dict[str, list[str]] = {}
    quoted_table = _quote_qualified(schema_name, table_name)
    for column_name, column_type in columns:
        if column_type and not any(
            token in column_type.upper() for token in ("CHAR", "TEXT", "CLOB")
        ):
            continue
        quoted_column = _quote_identifier(column_name)
        cardinality_row = conn.execute(
            f"SELECT COUNT(DISTINCT {quoted_column}) FROM {quoted_table} "
            f"WHERE {quoted_column} IS NOT NULL"
        ).fetchone()
        cardinality = cardinality_row[0] if cardinality_row is not None else None
        if cardinality is None or cardinality < 1 or cardinality > max_cardinality:
            continue
        rows = conn.execute(
            f"SELECT {quoted_column}, COUNT(*) AS n "
            f"FROM {quoted_table} WHERE {quoted_column} IS NOT NULL "
            f"GROUP BY {quoted_column} ORDER BY n DESC, {quoted_column} LIMIT %s",
            (limit,),
        ).fetchall()
        values = [str(row[0])[:60] for row in rows if str(row[0]).strip()]
        if values:
            hints[column_name] = values
    return hints


@dataclass(frozen=True)
class _Catalogue:
    """One pinned catalogue read: columns, keys, and each bare name's home schema."""

    columns_by_table: dict[tuple[str, str], list[tuple[str, str, str]]]
    pk_by_table: dict[tuple[str, str], list[str]]
    fk_by_table: dict[tuple[str, str], list[_ForeignKey]]
    bare_home: dict[str, str]


class PostgresEngine:
    """Read-only PostgreSQL backend.

    Read-only comes from two independent mechanisms - see the module
    docstring - not one. `dsn` is held whole (unlike the file-path engines):
    libpq needs the full URL including its scheme, so `open_engine` passes it
    through unstripped.

    Attributes:
        dsn: The full `postgresql://` (or `postgres://`) connection URL.
    """

    name: str = "postgres"
    sqlglot_dialect: str = "postgres"
    # `pg_` covers pg_catalog's own tables (pg_class, pg_constraint, ...) and
    # PostgreSQL's `pg_*` administrative views alike; `information_schema` is
    # the standard SQL catalogue view. Both are readable from a read-only
    # `aipa_ro` connection the same way DuckDB's catalogue surfaces are (see
    # `DuckDBEngine.internal_prefixes`), so both are blocked here for the same
    # reason. This is the simple version Step 5 asked for; Task 4 is where
    # PostgreSQL gets the same default-deny `allowed_functions` treatment
    # DuckDB has.
    internal_prefixes: tuple[str, ...] = ("pg_",)
    internal_names: frozenset[str] = frozenset({"information_schema"})
    # Task 4 (2026-09-26) default-deny allowlist - see `DuckDBEngine.
    # allowed_functions` for the general mechanism (`safety.
    # _references_disallowed_function`: every function call anywhere in the
    # query, not just in a table-source position, must resolve to a name
    # here) and `_FUNCTION_NAME_OVERRIDES` in `safety.py` for the round-trip
    # trap this list hit in the same shape DuckDB did.
    #
    # Built from `DuckDBEngine.allowed_functions` (127 names) as a starting
    # point per the task brief, kept only where PostgreSQL genuinely
    # registers a function under that name (verified against a live
    # `pg_proc` sweep, 2026-09-26 - see `docs/superpowers/sdd/
    # task-4-report.md` for the full list of names checked and dropped) or
    # provides its own equivalent spelling. Every entry below was confirmed
    # to exist in `pg_proc` (`pg_catalog`/`public`), *except* the six SQL
    # syntax forms sqlglot represents as `exp.Func` subclasses for parsing
    # convenience but PostgreSQL implements as grammar, not a callable in
    # `pg_proc`: `case`, `if` (every `CASE ... WHEN` branch parses to a
    # child `exp.If` node, so both must be listed together or no `CASE`
    # expression validates - verified live, matching why DuckDB's own list
    # carries both), `cast`, `extract`, `coalesce`, `nullif`, `greatest`,
    # `least`, `trim` - these were instead verified by direct execution (see
    # `ANALYTICS_CORPUS` below).
    #
    # Dropped rather than mapped, because PostgreSQL has no function (built-in
    # or otherwise) a caller could reach under that name or any real
    # equivalent: `median`, `first`, `last`, `approx_count_distinct`,
    # `arg_min`, `arg_max`, `count_if`, `quantile`/`quantile_cont`/
    # `quantile_disc` (PostgreSQL's own `percentile_cont`/`percentile_disc`
    # are ordered-set aggregates verified to exist, but were left off anyway -
    # not needed for a realistic analytics question and not worth adding a
    # second, DuckDB-authored override token, `"quantile_cont"`/
    # `"quantile_disc"`, that no PostgreSQL catalogue entry matches),
    # `string_split` (`string_to_array` is the real equivalent, also left
    # off - no query in the corpus needs it), `regexp_extract`,
    # `regexp_full_match`, `contains`, `ends_with`, `datepart`, `date_diff`,
    # `date_add`, `date_sub` (interval arithmetic covers this: `d + INTERVAL
    # '1 day'` is a plain `exp.Add`/`exp.Sub` node, not a function call, so
    # it is never even subject to this gate), `strftime`/`strptime`/`now`/
    # `to_char`/`to_timestamp` (PostgreSQL's real equivalents exist and were
    # verified - `to_char`, `now` - but every one of them either round-trips
    # through an *existing* DuckDB-authored override token
    # (`exp.TimeToStr`/`exp.StrToTime` already resolve to `"strftime"`/
    # `"strptime"`) or adds a second date/time spelling
    # (`current_timestamp`, listed below, already covers `now()`) with no
    # query in the corpus needing the extra one), `epoch`/`epoch_ms`
    # (`extract(epoch FROM ...)`, using `extract` below, covers this),
    # `last_day` and the standalone `year`/`month`/`day`/`hour`/`minute`/
    # `second`/`dayofweek`/`dayofyear`/`week`/`isodow`/`quarter` wrappers
    # (none exist as PostgreSQL functions; `extract`, listed below, covers
    # every one of them), `try_cast` (no PostgreSQL equivalent - `CAST`
    # raises rather than returning `NULL`), `range`, `json_each`, `json_tree`
    # and every `list_*`/`json_*` DuckDB entry (PostgreSQL's array/JSON
    # accessor surface is a different set of names entirely, and nothing in
    # the sample schema or a plausible business question over it needs
    # them - left off per the task brief's "when in doubt, leave a function
    # off the list"). `unnest` was dropped for the same reason even though
    # it is a real PostgreSQL function (verified in `pg_proc`): nothing in
    # this schema has an array column, and the only way to exercise it
    # without one - a bare `ARRAY[...]` literal - parses to `exp.Array`,
    # which already round-trips through the DuckDB-authored `"list_value"`
    # override token, not a name in this list. Adding `"list_value"` here
    # just to admit a function this schema has no legitimate use for was
    # not worth it.
    #
    # `date_part` is genuinely real (verified in `pg_proc`) but is not its
    # own entry: PostgreSQL's dialect parser normalises `DATE_PART(...)` into
    # the same `exp.Extract` node `EXTRACT(... FROM ...)` produces (verified
    # 2026-09-26), so `"extract"` alone already covers both spellings, the
    # same way `"lpad"` alone covers `lpad`/`rpad` via the existing
    # `exp.Pad` override. `rpad` is the same case: it round-trips through
    # that existing `exp.Pad` -> `"lpad"` override rather than needing a
    # second entry.
    #
    # `tests/test_engine_postgres.py::test_allowed_function_round_trip`
    # proves every entry below round-trips: parsed under the `postgres`
    # dialect, resolved via `safety._resolve_function_name`, and found back
    # in this set.
    allowed_functions: frozenset[str] | None = frozenset(
        {
            # -- Aggregates --
            "count",
            "sum",
            "avg",
            "min",
            "max",
            "mode",
            "stddev",
            "stddev_pop",
            "stddev_samp",
            "variance",
            "var_pop",
            "string_agg",
            "array_agg",
            "bool_and",
            "bool_or",
            "corr",
            "covar_pop",
            "covar_samp",
            # -- Window functions --
            "row_number",
            "rank",
            "dense_rank",
            "percent_rank",
            "cume_dist",
            "ntile",
            "lag",
            "lead",
            "first_value",
            "last_value",
            "nth_value",
            # -- String functions --
            "upper",
            "lower",
            "concat",
            "concat_ws",
            "length",
            "trim",
            "substring",
            "replace",
            "split_part",
            "lpad",
            "position",
            "regexp_replace",
            "regexp_matches",
            "starts_with",
            "reverse",
            "left",
            "right",
            "repeat",
            "initcap",
            # -- Date and time functions --
            "date_trunc",
            "extract",
            "age",
            "current_date",
            "current_timestamp",
            "make_date",
            # -- Numeric functions --
            "round",
            "ceil",
            "floor",
            "abs",
            "power",
            "sqrt",
            "sign",
            "exp",
            "ln",
            "log",
            "cbrt",
            "greatest",
            "least",
            # -- Conditional / type functions --
            "coalesce",
            "nullif",
            "if",
            "case",
            "cast",
            # `COLLATE "C"` (`SELECT name COLLATE "C" FROM customers`) is
            # ordinary SQL grammar, not a call to any `pg_proc` entry, but
            # sqlglot's `exp.Collate` is an `exp.Func` subclass for parsing
            # convenience the same way `case`/`if`/`extract` are - its own
            # `.sql_name()` resolves to `"collate"` (verified 2026-09-26:
            # `find_all(exp.Func)` reaches it, so it was wrongly rejected by
            # default-deny before this entry existed). Safe to allow: the
            # right-hand identifier only ever names a collation PostgreSQL
            # resolves against `pg_collation`, exposing at most whether a
            # given collation name exists, never row data or catalogue
            # contents - nothing like the OID-cast/dot-call bypasses this
            # entry sits beside in the same review round.
            "collate",
            # -- Table functions --
            "generate_series",
        }
    )
    # `work_limit` for PostgreSQL is milliseconds (`SET LOCAL statement_timeout`
    # - see `execute()`), not SQLite's VM-step count. `DEFAULT_MAX_VM_STEPS`
    # (100_000) is a VM-instruction budget and means something completely
    # different in this unit - passing it straight through as a millisecond
    # budget is a ~100-second timeout, not the design's intended 5 seconds
    # (`docs/superpowers/specs/2026-09-14-phase-3-engine-abstraction-design.md`
    # §4.5: `QUERY_ABORTED_AFTER_5000_MS` for both DuckDB and PostgreSQL).
    # `execution.execute_query` reads this attribute when its own caller does
    # not pass `max_vm_steps` explicitly - see that module for the fix.
    default_work_limit: int = DEFAULT_WORK_LIMIT_MS
    schema_header: str = "PostgreSQL schema (DDL)"
    # PostgreSQL's own dialect section - see `SQLiteEngine.prompt_dialect_
    # section` for the shared-body/per-engine split `_assemble_prompt` fills
    # this into, and `DuckDBEngine`'s copy for the sibling this deliberately
    # mirrors in shape.
    #
    # Three choices worth recording:
    # - ILIKE, not `LOWER(col) = LOWER('value')`, for case-insensitive text
    #   matching. `_PROMPT_BODY`'s shared "CRITICAL TEXT SEARCHING RULES"
    #   section still tells every engine to use `LOWER(...)`/`LIKE` - it is a
    #   correct instruction for PostgreSQL too, just not the idiomatic one -
    #   so this section adds the PostgreSQL-native alternative rather than
    #   replacing shared text a sha256 test elsewhere pins. `ILIKE` parses to
    #   `sqlglot.exp.ILike`, which is not an `exp.Func` subclass (verified
    #   2026-09-26), so `safety._references_disallowed_function` never sees
    #   it and it needs no entry on `allowed_functions` above.
    # - EXTRACT/DATE_TRUNC/INTERVAL over strftime: both `"extract"` and
    #   `"date_trunc"` are already on `allowed_functions`; `strftime` is not
    #   a PostgreSQL function at all, so telling the model to use it would
    #   produce SQL PostgreSQL itself rejects, not just SQL the validator
    #   blocks.
    # - Double-quoting guidance: PostgreSQL folds an unquoted identifier to
    #   lowercase and matches a quoted one case-sensitively. The schema text
    #   the model is shown already carries each identifier's real casing, so
    #   this just tells the model to copy it rather than invent quoting.
    prompt_dialect_section: str = """\
POSTGRESQL DIALECT (must follow):
- Generate PostgreSQL-compatible SQL only.
- For dates/timestamps use EXTRACT(field FROM col), DATE_TRUNC('unit', col), and INTERVAL arithmetic (col + INTERVAL '1 day'); do NOT use strftime, date(), or datetime() - those are SQLite functions and do not exist in PostgreSQL.
- For case-insensitive text matching, prefer `column ILIKE '%value%'` over LOWER(column) = LOWER('value'). ILIKE is PostgreSQL's own case-insensitive operator, not a function call, so it works regardless of the allowed-function list, and it is the idiomatic PostgreSQL spelling.
- The schema below shows every identifier double-quoted; copy each name with that exact casing, quoted. PostgreSQL folds an unquoted identifier to lowercase but matches a quoted one case-sensitively, so copying the shown spelling is what makes a mixed-case name resolve.
- To collect several values into one row use string_agg(col, ', ') or array_agg(col); GROUP_CONCAT and LISTAGG do not exist in PostgreSQL.
"""  # noqa: E501
    # See `SQLiteEngine.prompt_dialect_name` for what this substitutes into
    # and why it is a per-engine value.
    prompt_dialect_name: str = "PostgreSQL"
    # PostgreSQL's own internal-catalogue rule, in place of SQLite's
    # `sqlite_master`/DuckDB's `duckdb_tables()`. Names exactly the two
    # surfaces `internal_prefixes`/`internal_names` above block (`pg_*` and
    # `information_schema`), not a generic template.
    prompt_engine_rules_block: str = """\
- Do NOT reference pg_catalog, information_schema, or any other internal PostgreSQL system catalog or view.
- Prefer simple SQL compatible with PostgreSQL.
"""  # noqa: E501

    def __init__(self, dsn: str) -> None:
        """Hold the full PostgreSQL connection URL.

        Args:
            dsn: A `postgresql://` or `postgres://` URL, scheme included.
        """
        self.dsn = dsn
        # See `engines/base.py::extra_schemas_from_env` and `_scope_schema_
        # names` above - read once here so this instance's scope is fixed
        # for its whole lifetime.
        self.extra_schemas: frozenset[str] = extra_schemas_from_env()
        # See the `default_schema` property below - `None` means "not yet
        # asked the server."
        self._search_path: tuple[str, ...] | None = None
        # See `shadowed_function_names` below - `None` means "not yet asked
        # the server," the same sentinel `_search_path` uses and for the
        # same reason (no real answer is ever `None`).
        self._shadowed_function_names: frozenset[str] | None = None

    @property
    def default_schema(self) -> str:
        """The first schema of this role's effective search path.

        History. This was the constant `"public"` until review finding 3
        (2026-09-26) showed a schema named after the role shadowing it under
        the stock `"$user", public` path; it then became `current_schema()`,
        which is exactly the first entry of the path. Codex's Finding 2 then
        showed that one entry is not a model of the path at all: PostgreSQL
        resolves a bare name by walking *every* entry, so a table missing
        from the first falls through to a later one. Since 2026-09-27 this
        property no longer decides anything about bare names - each table's
        own resolution does (`resolve_bare_relation_names`, recorded on its
        chunk as `SchemaChunk.home_schema`) - and it remains only because the
        `Engine` protocol declares it and `table_name_spellings` falls back to
        it for a chunk that records no home schema, which no chunk this
        engine builds does.

        Cached per instance, like `shadowed_function_names`: `schema.py` and
        `pipeline.py` build a fresh engine per call, so "per instance" means
        "per question", not per process.

        Returns:
            The first effective search-path schema, or `_PUBLIC_SCHEMA` when
            the path is empty.
        """
        if self._search_path is None:
            with _connect_read_only(self.dsn) as conn:
                self._pinned(conn)
        assert self._search_path is not None
        return self._search_path[0] if self._search_path else _PUBLIC_SCHEMA

    def _pinned(self, conn: psycopg.Connection[tuple[Any, ...]]) -> list[str]:
        """`_pin_search_path(conn)`, remembering the first answer for `default_schema`.

        Every method below that opens a connection calls this first, so each
        one runs pinned and pays for no second connection just to answer
        `default_schema` - `tests/test_engine_postgres.py::test_schema_
        extraction_uses_the_read_only_connection` pins those methods to
        exactly one `_connect_read_only` call each.
        """
        search_path = _pin_search_path(conn)
        if self._search_path is None:
            self._search_path = tuple(search_path)
        return search_path

    def check_reachable(self) -> None:
        """Raise if the server cannot be reached, or the role is too privileged.

        Two independent failure modes, two exception types:

        * `EngineUnreachableError` if the connection itself fails - wrong
          host, wrong credentials, server down. The message never includes
          `self.dsn` - it carries a password. Only the exception's class
          name is reported.
        * `EngineForbiddenError` (2026-09-26 decision) if the connection
          succeeds but the connecting role is a superuser or holds
          `pg_read_server_files`, `pg_write_server_files` or
          `pg_execute_server_program` - see `_refuse_if_role_is_
          overprivileged` for why this engine checks that at all. Re-raised
          before the broad `except Exception` below can wrap it into an
          `EngineUnreachableError`, which would misdescribe "reachable but
          refused" as "unreachable."

        Pinned (`_pin_search_path`) before the privilege check, so the
        `pg_has_role` calls that decide it resolve to the built-in and not to
        a same-named overload on the role's path.
        """
        try:
            with psycopg.connect(self.dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
                conn.execute("SELECT 1")
                _pin_search_path(conn)
                _refuse_if_role_is_overprivileged(conn)
        except EngineForbiddenError:
            raise
        except Exception as exc:
            raise EngineUnreachableError(
                f"cannot connect to PostgreSQL: {type(exc).__name__}"
            ) from exc

    def execute(self, sql: str, *, max_rows: int, work_limit: int) -> QueryResult:
        """Execute already-validated SQL under a read-only, search-path-pinned transaction.

        Decision (2026-09-27, owner-approved). The statement runs with
        `SET LOCAL search_path = pg_catalog` (`_pin_search_path`), so every
        unqualified function, operator and type resolves to the built-in or
        not at all - a user-defined `public.||(text, integer)` operator or
        `public.lower(integer)` overload is unreachable however exact its
        argument match. Under the pin the server no longer resolves a bare
        *table* name either, so before running it the engine qualifies each
        one itself (`safety.qualify_bare_table_references`), using
        `resolve_bare_relation_names` - the same function that decided which
        tables `table_names()` advertises bare, so the validator and the
        executor cannot disagree about what a bare name means. The rewrite
        only inserts `"schema".` before a table identifier; see that
        function's docstring for why it splices rather than regenerates.

        `QueryResult.sql` is the SQL that actually ran - the qualified text.

        Args:
            sql: A query already cleared by `is_safe_query`.
            max_rows: Maximum rows returned before the result is marked truncated.
            work_limit: Milliseconds a query may run before PostgreSQL cancels it
                via `statement_timeout`. `0` disables the guard.

        Returns:
            A `QueryResult`. Its `error` is `RESULT_TRUNCATED_TO_<n>_ROWS` when
            more rows were available than `max_rows` allowed, or
            `QUERY_ABORTED_AFTER_<n>_MS` when the guard cancelled the query.

        Raises:
            ValueError: If `max_rows` is less than 1.
            safety.TableQualificationError: If the qualified text did not
                re-parse to the validated statement - nothing is executed.
            Exception: For genuine SQL errors, such as a missing table or a
                write refused by the read-only transaction/role. Only the
                statement-timeout cancellation is converted to a returned
                error - `pipeline._repair_sql` depends on every other
                exception surfacing so it can attempt a repair.
        """
        if max_rows < 1:
            raise ValueError("max_rows must be at least 1")

        conn = _connect_read_only(self.dsn)
        try:
            with conn.cursor() as cur:
                search_path = _pin_search_path(conn)
                executed_sql = qualify_bare_table_references(
                    sql,
                    dialect=self.sqlglot_dialect,
                    resolve=lambda names: resolve_bare_relation_names(
                        conn, names, search_path=search_path
                    ),
                )
                if work_limit > 0:
                    # `SET LOCAL` scopes the budget to this one transaction,
                    # so it cannot leak into another statement on a pooled or
                    # reused connection. Not a bind parameter: PostgreSQL's
                    # `SET` does not accept one for its value, only a
                    # literal - `work_limit` is our own trusted int, never
                    # LLM output, so formatting it directly is safe.
                    cur.execute(f"SET LOCAL statement_timeout = {int(work_limit)}")
                try:
                    cur.execute(executed_sql)
                except psycopg_errors.QueryCanceled:
                    return QueryResult(
                        columns=[],
                        rows=[],
                        sql=executed_sql,
                        error=f"QUERY_ABORTED_AFTER_{work_limit}_MS",
                    )
                rows = cur.fetchmany(max_rows + 1)
                columns = [d.name for d in cur.description] if cur.description else []
            capped_rows = rows[:max_rows]
            if len(rows) > max_rows:
                return QueryResult(
                    columns=columns,
                    rows=[tuple(r) for r in capped_rows],
                    sql=executed_sql,
                    error=f"RESULT_TRUNCATED_TO_{max_rows}_ROWS",
                )
            return QueryResult(
                columns=columns, rows=[tuple(r) for r in capped_rows], sql=executed_sql
            )
        finally:
            conn.close()

    def _read_catalogue(self, conn: psycopg.Connection[tuple[Any, ...]]) -> _Catalogue:
        """Everything `raw_schema()`/`schema_chunks()` need, from one pinned connection.

        Scope is `_scope_schema_names` (the search path plus opted-in
        extras); `bare_home` is `resolve_bare_relation_names` over every table
        read and every table a foreign key references, which is what decides
        each one's spelling.
        """
        search_path = self._pinned(conn)
        schema_names = _scope_schema_names(
            conn,
            search_path=search_path,
            extra_schemas=self.extra_schemas,
            internal_prefixes=self.internal_prefixes,
            internal_names=self.internal_names,
        )
        columns_by_table = _fetch_columns(conn, schema_names)
        pk_by_table = _fetch_primary_keys(conn, schema_names)
        fk_by_table = _fetch_foreign_keys(conn, schema_names)
        referenced = {fk.ref_table for fks in fk_by_table.values() for fk in fks}
        bare_home = resolve_bare_relation_names(
            conn,
            {table for _, table in columns_by_table} | referenced,
            search_path=search_path,
        )
        return _Catalogue(columns_by_table, pk_by_table, fk_by_table, bare_home)

    def raw_schema(self) -> str:
        """Extract synthesised `CREATE TABLE` statements for every readable table.

        Every schema on the role's search path plus whatever is opted into
        via `AIPA_EXTRA_SCHEMAS` - see `_scope_schema_names`. A table is
        written bare when its bare name resolves to it and schema-qualified
        otherwise (`_display_table`), which is both how a query must spell it
        and how `table_names()` advertises it, so nothing is shown here that
        the validator would refuse.

        Reads through the same read-only, least-privilege, pinned connection
        `execute()` uses (`_connect_read_only`) rather than a plain
        `psycopg.connect` - only fixed catalogue SQL runs here, never
        LLM-authored text, so the practical risk was always low, but there is
        no reason for a schema-reading method to hold a connection with a
        wider guarantee than the one that runs real queries.

        Returns:
            The `CREATE TABLE` statements, one per table, semicolon-terminated
            and separated by blank lines, ordered by schema then table name.
        """
        with _connect_read_only(self.dsn) as conn:
            catalogue = self._read_catalogue(conn)
        return "\n\n".join(
            _table_ddl(
                schema_name,
                table_name,
                catalogue.columns_by_table[(schema_name, table_name)],
                catalogue.pk_by_table.get((schema_name, table_name), []),
                catalogue.fk_by_table.get((schema_name, table_name), []),
                bare_home=catalogue.bare_home,
            )
            for schema_name, table_name in sorted(catalogue.columns_by_table)
        )

    def schema_chunks(self) -> list[SchemaChunk]:
        """Build table-level schema chunks for retrieval without reading row data.

        Covers every schema on the role's search path plus whatever is opted
        into via `AIPA_EXTRA_SCHEMAS` - see `_scope_schema_names`.
        `_fetch_columns`/`_fetch_primary_keys`/`_fetch_foreign_keys` key
        everything by `(schema, table)` rather than bare `table_name`, which
        is what makes more than one schema safe: two schemas sharing a table
        name stay two entries with their own columns, keys and value hints,
        where a bare-name key would silently merge them (`duckdb.py`'s module
        docstring records what that merge did in practice).

        A chunk is spelled bare (`schema_name` empty, `home_schema` set) only
        when its bare name resolves to it (`resolve_bare_relation_names`), and
        records `schema_name` otherwise, so `chunk.qualified_name` is exactly
        the spelling `table_names()` advertises and `safety.py` accepts - and
        the bare spelling means what the server would make it mean.

        Reads through `_connect_read_only`, the same connection `execute()`
        uses - see `raw_schema()`'s docstring for why.
        """
        with _connect_read_only(self.dsn) as conn:
            catalogue = self._read_catalogue(conn)
            bare_home = catalogue.bare_home

            chunks: list[SchemaChunk] = []
            for schema_name, table_name in sorted(catalogue.columns_by_table):
                typed_columns = catalogue.columns_by_table[(schema_name, table_name)]
                columns = [c for c, _, _ in typed_columns]
                pk_columns = catalogue.pk_by_table.get((schema_name, table_name), [])
                foreign_keys = catalogue.fk_by_table.get((schema_name, table_name), [])
                # Spelled the way the referenced table's own chunk spells
                # itself (bare when the bare name reaches it, qualified
                # otherwise), so a foreign-key edge and a chunk identity are
                # the same kind of key - PostgreSQL, unlike DuckDB, does allow
                # a foreign key to cross schemas, so `fk.ref_schema` is read
                # rather than assumed.
                foreign_tables = sorted(
                    {_plain_table(fk.ref_schema, fk.ref_table, bare_home) for fk in foreign_keys}
                )
                ddl = _table_ddl(
                    schema_name,
                    table_name,
                    typed_columns,
                    pk_columns,
                    foreign_keys,
                    bare_home=bare_home,
                )
                untyped_columns = [(c, t) for c, t, _ in typed_columns]
                value_hints = _value_hints_for_table(conn, schema_name, table_name, untyped_columns)
                value_text = " ".join(value for values in value_hints.values() for value in values)
                # The chunk's own spelling leads `search_text`: for a table
                # spelled bare it is the same string as the bare name, so
                # single-schema retrieval scoring is unchanged.
                display_name = _plain_table(schema_name, table_name, bare_home)
                search_text = " ".join([display_name, ddl, *columns, *foreign_tables, value_text])
                bare = _is_bare(schema_name, table_name, bare_home)
                chunks.append(
                    SchemaChunk(
                        table_name=table_name,
                        ddl=ddl,
                        columns=columns,
                        foreign_tables=foreign_tables,
                        search_text=search_text,
                        schema_name="" if bare else schema_name,
                        home_schema=schema_name if bare else "",
                        value_hints=value_hints,
                    )
                )
        return chunks

    def schema_fingerprint(self) -> tuple[object, ...]:
        """Hash table names, column names/types and foreign keys into a cache key.

        Covers the same schemas `raw_schema()`/`schema_chunks()` do
        (`_scope_schema_names`), so a table appearing in or vanishing from any
        of them invalidates `schema.py`'s cache. Nothing row-derived
        goes into the hash - no row count, no value-hint query - which is
        what `tests/test_engine_postgres.py::
        test_the_fingerprint_changes_on_ddl_but_not_on_insert` proves
        directly: this must be stable across a plain `INSERT`, or every
        write to the database would invalidate `schema.py`'s cache.
        `is_nullable` and `ordinal_position` are included beyond the design
        doc's literal "table names, column names and types, and foreign
        keys" - a nullability change or a column reorder is still
        schema-derived, not row-derived, and either should still invalidate
        the cache. Foreign keys are included too, so adding or dropping one
        with no column-level change still invalidates.

        The bare-name resolution (`resolve_bare_relation_names` over every
        table read) is hashed as well (2026-09-27): a relation created
        earlier on the search path - even one the role cannot read, which
        `information_schema.columns` would never show - changes what a bare
        name means, and with it which chunks are spelled bare. The returned
        tuple leads with the effective search path itself for the same
        reason.

        The returned tuple's second element (2026-09-26) is `self.extra_
        schemas` itself, sorted - not merely implied by `schema_names`
        changing. `schema_names` is the *effective* (opted-in AND
        privileged) set; opting a schema in that the role cannot use leaves
        `schema_names` unchanged, so hashing only `schema_names` would let a
        config change that has no visible effect skip invalidation, which is
        correct, but a config change from one *usable* opt-in set to another
        must always invalidate even in the edge case where both sets happen
        to resolve to the same usable schemas today and diverge only once a
        grant changes later - included directly rather than relying on that
        coincidence.

        Reads through `_connect_read_only`, the same connection `execute()`
        uses - see `raw_schema()`'s docstring for why.
        """
        with _connect_read_only(self.dsn) as conn:
            search_path = self._pinned(conn)
            schema_names = _scope_schema_names(
                conn,
                search_path=search_path,
                extra_schemas=self.extra_schemas,
                internal_prefixes=self.internal_prefixes,
                internal_names=self.internal_names,
            )
            column_rows = conn.execute(
                "SELECT table_schema, table_name, column_name, data_type, "
                "is_nullable, ordinal_position "
                "FROM information_schema.columns "
                "WHERE table_schema = ANY(%s) "
                "ORDER BY table_schema, table_name, ordinal_position",
                (schema_names,),
            ).fetchall()
            fk_rows = conn.execute(
                "SELECT nsp.nspname, cls.relname, con.conname, "
                "array_agg(att.attname ORDER BY k.ord) AS local_columns, "
                "fnsp.nspname, fcls.relname, "
                "array_agg(fatt.attname ORDER BY k.ord) AS ref_columns "
                "FROM pg_constraint con "
                "JOIN pg_class cls ON cls.oid = con.conrelid "
                "JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace "
                "JOIN pg_class fcls ON fcls.oid = con.confrelid "
                "JOIN pg_namespace fnsp ON fnsp.oid = fcls.relnamespace "
                "JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY "
                "AS k(local_attnum, ref_attnum, ord) ON true "
                "JOIN pg_attribute att "
                "ON att.attrelid = con.conrelid AND att.attnum = k.local_attnum "
                "JOIN pg_attribute fatt "
                "ON fatt.attrelid = con.confrelid AND fatt.attnum = k.ref_attnum "
                "WHERE con.contype = 'f' AND nsp.nspname = ANY(%s) "
                "GROUP BY con.oid, nsp.nspname, cls.relname, con.conname, "
                "fnsp.nspname, fcls.relname "
                "ORDER BY nsp.nspname, cls.relname, con.conname",
                (schema_names,),
            ).fetchall()
            bare_home = resolve_bare_relation_names(
                conn,
                {str(row[1]) for row in column_rows} | {str(row[5]) for row in fk_rows},
                search_path=search_path,
            )
        digest_input = repr((schema_names, column_rows, fk_rows, sorted(bare_home.items())))
        digest = hashlib.sha256(digest_input.encode()).hexdigest()
        return (tuple(search_path), tuple(sorted(self.extra_schemas)), digest)

    def table_names(self) -> frozenset[str]:
        """Every valid spelling of every user table, lowercased, via the cache.

        A table in `public` is accepted bare and as `public.<table>`; a table
        in any other schema is accepted only as `<schema>.<table>`, never
        bare - see `Engine.table_names` for the rule and
        `base.table_name_spellings` for the one implementation of it.

        Routed through `schema.get_schema_chunks` so this shares `schema.py`'s
        single fingerprint-keyed cache rather than a second, driftable one
        here - `self.dsn` already carries its own scheme, unlike the
        file-path engines, so it needs no reattaching before the call.

        Deferred import for the same circular-import reason as
        `SQLiteEngine.table_names` - see that method's docstring.
        """
        from ..schema import get_schema_chunks

        return table_name_spellings(get_schema_chunks(self.dsn), default_schema=self.default_schema)

    def table_columns(self) -> Mapping[str, frozenset[str]]:
        """Each table spelling mapped to that table's own columns, lowercased.

        This is what `safety.is_safe_query`'s default-deny column check
        (`_references_unresolvable_qualified_column`) calls to tell a real
        qualified column reference from PostgreSQL's `alias.name` ->
        `name(alias)` function-call sugar. PostgreSQL is the only engine
        that actually reaches it.

        Per table, not unioned across tables: this engine reads every
        search-path schema plus every opted-in extra (`_scope_schema_names`),
        so a union
        is still a union over more than one table's columns, which is
        precisely what re-armed that sugar as a bypass on 2026-09-26 (a
        column named `lo_get` in any readable schema was enough). See
        `Engine.table_columns` and `base.table_column_spellings`.

        Routed through `schema.get_schema_chunks` so it shares the same
        fingerprint-keyed cache `table_names` reads - the two resolve to the
        same chunk list, so a query that triggers both pays for one
        catalogue read, not two.

        Deferred import for the same circular-import reason as
        `SQLiteEngine.table_names` - see that method's docstring.
        """
        from ..schema import get_schema_chunks

        return table_column_spellings(
            get_schema_chunks(self.dsn), default_schema=self.default_schema
        )

    def shadowed_function_names(self) -> frozenset[str]:
        """Which of `allowed_functions` has an executable overload outside `pg_catalog`.

        See `Engine.shadowed_function_names` for what this answers and why,
        and `_fetch_shadowed_function_names` for the catalogue query.

        **Caching decision (2026-09-26).** Computed at most once per
        `PostgresEngine` instance, cached in `self._shadowed_function_names`
        - the same pattern `default_schema` already uses, for the same
        reason: `schema.py`'s `get_schema_chunks` and every top-level
        `pipeline.ask_database`/`ask_database_with_sql` call build a fresh
        engine instance via `open_engine(dsn)` (see `pipeline.py`), so "once
        per instance" already means "once per question," not a
        process-lifetime cache that could go stale across questions. Within
        one question, `is_safe_query` runs at least once (the generated SQL)
        and up to a second time (`pipeline._repair_sql`'s retry) against the
        *same* engine instance - both reuse this one query instead of paying
        for it twice.

        **What happens if an overload is created mid-session.** If a DBA (or
        another application's role sharing this database - see this
        engine's module docstring and `docs/3_decisions.md`'s 2026-09-26
        entry for why that is the honest precondition, since `aipa_ro`
        itself cannot `CREATE` in any schema it does not own) creates a
        shadowing overload *after* this instance already cached an answer,
        that overload is invisible to this instance for the rest of its
        lifetime - i.e., for the rest of the current question, including its
        one repair retry. It is visible from the *next* question onward,
        since that opens a new `PostgresEngine` and pays for a fresh query.
        This mirrors `default_schema`'s own documented staleness window
        exactly (a `search_path`-affecting change made mid-connection), and
        the same argument applies: nothing else in this engine's lifetime
        (a short-lived, per-question instance) can change a role's function
        privileges or the catalogue's contents either, so a cache scoped to
        the instance is not a weaker guarantee than re-querying on every
        call, only a cheaper one.

        Reads through `_connect_read_only`, the same connection `execute()`
        uses - see `raw_schema()`'s docstring for why.
        """
        if self._shadowed_function_names is None:
            with _connect_read_only(self.dsn) as conn:
                self._pinned(conn)
                self._shadowed_function_names = _fetch_shadowed_function_names(
                    conn, allowed_functions=self.allowed_functions or frozenset()
                )
        return self._shadowed_function_names


__all__ = ["PostgresEngine"]
