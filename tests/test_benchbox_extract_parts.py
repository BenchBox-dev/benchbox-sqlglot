"""``EXTRACT(DOW FROM x)`` and ``EXTRACT(EPOCH FROM (a - b))`` written for engines without those fields.

DuckDB (the NYC Taxi source dialect) counts weekdays from Sunday = 0 and reads the difference of two
timestamps in seconds. BigQuery, Spark, Databricks, SingleStore and SQLite have neither field, and
Snowflake's ``DATE_PART(EPOCH, (a - b))`` does not subtract timestamps. The text assertions pin the
exact output; the execution tests run it on SQLite and PySpark and compare the rows with DuckDB and
with the values worked out by hand. SingleStore has no engine in CI (``downstream/ENGINE-GAPS.md``),
so its output is asserted as text.
"""

import datetime
import unittest

import sqlglot
from sqlglot import exp, parse_one
from sqlglot.benchbox.extract_parts import epoch_difference, is_day_of_week
from tests import engines
from tests.engines import requires_engine

DOW = "SELECT EXTRACT(DOW FROM pickup_datetime) AS dow FROM trips"
DOW_GROUPED = (
    "SELECT EXTRACT(DOW FROM pickup_datetime) AS d, COUNT(*) FROM trips "
    "GROUP BY EXTRACT(DOW FROM pickup_datetime)"
)
DOW_IN = (
    "SELECT CASE WHEN EXTRACT(DOW FROM pickup_datetime) IN (0, 6) THEN 'weekend' ELSE 'weekday' END "
    "FROM trips"
)
EPOCH = "SELECT EXTRACT(EPOCH FROM (dropoff_datetime - pickup_datetime)) AS secs FROM trips"
EPOCH_BUCKET = (
    "SELECT FLOOR(EXTRACT(EPOCH FROM (dropoff_datetime - pickup_datetime)) / 60 / 5) * 5 AS b "
    "FROM trips WHERE EXTRACT(EPOCH FROM (dropoff_datetime - pickup_datetime)) BETWEEN 60 AND 7200"
)
EPOCH_AVG = (
    "SELECT AVG(EXTRACT(EPOCH FROM (pickup_datetime - request_datetime)) / 60) AS a FROM trips"
)
EPOCH_PLAIN = "SELECT EXTRACT(EPOCH FROM pickup_datetime) AS e FROM trips"


def translate(sql, dialect):
    return sqlglot.transpile(sql, read="duckdb", write=dialect)[0]


class TestShapes(unittest.TestCase):
    def extract(self, sql):
        return parse_one(sql, read="duckdb").find(exp.Extract)

    def test_day_of_week(self):
        self.assertTrue(is_day_of_week(self.extract(DOW)))
        self.assertFalse(is_day_of_week(self.extract(EPOCH)))

    def test_epoch_difference_returns_both_operands(self):
        a, b = epoch_difference(self.extract(EPOCH))
        self.assertEqual((a.sql(), b.sql()), ("dropoff_datetime", "pickup_datetime"))

    def test_other_epoch_shapes_are_not_differences(self):
        for sql in (
            EPOCH_PLAIN,
            "SELECT EXTRACT(EPOCH FROM (a + b)) FROM t",
            "SELECT EXTRACT(EPOCH FROM (a)) FROM t",
            "SELECT EXTRACT(HOUR FROM (a - b)) FROM t",
        ):
            with self.subTest(sql=sql):
                self.assertIsNone(epoch_difference(self.extract(sql)))


