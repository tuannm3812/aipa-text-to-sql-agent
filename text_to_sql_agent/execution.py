"""Read-only query execution, delegated to the resolved `Engine`."""

from __future__ import annotations

from .config import DEFAULT_MAX_ROWS, DEFAULT_MAX_VM_STEPS
from .engines import open_engine
from .types import QueryResult


def execute_query(
    db_path: str,
    sql_string: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_vm_steps: int = DEFAULT_MAX_VM_STEPS,
) -> QueryResult:
    """Execute a validated SELECT against the database at `db_path`.

    A thin delegation to the engine resolved from `db_path` by `open_engine`;
    today that is always `SQLiteEngine`. See `Engine.execute` for the
    behaviour this wraps.

    Args:
        db_path: Filesystem path (or DSN) to the database.
        sql_string: A query already cleared by `is_safe_query`.
        max_rows: Maximum rows returned before the result is marked truncated.
        max_vm_steps: Work-unit budget a query may run before it is aborted.
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
    return open_engine(db_path).execute(sql_string, max_rows=max_rows, work_limit=max_vm_steps)
