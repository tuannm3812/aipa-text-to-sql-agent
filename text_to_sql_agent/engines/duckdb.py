"""DuckDB engine: read-only execution, DDL extraction, and schema-chunk building.

See `text_to_sql_agent.engines.base` for the protocol this implements, and
`text_to_sql_agent.engines.sqlite` for the sibling this deliberately mirrors in
shape. Read-only comes from DuckDB's own `read_only=True` connect flag, which
was verified (2026-09-14) to refuse `INSERT`, `UPDATE`, `DELETE`, `DROP` and
`CREATE` natively, each raising `duckdb.InvalidInputException`.

`read_only=True` alone is **not enough**: it protects the database file, not
the filesystem. Probed 2026-09-18, a read-only connection could still run
`SELECT * FROM read_csv('<file>')`, `SELECT * FROM '<file>'` (a bare quoted
path, with no function name for `is_safe_query` to catch), `glob(...)` to list
directories, and `COPY ... TO` to write a file to disk. Every connection this
module opens also passes `config={"enable_external_access": False}`, which
refuses all of the above with `duckdb.PermissionException`. This is the
load-bearing defence against those four; `tests/test_engine_duckdb.py` pins it
directly, bypassing `is_safe_query`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

from ..config import DEFAULT_VALUE_HINT_LIMIT, DEFAULT_VALUE_HINT_MAX_CARDINALITY
from ..types import QueryResult, SchemaChunk
from .base import EngineUnreachableError

# DuckDB groups tables into schemas ("main" is the default a bare filesystem
# path connects into, same as PostgreSQL). `safety.py`'s
# `_references_unknown_table` only accepts an absent or `main` schema
# qualifier (see the `schema and schema != "main"` check there), so `main` is
# this engine's whole supported catalogue surface - not a full multi-schema
# implementation, deliberately deferred to Phase 3b alongside PostgreSQL. See
# the 2026-09-21 review finding in `docs/6_agent_log.md`: before every
# catalogue query below was filtered to this schema, a table outside it could
# either crash schema building outright (a same-named table in another
# schema, read through an unqualified value-hint query) or be advertised by
# `raw_schema()`/`schema_chunks()` and then rejected by the validator, which
# already only ever accepted `main`.
_MAIN_SCHEMA = "main"


def _connect_read_only(db_path: str) -> duckdb.DuckDBPyConnection:
    """Open a read-only DuckDB connection with filesystem access disabled.

    Both settings are mandatory together: `read_only=True` refuses writes to
    the database file itself, but does nothing to stop `read_csv`, a bare
    quoted path, `glob`, or `COPY ... TO` from touching the filesystem.
    `enable_external_access=False` is what refuses those.
    """
    return duckdb.connect(db_path, read_only=True, config={"enable_external_access": False})


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_qualified(schema_name: str, table_name: str) -> str:
    """Quote `schema_name.table_name` so it can only ever resolve to that table.

    An unqualified quoted table name resolves against DuckDB's search path
    (`main` by default), which is exactly how a value-hint query for one
    `main`-schema table silently read a same-named table in another schema -
    see `_MAIN_SCHEMA`'s module docstring note. Every catalogue query this
    module runs is already filtered to `schema_name = 'main'`, so this only
    ever qualifies with `main` today, but qualifying explicitly (rather than
    relying on the filter alone) means a value-hint query can never resolve
    to a different schema's table even if that filter is ever loosened.
    """
    return _quote_identifier(schema_name) + "." + _quote_identifier(table_name)


def _value_hints_for_table(
    conn: duckdb.DuckDBPyConnection,
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
        # COUNT(...) always returns exactly one row; the None case only
        # satisfies duckdb's stub, which types fetchone() as Optional.
        cardinality = cardinality_row[0] if cardinality_row is not None else None
        if cardinality is None or cardinality < 1 or cardinality > max_cardinality:
            continue
        rows = conn.execute(
            f"""
            SELECT {quoted_column}, COUNT(*) AS n
            FROM {quoted_table}
            WHERE {quoted_column} IS NOT NULL
            GROUP BY {quoted_column}
            ORDER BY n DESC, {quoted_column}
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        values = [str(row[0])[:60] for row in rows if str(row[0]).strip()]
        if values:
            hints[column_name] = values
    return hints