class TestDayOfWeek(unittest.TestCase):
    def test_every_dialect(self):
        for dialect, expected in {
            "bigquery": "SELECT (EXTRACT(DAYOFWEEK FROM pickup_datetime) - 1) AS dow FROM trips",
            "spark": "SELECT (DAYOFWEEK(pickup_datetime) - 1) AS dow FROM trips",
            "databricks": "SELECT (DAYOFWEEK(pickup_datetime) - 1) AS dow FROM trips",
            "singlestore": "SELECT (DAYOFWEEK(pickup_datetime) - 1) AS dow FROM trips",
            "sqlite": "SELECT CAST(STRFTIME('%w', pickup_datetime) AS INTEGER) AS dow FROM trips",
            "snowflake": "SELECT DATE_PART(DAYOFWEEK, pickup_datetime) AS dow FROM trips",
            "duckdb": "SELECT EXTRACT(DAYOFWEEK FROM pickup_datetime) AS dow FROM trips",
        }.items():
            with self.subTest(dialect=dialect):
                self.assertEqual(translate(DOW, dialect), expected)

    def test_group_by_and_in_list(self):
        self.assertEqual(
            translate(DOW_GROUPED, "bigquery"),
            "SELECT (EXTRACT(DAYOFWEEK FROM pickup_datetime) - 1) AS d, COUNT(*) FROM trips "
            "GROUP BY (EXTRACT(DAYOFWEEK FROM pickup_datetime) - 1)",
        )
        self.assertEqual(
            translate(DOW_IN, "sqlite"),
            "SELECT CASE WHEN CAST(STRFTIME('%w', pickup_datetime) AS INTEGER) IN (0, 6) "
            "THEN 'weekend' ELSE 'weekday' END FROM trips",
        )
        self.assertEqual(
            translate(DOW_IN, "singlestore"),
            "SELECT CASE WHEN (DAYOFWEEK(pickup_datetime) - 1) IN (0, 6) "
            "THEN 'weekend' ELSE 'weekday' END FROM trips",
        )

    def test_any_operand_not_only_a_column(self):
        self.assertEqual(
            translate("SELECT EXTRACT(DOW FROM CAST(a AS DATE)) FROM t", "spark"),
            "SELECT (DAYOFWEEK(CAST(a AS DATE)) - 1) FROM t",
        )

    def test_sqlite_refuses_an_operand_typed_as_a_number(self):
        with self.assertRaises(sqlglot.errors.UnsupportedError):
            sqlglot.transpile(
                "SELECT EXTRACT(DOW FROM CAST(a AS TIMESTAMP)) FROM t",
                read="duckdb",
                write="sqlite",
                unsupported_level=sqlglot.ErrorLevel.RAISE,
            )

    def test_other_fields_and_dialects_are_unchanged(self):
        self.assertEqual(
            translate("SELECT EXTRACT(HOUR FROM x) FROM t", "bigquery"),
            "SELECT EXTRACT(HOUR FROM x) FROM t",
        )
        for dialect in ("postgres", "mysql"):
            self.assertEqual(
                sqlglot.transpile(DOW, read="postgres", write=dialect)[0],
                "SELECT EXTRACT(DOW FROM pickup_datetime) AS dow FROM trips",
            )


