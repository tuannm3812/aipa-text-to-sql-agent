from __future__ import annotations

import pytest

import text_to_sql_agent as agent

# Read-only queries that must be allowed. The first four are rejected by the
# keyword regex today: REPLACE is a standard SQLite string function, and the
# rest carry a keyword inside a string literal.
LEGITIMATE = [
    "SELECT REPLACE(name, 'a', 'b') FROM customers",
    "SELECT * FROM orders WHERE note = 'please update'",
    "SELECT * FROM orders WHERE status = 'delete'",
    "SELECT * FROM t WHERE action = 'create'",
    "SELECT name FROM customers;",
    "SELECT * FROM t WHERE x = 'a;b'",
    "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM sales "
    "GROUP BY customer_id) SELECT * FROM totals",
    "SELECT a, COUNT(*) FROM t GROUP BY a HAVING COUNT(*) > 2",
    "SELECT strftime('%Y', d) AS yr, SUM(amt) FROM s GROUP BY yr",
    # Scalar sqlite_* functions read no internal table; only a table-source
    # position does. Narrowed after a review found these wrongly rejected.
    "SELECT sqlite_version()",
    "SELECT sqlite_source_id()",
    "SELECT sqlite_version() AS v FROM customers",
    # A scalar stays a scalar inside nesting. Classifying it from an enclosing
    # query's subquery position rejected all three of these.
    "SELECT (SELECT sqlite_version())",
    "SELECT * FROM (SELECT sqlite_version() AS v)",
    "WITH x AS (SELECT sqlite_version() AS v) SELECT * FROM x",
    "SELECT name FROM customers WHERE sqlite_version() IS NOT NULL",
    # The old text-based internals check matched "sqlite_master" anywhere in
    # the string, including inside a literal, and a real table just happening
    # to be prefixed "sqlite_" would have been indistinguishable from it.
    "SELECT * FROM t WHERE note = 'sqlite_master'",
    "SELECT * FROM my_sqlite_notes",
    # Trailing-semicolon handling: a single trailing semicolon plus trailing
    # whitespace, and multiple trailing semicolons, must both still resolve
    # to exactly one statement.
    "SELECT 1;  ",
    "SELECT 1;;",
]

# Anything that writes, inspects internals, or smuggles a second statement.
DANGEROUS = [
    "DELETE FROM customers",
    "DROP TABLE customers",
    "UPDATE customers SET name = 'x'",
    "INSERT INTO customers VALUES (1, 'x')",
    "PRAGMA table_info(customers)",
    "ATTACH DATABASE 'other.db' AS other",
    "VACUUM",
    "REINDEX",
    "CREATE TABLE x (a INTEGER)",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM sqlite_schema",
    "SELECT 1; DROP TABLE customers",
    "SELECT * FROM t; DELETE FROM t;",
    "SELECT 1; SELECT 2",
    "WITH x AS (SELECT 1) DELETE FROM t",
    "",
    "   ",
    # SQLite internals reachable as ordinary tables/table-valued functions,
    # not just sqlite_master/sqlite_schema: the authorizer does not deny
    # SQLITE_READ for these, so this check is their only defence.
    "SELECT * FROM dbstat",
    "SELECT * FROM sqlite_stat1",
    "SELECT * FROM sqlite_sequence",
    "SELECT * FROM sqlite_temp_master",
    "SELECT * FROM sqlite_temp_schema",
    "SELECT * FROM pragma_table_list",
    "SELECT * FROM pragma_table_info('customers')",
    "SELECT * FROM SQLITE_MASTER",
    # dbstat is also a table-valued function: calling it with an argument
    # routes it through the Anonymous branch, which previously checked only
    # the prefix set and never the name set dbstat belongs to.
    "SELECT * FROM dbstat('main')",
    # A quoted table-valued function name parses as an Identifier, not a
    # str, so `.this` alone raised AttributeError instead of returning False.
    "SELECT * FROM \"pragma_table_info\"('customers')",
    "WITH x AS (SELECT * FROM dbstat('main')) SELECT * FROM x",
    "SELECT * FROM dbstat('main') AS d",
    "SELECT * FROM t JOIN pragma_table_info('c') p",
    "SELECT (SELECT count(*) FROM dbstat('main'))",
    "SELECT * FROM (SELECT * FROM dbstat('main'))",
]


@pytest.mark.parametrize("sql", LEGITIMATE)
def test_is_safe_query_allows_read_only_queries(sql: str) -> None:
    assert agent.is_safe_query(sql), f"should have been allowed: {sql!r}"


@pytest.mark.parametrize("sql", DANGEROUS)
def test_is_safe_query_blocks_unsafe_sql(sql: str) -> None:
    assert not agent.is_safe_query(sql), f"should have been blocked: {sql!r}"


