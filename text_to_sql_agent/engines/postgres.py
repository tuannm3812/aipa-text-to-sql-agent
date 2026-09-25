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
are placeholders - Task 7 writes PostgreSQL's real prompt fragments.
`allowed_functions` (Task 4, 2026-09-26) is PostgreSQL's own default-deny
allowlist, matching `DuckDBEngine`'s Task 6b work - see that attribute's own
comment for how it was built and verified.

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
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import errors as psycopg_errors

from ..config import (
    DEFAULT_VALUE_HINT_LIMIT,
    DEFAULT_VALUE_HINT_MAX_CARDINALITY,
    DEFAULT_WORK_LIMIT_MS,
)
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

# See `PostgresEngine.default_schema`. This is what a bare, unqualified table
# name resolves against for a role whose `search_path` is PostgreSQL's
# default, and the only schema whose tables `table_names()` also advertises
# under their bare names. It is no longer the only schema the catalogue reads
# - see `_user_schema_names`.
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


def _user_schema_names(
    conn: psycopg.Connection[tuple[Any, ...]],
    *,
    default_schema: str,
    extra_schemas: frozenset[str],
    internal_prefixes: tuple[str, ...],
    internal_names: frozenset[str],
) -> list[str]:
    """This role's `default_schema` plus its opted-in, privileged extras.

    Decision (2026-09-26): schema scope is opt-in, not "every schema this
    role can read". Task 6 read every schema `has_schema_privilege` allowed,
    which means a deployment that granted `aipa_ro` `USAGE` on a staging or
    PII schema for some unrelated tool would have that schema's tables,
    columns and DDL silently sent to the LLM provider. Reading is now
    restricted to `default_schema` and whatever `engines/base.py`'s
    `extra_schemas_from_env()` (`AIPA_EXTRA_SCHEMAS`) names - candidates the
    caller supplies, not a value this function reads itself, so a single
    engine instance's opt-in set is fixed for its lifetime rather than
    re-read per catalogue query.

    `pg_catalog`, `pg_toast`, any `pg_temp_*`/`pg_toast_temp_*` and
    `information_schema` are PostgreSQL's internals, not the user's data.
    Review finding 1 (2026-09-26): the candidate list used to be filtered by
    nothing beyond membership in `[default_schema, *extra_schemas]` - the
    assumption recorded here until this fix was that neither could ever
    *spell* an internal schema, so there was "no separate exclusion left to
    state". That assumption was wrong the moment an operator's
    `AIPA_EXTRA_SCHEMAS` (or the database itself) named a schema like
    `PG_evil` or `Information_Schema`: nothing here rejected it, so it was
    read into `raw_schema()`/`table_names()` and then permanently refused by
    `safety._references_internals`, which lowercases before comparing -
    advertised, then rejected, the layer-disagreement `docs/3_decisions.md`'s
    2026-09-25 entry closed in the opposite direction. `is_internal_schema_
    name` now applies the identical case-insensitive comparison here, before
    a candidate ever reaches the privilege check below, so a schema
    `safety.py` would refuse to let a query reference is never read at all.

    `has_schema_privilege` is still what keeps the rest honest: `aipa_ro` is
    a least-privilege role, and a schema it holds no `USAGE` on is one whose
    tables it could not read even if opted in - naming a schema in
    `AIPA_EXTRA_SCHEMAS` does not by itself grant access to it. A schema that
    is opted in but not granted, or granted but not opted in, is absent
    either way; only the intersection is read.
    """
    candidates = [
        name
        for name in [default_schema, *sorted(extra_schemas)]
        if not is_internal_schema_name(
            name, internal_prefixes=internal_prefixes, internal_names=internal_names
        )
    ]
    if not candidates:
        return []
    rows = conn.execute(
        "SELECT nspname FROM pg_namespace "
        "WHERE nspname = ANY(%s) AND has_schema_privilege(nspname, 'USAGE') "
        "ORDER BY nspname",
        (candidates,),
    ).fetchall()
    return [name for (name,) in rows]