class TestEpochDifference(unittest.TestCase):
    def test_every_dialect(self):
        for dialect, expected in {
            "bigquery": "SELECT (UNIX_SECONDS(TIMESTAMP(dropoff_datetime)) - "
            "UNIX_SECONDS(TIMESTAMP(pickup_datetime))) AS secs FROM trips",
            "spark": "SELECT (UNIX_TIMESTAMP(dropoff_datetime) - UNIX_TIMESTAMP(pickup_datetime)) "
            "AS secs FROM trips",
            "databricks": "SELECT (UNIX_TIMESTAMP(dropoff_datetime) - "
            "UNIX_TIMESTAMP(pickup_datetime)) AS secs FROM trips",
            "sqlite": "SELECT (CAST(STRFTIME('%s', dropoff_datetime) AS INTEGER) - "
            "CAST(STRFTIME('%s', pickup_datetime) AS INTEGER)) AS secs FROM trips",
            "snowflake": "SELECT (DATEDIFF(second, pickup_datetime, dropoff_datetime)) AS secs FROM trips",
            "singlestore": "SELECT TIMESTAMPDIFF(SECOND, pickup_datetime, dropoff_datetime) "
            "AS secs FROM trips",
        }.items():
            with self.subTest(dialect=dialect):
                self.assertEqual(translate(EPOCH, dialect), expected)

    def test_inside_arithmetic_between_and_aggregates(self):
        self.assertEqual(
            translate(EPOCH_BUCKET, "bigquery"),
            "SELECT FLOOR((UNIX_SECONDS(TIMESTAMP(dropoff_datetime)) - "
            "UNIX_SECONDS(TIMESTAMP(pickup_datetime))) / 60 / 5) * 5 AS b FROM trips "
            "WHERE (UNIX_SECONDS(TIMESTAMP(dropoff_datetime)) - "
            "UNIX_SECONDS(TIMESTAMP(pickup_datetime))) BETWEEN 60 AND 7200",
        )
        self.assertEqual(
            translate(EPOCH_BUCKET, "snowflake"),
            "SELECT FLOOR((DATEDIFF(second, pickup_datetime, dropoff_datetime)) / 60 / 5) * 5 AS b "
            "FROM trips WHERE (DATEDIFF(second, pickup_datetime, dropoff_datetime)) BETWEEN 60 AND 7200",
        )
        self.assertEqual(
            translate(EPOCH_AVG, "spark"),
            "SELECT AVG((UNIX_TIMESTAMP(pickup_datetime) - UNIX_TIMESTAMP(request_datetime)) / 60) "
            "AS a FROM trips",
        )

    def test_singlestore_is_a_difference_not_a_unix_timestamp_of_one(self):
        # MySQL's EXTRACT(EPOCH) rewrite, UNIX_TIMESTAMP((a - b)), subtracts datetimes as numbers
        # (20190105084530 - 20190105081500), which is not a number of seconds
        sql = translate(EPOCH_BUCKET, "singlestore")
        self.assertNotIn("UNIX_TIMESTAMP", sql)
        self.assertEqual(sql.count("TIMESTAMPDIFF(SECOND, pickup_datetime, dropoff_datetime)"), 2)

    def test_epoch_of_a_single_value_keeps_the_dialect_form(self):
        self.assertEqual(
            translate(EPOCH_PLAIN, "singlestore"),
            "SELECT UNIX_TIMESTAMP(pickup_datetime) AS e FROM trips",
        )
        self.assertEqual(
            translate(EPOCH_PLAIN, "snowflake"),
            "SELECT DATE_PART(EPOCH, pickup_datetime) AS e FROM trips",
        )
        self.assertEqual(
            translate(EPOCH_PLAIN, "bigquery"),
            "SELECT EXTRACT(EPOCH FROM pickup_datetime) AS e FROM trips",
        )

    def test_sqlite_refuses_an_operand_typed_as_a_number(self):
        with self.assertRaises(sqlglot.errors.UnsupportedError):
            sqlglot.transpile(
                "SELECT EXTRACT(EPOCH FROM (CAST(a AS TIMESTAMP) - b)) FROM t",
                read="duckdb",
                write="sqlite",
                unsupported_level=sqlglot.ErrorLevel.RAISE,
            )

    def test_does_not_modify_the_source_tree(self):
        tree = parse_one(EPOCH, read="duckdb")
        before = tree.sql("duckdb")
        for dialect in ("bigquery", "spark", "sqlite", "snowflake", "singlestore"):
            tree.sql(dialect)
        self.assertEqual(tree.sql("duckdb"), before)


# rows: pickup, dropoff, request; Saturday, Sunday into Monday, Wednesday
TRIPS = [
    ("2019-01-05 08:15:00", "2019-01-05 08:45:30", "2019-01-05 08:10:00"),
    ("2019-01-06 23:59:59", "2019-01-07 00:00:01", "2019-01-06 23:50:00"),
    ("2019-01-09 12:00:00", "2019-01-09 13:00:00", "2019-01-09 11:55:00"),
]
EXPECTED_DOW = [6, 0, 3]
EXPECTED_SECONDS = [
    (datetime.datetime.fromisoformat(d) - datetime.datetime.fromisoformat(p)).total_seconds()
    for p, d, _ in TRIPS
]


def numbers(rows):
    return [tuple(float(v) for v in row) for row in rows]


