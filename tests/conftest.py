"""Shared fixtures for the text-to-sql agent test suite."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


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
