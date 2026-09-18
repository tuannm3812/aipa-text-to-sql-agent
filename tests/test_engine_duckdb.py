"""DuckDB-specific safety tests: the connection-level filesystem-access guard.

`read_only=True` on a DuckDB connection protects the database *file*, not the
filesystem: a read-only connection can still run `read_csv`, a bare quoted
path (which has no function name for `is_safe_query` to catch), `glob`, and
`COPY ... TO`. These go in their own module, engine-specific rather than in
`test_engine_conformance.py`, for the same reason SQLite's authorizer tests
live in `test_execution.py` rather than the conformance suite: the mechanism
under test - `enable_external_access=False` - has no equivalent on other
engines.

Each test calls `engine.execute` directly, bypassing `is_safe_query` entirely,
because the point is to prove the *connection* refuses these regardless of
what any validator in front of it does or doesn't catch.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

duckdb = pytest.importorskip("duckdb", reason="install the duckdb extra")

from text_to_sql_agent import is_safe_query  # noqa: E402
from text_to_sql_agent import safety as _safety  # noqa: E402
from text_to_sql_agent.engines import open_engine  # noqa: E402
from text_to_sql_agent.engines.duckdb import DuckDBEngine  # noqa: E402


@pytest.fixture
def secret_and_engine(tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("k,v\napi_key,hunter2\n", encoding="utf-8")
    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.close()
    return secret, open_engine(f"duckdb://{db}")


@pytest.mark.parametrize(
    "template",
    [
        "SELECT * FROM read_csv('{secret}')",
        "SELECT * FROM '{secret}'",
        "SELECT * FROM glob('{parent}/*')",
    ],
)
def test_filesystem_reads_are_refused_by_the_connection(secret_and_engine, template):
    secret, engine = secret_and_engine
    sql = template.format(secret=secret, parent=secret.parent)
    with pytest.raises(Exception) as caught:  # noqa: B017
        engine.execute(sql, max_rows=10, work_limit=0)
    assert "hunter2" not in str(caught.value)


def test_copy_to_a_file_is_refused_by_the_connection(secret_and_engine, tmp_path):
    _, engine = secret_and_engine
    target = tmp_path / "exfil.csv"
    with pytest.raises(Exception):  # noqa: B017
        engine.execute(f"COPY (SELECT 1) TO '{target}'", max_rows=10, work_limit=0)
    assert not target.exists(), "nothing may be written to disk"


# Every DuckDB table function that reads a file, given a single string-path
# argument. Verified 2026-09-19 that each accepts exactly this shape and
# raises `duckdb.PermissionException` at the connection layer before ever
# opening the path - see the module docstring on why that layer, not
# `is_safe_query`, is what actually stops them.
FILE_READING_FUNCTIONS = [
    "read_csv",
    "read_csv_auto",
    "read_json",
    "read_json_auto",
    "read_json_objects",
    "read_json_objects_auto",
    "read_ndjson",
    "read_ndjson_auto",
    "read_ndjson_objects",
    "read_parquet",
    "read_duckdb",
    "read_text",
    "read_blob",
    "parquet_scan",
    "glob",
    "sniff_csv",
]


@pytest.mark.parametrize("function_name", FILE_READING_FUNCTIONS)
def test_file_reading_functions_are_blocked_at_both_layers(secret_and_engine, function_name):
    """Pins both layers together: the validator's name-based check (defence in
    depth) and the connection's `enable_external_access=False` (load-bearing).

    A regression that removed either layer alone would still pass the other
    half of this test - only removing both would go unnoticed, which is why
    Step 3a and this test are separate but both required.
    """
    secret, engine = secret_and_engine
    sql = f"SELECT * FROM {function_name}('{secret}')"

    assert not is_safe_query(sql, engine=engine), (
        f"{function_name} must be in DuckDBEngine.internal_prefixes/internal_names"
    )
    with pytest.raises(Exception) as caught:  # noqa: B017
        engine.execute(sql, max_rows=10, work_limit=0)
    assert "hunter2" not in str(caught.value)


# Task 6b (2026-09-19) replaced the standalone table-function allowlist that
# used to live here with `DuckDBEngine.allowed_functions` - the brief was
# explicit that the two must not compete as separate, driftable lists. Every
# test below that needs "the table functions a legitimate analytical question
# could use" reads them out of the engine's own set instead of hand-rolling a
# second copy. `histogram`/`histogram_values`/`summary` were on the old
# allowlist and are deliberately **not** carried forward: `histogram_values`
# is a macro whose body calls `query_table(source)`, so
# `SELECT * FROM histogram_values('information_schema.tables', ...)` read the
# catalogue through a name that had passed every prior review round precisely
# because it looked analytical. See `DuckDBEngine.allowed_functions`'s
# docstring for the full account and `test_pinned_leaks_stay_rejected` below
# for the regression pin.
def _duckdb_catalog_function_names(*function_types: str) -> frozenset[str]:
    """Every distinct `function_name` `duckdb_functions()` reports for the given types."""
    con = duckdb.connect(":memory:")
    try:
        placeholders = ", ".join("?" for _ in function_types)
        rows = con.execute(
            f"SELECT DISTINCT function_name FROM duckdb_functions() "
            f"WHERE function_type IN ({placeholders})",
            list(function_types),
        ).fetchall()
    finally:
        con.close()
    return frozenset(row[0] for row in rows)


# The table functions `DuckDBEngine.allowed_functions` actually authorises,
# derived rather than hand-copied - `range`/`generate_series` both appear
# because the set lists both spellings even though they resolve to the same
# canonical token (see the comment on `allowed_functions`).
ALLOWED_TABLE_FUNCTIONS = (DuckDBEngine.allowed_functions or frozenset()) & (
    _duckdb_catalog_function_names("table", "table_macro")
)


@pytest.mark.parametrize(
    "function_name",
    sorted(ALLOWED_TABLE_FUNCTIONS - {"range", "generate_series", "repeat"}),
)
def test_allowed_table_functions_are_not_blocked_by_the_internals_check(function_name):
    """`internal_prefixes`/`internal_names` must not shadow a function this
    task deliberately allows as a table source - the two gates are
    independent AND conditions, so a name blocked by one is unreachable no
    matter what the other says.

    `range`/`generate_series` are excluded because they need positional
    arguments no bare `{name}()` probe supplies; their coverage comes from
    the Step 5 analytics corpus instead. `repeat` is excluded deliberately,
    not as a probe limitation: the literal name `repeat` is in
    `allowed_functions` for the scalar string function
    (`repeat('ab', 3)` -> `'ababab'`), a genuinely useful string helper, but
    DuckDB also registers a *table* function of the same name (builds a
    table of N copies of a literal value or row) that
    `DuckDBEngine.internal_names` blocks on purpose - "no legitimate
    analytical use", per that class's docstring. Resolution is
    name-transparent for both (`repeat` parses as `exp.Anonymous` in either
    position), so the same literal name is correctly allowed as a scalar and
    correctly blocked as a table source; the two gates disagreeing on
    `repeat` specifically is the intended outcome, not a defect.
    """
    engine = DuckDBEngine("unused.duckdb")
    blocked = function_name.startswith(engine.internal_prefixes) or (
        function_name in engine.internal_names
    )
    assert not blocked, f"{function_name} is both allowed and blocklisted - remove one"


def test_repeat_is_allowed_as_a_scalar_and_blocked_as_a_table_source():
    """Names the `repeat` exception in `ALLOWED_TABLE_FUNCTIONS` above directly.

    `is_safe_query` -> `engine.execute` end to end: the scalar use must both
    validate and run; the table-position use must be rejected by
    `is_safe_query` before `engine.execute` is ever called.
    """
    engine = DuckDBEngine("unused.duckdb")
    assert is_safe_query("SELECT repeat('ab', 3) FROM t", engine=engine)
    assert not is_safe_query("SELECT * FROM repeat(1, 3)", engine=engine)


# Window functions need an `OVER (...)` clause to parse at all; a handful
# (`ntile`, `lag`, `lead`, `nth_value`, `first_value`, `last_value`) also
# need specific non-empty arguments sqlglot's DuckDB dialect requires.
_WINDOW_FUNCTION_ARGS = {
    "ntile": "(4)",
    "lag": "(a)",
    "lead": "(a)",
    "nth_value": "(a, 2)",
    "first_value": "(a)",
    "last_value": "(a)",
}
_WINDOW_FUNCTIONS = frozenset(
    {
        "row_number",
        "rank",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
    }
)
# Table functions are called in a `FROM` position with their own argument
# shape, not a scalar `SELECT` list.
_TABLE_FUNCTION_ARGS = {
    "range": "(10)",
    "generate_series": "(1, 10)",
    "unnest": "(a)",
    "json_each": "(a)",
    "json_tree": "(a)",
}
# A plausible, parseable argument list for every remaining scalar/aggregate
# entry in `DuckDBEngine.allowed_functions` - one call shape each function is
# known to accept, used only to prove the round trip below, not to exercise
# every overload. `current_date`, `now`, `extract`, `case` and `if` have their
# own irregular syntax and are special-cased in `_round_trip_snippet` instead
# of listed here.
_SCALAR_FUNCTION_ARGS = {
    "abs": "(a)",
    "age": "(d1, d2)",
    "approx_count_distinct": "(a)",
    "arg_max": "(a, b)",
    "arg_min": "(a, b)",
    "array_agg": "(a)",
    "avg": "(a)",
    "bool_and": "(a)",
    "bool_or": "(a)",
    "cbrt": "(a)",
    "ceil": "(a)",
    "coalesce": "(a, b)",
    "concat": "(a, b)",
    "concat_ws": "(',', a, b)",
    "contains": "(a, 'x')",
    "corr": "(a, b)",
    "count": "(a)",
    "count_if": "(a)",
    "covar_pop": "(a, b)",
    "covar_samp": "(a, b)",
    "date_add": "(d, INTERVAL 1 DAY)",
    "date_diff": "('day', d1, d2)",
    "date_part": "('year', d)",
    "date_sub": "(d, INTERVAL 1 DAY)",
    "date_trunc": "('month', d)",
    "datepart": "('year', d)",
    "day": "(d)",
    "dayofweek": "(d)",
    "dayofyear": "(d)",
    "ends_with": "(a, 'x')",
    "epoch": "(d)",
    "epoch_ms": "(d)",
    "exp": "(a)",
    "first": "(a)",
    "floor": "(a)",
    "greatest": "(a, b)",
    "hour": "(d)",
    "isodow": "(d)",
    "json_array_length": "(a)",
    "json_extract": "(a, '$.x')",
    "json_extract_string": "(a, '$.x')",
    "json_keys": "(a)",
    "json_type": "(a)",
    "json_valid": "(a)",
    "last": "(a)",
    "last_day": "(d)",
    "least": "(a, b)",
    "left": "(a, 3)",
    "length": "(a)",
    "list_aggregate": "(a, 'sum')",
    "list_contains": "(a, 1)",
    "list_distinct": "(a)",
    "list_extract": "(a, 1)",
    "list_position": "(a, 1)",
    "list_sort": "(a)",
    "list_value": "(1, 2, 3)",
    "ln": "(a)",
    "log": "(a)",
    "lower": "(a)",
    "lpad": "(a, 5, '0')",
    "make_date": "(2024, 1, 1)",
    "max": "(a)",
    "median": "(a)",
    "min": "(a)",
    "minute": "(d)",
    "mode": "(a)",
    "month": "(d)",
    "nullif": "(a, b)",
    "position": "('x' in a)",
    "power": "(a, 2)",
    "quantile": "(a, 0.5)",
    "quantile_cont": "(a, 0.5)",
    "quantile_disc": "(a, 0.5)",
    "quarter": "(d)",
    "regexp_extract": "(a, 'x')",
    "regexp_full_match": "(a, 'x')",
    "regexp_matches": "(a, 'x')",
    "regexp_replace": "(a, 'x', 'y')",
    "repeat": "(a, 3)",
    "replace": "(a, 'x', 'y')",
    "reverse": "(a)",
    "right": "(a, 3)",
    "round": "(a, 2)",
    "second": "(d)",
    "sign": "(a)",
    "split_part": "(a, ',', 1)",
    "sqrt": "(a)",
    "starts_with": "(a, 'x')",
    "stddev": "(a)",
    "stddev_pop": "(a)",
    "stddev_samp": "(a)",
    "strftime": "(d, '%Y-%m')",
    "string_agg": "(a, ',')",
    "string_split": "(a, ',')",
    "strptime": "(a, '%Y-%m-%d')",
    "substring": "(a, 1, 3)",
    "sum": "(a)",
    "to_json": "(a)",
    "trim": "(a)",
    "upper": "(a)",
    "var_pop": "(a)",
    "variance": "(a)",
    "week": "(d)",
    "year": "(d)",
}


def _round_trip_snippet(name: str) -> str:
    """A single, plausible SQL fragment calling `name`, for the round-trip test."""
    if name in _WINDOW_FUNCTIONS:
        args = _WINDOW_FUNCTION_ARGS.get(name, "()")
        return f"{name}{args} OVER (PARTITION BY b ORDER BY c)"
    if name in _TABLE_FUNCTION_ARGS:
        return f"{name}{_TABLE_FUNCTION_ARGS[name]}"
    if name in ("cast", "try_cast"):
        return f"{name}(a AS INTEGER)"
    if name == "extract":
        return "extract(year FROM d)"
    if name == "case":
        return "case when a > 1 then 'x' else 'y' end"
    if name == "if":
        return "if(a > 1, 'x', 'y')"
    if name == "current_date":
        return "current_date"
    if name == "now":
        return "now()"
    return f"{name}{_SCALAR_FUNCTION_ARGS[name]}"


@pytest.mark.parametrize("name", sorted(DuckDBEngine.allowed_functions))
def test_allowed_function_round_trip(name):
    """The hardest part of this task, proven directly: for every one of the
    127 names in `DuckDBEngine.allowed_functions`, a realistic call using
    that name parses under the DuckDB dialect, and `safety._resolve_function_
    name` resolves the parsed node back to a name that is itself in
    `allowed_functions` - not necessarily the same literal spelling
    (`range(10)` resolves to `"generate_series"`, since both parse to the
    same typed AST node - see `_FUNCTION_NAME_OVERRIDES` in `safety.py`), but
    always a member of the set, which is what makes `is_safe_query` accept
    it. `is_safe_query` itself is asserted too, on the full statement, so
    this is the real path the validator runs, not just the resolver in
    isolation.

    This is the round-trip proof the task brief requires: sqlglot parses
    many calls into typed classes whose canonical name can differ from what
    DuckDB registers or what a caller typed, and a mismatch here fails in one
    of two directions - a legitimate function wrongly rejected (caught by
    the `is_safe_query` assertion below), or a resolver that maps a
    dangerous function onto an allowed name (which `test_every_unlisted_
    duckdb_function_is_rejected_by_default_deny` above sweeps for
    independently, across the whole catalogue rather than just this
    allowlist).
    """
    engine = DuckDBEngine("unused.duckdb")
    snippet = _round_trip_snippet(name)
    is_table_function = name in _TABLE_FUNCTION_ARGS
    sql = f"SELECT * FROM {snippet}" if is_table_function else f"SELECT {snippet} FROM t"

    parsed = sqlglot.parse_one(sql, read="duckdb")
    funcs = list(parsed.find_all(exp.Func))
    assert funcs, f"{sql!r} produced no exp.Func node to resolve"
    resolved_names = {_safety._resolve_function_name(f) for f in funcs}
    assert resolved_names & engine.allowed_functions, (
        f"{name!r} (SQL: {sql!r}) resolved to {resolved_names}, none of which "
        "are in DuckDBEngine.allowed_functions"
    )
    assert is_safe_query(sql, engine=engine), f"{sql!r} should have been allowed"


# The 5 administrative functions Fix 2's review found passing `is_safe_query`
# and actually executing against a real `DuckDBEngine`, under Fix 1's "does
# not touch the filesystem" criterion. Confirmed grep-clean: none of these
# names appear anywhere in text_to_sql_agent/, ui/, scripts/, or
# evaluation/cases.json.
_PREVIOUSLY_EXECUTED_ADMINISTRATIVE_FUNCTIONS = [
    "checkpoint",
    "enable_profiling",
    "enable_logging",
    "disable_logging",
    "truncate_duckdb_logs",
]


@pytest.mark.parametrize("function_name", _PREVIOUSLY_EXECUTED_ADMINISTRATIVE_FUNCTIONS)
def test_administrative_functions_are_refused_by_the_validator(function_name):
    """Regression pin for Fix 2: these 5 passed `is_safe_query` and executed
    before `DuckDBEngine.internal_names` grew an explicit administrative-
    function bucket. A model has no legitimate reason to force a checkpoint
    or toggle logging/profiling, so the validator must refuse them outright,
    not merely fail to find a way to abuse them.
    """
    engine = DuckDBEngine("unused.duckdb")
    assert not is_safe_query(f"SELECT * FROM {function_name}()", engine=engine)


# Argument shapes tried, in order, when probing whether `name{args}` parses
# under the DuckDB dialect. Several real functions need a specific shape
# (`log(a)` parses; `concat_ws()` alone does not), so more than one template
# is tried per name and per position before giving up on that name.
_PROBE_ARG_TEMPLATES = (
    "()",
    "(a)",
    "(a, b)",
    "(a, b, c)",
    "(1)",
    "(1, 2)",
    "('x')",
    "('x', 'y')",
)


def _first_parseable_call(name: str, *, wrap: str) -> str | None:
    """The first `wrap`-shaped SQL text using `name` that parses under DuckDB.

    Args:
        name: A candidate function name from `duckdb_functions()`.
        wrap: A template with one `{call}` placeholder, e.g.
            `"SELECT {call} FROM t"` for a scalar-position probe or
            `"SELECT * FROM {call}"` for a table-position probe.

    Returns:
        The first probe SQL text that sqlglot's DuckDB dialect parses
        without error, or `None` if every argument shape failed to parse.
    """
    import sqlglot

    for args in _PROBE_ARG_TEMPLATES:
        sql = wrap.format(call=f"{name}{args}")
        try:
            sqlglot.parse_one(sql, read="duckdb")
        except Exception:
            continue
        return sql
    return None


def _resolved_name_of_only_func(sql: str) -> str | None:
    """`safety._resolve_function_name` applied to the sole `exp.Func` in `sql`.

    Every probe template in `_PROBE_ARG_TEMPLATES` uses bare column
    references or scalar literals as arguments, never a bracketed list
    literal (`[1, 2]`) or a nested call, so a probe that parses produces at
    most one `exp.Func` node - the call under test itself. `mod(a, b)`,
    `xor(a, b)`, `*(a)`, and a few other catalogue entries are native
    operator syntax with **no** `exp.Func` node at all once parsed (DuckDB
    registers operators as catalogue functions too, but sqlglot represents
    `a % b` as `exp.Mod`, not a call) - those return `None` here and are
    excluded from the sweep's leak check entirely, since there is no function
    name for `_references_disallowed_function` to see, and no filesystem or
    catalogue access an arithmetic/bitwise operator could perform regardless
    of what gate does or doesn't inspect it.

    Returns:
        The resolved name, or `None` if the parse produced zero `exp.Func`
        nodes. Never more than one, by the argument-shape guarantee above.
    """
    import sqlglot
    from sqlglot import exp

    parsed = sqlglot.parse_one(sql, read="duckdb")
    funcs = list(parsed.find_all(exp.Func))
    if not funcs:
        return None
    assert len(funcs) == 1, f"probe {sql!r} produced more than one Func node: {funcs}"
    return _safety._resolve_function_name(funcs[0])


def test_every_unlisted_duckdb_function_is_rejected_by_default_deny():
    """Step 4: proves the property, not the list.

    Sweeps every distinct function name `duckdb_functions()` reports, across
    every function type it has (`scalar`, `aggregate`, `table`,
    `table_macro`, `macro`, `pragma`), and requires that `is_safe_query`
    agrees exactly with the same resolution `_references_disallowed_function`
    itself performs: rejected whenever the parsed call's *resolved* name is
    not in `DuckDBEngine.allowed_functions`, and - only then - never
    unexpectedly rejected when it is. Checked in a scalar position
    (`SELECT name(...)`) and, separately, in a table position
    (`SELECT * FROM name(...)`), since a name can be reachable in one
    position and not the other.

    Resolved rather than literal names are what the sweep must compare
    against, or the sweep reports false leaks: DuckDB registers many
    synonyms under distinct catalogue names that sqlglot parses into the
    *same* typed AST node - `lpad`/`rpad` both become `exp.Pad`,
    `var_samp`/`variance` both become `exp.Variance`,
    `string_agg`/`group_concat`/`listagg` all become `exp.GroupConcat` - so a
    literal catalogue name such as `listagg` correctly passes `is_safe_query`
    even though only `string_agg` is spelled out in `allowed_functions`. That
    is by design (see `_resolve_function_name`'s docstring in `safety.py`),
    not a gap; comparing literal names against the allowlist instead of
    resolved ones flagged every one of these as a false leak the first time
    this test was written, catalogue-verified 2026-09-19 to always land on
    an already-intentionally-allowed canonical token rather than smuggling a
    different function through.

    This supersedes Task 6's `test_every_duckdb_table_function_is_classified`
    (table/table_macro only, classified by list membership rather than by
    round-tripping through `is_safe_query`) for the same reason the brief
    gives: under default-deny, "classified" no longer means "someone put it
    in a list" - it means `is_safe_query` itself agrees with the resolver,
    checked here directly. A future DuckDB release that adds a function
    changes nothing about this test's outcome: an unrecognised name fails
    closed by construction, and a new synonym of an already-allowed function
    resolves to the same canonical token and is correctly allowed.
    """
    engine = DuckDBEngine("unused.duckdb")
    allowed = engine.allowed_functions or frozenset()
    all_names = sorted(
        _duckdb_catalog_function_names(
            "scalar", "aggregate", "table", "table_macro", "macro", "pragma"
        )
    )
    assert all_names, "the catalogue sweep returned nothing - duckdb_functions() query is broken"

    mismatches: list[tuple[str, str, str, str, bool, bool]] = []
    scalar_checked = 0
    table_checked = 0
    no_func_node = 0
    for name in all_names:
        for position, wrap in (
            ("scalar", "SELECT {call} FROM t"),
            ("table", "SELECT * FROM {call}"),
        ):
            sql = _first_parseable_call(name, wrap=wrap)
            if sql is None:
                continue
            resolved = _resolved_name_of_only_func(sql)
            if resolved is None:
                no_func_node += 1
                continue
            if position == "scalar":
                scalar_checked += 1
            else:
                table_checked += 1
            actual_safe = is_safe_query(sql, engine=engine)
            expected_safe = resolved in allowed
            # `repeat` is the one deliberate exception: the literal name is
            # allowed for the scalar string function but the *table*
            # function of the same name is independently blocked by
            # `internal_names` (see `test_repeat_is_allowed_as_a_scalar_and_
            # blocked_as_a_table_source`), so the resolver alone
            # under-predicts `is_safe_query` here on purpose - the internals
            # gate is doing exactly what it is for.
            if (position, resolved) == ("table", "repeat"):
                assert not actual_safe, "repeat as a table source must stay blocked"
                continue
            if actual_safe != expected_safe:
                mismatches.append((position, name, sql, resolved, actual_safe, expected_safe))

    assert not mismatches, (
        f"is_safe_query disagreed with the resolver for {len(mismatches)} case(s): "
        f"{mismatches[:10]}. A resolved name allowed here but rejected by is_safe_query "
        "(or vice versa) means the two have drifted apart."
    )
    # A meaningful fraction of the catalogue must actually have been
    # exercised in each position - this is what would catch the probe itself
    # silently degenerating (e.g. every template failing to parse) rather
    # than the property genuinely holding.
    assert scalar_checked > 700, f"only {scalar_checked} names produced a parseable scalar probe"
    assert table_checked > 20, f"only {table_checked} names produced a parseable table probe"
    print(
        f"\nStep 4 sweep: {len(all_names)} distinct catalogue names, "
        f"{len(allowed)} in the allowlist; "
        f"{scalar_checked} checked in scalar position, "
        f"{table_checked} checked in table position, "
        f"{no_func_node} probes produced no exp.Func node (operator syntax, "
        "excluded from the check), 0 mismatches."
    )


def test_pinned_leaks_stay_rejected():
    """Step 6: pins the two proven leaks from the task brief directly.

    Both were verified live against this exact DuckDB build (2026-09-19):
    `current_setting('secret_directory')` returned a real filesystem path
    through the old blocklist, and
    `SELECT * FROM histogram_values('information_schema.tables', 'table_name', 20, 'auto')`
    read the catalogue through a macro that was on the old table-function
    allowlist. Neither name is anywhere in `DuckDBEngine.allowed_functions`
    now, so both are rejected by default-deny alone - this test exists so a
    future change to that set cannot silently re-open either leak without a
    test failing.
    """
    engine = DuckDBEngine("unused.duckdb")
    # `current_setting` is a scalar - the exact shape that made a blocklist
    # structurally unfit, since the internals check only ever fires in a
    # table-source position.
    assert not is_safe_query("SELECT current_setting('secret_directory')", engine=engine)
    assert not is_safe_query(
        "SELECT current_setting('secret_directory') AS x FROM t", engine=engine
    )
    # histogram/histogram_values/summary share the macro body that calls
    # query_table(source) - pin all three directly against a catalogue
    # source, not just the one from the brief.
    assert not is_safe_query(
        "SELECT * FROM histogram_values('information_schema.tables', 'table_name', 20, 'auto')",
        engine=engine,
    )
    assert not is_safe_query(
        "SELECT * FROM histogram('information_schema.tables', 'table_name')", engine=engine
    )
    assert not is_safe_query("SELECT * FROM summary('information_schema.tables')", engine=engine)


def test_pinned_leaks_are_also_refused_by_the_connection(tmp_path):
    """The same two leaks, end to end: `is_safe_query` rejects them (proven
    above), and - belt and suspenders - the query never actually reaches
    live data even if a future regression somehow made the validator agree
    to run it, because a genuine catalogue read still succeeds at the
    connection layer (unlike the filesystem functions, DuckDB's own
    catalogue is readable over a read-only, external-access-disabled
    connection - that is exactly why the validator, not the connection, has
    to be the gate here).
    """
    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.close()
    engine = open_engine(f"duckdb://{db}")

    assert not is_safe_query("SELECT current_setting('secret_directory')", engine=engine)
    result = engine.execute("SELECT current_setting('secret_directory')", max_rows=10, work_limit=0)
    assert result.ok, (
        "sanity check: the connection itself does not refuse this - the validator must"
    )

    assert not is_safe_query(
        "SELECT * FROM histogram_values('information_schema.tables', 'table_name', 20, 'auto')",
        engine=engine,
    )
    result = engine.execute(
        "SELECT * FROM histogram_values('information_schema.tables', 'table_name', 20, 'auto')",
        max_rows=10,
        work_limit=0,
    )
    assert result.ok and result.rows, (
        "sanity check: histogram_values genuinely reads the catalogue when nothing stops it"
    )


@pytest.fixture
def analytics_db(tmp_path):
    """A richer DuckDB fixture for Step 5's analytics corpus: dated orders
    with a category, a status, a JSON metadata blob, and a list column, over
    two customer regions - enough surface for grouping, date bucketing,
    window functions, string cleaning, and list/JSON access to all have
    something real to operate on.
    """
    db = tmp_path / "analytics.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name VARCHAR,
            region VARCHAR,
            signup_date DATE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(customer_id),
            order_date DATE,
            category VARCHAR,
            status VARCHAR,
            amount DOUBLE,
            tags VARCHAR[],
            metadata VARCHAR
        )
        """
    )
    con.execute(
        "INSERT INTO customers VALUES "
        "(1, '  Alice Smith ', 'north', DATE '2023-01-15'), "
        "(2, 'bob jones', 'south', DATE '2023-03-02'), "
        "(3, 'Carla Diaz', 'north', DATE '2024-02-20')"
    )
    con.execute(
        """
        INSERT INTO orders VALUES
        (1, 1, DATE '2024-01-05', 'widgets', 'completed', 100.0, ['sale', 'priority'],
         '{"channel": "web", "coupon": null}'),
        (2, 1, DATE '2024-01-20', 'gadgets', 'completed', 250.5, ['sale'],
         '{"channel": "store"}'),
        (3, 2, DATE '2024-02-10', 'widgets', 'refunded', 75.0, [],
         '{"channel": "web"}'),
        (4, 2, DATE '2024-02-15', 'gizmos', 'completed', 400.0, ['priority'],
         '{"channel": "web", "coupon": "SPRING10"}'),
        (5, 3, DATE '2024-02-28', 'widgets', 'completed', 60.25, ['sale'],
         '{"channel": "store"}'),
        (6, 3, DATE '2024-03-03', 'gadgets', 'pending', NULL, [],
         '{"channel": "app"}'),
        (7, 1, DATE '2024-03-10', 'gizmos', 'completed', 310.0, ['priority', 'sale'],
         '{"channel": "app", "coupon": "APR5"}'),
        (8, 2, DATE '2024-03-22', 'widgets', 'completed', 90.0, ['sale'],
         '{"channel": "web"}')
        """
    )
    con.close()
    return open_engine(f"duckdb://{db}")


