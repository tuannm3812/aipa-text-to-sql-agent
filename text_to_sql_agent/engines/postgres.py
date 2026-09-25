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
comment for how it was built and verified. Every schema method below is the
straightforward version Step 5 of the task brief asked for - DDL synthesised
from `information_schema.columns`, chunks carrying columns and foreign keys
read from `pg_constraint`, and a fingerprint hashing a catalogue query - not
the schema-qualified, value-hint-hardened version Task 5 and Task 6 build on
top of it.
"""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
from psycopg import errors as psycopg_errors

from ..config import DEFAULT_VALUE_HINT_LIMIT, DEFAULT_VALUE_HINT_MAX_CARDINALITY
from ..types import QueryResult, SchemaChunk
from .base import EngineUnreachableError

_CONNECT_TIMEOUT_SECONDS = 5

# See `PostgresEngine.default_schema`. Every catalogue query in this module is
# filtered to this one schema - Task 6 is what makes multi-schema identity a
# first-class concept across all three engines at once.
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


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_qualified(schema_name: str, table_name: str) -> str:
    return _quote_identifier(schema_name) + "." + _quote_identifier(table_name)


def _fetch_columns(
    conn: psycopg.Connection[tuple[Any, ...]], schema_name: str
) -> dict[str, list[tuple[str, str, str]]]:
    """Every table's `(column_name, data_type, is_nullable)`, keyed by table name."""
    rows = conn.execute(
        "SELECT table_name, column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
        (schema_name,),
    ).fetchall()
    columns_by_table: dict[str, list[tuple[str, str, str]]] = {}
    for table_name, column_name, data_type, is_nullable in rows:
        columns_by_table.setdefault(table_name, []).append((column_name, data_type, is_nullable))
    return columns_by_table


def _fetch_foreign_tables(
    conn: psycopg.Connection[tuple[Any, ...]], schema_name: str
) -> dict[str, set[str]]:
    """Every table's referenced tables, via `pg_constraint`.

    `information_schema.table_constraints`/`constraint_column_usage` only
    show what `aipa_ro` owns, which is nothing - probed 2026-09-26, both
    returned zero rows for `aipa_ro` despite it holding `SELECT` on every
    table. `pg_constraint` is a plain catalog table, readable like any other
    under `GRANT SELECT ... IN SCHEMA public`'s reach for the catalog itself,
    and returned the expected row.
    """
    rows = conn.execute(
        "SELECT con.conrelid::regclass::text AS table_name, "
        "confrel.relname AS referenced_table "
        "FROM pg_constraint con "
        "JOIN pg_namespace nsp ON nsp.oid = con.connamespace "
        "LEFT JOIN pg_class confrel ON confrel.oid = con.confrelid "
        "WHERE con.contype = 'f' AND nsp.nspname = %s",
        (schema_name,),
    ).fetchall()
    fk_by_table: dict[str, set[str]] = {}
    for table_name, referenced_table in rows:
        if referenced_table:
            fk_by_table.setdefault(table_name, set()).add(referenced_table)
    return fk_by_table


