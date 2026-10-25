import unittest

import sqlglot
from sqlglot.errors import UnsupportedError

GLOBAL_AGGREGATE = "SELECT SUM(x) AS s FROM t WHERE y > 0 GROUP BY () HAVING SUM(x) IS NOT NULL"


class TestEmptyGroupBy(unittest.TestCase):
    def test_dropped_where_unsupported(self):
        for dialect in ("snowflake", "spark", "databricks", "datafusion"):
            with self.subTest(dialect=dialect):
                self.assertEqual(
                    sqlglot.transpile(GLOBAL_AGGREGATE, read="postgres", write=dialect)[0],
                    "SELECT SUM(x) AS s FROM t WHERE y > 0 HAVING SUM(x) IS NOT NULL",
                )
                self.assertEqual(
                    sqlglot.transpile(
                        "SELECT COUNT(*) FROM t GROUP BY ()", read="postgres", write=dialect
                    )[0],
                    "SELECT COUNT(*) FROM t",
                )

    def test_sqlite_drops_the_clause_and_lifts_having_into_a_subquery(self):
        self.assertEqual(
            sqlglot.transpile(
                "SELECT COUNT(*) FROM t GROUP BY ()", read="postgres", write="sqlite"
            )[0],
            "SELECT COUNT(*) FROM t",
        )
        self.assertEqual(
            sqlglot.transpile(GLOBAL_AGGREGATE, read="postgres", write="sqlite")[0],
            "SELECT s FROM (SELECT SUM(x) AS s, SUM(x) IS NOT NULL AS _having FROM t WHERE y > 0) "
            "AS _global WHERE _having",
        )
        self.assertEqual(
            sqlglot.transpile(
                "SELECT 100 * SUM(x) / SUM(y), COUNT(*) AS n FROM t GROUP BY () HAVING SUM(y) > 0",
                read="postgres",
                write="sqlite",
            )[0],
            "SELECT _col_0, n FROM (SELECT CAST(100 * SUM(x) AS REAL) / SUM(y) AS _col_0, COUNT(*) AS n, "
            "SUM(y) > 0 AS _having FROM t) AS _global WHERE _having",
        )
        self.assertEqual(
            sqlglot.transpile(
                "SELECT 1 AS _having, SUM(x) AS s FROM t GROUP BY () HAVING SUM(x) > 0",
                read="postgres",
                write="sqlite",
            )[0],
            "SELECT _having, s FROM (SELECT 1 AS _having, SUM(x) AS s, SUM(x) > 0 AS _having_ "
            "FROM t) AS _global WHERE _having_",
        )

    def test_kept_where_supported(self):
        for dialect in ("postgres", "duckdb", "bigquery", "trino", "clickhouse"):
            with self.subTest(dialect=dialect):
                self.assertIn(
                    "GROUP BY ()",
                    sqlglot.transpile(GLOBAL_AGGREGATE, read="postgres", write=dialect)[0],
                )

    def test_kept_when_dropping_would_change_the_result(self):
        for query in (
            "SELECT 1 FROM t GROUP BY ()",
            "SELECT k FROM t GROUP BY (), k",
            "SELECT SUM(x) OVER () FROM t GROUP BY ()",
        ):
            with self.subTest(query=query):
                for dialect in ("snowflake", "spark", "datafusion", "sqlite"):
                    generated = sqlglot.transpile(query, read="postgres", write=dialect)[0]
                    self.assertIn("GROUP BY", generated)
                    with self.assertRaises(UnsupportedError):
                        sqlglot.transpile(
                            query,
                            read="postgres",
                            write=dialect,
                            unsupported_level=sqlglot.ErrorLevel.RAISE,
                        )

    def test_grouping_sets_are_not_empty_group_by(self):
        query = "SELECT SUM(x) FROM t GROUP BY GROUPING SETS (())"
        for dialect in ("snowflake", "spark", "datafusion"):
            self.assertEqual(
                sqlglot.transpile(query, read="postgres", write=dialect)[0].count("GROUPING SETS"),
                1,
            )


if __name__ == "__main__":
    unittest.main()
