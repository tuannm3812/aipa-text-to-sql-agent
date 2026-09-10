from __future__ import annotations

import unittest

import text_to_sql_agent as agent


class TestSafety(unittest.TestCase):
    def test_is_safe_query_allows_read_only_queries(self) -> None:
        self.assertTrue(agent.is_safe_query("SELECT name FROM customers;"))
        self.assertTrue(
            agent.is_safe_query(
                "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM sales "
                "GROUP BY customer_id) SELECT * FROM totals"
            )
        )

    def test_is_safe_query_blocks_unsafe_sql(self) -> None:
        for sql in [
            "DELETE FROM customers",
            "DROP TABLE customers",
            "UPDATE customers SET name = 'x'",
            "PRAGMA table_info(customers)",
            "SELECT * FROM sqlite_master",
        ]:
            with self.subTest(sql=sql):
                self.assertFalse(agent.is_safe_query(sql))