def _fetch_columns(
    conn: psycopg.Connection[tuple[Any, ...]], schema_names: list[str]
) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """Every table's `(column_name, data_type, is_nullable)`, keyed by `(schema, table)`.

    Keyed by the pair, never by bare `table_name` - the 2026-09-25 DuckDB fix
    (see `duckdb.py`'s module docstring) is what a bare-name key does the
    moment a query result spans two schemas holding a same-named table:
    their columns land in the same list, silently merged. Every caller in
    this module now passes `default_schema` plus every opted-in
    `AIPA_EXTRA_SCHEMAS` entry (`_user_schema_names`), so `schema_names` is
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


def _plain_table(schema_name: str, table_name: str, default_schema: str) -> str:
    """`schema.table` outside `default_schema`, the bare name inside it, unquoted.

    The same rule `SchemaChunk.qualified_name` applies, for the two places a
    chunk cannot apply it for itself: the names it lists in `foreign_tables`
    (which must match how the referenced table's own chunk spells itself, or
    `rag.py`'s neighbour graph cannot join the two) and the leading term of
    its `search_text`. `_display_table` below is the quoted form of the same
    rule, for DDL text.
    """
    if schema_name == default_schema:
        return table_name
    return f"{schema_name}.{table_name}"


def _display_table(schema_name: str, table_name: str, default_schema: str) -> str:
    """Bare-quoted for `default_schema`, schema-qualified otherwise.

    Keeps `raw_schema()`/`schema_chunks()`'s output byte-for-byte the same as
    before this task for the common single-schema (`public`-only) case,
    while still disambiguating a table that lives somewhere else.
    """
    if schema_name == default_schema:
        return _quote_identifier(table_name)
    return _quote_qualified(schema_name, table_name)


def _table_ddl(
    schema_name: str,
    table_name: str,
    typed_columns: list[tuple[str, str, str]],
    pk_columns: list[str],
    foreign_keys: list[_ForeignKey],
    *,
    default_schema: str,
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
        ref_table = _display_table(fk.ref_schema, fk.ref_table, default_schema)
        lines.append(f"FOREIGN KEY ({local_list}) REFERENCES {ref_table} ({ref_list})")
    body = ",\n  ".join(lines)
    table_ref = _display_table(schema_name, table_name, default_schema)
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
- Double-quote an identifier only when the schema below shows it quoted, and copy that casing exactly - PostgreSQL folds an unquoted identifier to lowercase but matches a quoted one case-sensitively.
"""  # noqa: E501
    # See `SQLiteEngine.prompt_dialect_name` for what this substitutes into
    # and why it is a per-engine value. Already correct before this task -
    # only `prompt_dialect_section` and `prompt_engine_rules_block` were
    # placeholders.
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
        # See `engines/base.py::extra_schemas_from_env` and `_user_schema_
        # names` above - read once here so this instance's scope is fixed
        # for its whole lifetime.
        self.extra_schemas: frozenset[str] = extra_schemas_from_env()
        # See the `default_schema` property below - `None` means "not yet
        # asked the server."
        self._default_schema: str | None = None

    @property
    def default_schema(self) -> str:
        """This role's actual default schema, per PostgreSQL's own `current_schema()`.

        Decision (2026-09-26, review finding 3). This used to be the class
        constant `"public"` (`_PUBLIC_SCHEMA`). PostgreSQL does not resolve a
        bare table name against a fixed schema - it resolves it against
        `search_path`, whose stock value is `"$user", public`: the schema
        named after the connecting role, if one exists, before `public`.
        `aipa_ro` is a role name, and nothing stops a deployment from also
        having a schema named `aipa_ro` - at which point the constant and the
        server's own answer diverge, and this class was *asserting* a false
        identity: `table_names()` advertised bare `customers` as
        `public.customers`, `is_safe_query` validated it against that
        identity, and PostgreSQL itself executed it against `aipa_ro.
        customers` instead - a different real table, approved under the
        wrong name. Querying `current_schema()` is the fix: it is PostgreSQL's
        own answer to "what does a bare name resolve to on this connection",
        not this engine's guess at one.

        Cached in `self._default_schema` after the first successful query -
        `current_schema()` cannot change mid-connection for this engine's
        purposes (`execute()` opens its own short-lived connection per call
        and never runs `SET search_path`, and no validated query can run one
        either: `search_path` reads as a settings function, refused by
        `safety.py` the same way `current_setting(...)` is). This also keeps
        every read-only call site (`raw_schema`, `schema_chunks`,
        `schema_fingerprint`, `table_names`, `table_columns`) consistent
        within one `PostgresEngine` instance's lifetime - `schema.py`'s
        `get_schema_chunks` builds a fresh instance from `open_engine` on
        every call (see that module), so "per instance" here already means
        "once per top-level call," not a process-lifetime cache that could go
        stale. It is also why this stays consistent with `schema_fingerprint`
        and `schema.py`'s own `lru_cache`: `schema_fingerprint()` includes
        `self.default_schema` directly in the tuple it returns, so a
        `search_path` change on the server (a schema created or dropped that
        changes what `"$user", public` resolves to) still changes the
        fingerprint and invalidates that cache the same way any other schema
        change does.

        A plain instance-level cache, not `functools.cached_property`: this
        class already declares `default_schema` at the `Engine` Protocol's
        expected name and type (`str`), and `cached_property` would still
        need `self._default_schema` distinguished from "unset" - `None` is
        never a valid schema name, so it is an unambiguous sentinel.

        Returns:
            `current_schema()`'s answer, or `_PUBLIC_SCHEMA` if the server
            reports no default schema at all (an empty `search_path`) -
            matching PostgreSQL's own behaviour for a bare table reference
            in that case.
        """
        if self._default_schema is None:
            with _connect_read_only(self.dsn) as conn:
                self._resolve_default_schema(conn)
        assert self._default_schema is not None
        return self._default_schema

    def _resolve_default_schema(self, conn: psycopg.Connection[tuple[Any, ...]]) -> None:
        """Cache `current_schema()`'s answer using an already-open connection.

        `raw_schema`/`schema_chunks`/`schema_fingerprint` each call this
        first thing, inside the `with _connect_read_only(...)` block they
        already open for their own catalogue reads, so their first access to
        `self.default_schema` later in the same method hits the cache
        instead of opening a second connection just to resolve it.
        `tests/test_engine_postgres.py::
        test_schema_extraction_uses_the_read_only_connection` pins each of
        those three methods to exactly one `_connect_read_only` call - this
        is what keeps that true now that resolving `default_schema` needs a
        query of its own; only the `default_schema` property's *other*
        callers (`table_names`/`table_columns`, and a caller reading it
        directly before calling anything else) pay for a dedicated
        connection, and only once per instance.

        A no-op once cached - safe to call from every one of those three
        methods unconditionally.
        """
        if self._default_schema is not None:
            return
        row = conn.execute("SELECT current_schema()").fetchone()
        self._default_schema = row[0] if row and row[0] else _PUBLIC_SCHEMA

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
        """
        try:
            with psycopg.connect(self.dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
                conn.execute("SELECT 1")
                _refuse_if_role_is_overprivileged(conn)
        except EngineForbiddenError:
            raise
        except Exception as exc:
            raise EngineUnreachableError(
                f"cannot connect to PostgreSQL: {type(exc).__name__}"
            ) from exc

    def execute(self, sql: str, *, max_rows: int, work_limit: int) -> QueryResult:
        """Execute already-validated SQL under a read-only transaction.

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
                if work_limit > 0:
                    # `SET LOCAL` scopes the budget to this one transaction,
                    # so it cannot leak into another statement on a pooled or
                    # reused connection. Not a bind parameter: PostgreSQL's
                    # `SET` does not accept one for its value, only a
                    # literal - `work_limit` is our own trusted int, never
                    # LLM output, so formatting it directly is safe.
                    cur.execute(f"SET LOCAL statement_timeout = {int(work_limit)}")
                try:
                    cur.execute(sql)
                except psycopg_errors.QueryCanceled:
                    return QueryResult(
                        columns=[],
                        rows=[],
                        sql=sql,
                        error=f"QUERY_ABORTED_AFTER_{work_limit}_MS",
                    )
                rows = cur.fetchmany(max_rows + 1)
                columns = [d.name for d in cur.description] if cur.description else []
            capped_rows = rows[:max_rows]
            if len(rows) > max_rows:
                return QueryResult(
                    columns=columns,
                    rows=[tuple(r) for r in capped_rows],
                    sql=sql,
                    error=f"RESULT_TRUNCATED_TO_{max_rows}_ROWS",
                )
            return QueryResult(columns=columns, rows=[tuple(r) for r in capped_rows], sql=sql)
        finally:
            conn.close()

    def raw_schema(self) -> str:
        """Extract synthesised `CREATE TABLE` statements for every readable table.

        `default_schema` plus whatever is opted into via `AIPA_EXTRA_SCHEMAS`
        - see `_user_schema_names`. A table outside `public` is written
        schema-qualified (`_display_table`), which is both how a query must
        spell it and how `table_names()` advertises it, so nothing is shown
        here that the validator would refuse.

        Reads through the same read-only, least-privilege connection
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
            self._resolve_default_schema(conn)
            schema_names = _user_schema_names(
                conn,
                default_schema=self.default_schema,
                extra_schemas=self.extra_schemas,
                internal_prefixes=self.internal_prefixes,
                internal_names=self.internal_names,
            )
            columns_by_table = _fetch_columns(conn, schema_names)
            pk_by_table = _fetch_primary_keys(conn, schema_names)
            fk_by_table = _fetch_foreign_keys(conn, schema_names)
        return "\n\n".join(
            _table_ddl(
                schema_name,
                table_name,
                columns_by_table[(schema_name, table_name)],
                pk_by_table.get((schema_name, table_name), []),
                fk_by_table.get((schema_name, table_name), []),
                default_schema=self.default_schema,
            )
            for schema_name, table_name in sorted(columns_by_table)
        )

    def schema_chunks(self) -> list[SchemaChunk]:
        """Build table-level schema chunks for retrieval without reading row data.

        Covers `default_schema` plus whatever is opted into via
        `AIPA_EXTRA_SCHEMAS` - see `_user_schema_names`.
        `_fetch_columns`/`_fetch_primary_keys`/`_fetch_foreign_keys` key
        everything by `(schema, table)` rather than bare `table_name`, which
        is what makes that widening safe: two schemas sharing a table name
        stay two entries with their own columns, keys and value hints, where
        a bare-name key would silently merge them (`duckdb.py`'s module
        docstring records what that merge did in practice).

        A chunk records `schema_name` only when the table is outside
        `default_schema`, so `chunk.qualified_name` is exactly the spelling
        `table_names()` advertises and `safety.py` accepts.

        Reads through `_connect_read_only`, the same connection `execute()`
        uses - see `raw_schema()`'s docstring for why.
        """
        with _connect_read_only(self.dsn) as conn:
            self._resolve_default_schema(conn)
            schema_names = _user_schema_names(
                conn,
                default_schema=self.default_schema,
                extra_schemas=self.extra_schemas,
                internal_prefixes=self.internal_prefixes,
                internal_names=self.internal_names,
            )
            columns_by_table = _fetch_columns(conn, schema_names)
            pk_by_table = _fetch_primary_keys(conn, schema_names)
            fk_by_table = _fetch_foreign_keys(conn, schema_names)

            chunks: list[SchemaChunk] = []
            for schema_name, table_name in sorted(columns_by_table):
                typed_columns = columns_by_table[(schema_name, table_name)]
                columns = [c for c, _, _ in typed_columns]
                pk_columns = pk_by_table.get((schema_name, table_name), [])
                foreign_keys = fk_by_table.get((schema_name, table_name), [])
                # Spelled the way the referenced table's own chunk spells
                # itself (bare inside `default_schema`, qualified outside),
                # so a foreign-key edge and a chunk identity are the same
                # kind of key - PostgreSQL, unlike DuckDB, does allow a
                # foreign key to cross schemas, so `fk.ref_schema` is read
                # rather than assumed.
                foreign_tables = sorted(
                    {
                        _plain_table(fk.ref_schema, fk.ref_table, self.default_schema)
                        for fk in foreign_keys
                    }
                )
                ddl = _table_ddl(
                    schema_name,
                    table_name,
                    typed_columns,
                    pk_columns,
                    foreign_keys,
                    default_schema=self.default_schema,
                )
                untyped_columns = [(c, t) for c, t, _ in typed_columns]
                value_hints = _value_hints_for_table(conn, schema_name, table_name, untyped_columns)
                value_text = " ".join(value for values in value_hints.values() for value in values)
                # The chunk's own qualified spelling leads `search_text`: for
                # a table in `default_schema` it is the same string as the
                # bare name, so single-schema retrieval scoring is unchanged.
                display_name = _plain_table(schema_name, table_name, self.default_schema)
                search_text = " ".join([display_name, ddl, *columns, *foreign_tables, value_text])
                chunks.append(
                    SchemaChunk(
                        table_name=table_name,
                        ddl=ddl,
                        columns=columns,
                        foreign_tables=foreign_tables,
                        search_text=search_text,
                        schema_name="" if schema_name == self.default_schema else schema_name,
                        value_hints=value_hints,
                    )
                )
        return chunks

    def schema_fingerprint(self) -> tuple[object, ...]:
        """Hash table names, column names/types and foreign keys into a cache key.

        Covers the same schemas `raw_schema()`/`schema_chunks()` do
        (`_user_schema_names`), so a table appearing in or vanishing from any
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
        the cache. Foreign keys are new in this hash; Task 2's version
        hashed columns alone, so adding or dropping a foreign key with no
        column-level change went unnoticed by the cache.

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
            self._resolve_default_schema(conn)
            schema_names = _user_schema_names(
                conn,
                default_schema=self.default_schema,
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
        digest_input = repr((schema_names, column_rows, fk_rows))
        digest = hashlib.sha256(digest_input.encode()).hexdigest()
        return (self.default_schema, tuple(sorted(self.extra_schemas)), digest)

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

        Per table, not unioned across tables: this engine reads `default_
        schema` plus every opted-in extra (`_user_schema_names`), so a union
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


__all__ = ["PostgresEngine"]
