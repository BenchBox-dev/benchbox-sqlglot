"""Execute integer division read from Netezza on every engine in CI.

Run by every engine job of ``benchbox-engines.yml``. The query shapes are the ratios of TPC-DS Q21
(``inv_after / inv_before``, two integer SUMs), Q78 (``ROUND(ss_qty / (...), 2)``) and Q83
(``x / (a + b + c) / 3.0 * 100``). DuckDB's `/` is exact and is the reference. Engines whose `/`
truncates integers (PostgreSQL, DataFusion, Trino, SQLite) must return the same quotients, and so
must the engines whose `/` is already exact (DuckDB, PySpark, chDB, StarRocks, Doris). A quotient of
integer literals keeps the source's truncating division where the engine truncates. T-SQL, Teradata,
Drill, Dremio, Redshift and Presto have no engine in CI: their casts are asserted as text in
``tests/dialects/test_netezza.py`` (``downstream/ENGINE-GAPS.md``).
"""

import unittest

import sqlglot
from tests import engines
from tests.engines import requires_engine

ROWS = (
    "SELECT 1 AS item, 4 AS before_qty, 10 AS after_qty, 1.25 AS price "
    "UNION ALL SELECT 2, 3, 2, 2.50 "
    "UNION ALL SELECT 3, 7, 14, 0.75 "
    "UNION ALL SELECT 4, 0, 5, 1.00"
)
RATIOS = f"""
SELECT item,
       CASE WHEN before_qty > 0 THEN after_qty / before_qty ELSE NULL END AS q21,
       ROUND(after_qty / (before_qty + 1), 2) AS q78,
       after_qty / (before_qty + after_qty + 1) / 3.0 * 100 AS q83,
       SUM(after_qty) OVER () / SUM(before_qty) OVER () AS aggregates,
       (after_qty + 1) / 2 AS parenthesised,
       after_qty / before_qty / 2 AS chained,
       price / after_qty AS decimal_dividend
FROM ({ROWS}) AS inv
WHERE before_qty > 0
ORDER BY item
"""
LITERALS = "SELECT 2020 / 3, (1000 - 64) / 10, 7 / 2 / 2"
EXPECTED = [
    (1, 10 / 4, 2.0, 10 / 15 / 3 * 100, 26 / 14, 5.5, 10 / 4 / 2, 1.25 / 10),
    (2, 2 / 3, 0.5, 2 / 6 / 3 * 100, 26 / 14, 1.5, 2 / 3 / 2, 2.5 / 2),
    (3, 14 / 7, 1.75, 14 / 22 / 3 * 100, 26 / 14, 7.5, 14 / 7 / 2, 0.75 / 14),
]


def translate(engine, sql):
    return sqlglot.transpile(sql, read="netezza", write=engines.ENGINES[engine].dialect)[0]


class ExactDivisionMixin:
    engine = ""
    truncating_literals = False

    def test_quotients_are_exact(self):
        with engines.connect(self.engine) as connection:
            rows = connection.execute(translate(self.engine, RATIOS))
        self.assertEqual(len(rows), len(EXPECTED))
        for actual, expected in zip(rows, EXPECTED):
            self.assertEqual(int(actual[0]), expected[0])
            for left, right in zip(actual[1:], expected[1:]):
                self.assertAlmostEqual(float(left), right, places=6)

    def test_literal_quotients_follow_the_source(self):
        with engines.connect(self.engine) as connection:
            rows = connection.execute(translate(self.engine, LITERALS))
        values = [float(v) for v in rows[0]]
        if self.truncating_literals:
            self.assertEqual(values, [673, 93, 1])
        else:
            self.assertAlmostEqual(values[0], 2020 / 3, places=6)


def case(name, truncating):
    @requires_engine(name)
    class Case(ExactDivisionMixin, unittest.TestCase):
        engine = name
        truncating_literals = truncating

    Case.__name__ = Case.__qualname__ = f"TestExactDivisionOn{name.title()}"
    return Case


TestExactDivisionOnDuckdb = case("duckdb", False)
TestExactDivisionOnSqlite = case("sqlite", True)
TestExactDivisionOnDatafusion = case("datafusion", True)
TestExactDivisionOnPostgres = case("postgres", True)
TestExactDivisionOnTrino = case("trino", True)
TestExactDivisionOnPyspark = case("pyspark", False)
TestExactDivisionOnChdb = case("chdb", False)
TestExactDivisionOnStarrocks = case("starrocks", False)
TestExactDivisionOnDoris = case("doris", False)


if __name__ == "__main__":
    unittest.main()