def test_is_safe_query_rejects_stacked_statements_even_when_all_are_selects() -> None:
    """The AST check only ever sees the first statement, so count them explicitly."""
    assert not agent.is_safe_query("SELECT 1; SELECT 2")


def test_is_safe_query_fails_closed_when_sqlglot_is_missing(monkeypatch) -> None:
    """Without a parser there is no safety check, so refuse rather than guess."""
    from text_to_sql_agent import safety

    monkeypatch.setattr(safety, "sqlglot", None)
    monkeypatch.setattr(safety, "exp", None)
    assert not safety.is_safe_query("SELECT 1")


# --- Mutation-kill tests: each guard below is proven load-bearing by an
# input that only that guard rejects. Each was verified by temporarily
# neutering the named guard, confirming the test below fails, then
# restoring it and confirming the suite is green again.


def test_is_safe_query_rejects_a_bare_parenthesised_select() -> None:
    """Kills the `_ALLOWED_PREFIX` guard.

    "(SELECT 1)" parses to a harmless `Subquery` node containing a `Select`,
    so it clears every AST-level check `_is_safe_ast` performs; only the
    literal `SELECT`/`WITH` prefix requirement rejects it. Neutering
    `_ALLOWED_PREFIX` (always matching) flips this from blocked to allowed.
    """
    assert not agent.is_safe_query("(SELECT 1)")


def test_is_safe_query_rejects_ast_with_no_forbidden_node_and_no_select(monkeypatch) -> None:
    """Kills the `allowed_roots` / `find(exp.Select)` guard in `_is_safe_ast`.

    No real SQL text can isolate this guard: anything sqlglot successfully
    parses from a string starting with `SELECT` always contains a `Select`
    node, and anything starting with `WITH` either contains one too or hits
    a forbidden DML node first. So this substitutes a synthetic parsed
    statement - a bare `Table` node - that is neither forbidden nor a
    SELECT, via `sqlglot.parse`, to reach the guard directly. Neutering the
    guard (e.g. `return True` once the forbidden-node check has passed)
    flips this from blocked to allowed.
    """
    from text_to_sql_agent import safety

    fake_statement = safety.exp.Table(this=safety.exp.Identifier(this="t"))
    monkeypatch.setattr(safety.sqlglot, "parse", lambda *args, **kwargs: [fake_statement])
    assert not safety.is_safe_query("SELECT 1")


def test_is_safe_query_treats_double_trailing_semicolon_as_one_statement() -> None:
    """Kills the `rstrip(";")` guard.

    `sqlglot.parse("SELECT 1;;", read="sqlite")` returns two elements
    (`[Select, None]`) - the second, empty statement past the final
    semicolon - so without stripping every trailing semicolon this would be
    rejected as multi-statement. A single trailing semicolon does not
    reproduce this (`sqlglot.parse("SELECT 1;", ...)` returns one element),
    which is why this needs the doubled semicolon specifically.
    """
    assert agent.is_safe_query("SELECT 1;;")


def test_is_safe_query_uses_the_engine_dialect() -> None:
    """A dialect-specific construct must parse under its own engine."""
    from text_to_sql_agent.engines import open_engine

    sqlite_engine = open_engine("data/university_agent.db")
    assert agent.is_safe_query("SELECT strftime('%Y', d) FROM t", engine=sqlite_engine)


def test_is_safe_query_defaults_to_sqlite_when_no_engine_is_given() -> None:
    assert agent.is_safe_query("SELECT * FROM customers")
    assert not agent.is_safe_query("SELECT * FROM sqlite_master")


class _FakeInternalsEngine:
    """A minimal stand-in for a second engine's dialect/internals attributes.

    DuckDB's real engine does not exist until Task 6, so this hand-rolls the
    three attributes `is_safe_query` reads rather than waiting on it.
    `internal_prefixes` is non-empty (unlike an earlier version of this
    fixture that left it `()`) so a test using this fixture can exercise the
    prefix branch of the internals check, not just the exact-name branch.
    """

    sqlglot_dialect = "sqlite"
    internal_prefixes: tuple[str, ...] = ("widget_",)
    internal_names = frozenset({"widgets"})
    allowed_functions: frozenset[str] | None = None


def test_is_safe_query_internals_list_comes_from_the_engine_not_a_module_constant() -> None:
    """The same SQL must be safe under one engine's internals rules and blocked under another's.

    "widgets" is an ordinary table under SQLite's internals list (it is not
    `sqlite_*`, `pragma_*`, or `dbstat`), but `_FakeInternalsEngine` above
    treats it as internal via an exact name match. If `is_safe_query` still
    consulted a leftover SQLite module constant instead of the passed-in
    engine, both assertions would come out the same way - only reading
    `internal_names` off the engine argument makes them differ.
    """
    from text_to_sql_agent.engines import open_engine

    sqlite_engine = open_engine("data/university_agent.db")
    sql = "SELECT * FROM widgets"
    assert agent.is_safe_query(sql, engine=sqlite_engine)
    assert not agent.is_safe_query(sql, engine=_FakeInternalsEngine())


