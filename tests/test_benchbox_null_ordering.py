"""Pin the NULL placement each target writes for the source dialects BenchBox translates from.

Netezza (TPC-H, TPC-DS, SSB and the catalog benchmarks) sorts NULL as the largest value; DuckDB
(TSBS DevOps, Data Vault) and ClickHouse (ClickBench) sort NULLs last in both directions. Every target
must keep that placement, either with an explicit NULLS FIRST/LAST or, where the engine has no such
clause or rejects it in the position, with a leading ``CASE WHEN key IS NULL`` sort key. Where neither
is possible the clause is dropped with an unsupported warning, which these tests assert.
Execution on the engines available in CI is in ``tests/test_benchbox_null_ordering_engines.py``.
"""

import unittest

import sqlglot
from sqlglot import ErrorLevel
from sqlglot.generator import logger as generator_logger

KEY = "CASE WHEN {0} IS NULL THEN 1 ELSE 0 END"


def sql(query, read, write):
    return sqlglot.transpile(query, read=read, write=write)[0]


class TestExplicitPlacement(unittest.TestCase):
    QUERY = "SELECT a, b FROM t ORDER BY a, b DESC"

    def check(self, read, expected):
        for dialect, text in expected.items():
            with self.subTest(f"{read} -> {dialect}"):
                self.assertEqual(sql(self.QUERY, read, dialect), text)

    def test_netezza_sorts_nulls_as_largest(self):
        self.check(
            "netezza",
            {
                "duckdb": "SELECT a, b FROM t ORDER BY a, b DESC NULLS FIRST",
                "spark": "SELECT a, b FROM t ORDER BY a NULLS LAST, b DESC NULLS FIRST",
                "databricks": "SELECT a, b FROM t ORDER BY a NULLS LAST, b DESC NULLS FIRST",
                "bigquery": "SELECT a, b FROM t ORDER BY a NULLS LAST, b DESC NULLS FIRST",
                "snowflake": "SELECT a, b FROM t ORDER BY a, b DESC",
                "trino": "SELECT a, b FROM t ORDER BY a, b DESC NULLS FIRST",
                "postgres": "SELECT a, b FROM t ORDER BY a, b DESC",
                "datafusion": "SELECT a, b FROM t ORDER BY a, b DESC",
                "clickhouse": "SELECT a, b FROM t ORDER BY a, b DESC NULLS FIRST",
                "sqlite": "SELECT a, b FROM t ORDER BY a NULLS LAST, b DESC NULLS FIRST",
            },
        )

    def test_duckdb_and_clickhouse_sort_nulls_last_in_both_directions(self):
        for read in ("duckdb", "clickhouse"):
            self.check(
                read,
                {
                    "spark": "SELECT a, b FROM t ORDER BY a NULLS LAST, b DESC",
                    "databricks": "SELECT a, b FROM t ORDER BY a NULLS LAST, b DESC",
                    "bigquery": "SELECT a, b FROM t ORDER BY a NULLS LAST, b DESC",
                    "snowflake": "SELECT a, b FROM t ORDER BY a, b DESC NULLS LAST",
                    "trino": "SELECT a, b FROM t ORDER BY a, b DESC",
                    "postgres": "SELECT a, b FROM t ORDER BY a, b DESC NULLS LAST",
                    "datafusion": "SELECT a, b FROM t ORDER BY a, b DESC NULLS LAST",
                    "clickhouse": "SELECT a, b FROM t ORDER BY a, b DESC",
                    "sqlite": "SELECT a, b FROM t ORDER BY a NULLS LAST, b DESC",
                },
            )


