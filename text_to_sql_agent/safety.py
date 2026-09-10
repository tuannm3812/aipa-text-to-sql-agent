"""Read-only SQL validation: regex pre-filtering plus an AST safety check."""

from __future__ import annotations

import re
from types import ModuleType

sqlglot: ModuleType | None
exp: ModuleType | None
try:
    import sqlglot
    from sqlglot import exp
except ModuleNotFoundError:  # pragma: no cover
    sqlglot = None
    exp = None

_DANGEROUS_SQL_PATTERN = re.compile(
    r"""
    (?ix)
    \b(
        insert|update|delete|drop|alter|create|replace|truncate|
        vacuum|pragma|attach|detach|reindex|analyze|
        begin|commit|rollback|savepoint|release
    )\b
    """.strip()
)


def _is_safe_ast(sql_string: str) -> bool:
    if sqlglot is None or exp is None:
        return True
    try:
        parsed = sqlglot.parse_one(sql_string, read="sqlite")
    except Exception:
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
    """Conservatively allow only read-only SELECT/CTE queries.

    Rejects anything empty, not starting with `SELECT`/`WITH`, matching a
    data-modifying or transaction-control keyword, referencing
    `sqlite_master`/`sqlite_schema`, or failing an AST-level safety check
    (when `sqlglot` is installed; otherwise the AST check is skipped and this
    function relies on the regex checks alone).

    Args:
        sql_string: The SQL text to validate.

    Returns:
        True if the query is judged safe to execute read-only.
    """
    if not sql_string or not sql_string.strip():
        return False
    s = sql_string.strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", s):
        return False
    if _DANGEROUS_SQL_PATTERN.search(s) is not None:
        return False
    if re.search(r"(?is)\bsqlite_master\b|\bsqlite_schema\b", s):
        return False
    return _is_safe_ast(s)