def test_is_safe_query_internal_prefixes_also_come_from_the_engine() -> None:
    """Same proof as above, but for `internal_prefixes` rather than `internal_names`.

    The previous test alone leaves `internal_prefixes` unexercised on the
    engine-argument path, since `_FakeInternalsEngine.internal_prefixes` used
    to be empty. "widget_log" matches no SQLite prefix (`sqlite_`, `pragma_`)
    but does match `_FakeInternalsEngine`'s `widget_` prefix, so this fails
    the same way the name-based test does if `is_safe_query` ever stopped
    reading `internal_prefixes` off the engine argument.
    """
    from text_to_sql_agent.engines import open_engine

    sqlite_engine = open_engine("data/university_agent.db")
    sql = "SELECT * FROM widget_log"
    assert agent.is_safe_query(sql, engine=sqlite_engine)
    assert not agent.is_safe_query(sql, engine=_FakeInternalsEngine())


@pytest.fixture(scope="module")
def duckdb_engine_with_tables(tmp_path_factory):
    """A real DuckDB file with tables `t` and `tables`, for Task 6b tests
    that need `is_safe_query`'s default-deny table check
    (`_references_unknown_table`) to have real tables to check against.

    `DuckDBEngine("unused.duckdb")`, used throughout this file's DuckDB
    tests before Fix 1, stopped being enough once that check started
    calling `engine.table_names()`, which needs a real, connectable
    database - a nonexistent path now raises rather than quietly validating
    on attributes alone. `tables` is created deliberately: it is what proves
    a table that merely *looks* like the `information_schema.tables`
    internal stays allowed when referenced unqualified.
    """
    duckdb = pytest.importorskip("duckdb", reason="install the duckdb extra")
    from text_to_sql_agent.engines import open_engine

    db = tmp_path_factory.mktemp("safety_default_deny") / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.execute('CREATE TABLE "tables" (a INTEGER)')
    con.close()
    return open_engine(f"duckdb://{db}")


def test_is_safe_query_blocks_a_schema_qualified_internal_reference(
    duckdb_engine_with_tables,
) -> None:
    """`information_schema.tables` must be blocked via its schema qualifier.

    Found during Task 6's Step 5 DuckDB probe: `table.name` alone is "tables",
    which is not itself internal (a table genuinely named "tables" must stay
    allowed unqualified), so the schema qualifier ("information_schema") has
    to be checked too, or a schema-qualified reference to it slips through.

    Task 6b's `_references_unknown_table` (Fix 1) enforces this same
    schema-qualifier rule independently, for a different reason: any schema
    other than `main` fails regardless of whether the bare name matches a
    real table. Both gates agree here, which is why `tables` - a table this
    engine's own database really has - is what proves the unqualified form
    stays allowed rather than merely "not yet found to be blocked".
    """
    engine = duckdb_engine_with_tables
    assert not agent.is_safe_query("SELECT * FROM information_schema.tables", engine=engine)
    assert not agent.is_safe_query("SELECT * FROM pg_catalog.pg_tables", engine=engine)
    # An unqualified table actually named "tables" (created by the
    # `duckdb_engine_with_tables` fixture) must stay allowed - it is the
    # schema qualifier, not the bare name, that makes the first two rejected.
    assert agent.is_safe_query("SELECT * FROM tables", engine=engine)


def test_is_safe_query_blocks_duckdb_filesystem_functions_it_can_name(
    duckdb_engine_with_tables,
) -> None:
    """`read_csv`/`read_parquet` parse as sqlglot's own expression classes, not
    `exp.Anonymous`, so their function name lives behind `.sql_name()` rather
    than `.name`. Found during Task 6's Step 5 probe: `read_csv(...)` passed
    `is_safe_query` before that branch existed. `enable_external_access=False`
    on the connection is still the load-bearing defence either way - this is
    defence-in-depth on top of it.

    The bare quoted-path form (`SELECT * FROM '<path>'`) used to be an
    accepted gap here, since it has no function name at all for a name-based
    check to match. Task 6b's default-deny gate closes it, but not with a
    quoting heuristic - Fix 1 replaced that first attempt
    (`_has_string_literal_table_source`, which missed the unquoted and
    double-quoted forms of the same replacement-scan read) with
    `_references_unknown_table`: any `FROM`/`JOIN` target that is not a real
    table is rejected, independent of quoting style and independent of
    `read_csv` even existing. See `test_is_safe_query_rejects_unknown_table_
    references` for that gate on its own.
    """
    engine = duckdb_engine_with_tables
    assert not agent.is_safe_query("SELECT * FROM read_csv('/etc/hosts')", engine=engine)
    assert not agent.is_safe_query("SELECT * FROM read_parquet('/etc/x.parquet')", engine=engine)
    assert not agent.is_safe_query("SELECT * FROM glob('/etc/*')", engine=engine)
    assert not agent.is_safe_query("SELECT * FROM '/etc/hosts'", engine=engine)


