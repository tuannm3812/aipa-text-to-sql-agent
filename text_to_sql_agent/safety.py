"""Read-only SQL validation: structural checks plus a sqlglot AST safety check."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
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
    """The `get_real_table_names` default for SQLite.

    Never called in practice, since `SQLiteEngine.allowed_functions` is
    `None` and `_references_unknown_table` only runs when it isn't - exists
    so the `engine=None` path has a value of the right type to pass down
    without instantiating `SQLiteEngine` or touching the filesystem.
    """
    return frozenset()


def _no_table_columns() -> Mapping[str, frozenset[str]]:
    """The `get_real_table_columns` default for SQLite.

    The column catalogue's counterpart to `_no_table_names`, and a separate
    function only because its type is now different: since the 2026-09-26
    scoping fix the column check resolves a qualifier against *one table's*
    columns, so it receives a table-keyed mapping rather than a flat set (see
    `_references_unresolvable_qualified_column`).
    """
    return {}


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
#
# This table is shared by every engine, not keyed per-dialect: a class such
# as `exp.GroupConcat` maps to the same `"string_agg"` token whether the
# statement was parsed under the `duckdb` or `postgres` dialect, because both
# engines register a real function under that name. `PostgresEngine.
# allowed_functions` (Task 4, 2026-09-26) reuses several of these entries
# unchanged for exactly that reason and adds exactly one PostgreSQL-only
# entry - `exp.ExplodingGenerateSeries` - documented at that entry.
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
        # PostgreSQL-only entry (Task 4, 2026-09-26): sqlglot's `postgres`
        # dialect parses *every* `generate_series(...)` call - table position
        # or scalar/select-list position alike - into `exp.
        # ExplodingGenerateSeries`, never the plain `exp.GenerateSeries` node
        # its own `.sql_name()` would resolve to `"generate_series"` for
        # unaided. Verified 2026-09-26: `SELECT * FROM generate_series(1,5)`,
        # `SELECT generate_series(1,5)` and a table-aliased form all produce
        # this one class under `read="postgres"`, whose own `.sql_name()` is
        # `EXPLODING_GENERATE_SERIES` - a token PostgreSQL does not register
        # any function under. Mapped to `"generate_series"`, PostgreSQL's own
        # real name and already `PostgresEngine.allowed_functions`'s only
        # table-function entry, rather than adding a second, misleading token
        # to that allowlist for the same function.
        exp.ExplodingGenerateSeries: "generate_series",
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


# False-rejection fix (2026-09-26): sqlglot models `AND`, `OR` and `EXISTS`
# as `exp.Func` subclasses (`exp.And`, `exp.Or`, `exp.Exists`) purely so its
# own AST has one base class to hang `Binary`/`SubqueryPredicate` behaviour
# off of - not because PostgreSQL or DuckDB treat them as catalogue
# functions. Before this fix, `_references_disallowed_function`'s
# `find_all(exp.Func)` walk reached every one of them and rejected the
# statement outright, since `"and"`/`"or"`/`"exists"` are in neither engine's
# `allowed_functions`: any query with two conditions joined by `AND`, or an
# `OR`, or an `EXISTS (...)` subquery anywhere in it - which is most real
# analytical SQL - was refused with `BLOCKED_UNSAFE_SQL`.
#
# The fix is structural (by node *type*), not by adding `"and"`/`"or"`/
# `"exists"` as three more literal strings to two allowlists. An allowlist
# entry means "this name may dispatch to a callable the engine looks up in
# its function catalogue" - that is what a reviewer reading
# `DuckDBEngine.allowed_functions`/`PostgresEngine.allowed_functions` is
# entitled to assume every entry means, and it is what makes those lists
# auditable. `AND`/`OR`/`EXISTS` do not fit that meaning at all: they are
# reserved-keyword grammar, not identifiers a caller writes and a catalogue
# resolves.
#
# Each of the three was checked to confirm it truly cannot reach a callable
# dispatch, not merely assumed:
#
#   - `exp.And`/`exp.Or`: only ever produced by parsing the infix keywords
#     `AND`/`OR` between two boolean expressions. `AND`/`OR` are reserved
#     words in both dialects' grammars, so there is no spelling
#     (`and(a, b)`, a schema-qualified `pg_catalog.and(...)`, a quoted
#     `"and"(...)`) that parses as a function call instead - verified
#     2026-09-26: `sqlglot.parse_one("SELECT and(true, false)", read=<either
#     dialect>)` raises `ParseError` ("Required keyword: 'this' missing"),
#     because the parser commits to the infix-operator production the moment
#     it sees the `AND` token and then has nowhere to put a following `(`.
#     There is no catalogue entry either grammar could route a call to even
#     if one were somehow written.
#   - `exp.Exists`: only ever produced by the `EXISTS ( ... )` predicate
#     grammar, which requires the parenthesised argument to immediately
#     follow the reserved word `EXISTS` and produces this node whether or
#     not there is a preceding qualifier - `SELECT exists(true)` and
#     `WHERE EXISTS (SELECT 1 ...)` both parse to `exp.Exists` (verified
#     2026-09-26, both dialects). Nothing about that grammar production ever
#     looks a name up in a function catalogue; `EXISTS` cannot be
#     schema-qualified, aliased, or shadowed by a user-defined function the
#     way an ordinary call name can.
#
#   `exp.Xor` was considered and deliberately *not* added here, as the
#   counter-example that proves the other three are not being exempted
#   merely because they are `Connector`/`SubqueryPredicate` subclasses.
#   Under the `postgres` dialect, `xor(true, false)` - real parenthesised
#   call syntax, not an infix keyword - parses to `exp.Xor` (verified
#   2026-09-26); DuckDB instead resolves that same spelling to
#   `exp.BitwiseXor`, a distinct, non-exempt class. Because `exp.Xor` *is*
#   reachable through ordinary call syntax under PostgreSQL, it stays
#   subject to the same default-deny walk as every other `exp.Func` - it is
#   simply never in `allowed_functions` today, which is a correct rejection
#   of an obscure function no business question over this schema needs, not
#   a false one.
#
# This lives here, in shared `safety.py`, rather than as three more entries
# in `DuckDBEngine.allowed_functions` and `PostgresEngine.allowed_functions`:
# every current and future default-deny engine benefits from one exemption
# list keyed on sqlglot's own AST shape, instead of every engine author
# needing to remember to re-add the same three non-functions by name.
_PURE_SYNTAX_FUNC_TYPES: tuple[type[sqlglot_exp.Expression], ...] = ()
if exp is not None:
    _PURE_SYNTAX_FUNC_TYPES = (exp.And, exp.Or, exp.Exists)


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

    `exp.Func` nodes in `_PURE_SYNTAX_FUNC_TYPES` (`AND`/`OR`/`EXISTS`) are
    skipped entirely rather than name-checked - see that constant's comment
    for why each one is pure grammar with no catalogue dispatch to gate.
    This only skips the check for the node itself; every descendant is still
    reached by this same `find_all(exp.Func)` walk, so
    `SELECT x FROM t WHERE a = 1 AND current_setting('x') = 'y'` is still
    rejected on `current_setting` regardless of the `AND` wrapping it.

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
        if isinstance(function, _PURE_SYNTAX_FUNC_TYPES):
            continue
        resolved = _resolve_function_name(function)
        if resolved not in allowed_functions:
            return True
        if _dispatches_to_disallowed_function(
            function, resolved, allowed_functions=allowed_functions
        ):
            return True
    return False


# Task 4 bypass 1 (2026-09-26 review round): PostgreSQL's grammar treats
# `(expr).name` as sugar for `name(expr)` - a single-argument function call -
# whenever `expr`'s type has no field called `name`. sqlglot parses this
# postfix form into `exp.Dot(this=<expr>, expression=exp.Identifier(name))`,
# never into an `exp.Func` subclass, so `_references_disallowed_function`'s
# `parsed.find_all(exp.Func)` walk cannot see it - confirmed live:
# `SELECT ('port').current_setting` parsed and re-validated as safe before
# this fix, and the same shape reaches every single-argument function in the
# catalogue (`pg_read_file`, `pg_relation_filepath`, chained/nested/quoted
# variants - see `tests/test_engine_postgres.py`).
#
# A dot-call that supplies its own parenthesised arguments
# (`('a,b').split_part(',', 1)`) parses `.expression` as `exp.Anonymous`
# instead - itself an `exp.Func` subclass `_references_disallowed_function`
# already walks (verified 2026-09-26) - so only the bare-identifier form (no
# trailing parens at all) is invisible to that check and needs a dedicated
# one here.
#
# This is deliberately gated on `dialect`, not applied to every engine that
# opts into default-deny: DuckDB parses the identical `exp.Dot` shape for its
# own, unrelated struct/map/JSON field-extraction syntax
# (`(struct_col).field_name`), which is legitimate and already exercised by
# `test_engine_duckdb.py`'s analytics corpus. Verified directly against a
# live DuckDB connection (2026-09-26) that DuckDB has no reading under which
# `(expr).name` ever means "call the function named `name`":
# `SELECT ('hello').upper` raises `Binder Error: Cannot extract field
# 'upper' from expression "'hello'" because it is not a struct, union, map,
# or json` - DuckDB never falls back to a function-call interpretation the
# way PostgreSQL does. A struct field name is arbitrary user data, not drawn
# from `allowed_functions`, so blanket-applying this check to DuckDB would
# reject nearly every real struct access rather than close a bypass that
# does not exist there. Restricting it to dialects where the dual reading is
# real is what keeps DuckDB's legitimate queries validating while closing
# the actual PostgreSQL gap.
#
# Task 4 bypass 3 (2026-09-26 review round, second fix): this constant now
# gates *three* rules, not one - `_references_disallowed_dot_call` above plus
# `_references_internal_column_name` and
# `_references_unresolvable_qualified_column` below, which handle the
# parenthesis-free spelling of the same sugar (`alias.name`). All three exist
# only because PostgreSQL reads dotted notation as a possible function call;
# all three must therefore be enabled and disabled together, for exactly the
# reasons spelled out above. `tests/test_safety.py::
# test_dot_call_dialects_contains_the_postgres_engines_own_dialect` pins this
# set against `PostgresEngine.sqlglot_dialect` so renaming that attribute's
# value cannot silently turn all three rules into no-ops with every other
# test still green.
_DOT_CALL_DIALECTS: frozenset[str] = frozenset({"postgres"})


def _references_disallowed_dot_call(
    parsed: sqlglot_exp.Expression,
    *,
    dialect: str,
    allowed_functions: frozenset[str],
    internal_prefixes: tuple[str, ...],
    internal_names: frozenset[str],
) -> bool:
    """True if a PostgreSQL `(expr).name` dot-call resolves to a disallowed name.

    Only runs for `dialect` in `_DOT_CALL_DIALECTS` - see that constant for
    why DuckDB's structurally identical `exp.Dot` nodes must not be checked
    the same way.

    Checked against both `allowed_functions` (the same default-deny gate
    `_references_disallowed_function` applies to an ordinary call) and
    `internal_prefixes`/`internal_names` (the same name rule
    `_references_internals` applies) - belt and suspenders: every `pg_`-
    prefixed name is already absent from `allowed_functions`, so the two
    checks agree today, but a future internals name that is ever
    accidentally allowlisted should still be caught here independently,
    the same way `is_safe_query`'s docstring insists neither of its two
    defences alone is sufficient.

    Args:
        parsed: The parsed statement.
        dialect: The dialect the statement was parsed under.
        allowed_functions: The engine's function allowlist.
        internal_prefixes: The engine's internal table/function name prefixes.
        internal_names: The engine's internal table/function names.

    Returns:
        True if the statement must be rejected.
    """
    if exp is None:
        return True
    if dialect not in _DOT_CALL_DIALECTS:
        return False
    for dot in parsed.find_all(exp.Dot):
        rhs = dot.expression
        if not isinstance(rhs, exp.Identifier):
            # `.expression` is an `exp.Func` subclass (own parenthesised
            # arguments were supplied) - `_references_disallowed_function`
            # already walks it via `find_all(exp.Func)`.
            continue
        name = (rhs.name or "").lower()
        if name in internal_names or name.startswith(internal_prefixes):
            return True
        if name not in allowed_functions:
            return True
    return False


# Task 4 bypass 3 (2026-09-26 review round, second fix): PostgreSQL's
# function-call sugar does not need the parentheses `_references_disallowed_
# dot_call` above keys on. `alias.name`, where `name` is not a column of
# `alias`, is *itself* a call of `name(alias)` - and sqlglot parses that into
# `exp.Column(this=Identifier(name), table=Identifier(alias))`. Not an
# `exp.Func`, so `_references_disallowed_function` cannot see it; not an
# `exp.Dot`, so `_references_disallowed_dot_call` cannot either; not an
# `exp.Table`, so `_references_internals` cannot. It was invisible to every
# gate in this file. Reproduced live as `aipa_ro`, each approved by
# `is_safe_query` before this fix:
#
#   SELECT g.pg_relation_filepath FROM generate_series(16384,16400) g
#       -> 'base/16384/16387' - on-disk relation paths
#   SELECT g.pg_get_indexdef FROM generate_series(16384,16500) g
#       -> 'CREATE UNIQUE INDEX customers_pkey ON public...'
#   SELECT c.pg_column_size FROM customers c          -> 34, 32
#   SELECT g.pg_sleep FROM generate_series(1,2) g     -> 3.0s elapsed
#
# and, proven by the reviewer and deliberately not re-run here,
# `SELECT g.pg_terminate_backend FROM generate_series(<pid>,<pid>) g`, which
# killed a live backend.
#
# The previous fix missed this because it verified that `t.col` parses to
# `exp.Column` and is therefore not *falsely rejected*. It never asked
# whether an `exp.Column` can *be* a call. It can.
#
# The reachable surface is narrower than an ordinary call's, and this was
# confirmed rather than assumed: the implicit argument is the range-table
# entry's own type, so only single-argument functions accepting that type are
# reachable - a real table's whole-row `record`, or a function scan's scalar.
# For the allowlisted `generate_series` that scalar is `integer`, implicitly
# coercible to `oid`/`regclass`/`regrole`/`regproc`/`bigint`, which is what
# puts the whole OID-taking catalogue surface plus `pg_sleep`,
# `pg_terminate_backend`, `pg_cancel_backend` and `pg_advisory_lock` in
# reach. `text`-taking functions (`pg_read_file`, `current_setting`) are not
# reachable this way.
#
# Two rules below close it, in that order:
#
#   1. `_references_internal_column_name` - the engine's own
#      `internal_prefixes`/`internal_names` rule, applied to an `exp.Column`'s
#      own name. Covers every case proven above and needs no catalogue read.
#   2. `_references_unresolvable_qualified_column` - full default-deny: a
#      *qualified* column's name must resolve to something real.
#
# Rule 1 is kept even though rule 2 subsumes it for every schema seen so far:
# rule 2 depends on a catalogue read that rule 1 does not, and this file's
# standing principle is that two independent defences are not redundant (see
# `is_safe_query`'s docstring).


def _references_internal_column_name(
    parsed: sqlglot_exp.Expression,
    *,
    dialect: str,
    internal_prefixes: tuple[str, ...],
    internal_names: frozenset[str],
) -> bool:
    """True if any column reference's own name is an engine-internal name.

    Part 1 of the bypass-3 fix documented above. Only runs for `dialect` in
    `_DOT_CALL_DIALECTS`, because only there can a column reference also be a
    function call.

    This is a *name* rule, and it costs a real user column actually named
    `pg_something`: such a column would be refused even in a perfectly
    ordinary `SELECT c.pg_notes FROM customers c`. That is the accepted trade
    here, and it is the same trade `is_safe_query` already makes elsewhere -
    `_references_internals` refuses a table named `pg_anything` on its name
    alone, whether it is the table's own name or its schema qualifier, and
    does so before any table list is consulted. A
    `pg_`-prefixed user column is vanishingly rare (PostgreSQL's own
    documentation reserves the prefix), and the cost is one unanswerable
    question against a proven remote-code-adjacent leak.

    Unqualified columns are checked too, not just qualified ones. The sugar
    needs a qualifier, so an unqualified `pg_x` cannot be a call - but a bare
    `pg_`-named column is equally suspect and nothing legitimate is lost by
    refusing both, whereas restricting this rule to qualified columns would
    invite exactly the "which positions did we remember?" reasoning that let
    this bypass through in the first place.

    Args:
        parsed: The parsed statement.
        dialect: The dialect the statement was parsed under.
        internal_prefixes: The engine's internal name prefixes.
        internal_names: The engine's internal names.

    Returns:
        True if the statement must be rejected.
    """
    if exp is None:
        return True
    if dialect not in _DOT_CALL_DIALECTS:
        return False
    for column in parsed.find_all(exp.Column):
        name = (column.name or "").lower()
        if not name:
            continue
        if name in internal_names or name.startswith(internal_prefixes):
            return True
    return False


def _query_bound_names(parsed: sqlglot_exp.Expression) -> frozenset[str]:
    """Every output name the statement binds for itself, lowercased.

    These are the names a qualified column reference can legitimately resolve
    to without appearing in the engine's catalogue at all - the second half of
    `_references_unresolvable_qualified_column`'s "resolves to something real"
    test. Three sources, each verified against live PostgreSQL:

    - `exp.Alias` - every `AS x`, which is what a derived table's or CTE's
      computed output column is named by (`SELECT t.total FROM (SELECT
      SUM(amount) AS total FROM sales) t`).
    - `exp.TableAlias`'s `columns` - an explicit column alias list, on a
      derived table, a CTE (`WITH t(x, y) AS (...)`) or a function scan
      (`generate_series(1,5) AS g(n)`).
    - The resolved name of any table-valued function - a function scan's
      single output column is named after the function itself, so `SELECT
      g.generate_series FROM generate_series(1,5) g` is legal PostgreSQL.
      Admitting these names is safe rather than circular: this function is
      only ever consulted *after* `_references_disallowed_function` has
      already rejected the statement if any function name in it is not in
      `allowed_functions`, so every name this branch contributes is an
      allowlisted one.

    Collected across the whole statement rather than per scope. A name bound
    in one scope and referenced in another would already fail to execute, so
    the looser set costs nothing in safety terms: the question this answers is
    only "could this name plausibly be a column rather than a function", and
    a name the query itself introduces is never a catalogue function name.

    Args:
        parsed: The parsed statement.

    Returns:
        The lowercased names the statement binds.
    """
    if exp is None:
        return frozenset()
    names: set[str] = set()
    for alias in parsed.find_all(exp.Alias):
        name = (alias.alias or "").lower()
        if name:
            names.add(name)
    for table_alias in parsed.find_all(exp.TableAlias):
        for column in table_alias.args.get("columns") or []:
            name = (column.name or "").lower()
            if name:
                names.add(name)
    for table in parsed.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            name = _resolve_function_name(table.this)
            if name:
                names.add(name)
    return frozenset(names)


def _alias_name(relation: sqlglot_exp.Expression) -> str:
    """The alias a relation is bound under in its `FROM`/`JOIN`, lowercased.

    Args:
        relation: A node in a table-source position.

    Returns:
        The alias, or `""` when the relation carries none.
    """
    if exp is None:
        return ""
    alias = relation.args.get("alias")
    if isinstance(alias, exp.TableAlias):
        return (alias.name or "").lower()
    return ""


def _alias_columns(relation: sqlglot_exp.Expression) -> frozenset[str]:
    """The explicit column alias list a relation carries, lowercased.

    `generate_series(1, 5) AS g(n)`, `WITH t(x, y) AS (...)` and
    `(VALUES (1)) AS v(x)` all rename their output columns this way, and all
    three parse to a `TableAlias` carrying a `columns` arg.

    Args:
        relation: A node in a table-source position.

    Returns:
        The lowercased alias-list column names; empty when there is no list.
    """
    if exp is None:
        return frozenset()
    alias = relation.args.get("alias")
    if not isinstance(alias, exp.TableAlias):
        return frozenset()
    names = {(column.name or "").lower() for column in (alias.args.get("columns") or [])}
    names.discard("")
    return frozenset(names)


def _scope_relations(scope: sqlglot_exp.Expression) -> list[sqlglot_exp.Expression]:
    """The relations `scope`'s own `FROM`/`JOIN`s bind - not a nested query's.

    One SQL scope's worth, deliberately: `_qualifier_binding` resolves a
    qualifier against the *nearest enclosing* scope that binds it, which is
    what stops the same alias, reused for a different relation in a different
    scope, from resolving to the wrong one. A comma join arrives in `joins`
    exactly like an explicit `JOIN` does, and a `LATERAL` arrives either as a
    `Join` wrapping an `exp.Lateral` or in the `laterals` arg depending on the
    spelling, so all three are collected here.

    Args:
        scope: A parsed node, normally an `exp.Select`.

    Returns:
        The relation nodes this scope binds directly.
    """
    if exp is None:
        return []
    relations: list[sqlglot_exp.Expression] = []
    from_arg = scope.args.get("from")
    if isinstance(from_arg, exp.From) and isinstance(from_arg.this, exp.Expression):
        relations.append(from_arg.this)
    for join in scope.args.get("joins") or []:
        target = join.this if isinstance(join, exp.Join) else None
        if isinstance(target, exp.Expression):
            relations.append(target)
    for lateral in scope.args.get("laterals") or []:
        if isinstance(lateral, exp.Expression):
            relations.append(lateral)
    return relations


def _relation_binding(
    relation: sqlglot_exp.Expression, *, real_table_columns: Mapping[str, frozenset[str]]
) -> tuple[frozenset[str], frozenset[str] | None]:
    """What one `FROM`/`JOIN` relation binds: its qualifiers, and its columns.

    The second element is the set of names a qualified reference through this
    relation may legitimately resolve to, or `None` for a relation whose
    output columns this module deliberately does not compute - see
    `_references_unresolvable_qualified_column` for which kinds those are and
    why resolving them exactly is not attempted.

    Args:
        relation: A node in a table-source position.
        real_table_columns: The engine's per-table column map, keyed by every
            spelling `Engine.table_names()` advertises.

    Returns:
        `(qualifiers, names)` - the spellings a column reference may use to
        qualify against this relation, and the names it may then resolve to
        (`None` meaning "resolve permissively").
    """
    if exp is None:
        return frozenset(), None
    alias = _alias_name(relation)
    alias_columns = _alias_columns(relation)

    # A function scan: `FROM generate_series(1, 5) g`, or the same thing
    # spelled `JOIN LATERAL generate_series(...) g`. This is the relation kind
    # the whole bypass runs through, and the one that must stay strict: its
    # only output column is named after the function itself (plus whatever an
    # explicit alias list renames it to), so a catalogue function reached as
    # `g.lo_get` resolves to nothing here however many real columns elsewhere
    # in the database happen to share that name.
    inner = relation.this
    if isinstance(relation, (exp.Table, exp.Lateral)) and isinstance(inner, exp.Func):
        function_name = _resolve_function_name(inner)
        names = alias_columns | ({function_name} if function_name else set())
        if alias:
            return frozenset({alias}), frozenset(names)
        # Unaliased, so PostgreSQL qualifies it by the function's own name:
        # `SELECT generate_series.generate_series FROM generate_series(1, 5)`.
        return frozenset({function_name} if function_name else set()), frozenset(names)

    if isinstance(relation, exp.Table):
        name = (relation.name or "").lower()
        schema = (relation.db or "").lower()
        # An unaliased table answers to its bare name and, on PostgreSQL, to
        # its schema-qualified one too; an aliased one answers only to the
        # alias, which is what PostgreSQL itself enforces.
        qualifiers = {alias} if alias else ({name} | ({f"{schema}.{name}"} if schema else set()))
        if not schema and name in _visible_cte_names(relation):
            # A CTE reference. Its output columns are the inner query's, which
            # this module does not compute - permissive.
            return frozenset(qualifiers), None
        columns = real_table_columns.get(f"{schema}.{name}" if schema else name)
        if columns is None:
            # Not a table this engine advertises. `_references_unknown_table`
            # has already rejected the statement in that case, so this is
            # unreachable in practice; permissive rather than strict so that a
            # future caller order cannot turn it into a silent false rejection.
            return frozenset(qualifiers), None
        return frozenset(qualifiers), frozenset(columns | alias_columns)

    # A derived table, a `VALUES` list, a `LATERAL` over a subquery, or
    # anything else that binds an alias - permissive, for the reasons in
    # `_references_unresolvable_qualified_column`'s docstring.
    return frozenset({alias} if alias else set()), None


def _qualifier_keys(column: sqlglot_exp.Column) -> tuple[str, ...]:
    """The spellings a qualified column's qualifier could be bound under.

    `analytics.thing.label` yields `("analytics.thing", "thing")`: the second
    is what resolves it when the statement spells the same table bare in its
    `FROM` (`SELECT public.customers.name FROM customers` is legal
    PostgreSQL).

    Args:
        column: A qualified `exp.Column`.

    Returns:
        The candidate qualifier keys, most specific first.
    """
    if exp is None:
        return ()
    table = (column.table or "").lower()
    if not table:
        return ()
    db_arg = column.args.get("db")
    schema = (db_arg.name or "").lower() if isinstance(db_arg, exp.Identifier) else ""
    return (f"{schema}.{table}", table) if schema else (table,)


def _qualifier_binding(
    column: sqlglot_exp.Column, *, real_table_columns: Mapping[str, frozenset[str]]
) -> frozenset[str] | None:
    """The names `column`'s qualifier binds, or `None` to resolve permissively.

    Walks outward from the column through its ancestors and stops at the
    first scope that binds the qualifier - which is both how PostgreSQL
    resolves it and what makes a correlated reference (`c.customer_id` inside
    a subquery, bound by the outer query's `FROM`) resolve at all.

    Args:
        column: A qualified `exp.Column`.
        real_table_columns: The engine's per-table column map.

    Returns:
        The bound names, or `None` when the qualifier is bound by a relation
        kind this module does not resolve, or is not bound in the statement at
        all.
    """
    if exp is None:
        return None
    keys = _qualifier_keys(column)
    if not keys:
        return None
    node: sqlglot_exp.Expression | None = column.parent
    while node is not None:
        if isinstance(node, exp.Select):
            for relation in _scope_relations(node):
                qualifiers, names = _relation_binding(
                    relation, real_table_columns=real_table_columns
                )
                if any(key in qualifiers for key in keys):
                    return names
        node = node.parent
    return None


def _referenced_table_columns(
    parsed: sqlglot_exp.Expression, real_table_columns: Mapping[str, frozenset[str]]
) -> frozenset[str]:
    """Every column of every real table the statement actually references.

    The permissive fallback's universe, and the whole point of the
    2026-09-26 scoping fix: it is the columns of *this statement's* tables,
    never every column in the database.

    Args:
        parsed: The parsed statement.
        real_table_columns: The engine's per-table column map.

    Returns:
        The lowercased column names, unioned over the referenced tables.
    """
    if exp is None:
        return frozenset()
    names: set[str] = set()
    for table in parsed.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            continue
        schema = (table.db or "").lower()
        name = (table.name or "").lower()
        columns = real_table_columns.get(f"{schema}.{name}" if schema else name)
        if columns:
            names |= columns
    return frozenset(names)


def _references_unresolvable_qualified_column(
    parsed: sqlglot_exp.Expression,
    *,
    dialect: str,
    get_real_table_columns: Callable[[], Mapping[str, frozenset[str]]],
) -> bool:
    """True if a qualified column reference names nothing the query can supply.

    Part 2 of the bypass-3 fix documented above, and the part that is
    default-deny rather than a name rule. `alias.name` is only accepted when
    `name` is a column the qualifier can actually supply
    (`Engine.table_columns()`) or a name the statement binds for itself
    (`_query_bound_names`). `g.pg_relation_filepath`, `g.lo_get` and every
    other catalogue function reached through the sugar is neither, so it is
    refused whether or not anyone thought to blocklist its name - which is
    the whole point, given this gate has now leaked three times on names.

    Only *qualified* references are checked. An unqualified `pg_sleep` cannot
    be the sugar: PostgreSQL's reading requires a range-table entry on the
    left to pass as the implicit argument. Leaving unqualified columns alone
    is what keeps the false-rejection surface near zero - every `GROUP BY
    month`, `ORDER BY total` and `HAVING n > 1` over a select-list alias is
    unqualified, and none of them reach this check at all. (Rule 1 above
    still covers unqualified `pg_`-named columns.)

    A qualified star (`d.*`, `exp.Column` whose `this` is `exp.Star`) is
    skipped: it names no identifier, so there is nothing to resolve, and
    PostgreSQL never reads it as a call.

    **The resolution model (rewritten 2026-09-26, second round).** The first
    version resolved against `Engine.column_names()`, a flat union of every
    column in the database. Phase 3b Task 6 then widened the engines from one
    schema to every schema the role can read, so that union became "every
    column in every readable schema" - and a table named after a
    single-argument catalogue function in *any* schema re-armed the payload.
    Reproduced with `ext.audit(lo_get integer, ...)` in a non-default schema
    `aipa_ro` may read: `SELECT g.lo_get FROM generate_series(<oid>, <oid>) g`
    validated again, and with a readable large object it returns its bytes.
    The universe is therefore now the tables **this statement references**,
    resolved per qualifier:

    - A qualifier bound to a real table resolves against *that table's*
      columns (plus any explicit column alias list). A self-join, a `USING`
      or `NATURAL` join and a correlated reference all resolve here without
      special cases, because each alias still names a real table.
    - A qualifier bound to a **function scan** (`generate_series(...) g`,
      including the `LATERAL` spelling) resolves against the function's own
      output column name and its alias list - nothing else. A function scan
      contributes no table columns at all, which is exactly why the payload
      above now fails to resolve: `lo_get` is not `generate_series`.
    - Resolution is scoped, nearest enclosing `FROM` first (see
      `_qualifier_binding`), so the same alias reused for a different relation
      in a nested scope cannot lend its columns to the other.

    **The fallback, and why it is the permissive direction.** Three qualifier
    kinds are deliberately *not* resolved exactly: a CTE name, a derived table
    or `VALUES` alias, and a qualifier the statement does not bind at all.
    Computing a CTE's or derived table's output columns means re-implementing
    name resolution through `SELECT *`, nested `USING`/`NATURAL` joins and
    `LATERAL` - the previous implementer avoided exactly that, and rightly:
    every gap in such a re-implementation is a *rejected legitimate query*.
    Those qualifiers instead resolve against `_referenced_table_columns` (the
    columns of the real tables this statement names) plus `_query_bound_names`
    (every `AS` alias, alias list and function-scan output name in it), which
    is a strict subset of the old whole-database union and closes the
    regression for them too: a payload that reaches no readable table cannot
    borrow a column name from one. The residual is narrow and was checked
    against PostgreSQL's own semantics rather than assumed: the sugar passes
    the range-table entry's own type as the argument, and a CTE or derived
    table's type is an anonymous `record`, which no OID-taking catalogue
    function (`lo_get`, `pg_relation_filepath`, `pg_terminate_backend`, ...)
    accepts - only `record`/`anyelement`-taking functions are reachable that
    way, and rule 1 above refuses the entire `pg_` surface of those on name
    alone. An unbound qualifier is refused by PostgreSQL itself with "missing
    FROM-clause entry" before any function resolution happens.

    Measured before shipping (2026-09-26, second round): 0 rejections across
    PostgreSQL's 35-query analytics corpus, DuckDB's 61-query corpus and all
    twelve shapes in `tests/test_engine_postgres.py::
    _LEGITIMATE_QUALIFIED_COLUMN_QUERIES` - see
    `.superpowers/sdd/task-6-report.md`.

    `get_real_table_columns` is a zero-argument callable for the same reason
    `_references_unknown_table`'s `get_real_table_names` is: a statement with
    no qualified column reference at all (`SELECT COUNT(*) FROM sales`) must
    not pay for a catalogue read to be told so.

    Args:
        parsed: The parsed statement.
        dialect: The dialect the statement was parsed under.
        get_real_table_columns: Returns each advertised table spelling's own
            column names, lowercased - see `Engine.table_columns()`.

    Returns:
        True if the statement must be rejected.
    """
    if exp is None:
        return True
    if dialect not in _DOT_CALL_DIALECTS:
        return False
    candidates: list[sqlglot_exp.Column] = []
    for column in parsed.find_all(exp.Column):
        if not column.args.get("table"):
            continue
        if isinstance(column.this, exp.Star):
            continue
        if (column.name or "").strip():
            candidates.append(column)
    if not candidates:
        return False
    real_table_columns = get_real_table_columns()
    # Computed at most once per statement, and only if some qualifier actually
    # falls back to it.
    fallback: frozenset[str] | None = None
    for column in candidates:
        resolvable = _qualifier_binding(column, real_table_columns=real_table_columns)
        if resolvable is None:
            if fallback is None:
                fallback = _referenced_table_columns(parsed, real_table_columns) | (
                    _query_bound_names(parsed)
                )
            resolvable = fallback
        if (column.name or "").lower() not in resolvable:
            return True
    return False


# Task 4 bypass 2 (2026-09-26 review round): `"cast"` is allowlisted for
# every engine that opts into default-deny, but nothing previously inspected
# a cast's *target type*. PostgreSQL's object-identifier ("OID") types -
# `regclass`, `regrole`, `regproc`, `regnamespace`, `regtype`, `regoper`,
# `regoperator`, `regconfig`, `regdictionary`, `regcollation`,
# `regprocedure` - each resolve a string to a row in exactly the catalogue
# `internal_prefixes`'s `pg_` rule exists to block (`pg_class`, `pg_authid`,
# `pg_proc`, `pg_namespace`, ...): `('customers'::regclass)::oid` and
# `'16384'::regclass::text` round-trip a name through `pg_class` with no
# function call and no table reference for `_references_internals` or
# `_references_disallowed_function` to see. Combined with `generate_series`
# supplying a loop of OIDs to cast, this is how the review enumerated every
# relation and role name in the database.
#
# Verified live (2026-09-26) that all eleven names above parse, under both
# `'x'::<type>` and `CAST('x' AS <type>)` spellings, to the identical shape:
# `exp.Cast(to=exp.ObjectIdentifier(this="<TYPE_NAME_UPPERCASE>"))` - not the
# `exp.DataType` node an ordinary cast target (`text`, `integer`, ...)
# produces. This is a different sqlglot node class specifically because
# PostgreSQL's object-identifier types are not part of the ordinary SQL type
# system, so no per-type-name special-casing is needed: the class itself
# already sorts every OID-type cast into one place to check.
#
# Confirmed inert for DuckDB (which has no `reg*` types at all): parsed under
# the `duckdb` dialect, `'x'::regclass` produces
# `exp.Cast(to=exp.DataType(this=Type.USERDEFINED, kind="regclass"))` -
# `exp.DataType`, never `exp.ObjectIdentifier` - so `isinstance(to,
# exp.ObjectIdentifier)` below is always `False` for a DuckDB-parsed
# statement and this check never fires there, without needing a dialect
# gate the way `_references_disallowed_dot_call` does.
_POSTGRES_OID_CAST_TYPES: frozenset[str] = frozenset(
    {
        "REGCLASS",
        "REGROLE",
        "REGPROC",
        "REGNAMESPACE",
        "REGTYPE",
        "REGOPER",
        "REGOPERATOR",
        "REGCONFIG",
        "REGDICTIONARY",
        "REGCOLLATION",
        "REGPROCEDURE",
    }
)


def _casts_to_object_identifier_type(parsed: sqlglot_exp.Expression) -> bool:
    """True if any `CAST`/`::` in the statement targets a PostgreSQL OID type.

    See `_POSTGRES_OID_CAST_TYPES` for which types and why, and for the
    live-verified proof this is a no-op for a DuckDB-parsed statement.

    Args:
        parsed: The parsed statement.

    Returns:
        True if the statement must be rejected.
    """
    if exp is None:
        return True
    for cast in parsed.find_all(exp.Cast):
        to = cast.args.get("to")
        if isinstance(to, exp.ObjectIdentifier) and str(to.this or "").upper() in (
            _POSTGRES_OID_CAST_TYPES
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

    A schema-qualified reference is checked in its qualified form: the
    candidate name is `schema.table`, and it must appear in
    `get_real_table_names()` exactly like a bare one must. Until Phase 3b
    Task 6 this was a hardcoded `schema == "main"` rule, which was wrong in
    both directions once a second engine existed - it rejected
    `public.customers` on PostgreSQL, whose default schema is `public`, and
    it could never have accepted a legitimate table outside the default
    schema on any engine. The engine now answers the question instead of a
    string literal in shared code, through the set it already had to
    provide: `analytics.thing` validates when that table exists,
    `analytics.missing` and `no_such_schema.thing` do not, and neither does
    the *bare* name of a table that lives outside the default schema, since
    `Engine.table_names()` deliberately does not advertise one (see its
    docstring - a bare name resolves against the default schema, so
    approving it would approve a query that then fails at execution).

    `information_schema.tables` is still rejected here regardless of whether
    a table literally named `tables` exists, because no engine's
    `table_names()` contains an internals schema - and `_references_
    internals`, which runs first and consults no table list at all, rejects
    it on its name before this function is reached. A catalog-qualified
    (three-part) reference is rejected outright rather than resolved: the
    engine's own catalog name varies per database file and nothing in the
    LLM's prompt ever teaches a three-part name, so there is no legitimate
    query this could cost.

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
    and the live-DuckDB proof that unscoped collection is wrong. Only an
    *unqualified* reference can name a CTE: `main.totals` names a table in
    the `main` schema, which is not where a CTE lives, on every engine here.

    `get_real_table_names` is a zero-argument callable rather than an
    already-computed set: it is only invoked once at least one table
    reference has survived the checks above (not a table-valued function,
    not catalog-qualified, not a visible CTE) and still needs a real name to
    compare against. A query with no `FROM`
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
        name = (table.name or "").lower()
        if not schema and name in _visible_cte_names(table):
            continue
        candidates.append(f"{schema}.{name}" if schema else name)
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
    get_real_table_columns: Callable[[], Mapping[str, frozenset[str]]],
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
        if _references_disallowed_dot_call(
            parsed,
            dialect=dialect,
            allowed_functions=allowed_functions,
            internal_prefixes=internal_prefixes,
            internal_names=internal_names,
        ):
            return False
        # Both column rules sit here, after the function and dot-call checks
        # and before the table check, for the same ordering reason the
        # comment above gives: the internal-name one needs no I/O at all, and
        # the default-deny one must run after `_references_disallowed_function`
        # so that `_query_bound_names` can admit a table-valued function's own
        # output-column name knowing that name is already allowlisted.
        if _references_internal_column_name(
            parsed,
            dialect=dialect,
            internal_prefixes=internal_prefixes,
            internal_names=internal_names,
        ):
            return False
        if _casts_to_object_identifier_type(parsed):
            return False
        if _references_unknown_table(parsed, get_real_table_names=get_real_table_names):
            return False
        if _references_unresolvable_qualified_column(
            parsed, dialect=dialect, get_real_table_columns=get_real_table_columns
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
            the parser dialect, its `internal_prefixes`/`internal_names` pick
            the internals blocklist, and its `allowed_functions` switches on
            default-deny function *and table* validation when it is a set
            rather than `None`, calling `engine.table_names()` only in that
            case and `engine.table_columns()` only when its `sqlglot_dialect`
            is additionally one where a qualified column can be a function
            call (`_DOT_CALL_DIALECTS`). Defaults to `None`, meaning SQLite - resolved from
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
        get_real_table_columns: Callable[[], Mapping[str, frozenset[str]]] = _no_table_columns
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
        # Same closure treatment, same reasons, for the column catalogue -
        # plus one more gate in front of it: the only check that calls this
        # (`_references_unresolvable_qualified_column`) also requires the
        # dialect to be in `_DOT_CALL_DIALECTS`, so DuckDB and SQLite never
        # read a column list at all.
        get_real_table_columns = (
            engine.table_columns if allowed_functions is not None else _no_table_columns
        )
    return _is_safe_ast(
        s,
        dialect=dialect,
        internal_prefixes=internal_prefixes,
        internal_names=internal_names,
        allowed_functions=allowed_functions,
        get_real_table_names=get_real_table_names,
        get_real_table_columns=get_real_table_columns,
    )
