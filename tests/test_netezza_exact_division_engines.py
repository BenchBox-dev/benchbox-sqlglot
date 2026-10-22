"""Execute integer division read from Netezza on the engines whose `/` truncates.

Run by the ``in-process`` and ``postgres`` jobs of ``benchbox-engines.yml``. The query shapes are the
ratios of TPC-DS Q21 (``inv_after / inv_before``, two integer SUMs), Q78 (``ROUND(ss_qty / (...), 2)``)
and Q83 (``x / (a + b + c) / 3.0 * 100``). DuckDB's `/` is exact and is the reference; PostgreSQL and
DataFusion must return the same quotients, while a quotient of integer literals keeps the source's
truncating division.
"""

import unittest

import sqlglot
from tests import engines
from tests.engines import requires_engine

SETUP = (
    "CREATE TABLE inv (item INT, before_qty BIGINT, after_qty BIGINT, price DECIMAL(7, 2))",
    "INSERT INTO inv VALUES (1, 4, 10, 1.25), (2, 3, 2, 2.50), (3, 7, 14, 0.75), (4, 0, 5, 1.00)",
)
RATIOS = """
SELECT item,
       CASE WHEN before_qty > 0 THEN after_qty / before_qty ELSE NULL END AS q21,
       ROUND(after_qty / (before_qty + 1), 2) AS q78,
       after_qty / (before_qty + after_qty + 1) / 3.0 * 100 AS q83,
       SUM(after_qty) OVER () / SUM(before_qty) OVER () AS aggregates,
       (after_qty + 1) / 2 AS parenthesised,
       after_qty / before_qty / 2 AS chained,
       price / after_qty AS decimal_dividend
FROM inv
WHERE before_qty > 0
ORDER BY item
"""
LITERALS = "SELECT 2020 / 3, (1000 - 64) / 10, 7 / 2 / 2"
EXPECTED = [
    (1, 10 / 4, 2.0, 10 / 15 / 3 * 100, 26 / 14, 5.5, 10 / 4 / 2, 1.25 / 10),
    (2, 2 / 3, 0.5, 2 / 6 / 3 * 100, 26 / 14, 1.5, 2 / 3 / 2, 2.5 / 2),
    (3, 14 / 7, 1.75, 14 / 22 / 3 * 100, 26 / 14, 7.5, 14 / 7 / 2, 0.75 / 14),
]


def run(engine, dialect, sql):
    with engines.connect(engine) as connection:
        for statement in SETUP:
            connection.execute(
                statement.replace("CREATE TABLE", "CREATE TEMP TABLE")
                if engine == "postgres"
                else statement
            )
        return connection.execute(sqlglot.transpile(sql, read="netezza", write=dialect)[0])


class ExactDivisionCase(unittest.TestCase):
    engine = ""

    def assert_exact(self):
        rows = run(self.engine, self.engine, RATIOS)
        self.assertEqual(len(rows), len(EXPECTED))
        for actual, expected in zip(rows, EXPECTED):
            self.assertEqual(actual[0], expected[0])
            for left, right in zip(actual[1:], expected[1:]):
                self.assertAlmostEqual(float(left), right, places=6)

    def assert_literals_truncate(self):
        with engines.connect(self.engine) as connection:
            rows = connection.execute(
                sqlglot.transpile(LITERALS, read="netezza", write=self.engine)[0]
            )
        self.assertEqual([tuple(int(v) for v in row) for row in rows], [(673, 93, 1)])


@requires_engine("duckdb")
class TestExactDivisionOnDuckDB(ExactDivisionCase):
    engine = "duckdb"

    def test_reference(self):
        self.assert_exact()


@requires_engine("datafusion")
class TestExactDivisionOnDataFusion(ExactDivisionCase):
    engine = "datafusion"

    def test_quotients_are_exact(self):
        self.assert_exact()

    def test_literal_quotients_truncate(self):
        self.assert_literals_truncate()


@requires_engine("postgres")
class TestExactDivisionOnPostgres(ExactDivisionCase):
    engine = "postgres"

    def test_quotients_are_exact(self):
        self.assert_exact()

    def test_literal_quotients_truncate(self):
        self.assert_literals_truncate()


@requires_engine("sqlite")
class TestExactDivisionOnSQLite(ExactDivisionCase):
    engine = "sqlite"

    def test_quotients_are_exact(self):
        self.assert_exact()


if __name__ == "__main__":
    unittest.main()
