"""Read-only SQL validation: structural checks plus a sqlglot AST safety check."""

from __future__ import annotations

import re
from collections.abc import Callable
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


def _no_table_names() -> frozenset[str]:
    """The `get_real_table_names` default for `engine=None` (SQLite).

    Never called in practice, since `SQLiteEngine.allowed_functions` is
    `None` and `_references_unknown_table` only runs when it isn't - exists
    so the `engine=None` path has a value of the right type to pass down
    without instantiating `SQLiteEngine` or touching the filesystem.
    """
    return frozenset()


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


# Fix 1 (2026-09-19 review round): DuckDB registers five functions whose
# *second argument* names another function to run at the SQL level rather
# than compute anything itself - `duckdb_functions()`'s own catalogue calls
# that parameter `function_name` for exactly these five:
# `list_aggregate`, `array_aggregate`, `list_aggr`, `array_aggr`,
# `aggregate` (all synonyms of the same scalar function; verified against
# `duckdb_functions()` directly, not guessed). `list_aggregate([...],
# 'histogram')` really does run `histogram` against the list, even though
# `histogram` is correctly rejected everywhere else in this file. Only
# `list_aggregate` is in `DuckDBEngine.allowed_functions` today - the other
# four are already rejected outright by `_references_disallowed_function`
# since no literal spelling of them is in that set - but all five are listed
# here so the dispatch-argument rule below still applies if one of the other
# four is ever added.
_STRING_DISPATCH_FUNCTIONS: frozenset[str] = frozenset(
    {"list_aggregate", "array_aggregate", "list_aggr", "array_aggr", "aggregate"}
)


def _dispatches_to_disallowed_function(
    node: sqlglot_exp.Func, resolved_name: str, *, allowed_functions: frozenset[str]
) -> bool:
    """True if `node` is a string-dispatch call whose target isn't allowed.

    Being a member of `allowed_functions` by its own name is not enough for
    one of `_STRING_DISPATCH_FUNCTIONS`: its second argument must *also* be a
    string literal naming a function that is itself in `allowed_functions`.
    A non-literal second argument (a column, an expression, anything whose
    value is not known until DuckDB evaluates it) is rejected too, since this
    check cannot then know what it would dispatch to - and neither can any
    static check.

    This closes a gap the function-name gate cannot see by construction:
    `list_aggregate` itself resolves to an allowed name and is not rejected
    by `_references_disallowed_function`, but the aggregate it actually runs
    is named by a second, independent argument that gate never inspects.

    Args:
        node: A parsed `exp.Func` node already known to resolve to a name in
            `allowed_functions`.
        resolved_name: `_resolve_function_name(node)`, passed in rather than
            recomputed since the caller already has it.
        allowed_functions: The engine's function allowlist.

    Returns:
        True if `node` is one of `_STRING_DISPATCH_FUNCTIONS` and its
        dispatch argument is missing, not a string literal, or names a
        function not in `allowed_functions`.
    """
    if exp is None:
        return True
    if resolved_name not in _STRING_DISPATCH_FUNCTIONS:
        return False
    args = getattr(node, "expressions", None) or []
    if len(args) < 2:
        return True
    dispatch_arg = args[1]
    if not (isinstance(dispatch_arg, exp.Literal) and dispatch_arg.is_string):
        return True
    target = (dispatch_arg.this or "").lower()
    return target not in allowed_functions


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

    Also rejects a call whose own name is allowed but which dispatches, via a
    second string-literal argument, to a function that is not - see
    `_dispatches_to_disallowed_function`.

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
        resolved = _resolve_function_name(function)
        if resolved not in allowed_functions:
            return True
        if _dispatches_to_disallowed_function(
            function, resolved, allowed_functions=allowed_functions
        ):
            return True
    return False