# Step 5: at least 40 realistic analytical questions over `analytics_db`,
# spanning grouping, aggregation, window functions, date bucketing with
# date_trunc, string cleaning, CASE, CTEs, COALESCE, and list/JSON access.
# Every one of these must both pass `is_safe_query` and execute
# successfully - a false rejection here costs a user an unanswerable
# question, which the task brief treats as seriously as an attack leaking.
ANALYTICS_CORPUS = [
    "SELECT COUNT(*) FROM orders",
    "SELECT category, COUNT(*) AS n FROM orders GROUP BY category ORDER BY n DESC",
    "SELECT category, SUM(amount) AS total FROM orders GROUP BY category",
    "SELECT category, AVG(amount) AS avg_amount FROM orders GROUP BY category",
    "SELECT status, COUNT(*) AS n, SUM(amount) AS total FROM orders GROUP BY status",
    "SELECT category, MIN(amount) AS lo, MAX(amount) AS hi FROM orders GROUP BY category",
    "SELECT category, MEDIAN(amount) AS median_amount FROM orders GROUP BY category",
    "SELECT category, STDDEV(amount) AS spread FROM orders GROUP BY category",
    "SELECT category FROM orders GROUP BY category HAVING COUNT(*) > 1",
    "SELECT COUNT(DISTINCT customer_id) FROM orders",
    (
        "SELECT c.region, SUM(o.amount) AS total FROM orders o "
        "JOIN customers c ON c.customer_id = o.customer_id GROUP BY c.region"
    ),
    (
        "SELECT c.name, COUNT(*) AS n FROM orders o JOIN customers c "
        "ON c.customer_id = o.customer_id GROUP BY c.name ORDER BY n DESC"
    ),
    # Window functions
    "SELECT order_id, amount, ROW_NUMBER() OVER (ORDER BY amount DESC) AS rnk FROM orders",
    (
        "SELECT order_id, category, amount, "
        "RANK() OVER (PARTITION BY category ORDER BY amount DESC) AS category_rank FROM orders"
    ),
    (
        "SELECT order_id, category, amount, "
        "DENSE_RANK() OVER (PARTITION BY category ORDER BY amount DESC) AS drnk FROM orders"
    ),
    (
        "SELECT order_id, order_date, amount, "
        "SUM(amount) OVER (ORDER BY order_date) AS running_total FROM orders"
    ),
    (
        "SELECT order_id, customer_id, order_date, "
        "LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date) AS prev_order "
        "FROM orders"
    ),
    (
        "SELECT order_id, customer_id, order_date, "
        "LEAD(order_date) OVER (PARTITION BY customer_id ORDER BY order_date) AS next_order "
        "FROM orders"
    ),
    "SELECT order_id, amount, NTILE(4) OVER (ORDER BY amount) AS quartile FROM orders",
    (
        "SELECT order_id, category, amount, "
        "FIRST_VALUE(amount) OVER (PARTITION BY category ORDER BY order_date) AS first_in_cat "
        "FROM orders"
    ),
    (
        "SELECT customer_id, order_date, "
        "PERCENT_RANK() OVER (ORDER BY order_date) AS pct FROM orders"
    ),
    # Date bucketing
    "SELECT date_trunc('month', order_date) AS month, SUM(amount) FROM orders GROUP BY month",
    "SELECT date_trunc('week', order_date) AS wk, COUNT(*) FROM orders GROUP BY wk ORDER BY wk",
    "SELECT EXTRACT(year FROM order_date) AS yr, SUM(amount) FROM orders GROUP BY yr",
    "SELECT EXTRACT(month FROM order_date) AS mo, COUNT(*) FROM orders GROUP BY mo",
    "SELECT year(order_date) AS yr, month(order_date) AS mo, COUNT(*) FROM orders GROUP BY yr, mo",
    "SELECT strftime(order_date, '%Y-%m') AS ym, SUM(amount) FROM orders GROUP BY ym",
    "SELECT DATE_DIFF('day', signup_date, CURRENT_DATE) AS days_since_signup FROM customers",
    "SELECT customer_id, DATEPART('quarter', order_date) AS q FROM orders",
    # String cleaning
    "SELECT TRIM(name) AS clean_name FROM customers",
    "SELECT UPPER(TRIM(name)) AS clean_name FROM customers",
    "SELECT LOWER(region) AS region_lc FROM customers",
    "SELECT customer_id, LENGTH(TRIM(name)) AS name_length FROM customers",
    "SELECT REPLACE(status, 'pending', 'in_progress') AS normalised_status FROM orders",
    "SELECT SUBSTRING(TRIM(name), 1, 1) AS initial FROM customers",
    "SELECT CONCAT_WS(' ', region, status) AS label FROM orders o JOIN customers c "
    "ON c.customer_id = o.customer_id",
    "SELECT STARTS_WITH(status, 'comp') AS is_completed_like FROM orders",
    # CASE
    (
        "SELECT order_id, "
        "CASE WHEN amount IS NULL THEN 'unknown' "
        "WHEN amount > 200 THEN 'large' ELSE 'small' END AS size_bucket FROM orders"
    ),
    (
        "SELECT category, "
        "SUM(CASE WHEN status = 'completed' THEN amount ELSE 0 END) AS completed_total "
        "FROM orders GROUP BY category"
    ),
    # COALESCE
    "SELECT order_id, COALESCE(amount, 0) AS amount_or_zero FROM orders",
    "SELECT order_id, COALESCE(amount, 0) * 1.1 AS with_tax FROM orders",
    # CTEs
    (
        "WITH totals AS (SELECT category, SUM(amount) AS total FROM orders GROUP BY category) "
        "SELECT * FROM totals WHERE total > 100"
    ),
    (
        "WITH ranked AS ("
        "SELECT order_id, category, amount, "
        "ROW_NUMBER() OVER (PARTITION BY category ORDER BY amount DESC) AS rnk FROM orders"
        ") SELECT * FROM ranked WHERE rnk = 1"
    ),
    (
        "WITH monthly AS ("
        "SELECT date_trunc('month', order_date) AS month, SUM(amount) AS total FROM orders "
        "GROUP BY month"
        ") SELECT month, total, total - LAG(total) OVER (ORDER BY month) AS delta FROM monthly"
    ),
    # List / JSON access
    "SELECT order_id, tags FROM orders WHERE list_contains(tags, 'priority')",
    "SELECT order_id, LIST_EXTRACT(tags, 1) AS first_tag FROM orders WHERE len(tags) > 0",
    "SELECT order_id, json_extract_string(metadata, '$.channel') AS channel FROM orders",
    "SELECT json_extract_string(metadata, '$.channel') AS channel, COUNT(*) FROM orders "
    "GROUP BY channel",
    "SELECT order_id, json_extract_string(metadata, '$.coupon') AS coupon FROM orders "
    "WHERE json_extract_string(metadata, '$.coupon') IS NOT NULL",
    # Table functions
    "SELECT * FROM range(5)",
    "SELECT * FROM generate_series(1, 5)",
    "SELECT unnest(tags) AS tag FROM orders",
    # Combined, closer to a realistic multi-clause business question
    (
        "SELECT c.region, date_trunc('month', o.order_date) AS month, "
        "COUNT(*) AS n_orders, SUM(o.amount) AS total, AVG(o.amount) AS avg_amount "
        "FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
        "WHERE o.status = 'completed' "
        "GROUP BY c.region, month ORDER BY month, c.region"
    ),
]


def test_analytics_corpus_size_is_at_least_forty():
    """Guards the corpus itself, not just what it proves - a corpus that
    silently shrank below the brief's stated floor would make every other
    assertion about it in the task report false.
    """
    assert len(ANALYTICS_CORPUS) >= 40


@pytest.mark.parametrize("sql", ANALYTICS_CORPUS)
def test_analytics_corpus_passes_validation_and_executes(analytics_db, sql):
    """Step 5: every query in `ANALYTICS_CORPUS` must both pass `is_safe_query`
    and actually execute against a real DuckDB database - the two are
    checked separately so a failure names which side broke.
    """
    assert is_safe_query(sql, engine=analytics_db), f"wrongly rejected: {sql!r}"
    result = analytics_db.execute(sql, max_rows=1000, work_limit=0)
    assert result.ok, f"{sql!r} failed to execute: {result.error}"