class TestSimulatedPlacement(unittest.TestCase):
    def test_engines_without_a_nulls_clause_sort_on_a_case_key(self):
        for dialect in ("tsql", "mysql", "doris", "starrocks", "questdb"):
            with self.subTest(dialect):
                self.assertEqual(
                    sql("SELECT a, b FROM t ORDER BY a, b DESC", "netezza", dialect),
                    f"SELECT a, b FROM t ORDER BY {KEY.format('a')}, a, {KEY.format('b')} DESC, b DESC",
                )
                self.assertEqual(
                    sql("SELECT a, b FROM t ORDER BY a, b DESC", "duckdb", dialect),
                    f"SELECT a, b FROM t ORDER BY {KEY.format('a')}, a, b DESC",
                )

    def test_positional_order_by_keeps_the_source_placement(self):
        for dialect in ("tsql", "mysql", "doris", "starrocks", "questdb"):
            with self.subTest(dialect):
                self.assertEqual(
                    sql("SELECT a, b FROM t ORDER BY 2, 1 DESC", "netezza", dialect),
                    f"SELECT a, b FROM t ORDER BY {KEY.format('b')}, 2, {KEY.format('a')} DESC, 1 DESC",
                )

        self.assertEqual(
            sql("SELECT a, SUM(b) AS s FROM t GROUP BY a ORDER BY 2 DESC, 1", "netezza", "doris"),
            "SELECT a, SUM(b) AS s FROM t GROUP BY a ORDER BY "
            f"{KEY.format('SUM(b)')} DESC, 2 DESC, {KEY.format('a')}, 1",
        )

    def test_positional_order_by_that_cannot_be_simulated_is_dropped_with_a_warning(self):
        for query in (
            "SELECT DISTINCT a, b FROM t ORDER BY 1",
            "SELECT a FROM t UNION SELECT a FROM u ORDER BY 1",
            "SELECT * FROM t ORDER BY 1",
            "SELECT SUM(a) OVER (ORDER BY b) AS s FROM t ORDER BY 1",
        ):
            with self.subTest(query):
                with self.assertLogs(generator_logger, level="WARNING") as logs:
                    text = sql(query, "netezza", "tsql")
                self.assertNotIn("CASE WHEN", text.split("ORDER BY")[-1])
                self.assertIn("positional ordering", logs.output[0])

    def test_window_frames_that_admit_an_extra_sort_key(self):
        frames = (
            "",
            " ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            " ROWS BETWEEN 2 PRECEDING AND CURRENT ROW",
            " RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        )
        for dialect in ("bigquery", "tsql", "mysql", "doris", "starrocks"):
            for frame in frames:
                with self.subTest(f"{dialect}{frame}"):
                    self.assertEqual(
                        sql(
                            f"SELECT SUM(x) OVER (PARTITION BY p ORDER BY d{frame}) FROM t",
                            "netezza",
                            dialect,
                        ),
                        f"SELECT SUM(x) OVER (PARTITION BY p ORDER BY {KEY.format('d')}, d{frame}) FROM t",
                    )
                    self.assertEqual(
                        sql(
                            f"SELECT SUM(x) OVER (ORDER BY d DESC{frame}) FROM t", "netezza", dialect
                        ),
                        f"SELECT SUM(x) OVER (ORDER BY {KEY.format('d')} DESC, d DESC{frame}) FROM t",
                    )

    def test_range_offsets_keep_one_sort_key_and_warn(self):
        for dialect in ("bigquery", "tsql", "mysql", "doris"):
            with self.subTest(dialect):
                with self.assertLogs(generator_logger, level="WARNING") as logs:
                    text = sql(
                        "SELECT SUM(x) OVER (ORDER BY d RANGE BETWEEN 3 PRECEDING AND CURRENT ROW) FROM t",
                        "netezza",
                        dialect,
                    )
                self.assertNotIn("CASE WHEN", text)
                self.assertIn("not supported in window function SUM", logs.output[0])

    def test_questdb_cannot_sort_a_window_on_an_expression(self):
        with self.assertLogs(generator_logger, level="WARNING") as logs:
            text = sql(
                "SELECT SUM(x) OVER (ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t",
                "netezza",
                "questdb",
            )
        self.assertEqual(
            text, "SELECT SUM(x) OVER (ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t"
        )
        self.assertIn("not supported in window function SUM", logs.output[0])

    def test_questdb_reads_and_writes_nulls_as_smallest(self):
        self.assertEqual(
            sql("SELECT a FROM t ORDER BY a DESC", "questdb", "snowflake"),
            "SELECT a FROM t ORDER BY a DESC NULLS LAST",
        )
        self.assertEqual(
            sql("SELECT a FROM t ORDER BY a", "questdb", "postgres"),
            "SELECT a FROM t ORDER BY a NULLS FIRST",
        )


class TestBigQueryAggregateOrdering(unittest.TestCase):
    def test_aggregate_order_by_sorts_on_a_case_key(self):
        self.assertEqual(
            sql("SELECT ARRAY_AGG(x ORDER BY x) FROM t", "netezza", "bigquery"),
            f"SELECT ARRAY_AGG(x ORDER BY {KEY.format('x')}, x) FROM t",
        )
        self.assertEqual(
            sql("SELECT ARRAY_AGG(x ORDER BY y DESC) FROM t", "netezza", "bigquery"),
            f"SELECT ARRAY_AGG(x ORDER BY {KEY.format('y')} DESC, y DESC) FROM t",
        )

    def test_distinct_aggregate_cannot_take_an_extra_key_and_warns(self):
        with self.assertLogs(generator_logger, level="WARNING") as logs:
            text = sql("SELECT ARRAY_AGG(DISTINCT x ORDER BY x) FROM t", "netezza", "bigquery")
        self.assertEqual(text, "SELECT ARRAY_AGG(DISTINCT x ORDER BY x) FROM t")
        self.assertIn("not supported for aggregate function ARRAY_AGG", logs.output[0])

    def test_unsupported_level_raise_still_raises_for_the_remaining_drops(self):
        expression = sqlglot.parse_one(
            "SELECT SUM(x) OVER (ORDER BY d RANGE BETWEEN 3 PRECEDING AND CURRENT ROW) FROM t",
            read="netezza",
        )
        with self.assertRaises(sqlglot.errors.UnsupportedError):
            expression.sql("tsql", unsupported_level=ErrorLevel.RAISE)


if __name__ == "__main__":
    unittest.main()
