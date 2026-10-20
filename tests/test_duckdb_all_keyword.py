import unittest

import sqlglot
from tests import engines
from tests.engines import requires_engine


def to_duckdb(sql, identify=True, read="postgres"):
    return sqlglot.transpile(sql, read=read, write="duckdb", identify=identify)[0]


@requires_engine("duckdb")
class TestDuckDBAllKeywordExecution(unittest.TestCase):
    def test_group_by_all_and_order_by_all_execute(self):
        for source in (
            "WITH t AS (SELECT 1 AS x) SELECT x FROM t GROUP BY ALL",
            "WITH t AS (SELECT 1 AS x) SELECT x FROM t ORDER BY ALL",
            "WITH t AS (SELECT 1 AS x) SELECT x FROM t GROUP BY ALL ORDER BY ALL",
        ):
            for identify in (False, True):
                with self.subTest(source=source, identify=identify):
                    sql = to_duckdb(source, identify)
                    self.assertNotIn('"ALL"', sql)
                    self.assertEqual(engines.execute("duckdb", sql), [(1,)])

    def test_order_by_all_orders_by_every_output_column(self):
        rows = "(VALUES (2, 'b'), (1, 'z'), (NULL, 'q'), (1, 'a')) AS v(x, y)"
        for source, expected in (
            (
                f"SELECT x, y FROM {rows} ORDER BY ALL",
                [(1, "a"), (1, "z"), (2, "b"), (None, "q")],
            ),
            (
                f"SELECT x, y FROM {rows} ORDER BY ALL DESC",
                [(None, "q"), (2, "b"), (1, "z"), (1, "a")],
            ),
            (
                f"SELECT x, COUNT(*) FROM {rows} GROUP BY ALL ORDER BY ALL",
                [(1, 2), (2, 1), (None, 1)],
            ),
            (
                f"SELECT x FROM {rows} UNION ALL SELECT 0 ORDER BY ALL",
                [(0,), (1,), (1,), (2,), (None,)],
            ),
        ):
            with self.subTest(source=source):
                self.assertEqual(engines.execute("duckdb", to_duckdb(source)), expected)


if __name__ == "__main__":
    unittest.main()
