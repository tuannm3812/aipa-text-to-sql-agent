"""Read-only SQL validation: structural checks plus a sqlglot AST safety check."""

from __future__ import annotations

import re
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlglot import expressions as sqlglot_exp

    from .engines import Engine

sqlglot: ModuleType | None
exp: ModuleType | None
try:
    import sqlglot
    from sqlglot import exp
except ModuleNotFoundError:  # pragma: no cover
    sqlglot = None
    exp = None

_ALLOWED_PREFIX = re.compile(r"(?is)^(select|with)\b")


def _is_table_source(node: sqlglot_exp.Expression) -> bool:
    """True if `node` sits where a table would, rather than in a value position.

    A call reached through `FROM` or a `JOIN` is a table source; the same name
    used as a value — in a select list, a `WHERE`, a scalar subquery — is not.

    The walk stops at the first enclosing `SELECT`, which is what makes the
    distinction hold inside nesting. Treating any `Subquery` ancestor as proof
    of a table position was wrong: it rejected `SELECT (SELECT sqlite_version())`
    and `SELECT * FROM (SELECT sqlite_version() AS v)`, where the scalar is a
    value inside its own SELECT and the subquery belongs to an outer query. A
    genuine nested table read such as
    `SELECT (SELECT count(*) FROM dbstat('main'))` still reaches `FROM` before
    that boundary, so it stays rejected.

    Args:
        node: The parsed node to locate.

    Returns:
        True if this node sits in a table-source position in its own scope.
    """
    if exp is None:
        return False
    parent = node.parent
    while parent is not None:
        if isinstance(parent, (exp.From, exp.Join)):
            return True
        if isinstance(parent, exp.Select):
            return False
        parent = parent.parent
    return False


def _references_internals(
    parsed: sqlglot_exp.Expression,
    *,
    internal_prefixes: tuple[str, ...],
    internal_names: frozenset[str],
) -> bool:
    """True if the statement reads the engine's own schema or statistics tables.

    Checked against parsed table nodes rather than the raw text, so a table
    genuinely called `my_sqlite_notes` is fine and the string literal
    `'sqlite_master'` is not mistaken for a table reference.

    `internal_prefixes` and `internal_names` come from the calling engine
    (SQLite's `sqlite_`/`pragma_`/`dbstat` today) rather than being read from
    a module constant, so a future engine with different internals - or none
    at all - is not silently checked against SQLite's list.

    The `sqlglot_exp` type is imported under `TYPE_CHECKING` only, since
    `exp` itself is imported defensively (see the module-level try/except)
    and is annotated `ModuleType | None`, so `exp.Expression` cannot be named
    as a static type; `from __future__ import annotations` means this
    annotation never evaluates at runtime, so the defensive import stays
    intact when `sqlglot` is missing. The `if exp is None` guard below is
    still required regardless: it protects attribute access on the runtime
    *module* `exp`, not the type of the `parsed` parameter, and mypy cannot
    see across the caller's own `exp is None` check in `_is_safe_ast`.
    """
    if exp is None:
        return False
    for table in parsed.find_all(exp.Table):
        name = (table.name or "").lower()
        if name in internal_names or name.startswith(internal_prefixes):
            return True
    # Table-valued functions such as pragma_table_info(...) parse as anonymous
    # function calls, not as tables. `.name` (not `.this`) is used here too,
    # since it normalises both the bare form and a quoted name (an Identifier
    # node) to a plain string the same way the Table branch above relies on -
    # `.this` alone breaks on a quoted name and, for the bare form, misses
    # that "dbstat" is also a table-valued function (dbstat('main')), which
    # only the name-set check below (not just the prefix check) catches.
    #
    # Only functions in a *table source* position count. Applying the name
    # rules to every call also rejected harmless scalars like
    # `SELECT sqlite_version()`, which read no internal table.
    for function in parsed.find_all(exp.Anonymous):
        name = (function.name or "").lower()
        if not (name in internal_names or name.startswith(internal_prefixes)):
            continue
        if _is_table_source(function):
            return True
    return False


def _is_safe_ast(
    sql_string: str,
    *,
    dialect: str,
    internal_prefixes: tuple[str, ...],
    internal_names: frozenset[str],
) -> bool:
    """Reject anything that parses to more than one statement or writes data.

    Returns False when `sqlglot` is unavailable: without a parser there is no
    validation to perform, and refusing to run is the correct failure mode for
    a safety check.
    """
    if sqlglot is None or exp is None:
        return False
    try:
        statements = sqlglot.parse(sql_string, read=dialect)
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

    if _references_internals(
        parsed, internal_prefixes=internal_prefixes, internal_names=internal_names
    ):
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


def is_safe_query(sql_string: str, *, engine: Engine | None = None) -> bool:
    """Conservatively allow only single-statement, read-only SELECT/CTE queries.

    Rejects anything empty, not starting with `SELECT`/`WITH`, parsing to more
    than one statement, referencing the engine's own schema/statistics tables
    (`sqlite_*`/`pragma_*`/`dbstat` for SQLite), or containing a data-modifying
    node. Keyword matching is deliberately *not* used for any of this: a
    raw-text scan rejected legitimate SQL such as `REPLACE(...)` and string
    literals containing words like `update`, and would just as easily have let
    a literal `'sqlite_master'` masquerade as a real table reference in the
    other direction. Every structural rule here is checked against the parsed
    AST instead.

    This is only a partial second line of defence. `execution.py` opens the
    database read-only and installs a write authorizer, which independently
    blocks writes - but the authorizer does not block reads of the engine's
    own schema/statistics tables, so the internals check above is this
    function's alone to get right.

    Args:
        sql_string: The SQL text to validate.
        engine: The engine to validate against - its `sqlglot_dialect` picks
            the parser dialect and its `internal_prefixes`/`internal_names`
            pick the internals blocklist. Defaults to `None`, meaning
            SQLite - resolved from `SQLiteEngine`'s own class attributes, so
            this stays a single source rather than a second, driftable copy
            of its blocklist. Every pre-engine caller and the notebook keep
            working unchanged.

    Returns:
        True if the query is judged safe to execute read-only. False when
        `sqlglot` is unavailable, since no validation is possible.
    """
    if not sql_string or not sql_string.strip():
        return False
    s = sql_string.strip().rstrip(";").strip()
    if not _ALLOWED_PREFIX.match(s):
        return False
    if engine is None:
        # Deferred import, not a module-level one: it keeps `SQLiteEngine`'s
        # own class attributes as the single source for the default, rather
        # than a second, driftable copy of its blocklist living in this
        # module - while still never risking a circular import at module
        # load time, since this only runs inside a call with no engine.
        # Class attribute access instantiates nothing and performs no I/O.
        from .engines.sqlite import SQLiteEngine

        dialect: str = SQLiteEngine.sqlglot_dialect
        internal_prefixes: tuple[str, ...] = SQLiteEngine.internal_prefixes
        internal_names: frozenset[str] = SQLiteEngine.internal_names
    else:
        dialect = engine.sqlglot_dialect
        internal_prefixes = engine.internal_prefixes
        internal_names = engine.internal_names
    return _is_safe_ast(
        s,
        dialect=dialect,
        internal_prefixes=internal_prefixes,
        internal_names=internal_names,
    )