def test_is_safe_query_fails_closed_on_non_string_input() -> None:
    """Kills the empty/whitespace guard (`not sql_string or not sql_string.strip()`).

    No actual `str` value can isolate this guard either: any string for
    which `not sql_string.strip()` holds strips down to `""`, which
    `_ALLOWED_PREFIX` already rejects on its own, guard or no guard. `None`
    is the one input the guard's first half (`not sql_string`) exists to
    catch before `sql_string.strip()` is reached - removing the guard turns
    this from a clean `False` into an uncaught `AttributeError`.
    """
    from text_to_sql_agent import safety

    assert not safety.is_safe_query(None)  # type: ignore[arg-type]


# --- Task 6b: DuckDB's default-deny function *and table* allowlist ---
# The mechanism itself (`_resolve_function_name`, `_FUNCTION_NAME_OVERRIDES`,
# the round trip for every entry in `DuckDBEngine.allowed_functions`, the
# full-catalogue sweep, the analytics corpus, and the pinned leaks) is
# exercised in `tests/test_engine_duckdb.py`, since it only ever activates
# for an engine whose `allowed_functions` is a set - today, only DuckDB.
# These tests stay here because they are about `is_safe_query`'s general
# contract: that default-deny is skipped entirely for an engine that opts
# out (`allowed_functions is None`), that an unrecognised function is
# rejected regardless of position, and that a `FROM`/`JOIN` target naming no
# real table is rejected regardless of how it is quoted - the last of which
# is `_references_unknown_table`'s job and nothing else in this file's.


def test_is_safe_query_rejects_unknown_table_references(duckdb_engine_with_tables) -> None:
    """Fix 1 (2026-09-19 review round): every quoting style DuckDB's
    replacement scan accepts is rejected, plus a comma-join and a `JOIN`.

    Task 6b's first attempt (`_has_string_literal_table_source`, deleted)
    checked the token immediately after `FROM`/`JOIN` for a `STRING` token,
    which only ever caught the single-quoted form. Verified live with
    external access enabled (2026-09-19) that DuckDB's replacement scan
    reads a file identically whether the path is single-quoted, double-quoted,
    or not quoted at all - `FROM 'data.csv'`, `FROM "data.csv"` and `FROM
    data.csv` all read the file - so a token-type check could never have
    covered the last two: sqlglot's own parse tree gives `"data.csv"` and
    `'data.csv'` the *same* `exp.Identifier(quoted=True)` node, indistinguishable
    from a real double-quoted table name, and unquoted `data.csv` parses as
    an ordinary schema-qualified reference (`db="data"`, `name="csv"`), no
    different in shape from `main.orders`. `_references_unknown_table`
    replaces the heuristic with the actual question: does this name a real
    table? None of these five names ever could.
    """
    engine = duckdb_engine_with_tables
    assert not agent.is_safe_query("SELECT * FROM 'data.csv'", engine=engine)
    assert not agent.is_safe_query('SELECT * FROM "data.csv"', engine=engine)
    assert not agent.is_safe_query("SELECT * FROM data.csv", engine=engine)
    assert not agent.is_safe_query("SELECT * FROM t, 'data.csv'", engine=engine)
    assert not agent.is_safe_query("SELECT * FROM t JOIN 'data.csv' ON true", engine=engine)
    # Pinned here too, not just in test_is_safe_query_blocks_a_schema_
    # qualified_internal_reference: information_schema is a real DuckDB
    # schema, not a nonexistent path, so it exercises the schema-qualifier
    # branch of this same function rather than the "no such table" branch
    # the five assertions above exercise.
    assert not agent.is_safe_query("SELECT * FROM information_schema.tables", engine=engine)
    # Controls: a real double-quoted identifier, a real schema-qualified
    # reference to a real table, and a string literal that is not a
    # FROM/JOIN target, must all stay allowed.
    assert agent.is_safe_query('SELECT * FROM "t"', engine=engine)
    assert agent.is_safe_query("SELECT * FROM main.t", engine=engine)
    assert agent.is_safe_query("SELECT * FROM t WHERE a = 'data.csv'", engine=engine)


