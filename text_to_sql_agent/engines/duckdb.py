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


def _value_hints_for_table(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    columns: list[tuple[str, str]],
    *,
    max_cardinality: int = DEFAULT_VALUE_HINT_MAX_CARDINALITY,
    limit: int = DEFAULT_VALUE_HINT_LIMIT,
) -> dict[str, list[str]]:
    hints: dict[str, list[str]] = {}
    quoted_table = _quote_identifier(table_name)
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
    # this", not just "is it safe", and finding no case for either. See
    # `tests/test_engine_duckdb.py::VERIFIED_SAFE_TABLE_FUNCTIONS` for the
    # complementary allowlist: the small set of table functions kept
    # *allowed* because a legitimate analytical question could use them
    # (`range`, `generate_series`, `unnest`, `json_each`, `json_tree`,
    # `histogram`, `histogram_values`, `summary`) - everything else
    # `duckdb_functions()` reports is blocked, one way or another, by this
    # class's prefixes/names.
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
        """Extract CREATE TABLE statements for all user tables in DuckDB.

        Returns:
            The `CREATE TABLE` statements, one per table, semicolon-terminated
            and separated by blank lines, in table-name order.
        """
        conn = _connect_read_only(self.dsn)
        try:
            rows = conn.execute(
                "SELECT table_name, sql FROM duckdb_tables() ORDER BY table_name"
            ).fetchall()
        finally:
            conn.close()
        return "\n\n".join(sql.strip().rstrip(";") + ";" for _, sql in rows)

    def schema_chunks(self) -> list[SchemaChunk]:
        """Build table-level schema chunks for retrieval without reading row data."""
        conn = _connect_read_only(self.dsn)
        try:
            table_rows = conn.execute(
                "SELECT table_name, sql FROM duckdb_tables() ORDER BY table_name"
            ).fetchall()
            all_columns = conn.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "ORDER BY table_name, ordinal_position"
            ).fetchall()
            all_fks = conn.execute(
                "SELECT table_name, referenced_table FROM duckdb_constraints() "
                "WHERE constraint_type = 'FOREIGN KEY' AND referenced_table IS NOT NULL"
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
                value_hints = _value_hints_for_table(conn, table_name, typed_columns)
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


__all__ = ["DuckDBEngine"]