def _visible_cte_names(table: sqlglot_exp.Table) -> frozenset[str]:
    """The CTE aliases actually visible at `table`'s position, per DuckDB's own scoping.

    Fix 2 (2026-09-19 review round): a CTE is a real SQL scope, not a flat
    namespace over the whole statement. The previous implementation collected
    every `exp.CTE` alias in the parsed tree unscoped, which let an *inner*
    CTE's name leak into an *outer* sibling reference it can never actually
    resolve to - reproduced live against DuckDB:
    `SELECT * FROM (WITH "leak.csv" AS (SELECT 1) SELECT * FROM "leak.csv")
    x, "leak.csv"` - the outer `"leak.csv"` is not the inner query's CTE as
    far as DuckDB is concerned, so DuckDB attempts a replacement scan (a file
    read) on it, while the old unscoped check treated it as a known name and
    approved the query.

    This walks upward from `table` through its ancestors instead, admitting a
    `WITH`'s CTE names only at the points DuckDB itself would resolve them -
    each rule below was verified against a real DuckDB connection, not
    assumed from the SQL standard alone (see `tests/test_engine_duckdb.py`):

    - A CTE is visible in the `WITH`'s own main query, including inside any
      subquery nested in that main query. Modelled by the `else` branch
      below: whenever the walk rises into a node that carries a `with` arg
      from anywhere other than that same `with` arg, every name in it is
      admitted - true at any nesting depth, since the walk keeps climbing.
    - It is NOT visible outside the query that owns the `WITH`. Once the walk
      climbs past that query without re-entering through its `with` arg, that
      `WITH`'s names are never admitted again - which is exactly what makes
      the outer `"leak.csv"` above correctly unrecognised: its ancestor chain
      never passes through the inner `WITH` at all.
    - Within one `WITH` list, a CTE is visible only to CTEs defined *after*
      it, not before. Verified live: `WITH a AS (SELECT * FROM b), b AS
      (SELECT 1) SELECT * FROM a` does not resolve `b` against the second
      CTE - DuckDB attempts a replacement scan on `b` there (the forward
      reference is invisible) - while swapping the definition order so `b`
      comes first resolves it correctly. Modelled by the `exp.CTE` branch
      below, which only admits names at a smaller list index than the CTE
      whose body `table` sits in.
    - A `WITH RECURSIVE` CTE is visible inside its own body - modelled by
      also admitting its own index when the `with` node's `recursive` flag is
      set.
    - Shadowing (an inner CTE reusing an outer CTE's name) needs no special
      case: this function only answers "is this name visible as *some* CTE
      here", never "which one" - both the inner and outer definitions are
      legitimate CTEs, so finding either is correct regardless of which one
      DuckDB would actually bind to.

    Args:
        table: A parsed `exp.Table` node to compute CTE visibility for.

    Returns:
        The lowercased CTE aliases visible at `table`'s position.
    """
    if exp is None:
        return frozenset()
    visible: set[str] = set()
    child: sqlglot_exp.Expression = table
    parent = child.parent
    while parent is not None:
        if isinstance(parent, exp.CTE):
            with_node = parent.parent
            if isinstance(with_node, exp.With):
                ctes = with_node.expressions
                is_recursive = bool(with_node.args.get("recursive"))
                # Identity, not `list.index` (`==`), since `exp.Expression`
                # overrides equality structurally - two CTEs with
                # coincidentally identical bodies must not be confused with
                # each other here.
                own_index = next((i for i, c in enumerate(ctes) if c is parent), -1)
                for i, cte in enumerate(ctes):
                    if i < own_index or (is_recursive and i == own_index):
                        name = (cte.alias or "").lower()
                        if name:
                            visible.add(name)
                child = with_node
                parent = with_node.parent
                continue
        else:
            with_arg = parent.args.get("with")
            if isinstance(with_arg, exp.With) and with_arg is not child:
                for cte in with_arg.expressions:
                    name = (cte.alias or "").lower()
                    if name:
                        visible.add(name)
        child = parent
        parent = parent.parent
    return frozenset(visible)