def test_default_deny_table_check_is_load_bearing(monkeypatch, duckdb_engine_with_tables) -> None:
    """Kills `_references_unknown_table` directly, proving it - not some
    other check - is what rejects the unquoted and double-quoted forms.

    Neutering it (always returning `False`, as if every table existed) flips
    both from rejected to allowed. That both were rejected *before* this
    patch and are *not* rejected once it is neutered is the demonstration
    that this function is genuinely load-bearing for them, and that neither
    form was ever caught by anything else in `is_safe_query` - the same
    proof `test_is_safe_query_rejects_unknown_table_references` above states
    from the DuckDB-behaviour side; this states it from the code side.
    """
    from text_to_sql_agent import safety

    engine = duckdb_engine_with_tables
    assert not agent.is_safe_query("SELECT * FROM data.csv", engine=engine)
    assert not agent.is_safe_query('SELECT * FROM "data.csv"', engine=engine)

    monkeypatch.setattr(safety, "_references_unknown_table", lambda *args, **kwargs: False)

    assert agent.is_safe_query("SELECT * FROM data.csv", engine=engine)
    assert agent.is_safe_query('SELECT * FROM "data.csv"', engine=engine)


def test_list_aggregate_dispatch_argument_is_validated(duckdb_engine_with_tables) -> None:
    """Fix 1: `list_aggregate`'s own name being in `allowed_functions` is not
    enough - its second argument names a *different* function to actually
    run (`duckdb_functions()` itself calls that parameter `function_name`),
    and that name must independently be allowed too, or the call is
    rejected outright.

    `list_aggregate([...], 'histogram')` really does run `histogram` against
    the list at the SQL level, even though `histogram` is correctly rejected
    everywhere else `is_safe_query` would see it - proven live against this
    build before this fix existed. A non-literal second argument (a column
    reference here) is rejected too, since no static check can know what it
    would dispatch to.
    """
    engine = duckdb_engine_with_tables
    assert agent.is_safe_query("SELECT list_aggregate(a, 'sum') FROM t", engine=engine)
    assert agent.is_safe_query("SELECT list_aggregate(a, 'count') FROM t", engine=engine)
    assert not agent.is_safe_query("SELECT list_aggregate(a, 'histogram') FROM t", engine=engine)
    assert not agent.is_safe_query("SELECT list_aggregate(a, 'checkpoint') FROM t", engine=engine)
    assert not agent.is_safe_query("SELECT list_aggregate(a, a) FROM t", engine=engine)
    # The four aliases of the same scalar function are already rejected
    # outright by name (none are in DuckDBEngine.allowed_functions), so the
    # dispatch-argument rule never even has to run for them - but confirm
    # they stay rejected regardless of which argument they are given.
    for alias in ("array_aggregate", "list_aggr", "array_aggr", "aggregate"):
        assert not agent.is_safe_query(f"SELECT {alias}(a, 'sum') FROM t", engine=engine)


def test_is_safe_query_default_deny_is_skipped_when_allowed_functions_is_none() -> None:
    """Kills the `if allowed_functions is not None:` guard in `_is_safe_ast`.

    SQLite's `allowed_functions` is `None`, so neither the function-name
    gate nor the table-existence gate may run for it - both are DuckDB-only
    today. This uses a function name that is real DuckDB syntax but is
    nowhere in `DuckDBEngine.allowed_functions` (`current_setting`, proven
    live-leaking in the task brief) against `SQLiteEngine`: since sqlglot's
    SQLite dialect parses it as an ordinary unrecognised identifier/function
    call rather than rejecting the text outright, this only stays allowed
    under SQLite if default-deny is genuinely skipped rather than
    accidentally applied with an empty set.
    """
    from text_to_sql_agent.engines.sqlite import SQLiteEngine

    engine = SQLiteEngine("data/university_agent.db")
    assert engine.allowed_functions is None
    assert agent.is_safe_query("SELECT current_setting('x')", engine=engine)


def test_is_safe_query_rejects_an_unrecognised_function_in_any_position() -> None:
    """The core default-deny property, independent of any specific leak: a
    function name that is not real DuckDB syntax at all must be rejected
    both as a scalar and as a table source, because default-deny does not
    special-case "this name doesn't even exist" as somehow safer than a real
    but unlisted one - unknown means no, unconditionally.
    """
    pytest.importorskip("duckdb", reason="install the duckdb extra")
    from text_to_sql_agent.engines.duckdb import DuckDBEngine

    engine = DuckDBEngine("unused.duckdb")
    assert not agent.is_safe_query("SELECT totally_made_up_function(a) FROM t", engine=engine)
    assert not agent.is_safe_query("SELECT * FROM totally_made_up_function(a)", engine=engine)


