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
