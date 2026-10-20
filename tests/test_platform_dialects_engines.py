"""Execute the platform dialects' output and the date arithmetic rewrite on real engines.

Run by the ``postgres`` and ``in-process`` jobs of ``benchbox-engines.yml``; locally each
class skips when its engine is not available.
"""

import datetime
import unittest

import sqlglot
from sqlglot.benchbox.rewrites import date_integer_arithmetic_to_interval
from tests import engines
from tests.engines import requires_engine

DATE_COLUMNS = ["d_date"]


def rewrite_for_postgres(sql, read="netezza", **kwargs):
    tree = date_integer_arithmetic_to_interval(sqlglot.parse_one(sql, read=read), DATE_COLUMNS)
    return tree.sql("postgres", **kwargs)


def as_dates(rows):
    """Compare dates whether the engine returned a DATE or a midnight TIMESTAMP."""
    return [tuple(v.date() if isinstance(v, datetime.datetime) else v for v in row) for row in rows]


@requires_engine("postgres")
class TestDateArithmeticOnPostgres(unittest.TestCase):
    SETUP = (
        "CREATE TEMP TABLE date_dim (d_date_sk INT, d_date DATE, d_ts TIMESTAMP)",
        "INSERT INTO date_dim VALUES "
        "(1, DATE '2000-02-27', TIMESTAMP '2000-02-27 00:00:00'), "
        "(2, DATE '2000-12-30', TIMESTAMP '2000-12-30 00:00:00'), "
        "(3, DATE '2001-03-01', TIMESTAMP '2001-03-01 00:00:00')",
    )

    def run_all(self, *statements):
        with engines.connect("postgres") as connection:
            for statement in self.SETUP:
                connection.execute(statement)
            return [connection.execute(statement) for statement in statements]

    def test_rewritten_arithmetic_equals_native_date_arithmetic(self):
        native = "SELECT d_date + 3, d_date - 60, d_date_sk FROM date_dim ORDER BY d_date_sk"
        rewritten = rewrite_for_postgres(native)
        self.assertIn("INTERVAL '3 DAY'", rewritten)
        self.assertIn("INTERVAL '60 DAY'", rewritten)
        native_rows, rewritten_rows = self.run_all(native, rewritten)
        self.assertEqual(
            native_rows,
            [
                (datetime.date(2000, 3, 1), datetime.date(1999, 12, 29), 1),
                (datetime.date(2001, 1, 2), datetime.date(2000, 10, 31), 2),
                (datetime.date(2001, 3, 4), datetime.date(2000, 12, 31), 3),
            ],
        )
        self.assertEqual(as_dates(rewritten_rows), native_rows)

    def test_rewritten_predicate_selects_the_same_rows(self):
        native = (
            "SELECT d_date_sk FROM date_dim "
            "WHERE d_date + 5 BETWEEN DATE '2000-03-01' AND DATE '2001-01-04' ORDER BY d_date_sk"
        )
        native_rows, rewritten_rows = self.run_all(native, rewrite_for_postgres(native))
        self.assertEqual(native_rows, [(1,), (2,)])
        self.assertEqual(rewritten_rows, native_rows)

    def test_quoted_netezza_query_executes(self):
        sql = "SELECT d_date_sk FROM date_dim WHERE d_date - 1 = DATE '2000-02-26'"
        (rows,) = self.run_all(rewrite_for_postgres(sql, identify=True))
        self.assertEqual(rows, [(1,)])

    def test_integer_arithmetic_needs_the_rewrite_on_a_timestamp_column(self):
        sql = "SELECT d_ts + 3 FROM date_dim WHERE d_date_sk = 1"
        with engines.connect("postgres") as connection:
            for statement in self.SETUP:
                connection.execute(statement)
            with self.assertRaises(Exception) as raised:
                connection.execute(sql)
            self.assertIn("operator does not exist", str(raised.exception))
            rewritten = date_integer_arithmetic_to_interval(
                sqlglot.parse_one(sql, read="netezza"), ["d_ts"]
            ).sql("postgres")
            self.assertEqual(
                connection.execute(rewritten), [(datetime.datetime(2000, 3, 1, 0, 0),)]
            )

    def test_columns_outside_the_gate_keep_integer_arithmetic(self):
        sql = "SELECT d_date_sk + 3 FROM date_dim ORDER BY d_date_sk"
        rewritten = rewrite_for_postgres(sql)
        self.assertEqual(rewritten, sql)
        native_rows, rewritten_rows = self.run_all(sql, rewritten)
        self.assertEqual(native_rows, [(4,), (5,), (6,)])
        self.assertEqual(rewritten_rows, native_rows)


@requires_engine("datafusion")
class TestDataFusionDialectExecution(unittest.TestCase):
    def run_dialect(self, sql, read="postgres"):
        return engines.execute(
            "datafusion", sqlglot.transpile(sql, read=read, write="datafusion")[0]
        )

    def test_postgres_syntax_runs_through_the_datafusion_dialect(self):
        rows = self.run_dialect(
            "SELECT EXTRACT(YEAR FROM CAST('2024-03-05' AS DATE))::int, "
            "SUBSTRING('abcdef' FROM 2 FOR 3), 7 / 2, 7.0 / 2, 'a' || 'b'"
        )
        self.assertEqual(rows, [(2024, "bcd", 3, 3.5, "ab")])

    def test_window_and_paging(self):
        rows = self.run_dialect(
            "SELECT x, ROW_NUMBER() OVER (ORDER BY x DESC) FROM (VALUES (1), (2), (3)) AS v(x) "
            "ORDER BY x LIMIT 2 OFFSET 1"
        )
        self.assertEqual(rows, [(2, 2), (3, 1)])

    def test_netezza_source_reads_into_datafusion(self):
        rows = self.run_dialect(
            "WITH t AS (SELECT CAST('2000-02-27' AS DATE) AS d_date) SELECT d_date + 3 FROM t",
            read="netezza",
        )
        self.assertEqual(rows, [(datetime.date(2000, 3, 1),)])

    def test_rewritten_day_intervals_execute(self):
        sql = (
            "WITH t AS (SELECT CAST('2000-02-27' AS DATE) AS d_date) "
            "SELECT d_date + 3, d_date - 60 FROM t"
        )
        tree = date_integer_arithmetic_to_interval(
            sqlglot.parse_one(sql, read="netezza"), DATE_COLUMNS
        )
        rewritten = tree.sql("datafusion")
        self.assertIn("INTERVAL '3 DAY'", rewritten)
        self.assertEqual(
            engines.execute("datafusion", rewritten),
            [(datetime.date(2000, 3, 1), datetime.date(1999, 12, 29))],
        )


if __name__ == "__main__":
    unittest.main()