class _FakePostgresEngine:
    """A PostgreSQL-dialect engine with a hostile column, and no server.

    `is_safe_query` reads seven attributes off an engine, so the qualified-column
    resolution model can be pinned without a live PostgreSQL: the live proof
    lives in `tests/test_engine_postgres.py`, which skips wherever
    `AIPA_TEST_POSTGRES_DSN` is unset, and this is what keeps the model itself
    covered everywhere else.

    `other.audit` is the shape that caused the 2026-09-26 regression: a table
    outside the default schema whose column is named after a single-argument
    catalogue function (`lo_get`). Task 6 widened the engines from one schema
    to every readable schema, and while the validator resolved a qualifier
    against a flat union of every advertised column, that one column re-armed
    PostgreSQL's `alias.name` -> `name(alias)` bypass for every function scan
    in the database.
    """

    name = "fake-postgres"
    sqlglot_dialect = "postgres"
    default_schema = "public"
    internal_prefixes: tuple[str, ...] = ("pg_",)
    internal_names = frozenset({"information_schema"})
    allowed_functions: frozenset[str] | None = frozenset({"count", "sum", "generate_series"})

    def table_names(self) -> frozenset[str]:
        return frozenset({"customers", "public.customers", "other.audit"})

    def table_columns(self) -> dict[str, frozenset[str]]:
        customers = frozenset({"customer_id", "name"})
        return {
            "customers": customers,
            "public.customers": customers,
            "other.audit": frozenset({"lo_get", "note"}),
        }

    def shadowed_function_names(self) -> frozenset[str]:
        """Nothing is shadowed on this fake - see `Engine.shadowed_function_names`.

        Live coverage for a genuinely shadowed name lives in
        `tests/test_engine_postgres.py`, which needs a real `pg_proc` to
        create an overload in; this fake exists for the qualified-column
        resolution tests below, none of which is about function identity.
        """
        return frozenset()


# Each case is (accepted, sql). The resolution model under test: a qualifier
# bound to a real table resolves against that table's columns; one bound to a
# function scan resolves against the function's own output name alone; a CTE,
# derived table or unbound qualifier falls back to the columns of the tables
# this statement references plus the names it binds - never to every column in
# the database, which is what the regression turned the universe into.
_QUALIFIED_COLUMN_RESOLUTION_CASES: list[tuple[bool, str, str]] = [
    (
        False,
        "function_scan_borrowing_another_schemas_column",
        "SELECT g.lo_get FROM generate_series(1,5) g",
    ),
    (
        False,
        "function_scan_beside_the_hostile_table",
        "SELECT g.lo_get FROM other.audit a, generate_series(1,5) g",
    ),
    (False, "base_table_borrowing_another_tables_column", "SELECT c.lo_get FROM customers c"),
    # Nearest scope wins in both directions: the qualifier is resolved where
    # PostgreSQL resolves it, not by whichever relation in the statement
    # happens to share the alias.
    (
        False,
        "inner_scope_must_not_lend_its_table_to_an_outer_function_scan",
        "SELECT g.lo_get FROM generate_series(1,5) g WHERE EXISTS (SELECT 1 FROM other.audit g)",
    ),
    (
        True,
        "inner_function_scan_must_not_shadow_an_outer_real_table",
        "SELECT a.name FROM customers a WHERE EXISTS (SELECT 1 FROM generate_series(1,5) a)",
    ),
    (True, "its_own_tables_column", "SELECT a.lo_get FROM other.audit a"),
    (
        True,
        "function_scans_own_output_column",
        "SELECT g.generate_series FROM generate_series(1,5) g",
    ),
    (True, "function_scan_alias_list", "SELECT g.n FROM generate_series(1,5) AS g(n)"),
    (
        True,
        "derived_table_over_the_referenced_table",
        "SELECT t.lo_get FROM (SELECT * FROM other.audit) t",
    ),
    (
        True,
        "cte_over_the_referenced_table",
        "WITH t AS (SELECT * FROM other.audit) SELECT t.lo_get FROM t",
    ),
    (
        True,
        "schema_qualified_reference_to_a_bare_from",
        "SELECT public.customers.name FROM customers",
    ),
    (
        True,
        "correlated_reference_to_the_outer_scope",
        "SELECT c.name FROM customers c WHERE EXISTS "
        "(SELECT 1 FROM other.audit a WHERE a.lo_get = c.customer_id)",
    ),
]


@pytest.mark.parametrize(
    ("accepted", "label", "sql"),
    _QUALIFIED_COLUMN_RESOLUTION_CASES,
    ids=[label for _, label, _ in _QUALIFIED_COLUMN_RESOLUTION_CASES],
)
def test_qualified_column_resolution_is_scoped_to_the_referenced_tables(
    accepted: bool, label: str, sql: str
) -> None:
    """Both halves of the 2026-09-26 scoping fix, server-free.

    The rejections are the bypass: PostgreSQL reads `alias.name` as
    `name(alias)` wherever `name` is not a column the qualifier can supply, so
    a function scan must not be able to borrow one from a table elsewhere in
    the database. The acceptances are its price, and the reason the fix is
    scoped rather than a blanket refusal of unresolved qualifiers - a false
    rejection costs a user an answerable question.
    """
    engine = _FakePostgresEngine()
    assert agent.is_safe_query(sql, engine=engine) is accepted, f"{label}: {sql!r}"


