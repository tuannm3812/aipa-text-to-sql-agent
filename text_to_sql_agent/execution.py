"""Read-only query execution, delegated to the resolved `Engine`."""

from __future__ import annotations

from .config import DEFAULT_MAX_ROWS
from .engines import open_engine
from .types import QueryResult


def execute_query(
    db_path: str,
    sql_string: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_vm_steps: int | None = None,
) -> QueryResult:
    """Execute a validated SELECT against the database at `db_path`.

    A thin delegation to the engine resolved from `db_path` by `open_engine`.

    Args:
        db_path: Filesystem path (or DSN) to the database.
        sql_string: A query already cleared by `is_safe_query`.
        max_rows: Maximum rows returned before the result is marked truncated.
        max_vm_steps: Work-unit budget a query may run before it is aborted,
            in the resolved engine's own unit - SQLite counts VM steps,
            DuckDB and PostgreSQL count milliseconds (`Engine.execute`'s
            `work_limit`). `None` (the default) uses the resolved engine's
            own `Engine.default_work_limit`, so a SQLite database still gets
            SQLite's 100,000-VM-step budget and a PostgreSQL or DuckDB one
            gets its own 5,000-millisecond budget - passing one engine's
            default straight through to another used to conflate the two
            units (a SQLite-shaped `int` default meant every DuckDB/
            PostgreSQL query got a ~100-second timeout instead of ~5
            seconds). `0` disables the guard on any engine, same as before.
            An explicit non-zero value is passed straight through to
            whichever engine is resolved, unchanged.

    Returns:
        A `QueryResult`. Its `error` is `RESULT_TRUNCATED_TO_<n>_ROWS` when more
        rows were available than `max_rows` allowed, or
        `QUERY_ABORTED_AFTER_<n>_VM_STEPS`/`QUERY_ABORTED_AFTER_<n>_MS` (per
        the resolved engine's own unit) when the guard stopped the query.

    Raises:
        ValueError: If `max_rows` is less than 1.
        sqlite3.OperationalError: For genuine SQL errors, such as a missing
            table. Only the abort interrupt is converted to a returned error.
    """
    engine = open_engine(db_path)
    work_limit = engine.default_work_limit if max_vm_steps is None else max_vm_steps
    return engine.execute(sql_string, max_rows=max_rows, work_limit=work_limit)