def _table_ddl(table_name: str, typed_columns: list[tuple[str, str, str]]) -> str:
    """Synthesise a `CREATE TABLE` statement from catalogue columns.

    Not `pg_dump` fidelity - no constraints, defaults or indexes - just
    enough for the LLM prompt and `raw_schema()`/`schema_chunks()` to name
    every table and column with its type. Task 5 is where this gets hardened.
    """
    column_defs = []
    for column_name, data_type, is_nullable in typed_columns:
        not_null = "" if is_nullable == "YES" else " NOT NULL"
        column_defs.append(f"{_quote_identifier(column_name)} {data_type}{not_null}")
    body = ",\n  ".join(column_defs)
    return f"CREATE TABLE {_quote_identifier(table_name)} (\n  {body}\n);"


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
    default_schema: str = _PUBLIC_SCHEMA
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
    schema_header: str = "PostgreSQL schema (DDL)"
    # Placeholder. Task 7 writes PostgreSQL's real prompt fragments
    # (`prompt_dialect_section`, `prompt_dialect_name`,
    # `prompt_engine_rules_block`) - these three exist only so `PostgresEngine`
    # satisfies the `Engine` protocol today. Deliberately not crafted prompt
    # text: see `SQLiteEngine`/`DuckDBEngine`'s copies for what the real
    # versions look like once Task 7 writes PostgreSQL's own.
    prompt_dialect_section: str = "POSTGRESQL DIALECT: placeholder, written by Task 7.\n"
    prompt_dialect_name: str = "PostgreSQL"
    prompt_engine_rules_block: str = "Placeholder engine rules, written by Task 7.\n"

    def __init__(self, dsn: str) -> None:
        """Hold the full PostgreSQL connection URL.

        Args:
            dsn: A `postgresql://` or `postgres://` URL, scheme included.
        """
        self.dsn = dsn

    def check_reachable(self) -> None:
        """Raise `EngineUnreachableError` if the server cannot be reached.

        The message never includes `self.dsn` - it carries a password. Only
        the exception's class name is reported.
        """
        try:
            with psycopg.connect(self.dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
                conn.execute("SELECT 1")
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
        """Extract synthesised `CREATE TABLE` statements for every table in `public`.

        Returns:
            The `CREATE TABLE` statements, one per table, semicolon-terminated
            and separated by blank lines, in table-name order.
        """
        with psycopg.connect(self.dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            columns_by_table = _fetch_columns(conn, self.default_schema)
        return "\n\n".join(
            _table_ddl(table_name, columns_by_table[table_name])
            for table_name in sorted(columns_by_table)
        )

    def schema_chunks(self) -> list[SchemaChunk]:
        """Build table-level schema chunks for retrieval without reading row data."""
        with psycopg.connect(self.dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            columns_by_table = _fetch_columns(conn, self.default_schema)
            fk_by_table = _fetch_foreign_tables(conn, self.default_schema)

            chunks: list[SchemaChunk] = []
            for table_name in sorted(columns_by_table):
                typed_columns = columns_by_table[table_name]
                columns = [c for c, _, _ in typed_columns]
                foreign_tables = sorted(fk_by_table.get(table_name, set()))
                ddl = _table_ddl(table_name, typed_columns)
                untyped_columns = [(c, t) for c, t, _ in typed_columns]
                value_hints = _value_hints_for_table(
                    conn, self.default_schema, table_name, untyped_columns
                )
                value_text = " ".join(value for values in value_hints.values() for value in values)
                search_text = " ".join([table_name, ddl, *columns, *foreign_tables, value_text])
                chunks.append(
                    SchemaChunk(
                        table_name=table_name,
                        ddl=ddl,
                        columns=columns,
                        foreign_tables=foreign_tables,
                        search_text=search_text,
                        value_hints=value_hints,
                    )
                )
        return chunks

    def schema_fingerprint(self) -> tuple[object, ...]:
        """Hash `information_schema.columns` for `public` into a cache key.

        Deterministically ordered, so the hash is stable when nothing changes
        and changes whenever a table, column or column's type does - Task 5
        is where this grows the same semantics-hardening the other two
        engines' fingerprints already have (their file-`mtime`-based ones
        aren't a model PostgreSQL can follow at all, since there is no single
        file to stat).
        """
        with psycopg.connect(self.dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            rows = conn.execute(
                "SELECT table_name, column_name, data_type, is_nullable, ordinal_position "
                "FROM information_schema.columns "
                "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
                (self.default_schema,),
            ).fetchall()
        digest = hashlib.sha256(repr(rows).encode()).hexdigest()
        return (self.default_schema, digest)

    def table_names(self) -> frozenset[str]:
        """Every user table's name, lowercased, via the cached schema chunks.

        Routed through `schema.get_schema_chunks` so this shares `schema.py`'s
        single fingerprint-keyed cache rather than a second, driftable one
        here - `self.dsn` already carries its own scheme, unlike the
        file-path engines, so it needs no reattaching before the call.

        Deferred import for the same circular-import reason as
        `SQLiteEngine.table_names` - see that method's docstring.
        """
        from ..schema import get_schema_chunks

        chunks = get_schema_chunks(self.dsn)
        return frozenset(chunk.table_name.lower() for chunk in chunks)

    def column_names(self) -> frozenset[str]:
        """Every user table's column names, lowercased, unioned across tables.

        This is what `safety.is_safe_query`'s default-deny column check
        (`_references_unresolvable_qualified_column`) calls to tell a real
        qualified column reference from PostgreSQL's `alias.name` ->
        `name(alias)` function-call sugar. PostgreSQL is the only engine
        that actually reaches it.

        Routed through `schema.get_schema_chunks` so it shares the same
        fingerprint-keyed cache `table_names` reads - the two resolve to the
        same chunk list, so a query that triggers both pays for one
        catalogue read, not two.

        Deferred import for the same circular-import reason as
        `SQLiteEngine.table_names` - see that method's docstring.
        """
        from ..schema import get_schema_chunks

        chunks = get_schema_chunks(self.dsn)
        return frozenset(column.lower() for chunk in chunks for column in chunk.columns)


__all__ = ["PostgresEngine"]