class DuckDBEngine:
    """Read-only DuckDB backend.

    Opened via `read_only=True` plus `enable_external_access=False` on every
    connection - see the module docstring for why both are mandatory.

    Attributes:
        dsn: Filesystem path to the DuckDB database file.
    """

    name: str = "duckdb"
    sqlglot_dialect: str = "duckdb"
    # See `Engine.default_schema`. Same value as `_MAIN_SCHEMA` above - kept
    # as a separate protocol-facing attribute rather than aliased to it, so
    # this class's public surface doesn't depend on a module-private name.
    default_schema: str = _MAIN_SCHEMA
    # DuckDB's own catalogue functions (duckdb_tables(), duckdb_constraints(),
    # ...) and its Postgres-compatibility views (pg_catalog etc.) are both
    # readable from a read-only connection, unlike sqlite_master's write-only
    # style - so both prefixes are internal here. information_schema is the
    # standard SQL catalogue view and is readable too; sqlite_master is listed
    # so a query that names it explicitly against a DuckDB engine is still
    # rejected rather than merely failing to resolve. See Step 5 of the task
    # brief for the probe that verified this list against duckdb_functions().
    #
    # "read_", "parquet_" and "pragma_" are here for a different reason: they
    # are filesystem-access or internals-exposing table functions
    # (read_csv, read_parquet, parquet_metadata, pragma_table_info, ...), not
    # catalogue functions in the sense of the paragraph above. The connection
    # itself already refuses every filesystem one via
    # `enable_external_access=False` - see the module docstring - so this is
    # defence-in-depth, not the load-bearing guard. It also cannot be
    # complete: `SELECT * FROM '<path>'`, a bare quoted path, has no function
    # name at all for this AST check to match, which is exactly why the
    # connection setting, not this list, is what actually stops it.
    # "pragma_" mirrors SQLiteEngine's own "pragma_" prefix: these expose
    # engine internals (pragma_table_info, pragma_storage_info, ...), not
    # files, and nothing in this codebase calls them.
    #
    # `glob`, `sniff_csv`, `which_secret`, `query`, `query_table` and
    # `json_execute_serialized_sql` are named exactly rather than by prefix
    # because each is a one-off: `glob`/`sniff_csv` read the filesystem
    # directly; `which_secret` probes DuckDB's credential/secrets store;
    # `json_execute_serialized_sql` executes an opaque serialized query plan
    # that this AST check cannot see inside; `query`/`query_table` execute an
    # arbitrary SQL string or reference an arbitrary catalog table by string
    # argument, which is invisible to sqlglot's table/function-name walk the
    # same way a serialized plan is - probed 2026-09-19:
    # `SELECT * FROM query('SELECT * FROM duckdb_settings()')` and
    # `SELECT * FROM query_table('information_schema.tables')` both returned
    # real internal data through a read-only, external-access-disabled
    # connection, entirely bypassing the internals checks above, because the
    # internal reference was hidden inside a string literal rather than
    # appearing as its own parsed `Table`/`Func` node.
    #
    # The remaining exact names are administrative functions, Python-bridge
    # functions, and dev/test scaffolding - `checkpoint`, `force_checkpoint`,
    # `enable_logging`, `disable_logging`, `enable_profiling`,
    # `disable_profiling`, `truncate_duckdb_logs`, `arrow_scan`,
    # `arrow_scan_dumb`, `pandas_scan`, `python_map_function`, `seq_scan`,
    # `icu_calendar_names`, `test_all_types`, `test_vector_types`. None of
    # them read the filesystem, but "does not read the filesystem" is the
    # wrong bar for this list: the bar is whether a legitimate analytical
    # question could ever need the function, and none of these can - a
    # text-to-SQL agent answering business questions has no reason to force a
    # WAL checkpoint, toggle profiling, or call into DuckDB's Arrow/Pandas/
    # Python interop. Found by probing five of them (`checkpoint`,
    # `enable_profiling`, `enable_logging`, `disable_logging`,
    # `truncate_duckdb_logs`) directly against a real `DuckDBEngine` and
    # confirming they passed `is_safe_query` *and executed* - low impact
    # today, since `execute` opens a fresh connection each call so any state
    # change dies with it, but only because nobody had asked whether they
    # belonged, not because anything stopped them. The other ten were
    # already blocked in effect (they raised `BinderException`/
    # `InvalidInputException` against literal arguments a model could
    # actually write, since they dereference Python objects or catalog
    # internals no SQL literal can produce) but were unclassified at the
    # validator level the same way. `repeat`/`repeat_row` (build a table of N
    # copies of a literal value or row) are here for the same "no legitimate
    # analytical use" reason, not because they touch the filesystem or state -
    # they were on the allowlist first, moved after re-reviewing every
    # allowlist entry against "would a real business question ever need
    # this", not just "is it safe", and finding no case for either.
    #
    # Task 6b (2026-09-19) superseded this list's role as the primary
    # defence: a name-based blocklist against DuckDB's ~960 functions leaked
    # in four successive review rounds - `sniff_csv`; then `query`/
    # `query_table` (a string argument hiding an arbitrary query, invisible
    # to a name-based check); then the administrative functions above; then
    # `current_setting('secret_directory')`, a **scalar**, which no blocklist
    # entry could ever reach because the internals check below deliberately
    # fires only in a table-source position; and `histogram_values`, a macro
    # whose body calls `query_table(source)` and was on the *allowlist*, so
    # `SELECT * FROM histogram_values('information_schema.tables', ...)` read
    # the catalogue through a name nobody had reason to suspect. `histogram`
    # and `summary` share that macro body and are excluded for the same
    # reason. This blocklist stays - see `allowed_functions` below for why it
    # is still worth keeping as a second, independent gate - but the primary
    # guarantee is now `allowed_functions`'s default-deny: an unlisted
    # function is rejected regardless of position, not merely absent from a
    # list someone had to remember to grow.
    #
    # See Step 5 of the task brief (and its Fix 1/Fix 2 follow-ups) for the
    # probe that swept every table function `duckdb_functions()` reports and
    # verified this list against it;
    # `tests/test_engine_duckdb.py::test_every_duckdb_table_function_is_classified`
    # re-runs that sweep as a regression test so a function DuckDB adds later
    # fails closed instead of silently passing.
    internal_prefixes: tuple[str, ...] = (
        "duckdb_",
        "pg_",
        "sqlite_",
        "read_",
        "parquet_",
        "pragma_",
    )
    internal_names: frozenset[str] = frozenset(
        {
            "information_schema",
            "sqlite_master",
            "glob",
            "sniff_csv",
            "which_secret",
            "json_execute_serialized_sql",
            "query",
            "query_table",
            "checkpoint",
            "force_checkpoint",
            "enable_logging",
            "disable_logging",
            "enable_profiling",
            "disable_profiling",
            "truncate_duckdb_logs",
            "arrow_scan",
            "arrow_scan_dumb",
            "pandas_scan",
            "python_map_function",
            "seq_scan",
            "icu_calendar_names",
            "test_all_types",
            "test_vector_types",
            "repeat",
            "repeat_row",
        }
    )
    # Task 6b's default-deny gate (see `Engine.allowed_functions` and
    # `text_to_sql_agent/safety.py`'s `_references_disallowed_function`):
    # every function call `is_safe_query` finds anywhere in a DuckDB query -
    # scalar, aggregate, window or table position alike - must resolve to a
    # name in this set or the query is rejected outright. `internal_prefixes`/
    # `internal_names` above still run as an additional, independent gate;
    # this does not replace them, it closes what they structurally cannot
    # reach (a function used as a *value*, not a table source - see the long
    # comment above `internal_names`).
    #
    # Curated 2026-09-19 to the functions an analytical question over a
    # user's own schema could plausibly need: aggregates, window functions,
    # string/date/time/numeric/conditional functions, and list/JSON access.
    # No macro is included unless its body was read and shown not to reach
    # `query`, `query_table` or a catalogue object - none were; every entry
    # below is a scalar, aggregate, or (for the five table functions) a
    # genuine table function. `current_setting` and every other
    # configuration/session-introspection function are deliberately absent,
    # as are `histogram`/`histogram_values`/`summary` (see the note above
    # `internal_names`). When a function's place in a legitimate analytical
    # question was unclear, it was left out, per the task brief.
    #
    # Every entry here is the name DuckDB itself registers the function
    # under, not necessarily the literal spelling `_resolve_function_name`
    # sees at the call site - `lpad`/`rpad`, `var_samp`/`variance`,
    # `string_agg`/`group_concat`/`listagg`, `range`/`generate_series` and
    # several others are synonyms sqlglot parses into the same typed AST
    # node, which `_resolve_function_name` then resolves to one canonical
    # token from this set - see that function's docstring and
    # `_FUNCTION_NAME_OVERRIDES` in `safety.py` for the full mapping, and
    # `tests/test_engine_duckdb.py::test_allowed_function_round_trip` for the
    # proof that every entry below round-trips correctly: parsed under the
    # DuckDB dialect, resolved, and found in this set.
    allowed_functions: frozenset[str] | None = frozenset(
        {
            # -- Aggregates --
            "count",
            "sum",
            "avg",
            "min",
            "max",
            "median",
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
            "first",
            "last",
            "approx_count_distinct",
            "corr",
            "covar_pop",
            "covar_samp",
            "quantile",
            "quantile_cont",
            "quantile_disc",
            "arg_min",
            "arg_max",
            "count_if",
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
            "string_split",
            "lpad",
            "position",
            "regexp_replace",
            "regexp_extract",
            "regexp_matches",
            "regexp_full_match",
            "contains",
            "starts_with",
            "ends_with",
            "reverse",
            "left",
            "right",
            "repeat",
            # -- Date and time functions --
            "date_trunc",
            "extract",
            "date_part",
            "datepart",
            "date_diff",
            "date_add",
            "date_sub",
            "age",
            "current_date",
            "now",
            "strftime",
            "strptime",
            "epoch",
            "epoch_ms",
            "make_date",
            "last_day",
            "year",
            "month",
            "day",
            "hour",
            "minute",
            "second",
            "dayofweek",
            "dayofyear",
            "week",
            "isodow",
            "quarter",
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
            "try_cast",
            # -- List and JSON access --
            "list_extract",
            "list_value",
            "list_aggregate",
            "list_contains",
            "list_position",
            "list_sort",
            "list_distinct",
            "json_extract",
            "json_extract_string",
            "json_array_length",
            "json_keys",
            "json_type",
            "json_valid",
            "to_json",
            # -- Table functions --
            # `range` and `generate_series` both parse to the same typed AST
            # node and resolve to "generate_series"; only that name needs to
            # be here, but `range` costs nothing to list for a reader
            # checking this set against the brief.
            "range",
            "generate_series",
            "unnest",
            "json_each",
            "json_tree",
        }
    )
    # See SQLiteEngine.schema_header - this is the DuckDB counterpart llm.py's
    # user-prompt header uses when the target engine is DuckDB.
    schema_header: str = "DuckDB schema (DDL)"
    # This block must stay byte-identical to the corresponding block in
    # SQLiteEngine.prompt_dialect_section's counterpart once llm.py assembles
    # the prompt from a shared body plus this per-engine section - see the
    # matching note on SQLiteEngine.
    prompt_dialect_section: str = """\
DUCKDB DIALECT (must follow):
- Generate DuckDB-compatible SQL only.
- DuckDB supports EXTRACT, DATE_TRUNC and INTERVAL; prefer those over SQLite's strftime/date functions.
- For dates/timestamps use DuckDB functions like: date_trunc('year', col), date_trunc('month', col), CAST(col AS DATE).
"""  # noqa: E501
    # See `SQLiteEngine.prompt_dialect_name` for what this substitutes into
    # and why it is a per-engine value rather than dialect-neutral text: the
    # SQLite prompt's pinned bytes forced that choice, but nothing forces
    # DuckDB's own copy to say "SQLite" - this is the fix for the Phase 3a
    # review finding (2026-09-21) that every DuckDB generation and repair was
    # instructed to write "a SINGLE SQLite SELECT query" and told SQLite's
    # `=` is case-sensitive.
    prompt_dialect_name: str = "DuckDB"
    # DuckDB's own internal-tables rule, in place of SQLite's. Names the
    # catalogue surfaces a DuckDB read-only connection can actually read
    # (`duckdb_tables()`, `information_schema`, `pg_catalog` - see
    # `internal_prefixes`/`internal_names` above for the full validator-level
    # list) rather than sqlite_master, which does not exist in DuckDB.
    prompt_engine_rules_block: str = """\
- Do NOT reference duckdb_tables(), information_schema, pg_catalog, or any other internal DuckDB tables or functions.
- Prefer simple SQL compatible with DuckDB.
"""  # noqa: E501

    def __init__(self, dsn: str) -> None:
        """Hold the filesystem path to the DuckDB database file.

        Args:
            dsn: Filesystem path to the DuckDB database.
        """
        self.dsn = dsn

    def check_reachable(self) -> None:
        """Raise `EngineUnreachableError` if the database file does not exist."""
        if not Path(self.dsn).exists():
            raise EngineUnreachableError("input database not found")

    def execute(self, sql: str, *, max_rows: int, work_limit: int) -> QueryResult:
        """Execute a validated SELECT against DuckDB with read-only protections.

        Args:
            sql: A query already cleared by `is_safe_query`.
            max_rows: Maximum rows returned before the result is marked truncated.
            work_limit: Milliseconds a query may run before it is aborted via
                `connection.interrupt()`. `0` disables the guard.

        Returns:
            A `QueryResult`. Its `error` is `RESULT_TRUNCATED_TO_<n>_ROWS` when
            more rows were available than `max_rows` allowed, or
            `QUERY_ABORTED_AFTER_<n>_MS` when the guard stopped the query.

        Raises:
            ValueError: If `max_rows` is less than 1.
            Exception: For genuine SQL errors, such as a missing table. Only
                the abort interrupt is converted to a returned error.
        """
        if max_rows < 1:
            raise ValueError("max_rows must be at least 1")

        conn = _connect_read_only(self.dsn)
        try:
            timer: threading.Timer | None = None
            if work_limit > 0:
                timer = threading.Timer(work_limit / 1000, conn.interrupt)
                timer.start()
            try:
                cur = conn.execute(sql)
                rows = cur.fetchmany(max_rows + 1)
                columns = [d[0] for d in cur.description] if cur.description else []
            except duckdb.InterruptException:
                return QueryResult(
                    columns=[],
                    rows=[],
                    sql=sql,
                    error=f"QUERY_ABORTED_AFTER_{work_limit}_MS",
                )
            finally:
                if timer is not None:
                    timer.cancel()
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
        """Extract CREATE TABLE statements for every user table in `main`.

        Tables outside `main` are deliberately excluded - see `_MAIN_SCHEMA`'s
        module-level comment for why `main` is this engine's whole supported
        catalogue surface.

        Returns:
            The `CREATE TABLE` statements, one per table, semicolon-terminated
            and separated by blank lines, in table-name order.
        """
        conn = _connect_read_only(self.dsn)
        try:
            rows = conn.execute(
                "SELECT table_name, sql FROM duckdb_tables() "
                "WHERE schema_name = ? ORDER BY table_name",
                [_MAIN_SCHEMA],
            ).fetchall()
        finally:
            conn.close()
        return "\n\n".join(sql.strip().rstrip(";") + ";" for _, sql in rows)

    def schema_chunks(self) -> list[SchemaChunk]:
        """Build table-level schema chunks for retrieval without reading row data.

        Every catalogue query here is filtered to `main` - see `_MAIN_SCHEMA`'s
        module-level comment. A table in another schema is never included, so
        it can never be keyed by a bare `table_name` that collides with a
        `main`-schema table of the same name (the reviewer's live
        `BinderException` reproduction), and it can never be advertised here
        only for `safety.py`'s `main`-only validator to reject it.
        """
        conn = _connect_read_only(self.dsn)
        try:
            table_rows = conn.execute(
                "SELECT table_name, sql FROM duckdb_tables() "
                "WHERE schema_name = ? ORDER BY table_name",
                [_MAIN_SCHEMA],
            ).fetchall()
            all_columns = conn.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = ? ORDER BY table_name, ordinal_position",
                [_MAIN_SCHEMA],
            ).fetchall()
            all_fks = conn.execute(
                "SELECT table_name, referenced_table FROM duckdb_constraints() "
                "WHERE constraint_type = 'FOREIGN KEY' AND referenced_table IS NOT NULL "
                "AND schema_name = ?",
                [_MAIN_SCHEMA],
            ).fetchall()

            columns_by_table: dict[str, list[tuple[str, str]]] = {}
            for table_name, column_name, data_type in all_columns:
                columns_by_table.setdefault(table_name, []).append((column_name, data_type))

            fk_by_table: dict[str, set[str]] = {}
            for table_name, referenced_table in all_fks:
                fk_by_table.setdefault(table_name, set()).add(referenced_table)

            chunks: list[SchemaChunk] = []
            for table_name, ddl in table_rows:
                typed_columns = columns_by_table.get(table_name, [])
                columns = [c for c, _ in typed_columns]
                foreign_tables = sorted(fk_by_table.get(table_name, set()))
                value_hints = _value_hints_for_table(conn, _MAIN_SCHEMA, table_name, typed_columns)
                value_text = " ".join(value for values in value_hints.values() for value in values)
                search_text = " ".join(
                    [table_name, ddl or "", *columns, *foreign_tables, value_text]
                )
                chunks.append(
                    SchemaChunk(
                        table_name=table_name,
                        ddl=ddl.strip().rstrip(";") + ";",
                        columns=columns,
                        foreign_tables=foreign_tables,
                        search_text=search_text,
                        value_hints=value_hints,
                    )
                )
        finally:
            conn.close()
        return chunks

    def schema_fingerprint(self) -> tuple[object, ...]:
        """Return `(path, mtime_ns, size)`, like `SQLiteEngine`'s cache key.

        A DuckDB database is a file too, so the same filesystem-metadata
        fingerprint applies unchanged.
        """
        path = Path(self.dsn).resolve()
        stat = path.stat()
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))

    def table_names(self) -> frozenset[str]:
        """Every user table's name, lowercased, via the cached schema chunks.

        This is what `safety.is_safe_query`'s default-deny table check
        (`_references_unknown_table`) calls to tell a real table from a
        replacement-scan path or an unattached schema like
        `information_schema` - see that function's docstring for the full
        reasoning on why table existence, not a quoting heuristic, is what
        closes that gap.

        Routed through `schema.get_schema_chunks` with the `duckdb://`
        scheme reattached (`self.dsn` is the bare filesystem path -
        `open_engine` strips the scheme before constructing this class) so
        this shares `schema.py`'s single fingerprint-keyed cache rather than
        a second, driftable one here: the expensive catalogue read only
        happens again when `schema_fingerprint()` actually changes, and
        every other call is a cheap `Path.stat()` plus a cache hit.

        Deferred import for the same circular-import reason as
        `SQLiteEngine.table_names` - see that method's docstring.
        """
        from ..schema import get_schema_chunks

        chunks = get_schema_chunks(f"duckdb://{self.dsn}")
        return frozenset(chunk.table_name.lower() for chunk in chunks)

    def column_names(self) -> frozenset[str]:
        """Every user table's column names, lowercased, unioned across tables.

        Never reached by `safety.is_safe_query` for DuckDB: the column check
        that calls this is gated on `safety._DOT_CALL_DIALECTS`, which
        DuckDB is deliberately not in (DuckDB has no `alias.name` ->
        `name(alias)` function-call sugar - see
        `safety._references_unresolvable_qualified_column`). Implemented for
        real anyway, on the same principle as `table_names`.

        Deferred import for the same circular-import reason as
        `SQLiteEngine.table_names` - see that method's docstring.
        """
        from ..schema import get_schema_chunks

        chunks = get_schema_chunks(f"duckdb://{self.dsn}")
        return frozenset(column.lower() for chunk in chunks for column in chunk.columns)


__all__ = ["DuckDBEngine"]
