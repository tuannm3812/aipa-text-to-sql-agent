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

# sqlglot parses many function calls into typed classes (`exp.Count`,
# `exp.Upper`, `exp.TimestampTrunc`, ...) rather than leaving them as
# `exp.Anonymous`. Each typed class's own `.sql_name()` is sqlglot's
# *cross-dialect* canonical token, not necessarily the name DuckDB itself
# registers the function under - `date_trunc(...)` parses to `TimestampTrunc`,
# whose `.sql_name()` is `TIMESTAMP_TRUNC`, a name DuckDB does not recognise as
# a function at all. Comparing that token against an allowlist of *real*
# DuckDB names would either reject every legitimate call through that class or
# require the allowlist to carry sqlglot's internal vocabulary instead of
# DuckDB's own, which is what a reviewer actually needs to audit.
#
# This table maps each such class to one real, DuckDB-registered name for the
# function family it represents, chosen from `duckdb_functions()`'s own alias
# list for that class (verified 2026-09-19 by sweeping every scalar/
# aggregate/table/table_macro name DuckDB registers through sqlglot's DuckDB
# parser and grouping the results by resulting class - see
# `tests/test_engine_duckdb.py` for the round-trip proof this produces the
# right token for every entry in `DuckDBEngine.allowed_functions`). Only
# classes whose `.sql_name()` actually diverges from the chosen name are
# listed; every other typed class's `.sql_name()` lowered already equals it
# (`Upper` -> `upper`, `Count` -> `count`, `Round` -> `round`, ...), so no
# entry is needed there.
#
# A single class often represents several literal spellings DuckDB treats as
# synonyms (`lpad`/`rpad` both parse to `Pad`; `var_samp`/`variance` both
# parse to `Variance`; `string_agg`/`group_concat`/`listagg` all parse to
# `GroupConcat`). Resolving all of them to one canonical token is deliberate:
# the allowlist only needs to contain that one token, not every alias, and a
# caller who writes any alias gets the same, correctly-gated behaviour.
_FUNCTION_NAME_OVERRIDES: dict[type[sqlglot_exp.Expression], str] = {}
if exp is not None:
    _FUNCTION_NAME_OVERRIDES = {
        exp.TimestampTrunc: "date_trunc",
        exp.TimeToStr: "strftime",
        exp.StrToTime: "strptime",
        exp.GroupConcat: "string_agg",
        exp.DateDiff: "date_diff",
        exp.SortArray: "list_sort",
        exp.ArrayContains: "list_contains",
        exp.Pad: "lpad",
        exp.StrPosition: "position",
        exp.RegexpLike: "regexp_matches",
        exp.UnixToTime: "epoch_ms",
        exp.TimeToUnix: "epoch",
        exp.Array: "list_value",
        exp.VariancePop: "var_pop",
        exp.DayOfWeekIso: "isodow",
        exp.DateFromParts: "make_date",
        exp.PercentileCont: "quantile_cont",
        exp.PercentileDisc: "quantile_disc",
        exp.ApproxDistinct: "approx_count_distinct",
        exp.LogicalAnd: "bool_and",
        exp.LogicalOr: "bool_or",
        exp.Split: "string_split",
        exp.DayOfWeek: "dayofweek",
        exp.DayOfYear: "dayofyear",
        exp.JSONExtractScalar: "json_extract_string",
        # `unnest(...)` parses to `exp.Unnest` in a table (`FROM`) position
        # but to `exp.Explode` in a select-list/scalar position (DuckDB
        # supports both - `SELECT unnest(tags) FROM t` expands the list
        # in place, without a `FROM unnest(...)` at all). Verified 2026-09-19
        # that `Explode` has no other DuckDB name mapped to it.
        exp.Explode: "unnest",
    }


def _resolve_function_name(node: sqlglot_exp.Func) -> str:
    """Resolve a parsed function-call node to the name DuckDB itself calls it by.

    `exp.Anonymous` (sqlglot's catch-all for a function name it has no
    dedicated class for) is name-transparent by construction: `.name` is
    exactly the identifier the caller typed, lowered for a case-insensitive
    compare, with no reinterpretation in between. Every function this task's
    brief proved leaked through the old blocklist - `current_setting`,
    `query`, `query_table`, `histogram_values`, `checkpoint`, ... - parses as
    `exp.Anonymous` (verified 2026-09-19), so this is also the path that
    carries every dangerous name through untouched rather than through a
    class-based remapping that could coincidentally land on an allowed token.

    A typed class instead goes through `_FUNCTION_NAME_OVERRIDES` where its
    `.sql_name()` would otherwise diverge from DuckDB's own name, and falls
    back to `.sql_name()` lowered everywhere else.

    Args:
        node: A parsed `exp.Func` node.

    Returns:
        The lowercased name to compare against `Engine.allowed_functions`.
        Empty string if `sqlglot` is unavailable.
    """
    if exp is None:
        return ""
    if isinstance(node, exp.Anonymous):
        return (node.name or "").lower()
    override = _FUNCTION_NAME_OVERRIDES.get(type(node))
    if override is not None:
        return override
    # sqlglot ships its own type annotations, but `Func.sql_name` is one of
    # the methods it leaves untyped - mypy's strict `no-untyped-call` fires
    # here specifically because `node` is statically typed as
    # `sqlglot_exp.Func` (needed above for `isinstance`/dict-key correctness)
    # rather than reached through the module-level `exp: ModuleType | None`
    # escape hatch the rest of this file uses, whose `ModuleType.__getattr__
    # -> Any` is what lets every other `sqlglot` call through untyped.
    return node.sql_name().lower()  # type: ignore[no-any-return, no-untyped-call]


