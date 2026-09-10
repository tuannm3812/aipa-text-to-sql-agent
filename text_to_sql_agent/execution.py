"""Read-only SQLite query execution with row-count caps and a write authorizer."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .config import DEFAULT_MAX_ROWS, DEFAULT_MAX_VM_STEPS
from .types import QueryResult


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


def execute_query(
    db_path: str,
    sql_string: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_vm_steps: int = DEFAULT_MAX_VM_STEPS,
) -> QueryResult:
    """Execute a validated SELECT against SQLite with read-only protections.

    Args:
        db_path: Filesystem path to the SQLite database.
        sql_string: A query already cleared by `is_safe_query`.
        max_rows: Maximum rows returned before the result is marked truncated.
        max_vm_steps: SQLite VM steps a query may run before it is aborted.
            `0` disables the guard.

    Returns:
        A `QueryResult`. Its `error` is `RESULT_TRUNCATED_TO_<n>_ROWS` when more
        rows were available than `max_rows` allowed, or
        `QUERY_ABORTED_AFTER_<n>_VM_STEPS` when the guard stopped the query.

    Raises:
        ValueError: If `max_rows` is less than 1.
        sqlite3.OperationalError: For genuine SQL errors, such as a missing
            table. Only the abort interrupt is converted to a returned error.
    """
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1")

    with closing(_read_only_sqlite_connection(db_path)) as conn:
        if max_vm_steps > 0:
            conn.set_progress_handler(_abort_query, max_vm_steps)
        try:
            cur = conn.execute(sql_string)
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
                sql=sql_string,
                error=f"QUERY_ABORTED_AFTER_{max_vm_steps}_VM_STEPS",
            )
        capped_rows = rows[:max_rows]
        if len(rows) > max_rows:
            return QueryResult(
                columns=columns,
                rows=[tuple(r) for r in capped_rows],
                sql=sql_string,
                error=f"RESULT_TRUNCATED_TO_{max_rows}_ROWS",
            )
        return QueryResult(columns=columns, rows=[tuple(r) for r in capped_rows], sql=sql_string)
