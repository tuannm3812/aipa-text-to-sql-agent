from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import text_to_sql_agent as agent


def test_execute_query_caps_rows_and_reports_truncation(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE numbers (n INTEGER)")
        conn.executemany("INSERT INTO numbers VALUES (?)", [(1,), (2,), (3,)])
        conn.commit()

    result = agent.execute_query(str(db_path), "SELECT n FROM numbers ORDER BY n", max_rows=2)

    assert result.rows == [(1,), (2,)]
    assert result.error == "RESULT_TRUNCATED_TO_2_ROWS"


def test_execute_query_opens_database_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE numbers (n INTEGER)")
        conn.commit()

    with pytest.raises(sqlite3.DatabaseError):
        agent.execute_query(str(db_path), "DELETE FROM numbers")


def test_execute_query_returns_typed_error_when_aborted(tmp_path: Path) -> None:
    """A runaway query is aborted by the VM-step guard, not raised as OperationalError."""
    db_path = tmp_path / "big.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE n (i INTEGER)")
        conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(60_000)])
        conn.commit()

    result = agent.execute_query(str(db_path), "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i")

    assert not result.ok
    assert result.error == "QUERY_ABORTED_AFTER_100000_VM_STEPS"
    assert result.sql == "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i"


def test_execute_query_guard_can_be_disabled(tmp_path: Path) -> None:
    """max_vm_steps=0 turns the guard off, so a slow query completes."""
    db_path = tmp_path / "big.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE n (i INTEGER)")
        conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(2_000)])
        conn.commit()

    result = agent.execute_query(
        str(db_path), "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i", max_vm_steps=0
    )

    assert result.ok, result.error
    assert result.rows == [(2_000,)]


def test_execute_query_still_raises_real_operational_errors(tmp_path: Path) -> None:
    """A genuine SQL error must not be disguised as an abort."""
    db_path = tmp_path / "t.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()

    with pytest.raises(sqlite3.OperationalError):
        agent.execute_query(str(db_path), "SELECT * FROM table_that_does_not_exist")