def _references_disallowed_function(
    parsed: sqlglot_exp.Expression, *, allowed_functions: frozenset[str]
) -> bool:
    """True if any function call anywhere in `parsed` is not in `allowed_functions`.

    Unlike `_references_internals`, this has no table-source restriction: it
    walks every function call regardless of position - scalar, aggregate,
    window or table. That is the point. `current_setting('secret_directory')`
    is a **scalar**; the internals check above deliberately fires only in a
    table-source position (so `SELECT sqlite_version()` stays legal), which
    means no name added to a blocklist can ever reach a function used as a
    value. Default-deny closes that gap structurally: an unrecognised name is
    rejected regardless of where it appears, rather than relying on anyone
    having thought to list it.

    Args:
        parsed: The parsed statement.
        allowed_functions: The engine's function allowlist. Only called when
            this is not `None` - see `_is_safe_ast`.

    Returns:
        True if the statement must be rejected.
    """
    if exp is None:
        return True
    for function in parsed.find_all(exp.Func):
        if _resolve_function_name(function) not in allowed_functions:
            return True
    return False


def _has_string_literal_table_source(sql_string: str, *, dialect: str) -> bool:
    """True if a `FROM`/`JOIN` target is a quoted string literal, not a name.

    DuckDB accepts a bare quoted path as a table source - `FROM
    '/etc/passwd'` - with no function name involved at all, which is exactly
    why `_references_disallowed_function` alone cannot catch it. The parsed
    AST cannot either: sqlglot folds *both* a double-quoted identifier
    (`FROM "t"`) and a single-quoted string used as a table source (`FROM
    '/etc/passwd'`) into the same `exp.Identifier(quoted=True)` node, so
    `table.this` carries no trace of which quote character was used. The
    tokenizer keeps them apart - `IDENTIFIER` for the former, `STRING` for the
    latter - so this checks the token stream directly instead of re-deriving
    the distinction from a parse tree that has already erased it.

    Only the token immediately after `FROM`/`JOIN` is checked, which is
    narrow by design: a function call's argument list always has the
    function's own identifier token in that position (`FROM
    json_each('[1]')` sees `VAR` there, not `STRING`), and old-style comma
    joins (`FROM a, 'x'`) are not covered - the connection's
    `enable_external_access=False` is what's actually load-bearing against a
    bare path table source; this only makes the validator agree with it
    rather than relying on that second layer alone.

    Args:
        sql_string: The SQL text to check (already validated to parse).
        dialect: The sqlglot dialect to tokenize under.

    Returns:
        True if a string literal sits directly after `FROM` or `JOIN`.
    """
    if sqlglot is None:
        return False
    from sqlglot.tokens import TokenType

    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql_string)
    except Exception:
        return False
    for i, token in enumerate(tokens[:-1]):
        if (
            token.token_type in (TokenType.FROM, TokenType.JOIN)
            and tokens[i + 1].token_type == TokenType.STRING
        ):
            return True
    return False


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
        # A schema-qualified reference such as `information_schema.tables` or
        # `pg_catalog.pg_tables` has its internal marker in the schema
        # qualifier (`.db`), not the bare table name - `.name` alone is
        # "tables", which is not itself internal and must not be rejected
        # when unqualified (a table genuinely named "tables" is fine).
        # Probed 2026-09-18 against DuckDB: `SELECT * FROM
        # information_schema.tables` passed `is_safe_query` before this
        # qualifier check was added, because only `.name` was checked.
        schema_qualifier = (table.db or "").lower()
        if schema_qualifier and (
            schema_qualifier in internal_names or schema_qualifier.startswith(internal_prefixes)
        ):
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
    #
    # Not every table-valued function parses as `exp.Anonymous`: sqlglot gives
    # some DuckDB functions (`read_csv`, `read_parquet`, ...) their own
    # dedicated expression classes, where `.name` reads the first argument
    # (the file path) rather than the function name. `.sql_name()` is what
    # those classes expose the canonical function name through instead.
    # Probed 2026-09-18: `SELECT * FROM read_csv('/etc/hosts')` passed
    # `is_safe_query` before this branch existed, because `exp.Anonymous`
    # alone never matched sqlglot's `ReadCSV` node.
    for function in parsed.find_all(exp.Func):
        if isinstance(function, exp.Anonymous):
            name = (function.name or "").lower()
        else:
            name = function.sql_name().lower()
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
    allowed_functions: frozenset[str] | None,
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

    # Default-deny, additional to the internals check above rather than a
    # replacement for it: `allowed_functions` is only non-`None` for an
    # engine that opted into it (DuckDB). `None` (SQLite) skips both of these
    # entirely, so SQLite's behaviour is unchanged by construction, not just
    # by test coverage.
    if allowed_functions is not None:
        if _has_string_literal_table_source(sql_string, dialect=dialect):
            return False
        if _references_disallowed_function(parsed, allowed_functions=allowed_functions):
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
            the parser dialect, its `internal_prefixes`/`internal_names` pick
            the internals blocklist, and its `allowed_functions` switches on
            default-deny function validation when it is a set rather than
            `None`. Defaults to `None`, meaning SQLite - resolved from
            `SQLiteEngine`'s own class attributes, so this stays a single
            source rather than a second, driftable copy of its blocklist.
            Every pre-engine caller and the notebook keep working unchanged.

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
        allowed_functions: frozenset[str] | None = SQLiteEngine.allowed_functions
    else:
        dialect = engine.sqlglot_dialect
        internal_prefixes = engine.internal_prefixes
        internal_names = engine.internal_names
        allowed_functions = engine.allowed_functions
    return _is_safe_ast(
        s,
        dialect=dialect,
        internal_prefixes=internal_prefixes,
        internal_names=internal_names,
        allowed_functions=allowed_functions,
    )