class ExtractExecution:
    ENGINE = ""
    DIALECT = ""

    def trips(self):
        raise NotImplementedError

    def execute(self, sql):
        with engines.connect(self.ENGINE) as connection:
            return connection.execute(
                translate(sql, self.DIALECT).replace(" FROM trips", self.trips())
            )

    def test_day_of_week(self):
        rows = self.execute(
            "SELECT EXTRACT(DOW FROM pickup_datetime) AS dow FROM trips ORDER BY pickup_datetime"
        )
        self.assertEqual([int(r[0]) for r in rows], EXPECTED_DOW)

    def test_group_by_day_of_week(self):
        rows = self.execute(DOW_GROUPED + " ORDER BY d")
        self.assertEqual([(int(d), int(n)) for d, n in rows], [(0, 1), (3, 1), (6, 1)])

    def test_in_list_of_weekend_days(self):
        rows = self.execute(DOW_IN.replace("FROM trips", "FROM trips ORDER BY pickup_datetime"))
        self.assertEqual([r[0] for r in rows], ["weekend", "weekend", "weekday"])

    def test_epoch_difference_in_seconds(self):
        rows = self.execute(EPOCH + " ORDER BY pickup_datetime")
        self.assertEqual(numbers(rows), [(s,) for s in EXPECTED_SECONDS])

    def test_epoch_difference_in_a_predicate_and_expression(self):
        rows = self.execute(EPOCH_BUCKET + " ORDER BY b")
        self.assertEqual([int(r[0]) for r in rows], [30, 60])

    def test_average_of_epoch_differences(self):
        (row,) = self.execute(EPOCH_AVG)
        expected = sum(
            (
                datetime.datetime.fromisoformat(p) - datetime.datetime.fromisoformat(r)
            ).total_seconds()
            for p, _, r in TRIPS
        ) / (60 * len(TRIPS))
        self.assertAlmostEqual(float(row[0]), expected, places=6)


@requires_engine("sqlite")
class TestSQLiteExecution(ExtractExecution, unittest.TestCase):
    ENGINE = DIALECT = "sqlite"

    def trips(self):
        rows = " UNION ALL ".join(
            f"SELECT '{p}' AS pickup_datetime, '{d}' AS dropoff_datetime, '{r}' AS request_datetime"
            for p, d, r in TRIPS
        )
        return f" FROM ({rows}) AS trips"


@requires_engine("pyspark")
class TestSparkExecution(ExtractExecution, unittest.TestCase):
    ENGINE = "pyspark"
    DIALECT = "spark"

    def trips(self):
        rows = " UNION ALL ".join(
            f"SELECT CAST('{p}' AS TIMESTAMP) AS pickup_datetime, CAST('{d}' AS TIMESTAMP) AS "
            f"dropoff_datetime, CAST('{r}' AS TIMESTAMP) AS request_datetime"
            for p, d, r in TRIPS
        )
        return f" FROM ({rows}) AS trips"


@requires_engine("duckdb")
class TestDuckDBReference(unittest.TestCase):
    """The source dialect returns the values the other engines are compared with."""

    def run_source(self, sql):
        rows = " UNION ALL ".join(
            f"SELECT TIMESTAMP '{p}' AS pickup_datetime, TIMESTAMP '{d}' AS dropoff_datetime, "
            f"TIMESTAMP '{r}' AS request_datetime"
            for p, d, r in TRIPS
        )
        with engines.connect("duckdb") as connection:
            return connection.execute(sql.replace(" FROM trips", f" FROM ({rows}) AS trips"))

    def test_expected_values_are_what_duckdb_returns(self):
        rows = self.run_source(DOW.replace("FROM trips", "FROM trips ORDER BY pickup_datetime"))
        self.assertEqual([int(r[0]) for r in rows], EXPECTED_DOW)
        rows = self.run_source(EPOCH + " ORDER BY pickup_datetime")
        self.assertEqual(numbers(rows), [(s,) for s in EXPECTED_SECONDS])


if __name__ == "__main__":
    unittest.main()
