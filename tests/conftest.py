"""Shared fixtures for the text-to-sql agent test suite.

PostgreSQL tests need a real server. They read `AIPA_TEST_POSTGRES_DSN` and skip
with an explicit reason when it is unset or unreachable, so a developer without
Docker can still run the suite. CI sets the variable and asserts nothing skipped,
so a silent skip cannot hide a broken engine.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

POSTGRES_DSN_ENV = "AIPA_TEST_POSTGRES_DSN"

_SKIP_REASON = (
    f"set {POSTGRES_DSN_ENV} to a reachable PostgreSQL DSN to run these "
    "(docker compose -f docker/postgres.yml up -d)"
)


@pytest.fixture
def customers_db(tmp_path: Path) -> str:
    """A database with one `customers` table holding a single row."""
    db_path = tmp_path / "customers.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO customers VALUES (1, 'Alice')")
        conn.commit()
    return str(db_path)


@pytest.fixture
def customers_courses_db(tmp_path: Path) -> str:
    """`customers` (one row) plus an unrelated `courses` table.

    Used where a test needs one clearly relevant table and one clearly
    irrelevant one, to assert that retrieval excludes the latter.
    """
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE courses (course_id INTEGER PRIMARY KEY, course_name TEXT)")
        conn.execute("INSERT INTO customers VALUES (1, 'Alice')")
        conn.commit()
    return str(db_path)


@pytest.fixture
def customers_sales_courses_db(tmp_path: Path) -> str:
    """Three tables where `sales` has a foreign key to `customers`.

    `courses` is deliberately unrelated, so retrieval tests can assert it is
    *not* selected.
    """
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute(
            "CREATE TABLE sales (sale_id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, "
            "FOREIGN KEY(customer_id) REFERENCES customers(customer_id))"
        )
        conn.execute("CREATE TABLE courses (course_id INTEGER PRIMARY KEY, course_name TEXT)")
        conn.commit()
    return str(db_path)


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """A DSN for a reachable PostgreSQL, or skip with an explicit reason."""
    dsn = os.environ.get(POSTGRES_DSN_ENV)
    if not dsn:
        pytest.skip(_SKIP_REASON)
    psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        pytest.skip(f"{POSTGRES_DSN_ENV} is set but unreachable: {type(exc).__name__}")
    return dsn
