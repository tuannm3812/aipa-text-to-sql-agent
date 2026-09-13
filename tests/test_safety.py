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
