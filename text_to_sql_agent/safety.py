"""Read-only SQL validation: structural checks plus a sqlglot AST safety check."""

from __future__ import annotations

import re
from types import ModuleType
from typing import Any

sqlglot: ModuleType | None
exp: ModuleType | None
try:
    import sqlglot
    from sqlglot import exp
except ModuleNotFoundError:  # pragma: no cover
    sqlglot = None
    exp = None

_ALLOWED_PREFIX = re.compile(r"(?is)^(select|with)\b")

_INTERNAL_TABLE_PREFIXES = ("sqlite_", "pragma_")
_INTERNAL_TABLE_NAMES = frozenset({"dbstat"})


def _references_internals(parsed: Any) -> bool:
    """True if the statement reads SQLite's own schema or statistics tables.

    Checked against parsed table nodes rather than the raw text, so a table
    genuinely called `my_sqlite_notes` is fine and the string literal
    `'sqlite_master'` is not mistaken for a table reference.

    `parsed` is a `sqlglot.exp.Expression`, typed `Any` here because `exp` is
    imported defensively (see the module-level try/except) and so cannot be
    named as a static type; callers only ever reach this function after
    `sqlglot`/`exp` have already been confirmed non-`None`.
    """
    if exp is None:
        return False
    for table in parsed.find_all(exp.Table):
        name = (table.name or "").lower()
        if name in _INTERNAL_TABLE_NAMES or name.startswith(_INTERNAL_TABLE_PREFIXES):
            return True
    # Table-valued functions such as pragma_table_info(...) parse as anonymous
    # function calls, not as tables.
    for function in parsed.find_all(exp.Anonymous):
        if (function.this or "").lower().startswith(_INTERNAL_TABLE_PREFIXES):
            return True
    return False


def _is_safe_ast(sql_string: str) -> bool:
    """Reject anything that parses to more than one statement or writes data.

    Returns False when `sqlglot` is unavailable: without a parser there is no
    validation to perform, and refusing to run is the correct failure mode for
    a safety check.
    """
    if sqlglot is None or exp is None:
        return False
    try:
        statements = sqlglot.parse(sql_string, read="sqlite")
    except Exception:
        return False

    # `parse_one` would silently inspect only the first statement, so a payload
    # like "SELECT 1; DROP TABLE t" would be approved on the strength of its
    # harmless prefix. Count them instead.
    if len(statements) != 1:
        return False
    parsed = statements[0]
    if parsed is None:
        return False

    if _references_internals(parsed):
        return False

    forbidden = (
        exp.Alter,
        exp.Command,
        exp.Create,
        exp.Delete,
        exp.Drop,
        exp.Insert,
        exp.Merge,
        exp.Update,
    )
    if isinstance(parsed, forbidden) or any(parsed.find_all(*forbidden)):
        return False

    allowed_roots = (exp.Select, exp.Union, exp.With)
    return isinstance(parsed, allowed_roots) or parsed.find(exp.Select) is not None


def is_safe_query(sql_string: str) -> bool:
    """Conservatively allow only single-statement, read-only SELECT/CTE queries.

    Rejects anything empty, not starting with `SELECT`/`WITH`, parsing to more
    than one statement, referencing SQLite's own schema/statistics tables
    (`sqlite_*`, `pragma_*`, `dbstat`), or containing a data-modifying node.
    Keyword matching is deliberately *not* used for any of this: a raw-text
    scan rejected legitimate SQL such as `REPLACE(...)` and string literals
    containing words like `update`, and would just as easily have let a
    literal `'sqlite_master'` masquerade as a real table reference in the
    other direction. Every structural rule here is checked against the parsed
    AST instead.

    This is only a partial second line of defence. `execution.py` opens the
    database read-only and installs a write authorizer, which independently
    blocks writes - but the authorizer does not block reads of SQLite's own
    schema/statistics tables, so the internals check above is this
    function's alone to get right.

    Args:
        sql_string: The SQL text to validate.

    Returns:
        True if the query is judged safe to execute read-only. False when
        `sqlglot` is unavailable, since no validation is possible.
    """
    if not sql_string or not sql_string.strip():
        return False
    s = sql_string.strip().rstrip(";").strip()
    if not _ALLOWED_PREFIX.match(s):
        return False
    return _is_safe_ast(s)
