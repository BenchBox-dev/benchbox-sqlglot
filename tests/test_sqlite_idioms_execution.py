"""Execute the SQLite date idioms (``JULIANDAY``, ``DATE('now' ...)``) read as Netezza on real engines.

Run by the ``in-process``, ``postgres``, ``starrocks`` and ``doris`` jobs of
``benchbox-engines.yml``; locally each class skips when its engine is not available.
The rows are built with the idioms themselves, so every comparison is relative to the
engine's own current date.
"""

import datetime
import unittest

import sqlglot
from tests import engines
from tests.engines import requires_engine

PAIRS = (
    "SELECT 1 AS id, DATE('now', '-100 days') AS d1, DATE('now', '-60 days') AS d2 "
    "UNION ALL SELECT 2, DATE('now', '-10 days'), DATE('now', '-5 days') "
    "UNION ALL SELECT 3, DATE('now', '-95 days'), DATE('now')"
)
EPOCH_DAY_ONE = "2000-01-01"


def number(value):
    if value is None:
        return None
    return float(value)


class SqliteDateIdioms:
    ENGINE = ""
    DIALECT = ""

    def query(self, sql):
        translated = sqlglot.transpile(
            sql.format(pairs=f"({PAIRS}) AS pairs", epoch=self.date_literal(EPOCH_DAY_ONE)),
            read="netezza",
            write=self.DIALECT,
            unsupported_level=sqlglot.ErrorLevel.RAISE,
        )[0]
        with engines.connect(self.ENGINE) as connection:
            return connection.execute(translated)

    def date_literal(self, value):
        return f"DATE '{value}'"

    def test_julianday_difference_is_the_number_of_days(self):
        rows = self.query("SELECT id, JULIANDAY(d2) - JULIANDAY(d1) FROM {pairs} ORDER BY id")
        self.assertEqual([(int(r[0]), number(r[1])) for r in rows], [(1, 40), (2, 5), (3, 95)])

    def test_julianday_difference_in_a_predicate(self):
        rows = self.query(
            "SELECT id FROM {pairs} WHERE ABS(JULIANDAY(d1) - JULIANDAY(d2)) <= 30 ORDER BY id"
        )
        self.assertEqual([int(r[0]) for r in rows], [2])

    def test_relative_date_in_a_predicate(self):
        rows = self.query("SELECT id FROM {pairs} WHERE d2 >= DATE('now', '-30 days') ORDER BY id")
        self.assertEqual([int(r[0]) for r in rows], [2, 3])

    def test_julianday_of_now_and_aggregates(self):
        (row,) = self.query(
            "SELECT JULIANDAY(DATE('now')) - JULIANDAY(MIN(d1)), "
            "GREATEST(JULIANDAY(MAX(d2)) - JULIANDAY(MIN(d1)), 1) * 30.44, "
            "JULIANDAY(DATE('now')) - JULIANDAY(DATE('now', '-30 days')) "
            "FROM {pairs}"
        )
        self.assertEqual([number(v) for v in row], [100, 3044.0, 30])

    def test_julianday_alone_is_a_day_number_that_composes(self):
        (row,) = self.query(
            "SELECT JULIANDAY({epoch}), JULIANDAY({epoch}) * 2, 1 - JULIANDAY({epoch})"
        )
        self.assertEqual([number(v) for v in row], [2451545, 4903090, -2451544])

    def test_now_is_the_engine_current_date(self):
        (row,) = self.query("SELECT DATE('now'), DATE('now', '-1 month') < DATE('now')")
        today = datetime.date.today()
        near = {str(today + datetime.timedelta(days=d)) for d in (-1, 0, 1)}
        self.assertIn(str(row[0])[:10], near)
        self.assertEqual(int(row[1]), 1)


@requires_engine("sqlite")
class TestSqliteDateIdiomsOnSqlite(SqliteDateIdioms, unittest.TestCase):
    ENGINE = "sqlite"
    DIALECT = "sqlite"

    def date_literal(self, value):
        return f"'{value}'"


@requires_engine("duckdb")
class TestSqliteDateIdiomsOnDuckDB(SqliteDateIdioms, unittest.TestCase):
    ENGINE = "duckdb"
    DIALECT = "duckdb"


@requires_engine("postgres")
class TestSqliteDateIdiomsOnPostgres(SqliteDateIdioms, unittest.TestCase):
    ENGINE = "postgres"
    DIALECT = "postgres"


@requires_engine("datafusion")
class TestSqliteDateIdiomsOnDataFusion(SqliteDateIdioms, unittest.TestCase):
    ENGINE = "datafusion"
    DIALECT = "datafusion"


@requires_engine("chdb")
class TestSqliteDateIdiomsOnClickHouse(SqliteDateIdioms, unittest.TestCase):
    ENGINE = "chdb"
    DIALECT = "clickhouse"

    def date_literal(self, value):
        return f"toDate('{value}')"


@requires_engine("pyspark")
class TestSqliteDateIdiomsOnDatabricksSyntax(SqliteDateIdioms, unittest.TestCase):
    ENGINE = "pyspark"
    DIALECT = "databricks"


@requires_engine("starrocks")
class TestSqliteDateIdiomsOnStarRocks(SqliteDateIdioms, unittest.TestCase):
    ENGINE = "starrocks"
    DIALECT = "starrocks"


@requires_engine("doris")
class TestSqliteDateIdiomsOnDoris(SqliteDateIdioms, unittest.TestCase):
    ENGINE = "doris"
    DIALECT = "doris"
