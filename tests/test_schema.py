from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import text_to_sql_agent as agent


def test_get_schema_excludes_internal_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()

    schema = agent.get_schema(str(db_path))

    assert "CREATE TABLE customers" in schema
    assert "sqlite_" not in schema
