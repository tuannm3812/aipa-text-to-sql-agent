from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import text_to_sql_agent_mvp as agent


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
