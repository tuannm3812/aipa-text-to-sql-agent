from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

import pandas as pd

import text_to_sql_agent as agent


class TestIngestion(unittest.TestCase):
    def test_normalize_table_name(self) -> None:
        self.assertEqual(
            agent.normalize_table_name("Sales Report", {"sales_report"}), "sales_report_2"
        )
        self.assertEqual(agent.normalize_table_name("2024 Sales!", set()), "table_2024_sales")
        self.assertEqual(agent.normalize_table_name("!!!", set()), "table")


def test_ingest_csvs_to_db_sanitizes_table_names(tmp_path: Path) -> None:
    csv_path = tmp_path / "2024 Sales Report.csv"
    pd.DataFrame({"amount": [10, 20]}).to_csv(csv_path, index=False)
    db_path = tmp_path / "out.db"

    agent.ingest_csvs_to_db([str(csv_path)], str(db_path))

    with closing(sqlite3.connect(db_path)) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        rows = conn.execute("SELECT amount FROM table_2024_sales_report").fetchall()

    assert tables == [("table_2024_sales_report",)]
    assert rows == [(10,), (20,)]
