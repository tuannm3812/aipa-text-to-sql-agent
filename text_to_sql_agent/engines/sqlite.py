"""SQLite engine: read-only execution, DDL extraction, and schema-chunk building.

Moved from `execution.py` and `schema.py` verbatim; only the enclosing
`def`/class shape changed to fit the `Engine` protocol. See
`text_to_sql_agent.engines.base` for the protocol these implement.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ..config import DEFAULT_VALUE_HINT_LIMIT, DEFAULT_VALUE_HINT_MAX_CARDINALITY
from ..types import QueryResult, SchemaChunk
from .base import EngineUnreachableError


def _sqlite_read_only_authorizer(action: int, *_args: Any) -> int:
    denied_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_PRAGMA,
        sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_ANALYZE,
    }
    return sqlite3.SQLITE_DENY if action in denied_actions else sqlite3.SQLITE_OK


def _read_only_sqlite_connection(db_path: str) -> sqlite3.Connection:
    resolved = Path(db_path).resolve()
    uri = f"{resolved.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    conn.set_authorizer(_sqlite_read_only_authorizer)
    return conn


def _abort_query() -> int:
    """SQLite progress handler: a non-zero return aborts the running query.

    Installed with an interval of `max_vm_steps`, so the first callback ends a
    query that exceeds the budget.
    """
    return 1


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _value_hints_for_table(
    conn: sqlite3.Connection,
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
        cardinality = conn.execute(
            f"SELECT COUNT(DISTINCT {quoted_column}) FROM {quoted_table} "
            f"WHERE {quoted_column} IS NOT NULL"
        ).fetchone()[0]
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
            (limit,),
        ).fetchall()
        values = [str(row[0])[:60] for row in rows if str(row[0]).strip()]
        if values:
            hints[column_name] = values
    return hints


class SQLiteEngine:
    """Read-only SQLite backend.

    Opened via a URI `mode=ro` connection plus a write authorizer.

    Attributes:
        dsn: Filesystem path to the SQLite database file.
    """

    name: str = "sqlite"
    sqlglot_dialect: str = "sqlite"
    internal_prefixes: tuple[str, ...] = ("sqlite_", "pragma_")
    internal_names: frozenset[str] = frozenset({"dbstat"})
    prompt_dialect_section: str = """\
SQLITE DIALECT (must follow):
- Generate SQLite-compatible SQL only.
- Do NOT use EXTRACT, DATE_TRUNC, ILIKE, INTERVAL, FILTER, DISTINCT ON.
- For dates/timestamps use SQLite functions like: strftime('%Y', col),
  strftime('%Y-%m', col), date(col), datetime(col).
"""

    def __init__(self, dsn: str) -> None:
        """Hold the filesystem path to the SQLite database file.

        Args:
            dsn: Filesystem path to the SQLite database.
        """
        self.dsn = dsn

    def check_reachable(self) -> None:
        """Raise `EngineUnreachableError` if the database file does not exist."""
        if not Path(self.dsn).exists():
            raise EngineUnreachableError("input database not found")

    def execute(self, sql: str, *, max_rows: int, work_limit: int) -> QueryResult:
        """Execute a validated SELECT against SQLite with read-only protections.

        Args:
            sql: A query already cleared by `is_safe_query`.
            max_rows: Maximum rows returned before the result is marked truncated.
            work_limit: SQLite VM steps a query may run before it is aborted.
                `0` disables the guard.

        Returns:
            A `QueryResult`. Its `error` is `RESULT_TRUNCATED_TO_<n>_ROWS` when
            more rows were available than `max_rows` allowed, or
            `QUERY_ABORTED_AFTER_<n>_VM_STEPS` when the guard stopped the query.

        Raises:
            ValueError: If `max_rows` is less than 1.
            sqlite3.OperationalError: For genuine SQL errors, such as a missing
                table. Only the abort interrupt is converted to a returned error.
        """
        if max_rows < 1:
            raise ValueError("max_rows must be at least 1")

        with closing(_read_only_sqlite_connection(self.dsn)) as conn:
            if work_limit > 0:
                conn.set_progress_handler(_abort_query, work_limit)
            try:
                cur = conn.execute(sql)
                rows = cur.fetchmany(max_rows + 1)
                columns = [d[0] for d in cur.description] if cur.description else []
            except sqlite3.OperationalError as exc:
                # SQLite's own error code identifies the interrupt directly, so
                # this is immune to any future change in the error message's
                # wording (`sqlite_errorcode` is populated on Python >= 3.11,
                # which this project requires).
                if exc.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT:
                    raise
                return QueryResult(
                    columns=[],
                    rows=[],
                    sql=sql,
                    error=f"QUERY_ABORTED_AFTER_{work_limit}_VM_STEPS",
                )
            capped_rows = rows[:max_rows]
            if len(rows) > max_rows:
                return QueryResult(
                    columns=columns,
                    rows=[tuple(r) for r in capped_rows],
                    sql=sql,
                    error=f"RESULT_TRUNCATED_TO_{max_rows}_ROWS",
                )
            return QueryResult(columns=columns, rows=[tuple(r) for r in capped_rows], sql=sql)

    def raw_schema(self) -> str:
        """Extract CREATE TABLE statements for all user tables in SQLite.

        Returns:
            The `CREATE TABLE` statements, one per table, semicolon-terminated
            and separated by blank lines, in table-name order.
        """
        with closing(sqlite3.connect(self.dsn)) as conn:
            rows = conn.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type='table'
                  AND name NOT LIKE 'sqlite_%'
                  AND sql IS NOT NULL
                ORDER BY name;
                """
            ).fetchall()
        return "\n\n".join(r[0].strip().rstrip(";") + ";" for r in rows)

    def schema_chunks(self) -> list[SchemaChunk]:
        """Build table-level schema chunks for retrieval without reading row data."""
        with closing(sqlite3.connect(self.dsn)) as conn:
            table_rows = conn.execute(
                """
                SELECT name, sql
                FROM sqlite_master
                WHERE type='table'
                  AND name NOT LIKE 'sqlite_%'
                  AND sql IS NOT NULL
                ORDER BY name;
                """
            ).fetchall()

            chunks: list[SchemaChunk] = []
            for table_name, ddl in table_rows:
                table_info = conn.execute(
                    f"PRAGMA table_info({_quote_identifier(table_name)})"
                ).fetchall()
                columns = [row[1] for row in table_info]
                typed_columns = [(row[1], row[2] or "") for row in table_info]
                foreign_tables = sorted(
                    {
                        row[2]
                        for row in conn.execute(
                            f"PRAGMA foreign_key_list({_quote_identifier(table_name)})"
                        ).fetchall()
                        if row[2]
                    }
                )
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
        return chunks

    def schema_fingerprint(self) -> tuple[object, ...]:
        """Return `(path, mtime_ns, size)`, today's `_db_cache_key` value."""
        path = Path(self.dsn).resolve()
        stat = path.stat()
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))