# --- The relation-kind sweep (final whole-phase review, 2026-09-26) ---------
#
# `safety._relation_binding` used to recognise two function-scan spellings and
# treat *every other* relation kind permissively. Two further PostgreSQL
# function-scan spellings landed in that permissive branch - `FROM ROWS FROM
# (...) g` and `FROM unnest(...) g` - and either one re-armed the `alias.name`
# bypass in full as soon as `unnest` was allowlisted, which any deployment
# with an array column needs. The default is now inverted: only the kinds
# `safety._relation_kind` recognises as opaque resolve permissively.
#
# This sweeps every relation node sqlglot's `postgres` dialect can put in a
# table-source position and pins which branch each one takes, so a future
# sqlglot release that introduces a fifth cannot quietly inherit the
# permissive one - it lands in `unrecognised`, which is a refusal.
_RELATION_KIND_SWEEP: list[tuple[str, str, str]] = [
    ("plain_table", "SELECT * FROM customers", "table"),
    ("schema_qualified_table", "SELECT * FROM public.customers", "table"),
    ("cte_reference", "WITH t AS (SELECT 1 AS x) SELECT * FROM t", "table"),
    ("derived_table", "SELECT * FROM (SELECT 1 AS x) t", "opaque"),
    ("parenthesised_join_tree", "SELECT * FROM (customers JOIN sales ON TRUE)", "opaque"),
    ("values_list", "SELECT * FROM (VALUES (1)) AS v(x)", "opaque"),
    ("lateral_subquery", "SELECT * FROM LATERAL (SELECT 1 AS x) s", "opaque"),
    ("function_scan", "SELECT * FROM generate_series(1,5) g", "function-scan"),
    ("function_scan_anonymous", "SELECT * FROM some_srf(1) g", "function-scan"),
    ("xmltable", "SELECT * FROM xmltable('/r' PASSING d COLUMNS a text) x", "function-scan"),
    ("lateral_function_scan", "SELECT * FROM LATERAL generate_series(1,5) g", "function-scan"),
    ("unnest", "SELECT * FROM unnest('{a}'::text[]) g", "function-scan"),
    ("unnest_with_ordinality", "SELECT * FROM unnest(ARRAY[1]) WITH ORDINALITY g", "function-scan"),
    ("rows_from", "SELECT * FROM ROWS FROM (generate_series(1,2)) g", "function-scan"),
    (
        "rows_from_multi",
        "SELECT * FROM ROWS FROM (generate_series(1,2), some_srf(3)) g(a, b)",
        "function-scan",
    ),
]


@pytest.mark.parametrize(
    ("label", "sql", "expected"),
    _RELATION_KIND_SWEEP,
    ids=[label for label, _, _ in _RELATION_KIND_SWEEP],
)
def test_every_postgres_relation_kind_takes_a_known_branch(
    label: str, sql: str, expected: str
) -> None:
    """Sweep of sqlglot's `postgres` dialect: each relation node in a `FROM`
    must land in the branch this pins, and only `opaque` resolves permissively.
    """
    sqlglot = pytest.importorskip("sqlglot")
    from text_to_sql_agent import safety

    parsed = sqlglot.parse_one(sql, read="postgres")
    relations = [
        relation
        for scope in parsed.find_all(safety.exp.Select)
        for relation in safety._scope_relations(scope)
    ]
    kinds = [safety._relation_kind(relation) for relation in relations]
    assert expected in kinds, f"{label}: {kinds}"
    assert safety._RELATION_UNRECOGNISED not in kinds, f"{label}: {kinds}"


class _FakePostgresEngineWithUnnest(_FakePostgresEngine):
    """`_FakePostgresEngine` with `unnest` allowlisted - the one legitimate
    entry that re-armed the bypass.

    The pin must not depend on a taste call in a different file:
    `PostgresEngine.allowed_functions` leaves `unnest` off only because
    nothing in the demo schema has an array column, and any deployment that
    does needs it back. `cast` is here for the `'{...}'::text[]` literal the
    payload uses to build an array without `ARRAY[...]` (which parses to
    `exp.Array` and resolves to the `list_value` token instead).
    """

    name = "fake-postgres-with-unnest"
    allowed_functions: frozenset[str] | None = frozenset(
        {"count", "sum", "generate_series", "unnest", "cast", "list_value"}
    )