def _references_unknown_table(
    parsed: sqlglot_exp.Expression, *, get_real_table_names: Callable[[], frozenset[str]]
) -> bool:
    """True if any `FROM`/`JOIN` target is not a real table or a CTE name.

    Default-deny extended from functions to tables (2026-09-19 review
    round): a bare quoted path (`FROM '/etc/passwd'`), a double-quoted one
    (`FROM "data.csv"`), an entirely unquoted one that merely looks
    schema-qualified (`FROM data.csv`, which DuckDB's replacement scan reads
    as a file the same as the other two), and a comma-joined or `JOIN`ed
    string literal (`FROM orders, '/etc/passwd'`) all parse to an
    `exp.Table` whose name is not a real table - none of them name any
    function at all, so `_references_disallowed_function` cannot reach any
    of them, and no per-quote-style token heuristic can either (DuckDB's
    replacement scan treats all three quoting styles identically; verified
    live 2026-09-19 with external access enabled that all three read the
    file). Checking table existence directly is the one rule that covers
    every quoting style, comma joins, and `JOIN`, uniformly.

    A schema-qualified reference is only accepted when the schema is `main`
    - DuckDB's fixed default schema name for every database this engine
    opens, verified via `SELECT current_schema()` - or absent entirely. Any
    other schema (`information_schema`, `pg_catalog`, or anything else) is
    rejected regardless of whether the bare table name happens to match one
    of the user's own tables, which is what makes `information_schema.tables`
    rejected here even though a table literally named `tables` is fine
    unqualified. A catalog-qualified (three-part) reference is rejected
    outright rather than resolved: the engine's own catalog name varies per
    database file and nothing in the LLM's prompt ever teaches a three-part
    name, so there is no legitimate query this could cost.

    A table-valued function call (`FROM range(5)`, `FROM read_csv(...)`,
    `FROM histogram_values(...)`) also parses to an `exp.Table`, but its
    `.this` is an `exp.Func` node rather than a plain identifier - those are
    left entirely to `_references_disallowed_function` instead, which is
    what actually gates them; treating a table-valued function's own name as
    "not a real table" here would reject `range(5)` even though it is
    correctly allowed.

    A CTE alias counts as a real table for this purpose (`WITH totals AS
    (...) SELECT * FROM totals`) only where DuckDB itself would actually
    resolve it to that CTE - see `_visible_cte_names` for the scoping rules
    and the live-DuckDB proof that unscoped collection is wrong.

    `get_real_table_names` is a zero-argument callable rather than an
    already-computed set: it is only invoked once at least one table
    reference has survived the checks above (not a table-valued function,
    not catalog-qualified, not schema-qualified to anything but `main`) and
    still needs a real name to compare against. A query with no `FROM`
    clause at all (`SELECT current_setting('x')`), or one whose only table
    reference is a table-valued function (`FROM range(5)`), never calls it -
    `Engine.table_names()` does at least a cache-key `Path.stat()`, and
    there is no reason to pay even that for a query this function will
    return `False` for regardless.

    Args:
        parsed: The parsed statement.
        get_real_table_names: Returns the engine's own table names,
            lowercased - see `Engine.table_names()`.

    Returns:
        True if the statement must be rejected.
    """
    if exp is None:
        return True
    candidates: list[str] = []
    for table in parsed.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            continue
        if (table.catalog or "").lower():
            return True
        schema = (table.db or "").lower()
        if schema and schema != "main":
            return True
        name = (table.name or "").lower()
        if name in _visible_cte_names(table):
            continue
        candidates.append(name)
    if not candidates:
        return False
    real_table_names = get_real_table_names()
    return any(name not in real_table_names for name in candidates)


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
    get_real_table_names: Callable[[], frozenset[str]],
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
    #
    # The function check runs before the table check deliberately: it never
    # needs `get_real_table_names()` (no I/O), and most rejected queries are
    # rejected on function name alone, so ordering this first means the
    # (cached, but still real) catalogue lookup behind the table check is
    # skipped for every one of those - not just an optimisation, since it is
    # also what lets tests exercise the function gate without needing a real
    # database backing every `FROM` clause they write.
    if allowed_functions is not None:
        if _references_disallowed_function(parsed, allowed_functions=allowed_functions):
            return False
        if _references_unknown_table(parsed, get_real_table_names=get_real_table_names):
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
            default-deny function *and table* validation when it is a set
            rather than `None`, calling `engine.table_names()` only in that
            case. Defaults to `None`, meaning SQLite - resolved from
            `SQLiteEngine`'s own class attributes, so this stays a single
            source rather than a second, driftable copy of its blocklist,
            and performs no I/O: `engine=None` never reads a table list,
            exactly as before this function had one to read.
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
        get_real_table_names: Callable[[], frozenset[str]] = _no_table_names
    else:
        dialect = engine.sqlglot_dialect
        internal_prefixes = engine.internal_prefixes
        internal_names = engine.internal_names
        allowed_functions = engine.allowed_functions
        # A closure, not an eagerly-computed value: `engine.table_names()`
        # reads the schema (cached by `schema.py`'s own fingerprint-keyed
        # cache, so this is a cheap fingerprint check on every call and a
        # real catalogue read only when the schema has actually changed),
        # and `_is_safe_ast`/`_references_unknown_table` only call it at all
        # when default-deny is on for this engine *and* the parsed statement
        # has a real table reference left to check - a query rejected on
        # function name alone, or one with no table reference at all, never
        # triggers it. The attribute itself is only read when
        # `allowed_functions is not None`, too: an engine that opts out of
        # default-deny is not required to implement `table_names()` at all
        # (see `Engine.table_names`'s docstring), and binding the method
        # unconditionally would raise `AttributeError` for one that doesn't,
        # even though it would never actually be called.
        get_real_table_names = (
            engine.table_names if allowed_functions is not None else _no_table_names
        )
    return _is_safe_ast(
        s,
        dialect=dialect,
        internal_prefixes=internal_prefixes,
        internal_names=internal_names,
        allowed_functions=allowed_functions,
        get_real_table_names=get_real_table_names,
    )