# Each of these validated at BASE (commit 50742db) with `unnest` allowlisted,
# and the first one executed against live PostgreSQL returning
# `('customers', 1)` - `to_regclass` resolving a relation name through
# `pg_class`, i.e. the `::regclass` catalogue enumeration this phase already
# closed once, reached again through a different node class.
_FUNCTION_SCAN_SPELLING_PAYLOADS: list[tuple[str, str]] = [
    (
        "unnest_aliased",
        "SELECT g.to_regclass, 1 AS to_regclass FROM unnest('{customers}'::text[]) g",
    ),
    (
        "unnest_unaliased",
        "SELECT unnest.to_regclass, 1 AS to_regclass FROM unnest('{customers}'::text[])",
    ),
    (
        "unnest_lo_get",
        "SELECT g.lo_get, 1 AS lo_get FROM unnest('{customers}'::text[]) g",
    ),
    (
        "unnest_borrowing_a_real_column",
        "SELECT g.lo_get FROM other.audit a, unnest('{customers}'::text[]) g",
    ),
    (
        "lateral_unnest",
        "SELECT g.to_regclass, 1 AS to_regclass FROM customers c "
        "JOIN LATERAL unnest('{customers}'::text[]) g ON TRUE",
    ),
    (
        "rows_from",
        "SELECT g.to_regclass, 1 AS to_regclass FROM ROWS FROM (generate_series(1,2)) g",
    ),
    (
        "rows_from_unnest",
        "SELECT g.to_regclass, 1 AS to_regclass FROM ROWS FROM (unnest('{customers}'::text[])) g",
    ),
    (
        "rows_from_with_alias_list",
        "SELECT g.to_regclass, 1 AS to_regclass FROM ROWS FROM (generate_series(1,2)) g(n)",
    ),
]


@pytest.mark.parametrize(
    ("label", "sql"),
    _FUNCTION_SCAN_SPELLING_PAYLOADS,
    ids=[label for label, _ in _FUNCTION_SCAN_SPELLING_PAYLOADS],
)
def test_no_function_scan_spelling_can_re_arm_the_column_call_bypass(label: str, sql: str) -> None:
    """Every function-scan spelling must resolve strictly, with `unnest`
    allowlisted rather than absent - so this pins the seam itself, not the
    accident of a name being off a list in another module.
    """
    engine = _FakePostgresEngineWithUnnest()
    assert not agent.is_safe_query(sql, engine=engine), f"wrongly accepted ({label}): {sql!r}"


def test_the_function_scan_fixture_is_actually_armed() -> None:
    """The negative assertions above would pass for the wrong reason if
    `unnest` were still refused on its name. A function scan's own output
    column must still validate through every spelling.
    """
    engine = _FakePostgresEngineWithUnnest()
    assert agent.is_safe_query("SELECT g.unnest FROM unnest('{a}'::text[]) g", engine=engine)
    assert agent.is_safe_query("SELECT g.n FROM unnest('{a}'::text[]) AS g(n)", engine=engine)
    assert agent.is_safe_query(
        "SELECT g.generate_series FROM generate_series(1,5) g", engine=engine
    )


_STRICT_RELATION_BINDINGS: list[tuple[str, str, frozenset[str], frozenset[str]]] = [
    (
        "rows_from",
        "SELECT * FROM ROWS FROM (generate_series(1,2)) g",
        frozenset({"g"}),
        frozenset({"generate_series"}),
    ),
    (
        "rows_from_multi_with_alias_list",
        "SELECT * FROM ROWS FROM (generate_series(1,2), some_srf(3)) g(a, b)",
        frozenset({"g"}),
        frozenset({"a", "b", "generate_series", "some_srf"}),
    ),
    (
        "rows_from_unaliased",
        "SELECT * FROM ROWS FROM (generate_series(1,2))",
        frozenset({"generate_series"}),
        frozenset({"generate_series"}),
    ),
    (
        "unnest",
        "SELECT * FROM unnest('{a}'::text[]) g",
        frozenset({"g"}),
        frozenset({"unnest"}),
    ),
    (
        "unnest_unaliased",
        "SELECT * FROM unnest('{a}'::text[])",
        frozenset({"unnest"}),
        frozenset({"unnest"}),
    ),
    (
        "lateral_unnest",
        "SELECT * FROM LATERAL unnest('{a}'::text[]) u",
        frozenset({"u"}),
        frozenset({"unnest"}),
    ),
]


@pytest.mark.parametrize(
    ("label", "sql", "qualifiers", "names"),
    _STRICT_RELATION_BINDINGS,
    ids=[label for label, _, _, _ in _STRICT_RELATION_BINDINGS],
)
def test_function_scan_spellings_bind_strictly(
    label: str, sql: str, qualifiers: frozenset[str], names: frozenset[str]
) -> None:
    """`ROWS FROM (...)` is refused end-to-end by `_references_unknown_table`
    too - its outer `exp.Table` carries no name for that rule to match - but
    that is an accident of how sqlglot spells the node, not a decision about
    this seam. This asserts the seam directly: each spelling binds exactly its
    own output names, so the refusal survives any change to the other rule.
    """
    sqlglot = pytest.importorskip("sqlglot")
    from text_to_sql_agent import safety

    parsed = sqlglot.parse_one(sql, read="postgres")
    relation = safety._scope_relations(parsed)[0]
    assert safety._relation_kind(relation) == safety._RELATION_FUNCTION_SCAN, label
    bound_qualifiers, bound_names = safety._relation_binding(relation, real_table_columns={})
    assert bound_qualifiers == qualifiers, label
    assert bound_names == names, label
