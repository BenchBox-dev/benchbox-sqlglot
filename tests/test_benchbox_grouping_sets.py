import math
import unittest
from decimal import Decimal

from sqlglot import ErrorLevel, UnsupportedError, parse_one
from sqlglot.benchbox.grouping_sets import expand_grouping_sets
from tests import engines
from tests.engines import requires_engine

SETUP = (
    "CREATE TABLE sales (region VARCHAR, store VARCHAR, yr INTEGER, amt INTEGER, price DOUBLE)",
    "INSERT INTO sales VALUES "
    "('east', 's1', 2000, 10, 1.5), ('east', 's1', 2001, 20, 2.25), ('east', 's2', 2000, 5, 3.0), "
    "('west', 's3', 2000, 7, 4.0), ('west', 's3', 2001, 9, NULL), ('west', 's4', 2001, 11, 5.5), "
    "(NULL, 's5', 2000, 3, 6.0), (NULL, NULL, 2001, 4, 7.25), ('north', 's6', 2002, NULL, 1.0), "
    "('south', 's7', 2002, 42, 8.0)",
    "CREATE TABLE empty_sales (region VARCHAR, amt INTEGER)",
)

ROLLUP_QUERIES = (
    "SELECT region, store, SUM(amt) AS total, GROUPING(region) AS g1, GROUPING(store) AS g2 "
    "FROM sales GROUP BY ROLLUP (region, store) ORDER BY region, store, g1, g2",
    "SELECT region, yr, COUNT(*) AS n, AVG(price) AS p, GROUPING(region, yr) AS g "
    "FROM sales GROUP BY CUBE (region, yr) ORDER BY region, yr, g",
    "SELECT region, store, yr, SUM(amt) AS total FROM sales "
    "GROUP BY GROUPING SETS ((region, store), (yr), ()) ORDER BY region, store, yr, total",
    "SELECT region, store, yr, SUM(amt) AS total, GROUPING(store) AS g FROM sales "
    "GROUP BY region, ROLLUP (store, yr) ORDER BY region, store, yr, g",
    "SELECT region, yr, SUM(amt) AS total, GROUPING(region) AS g1, GROUPING(yr) AS g2 FROM sales "
    "GROUP BY ROLLUP (region), ROLLUP (yr) ORDER BY region, yr, g1, g2",
    "SELECT SUM(amt) AS total, region, store, GROUPING(region) + GROUPING(store) AS lochierarchy, "
    "RANK() OVER (PARTITION BY GROUPING(region) + GROUPING(store), "
    "CASE WHEN GROUPING(store) = 0 THEN region END ORDER BY SUM(amt) DESC) AS rank_within_parent "
    "FROM sales GROUP BY ROLLUP (region, store) "
    "ORDER BY lochierarchy DESC, CASE WHEN lochierarchy = 0 THEN region END, rank_within_parent, "
    "region, store",
    "SELECT region, SUM(amt) AS total FROM sales GROUP BY ROLLUP (region) "
    "HAVING SUM(amt) > 20 OR GROUPING(region) = 1 ORDER BY region, total",
    "SELECT DISTINCT GROUPING(region) AS g, SUM(1) AS one FROM sales GROUP BY ROLLUP (region, store) "
    "ORDER BY g, one",
    "SELECT region, store, SUM(amt) AS total FROM sales GROUP BY ROLLUP (region, store) "
    "ORDER BY SUM(amt) DESC, region, store LIMIT 7 OFFSET 2",
    "SELECT region, SUM(amt) AS total FROM sales GROUP BY ROLLUP (region) ORDER BY 1, 2",
    "SELECT COUNT(*) AS n, MAX(total) AS biggest FROM "
    "(SELECT region, store, SUM(amt) AS total FROM sales GROUP BY ROLLUP (region, store)) AS x",
    "SELECT region, SUM(amt) AS total FROM sales WHERE yr = 2000 GROUP BY ROLLUP (region) "
    "UNION ALL SELECT 'all', SUM(amt) FROM sales ORDER BY 1, 2",
    "WITH s AS (SELECT * FROM sales WHERE yr > 2000) SELECT region, SUM(amt) AS total, "
    "GROUPING(region) AS g FROM s GROUP BY ROLLUP (region) ORDER BY region, g",
    "SELECT region, SUM(amt) AS total FROM empty_sales GROUP BY ROLLUP (region) ORDER BY region",
    "SELECT region, store, COUNT(amt) AS n FROM sales GROUP BY GROUPING SETS (ROLLUP (region), (store)) "
    "ORDER BY region, store, n",
)

MOMENT_SETUP = (
    "CREATE TABLE obs (g INTEGER, q INTEGER, v DOUBLE)",
    "INSERT INTO obs VALUES (1, 3, 1.5), (1, 5, 2.5), (1, 5, 4.0), (1, 9, NULL), "
    "(2, 7, 10.0), (3, NULL, NULL), (3, NULL, NULL), (4, 1, 3.0), (4, 1, 3.0), "
    "(5, 100000, 0.1), (5, 100002, 0.3), (5, 100004, 0.2)",
)

MOMENT_QUERY = (
    "SELECT g, STDDEV_SAMP(q), STDDEV_POP(q), VAR_SAMP(q), VAR_POP(q), STDDEV(q), VARIANCE(q), "
    "STDDEV_SAMP(v), STDDEV_POP(v), VAR_SAMP(v), VAR_POP(v), STDDEV_SAMP(q) / AVG(q) "
    "FROM obs GROUP BY g ORDER BY g"
)


def _cell(value):
    return float(value) if isinstance(value, (int, float, Decimal)) else value


def _same(left, right):
    left, right = _cell(left), _cell(right)
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    return left == right


class TestGroupingSetsTranslation(unittest.TestCase):
    def sqlite(self, sql, **kwargs):
        return parse_one(sql, read="duckdb").sql("sqlite", **kwargs)

    def test_rollup_becomes_a_union_of_group_bys(self):
        self.assertEqual(
            self.sqlite("SELECT a, SUM(x) AS s FROM t GROUP BY ROLLUP (a)"),
            "SELECT a AS a, SUM(x) AS s FROM t GROUP BY a "
            "UNION ALL SELECT NULL AS a, SUM(x) AS s FROM t",
        )

    def test_grouping_is_the_integer_it_would_return(self):
        self.assertEqual(
            self.sqlite(
                "SELECT a, b, GROUPING(a, b) AS g, COUNT(*) AS n FROM t GROUP BY GROUPING SETS ((a), (b), ())"
            ),
            "SELECT a AS a, NULL AS b, 1 AS g, COUNT(*) AS n FROM t GROUP BY a "
            "UNION ALL SELECT NULL AS a, b AS b, 2 AS g, COUNT(*) AS n FROM t GROUP BY b "
            "UNION ALL SELECT NULL AS a, NULL AS b, 3 AS g, COUNT(*) AS n FROM t",
        )

    def test_aggregate_arguments_keep_their_columns(self):
        sql = self.sqlite("SELECT a, MAX(a) AS m FROM t GROUP BY ROLLUP (a)")
        self.assertIn("SELECT NULL AS a, MAX(a) AS m FROM t", sql)

    def test_cube_and_rollup_set_counts(self):
        for source, columns, branches in (
            ("CUBE (a, b, c)", "a, b, c", 8),
            ("ROLLUP (a, b, c)", "a, b, c", 4),
            ("a, ROLLUP (b), CUBE (c)", "a, b, c", 4),
            ("GROUPING SETS ((a, b), (c), ())", "a, b, c", 3),
            ("ROLLUP (a), ROLLUP (b)", "a, b", 4),
        ):
            with self.subTest(source=source):
                sql = self.sqlite(f"SELECT {columns}, COUNT(*) AS n FROM t GROUP BY {source}")
                self.assertEqual(sql.count("UNION ALL") + 1, branches)
                self.assertNotIn("ROLLUP", sql)
                self.assertNotIn("CUBE", sql)

    def test_with_rollup_modifier(self):
        expression = parse_one("SELECT a, b, SUM(x) FROM t GROUP BY a, b WITH ROLLUP", read="mysql")
        self.assertEqual(expand_grouping_sets(expression).sql("sqlite").count("UNION ALL"), 2)

    def test_combined_rows_run_in_an_outer_select(self):
        sql = self.sqlite("SELECT a, SUM(x) AS s FROM t GROUP BY ROLLUP (a) ORDER BY a LIMIT 3")
        self.assertTrue(sql.startswith("SELECT _grouping_sets.a, _grouping_sets.s FROM (SELECT"))
        self.assertTrue(sql.endswith("ORDER BY a NULLS LAST LIMIT 3"))

    def test_union_operand_is_a_subselect(self):
        sql = self.sqlite(
            "SELECT 1 AS k, 2 AS s UNION ALL SELECT a, SUM(x) FROM t GROUP BY ROLLUP (a)"
        )
        self.assertIn("UNION ALL SELECT * FROM (SELECT a AS a", sql)

    def test_variance_is_not_expanded_where_unsound(self):
        for source in (
            "STDDEV_SAMP(DISTINCT x)",
            "STDDEV_SAMP(x) FILTER (WHERE y > 0)",
            "STDDEV_SAMP(x) OVER (PARTITION BY y)",
        ):
            with self.subTest(source=source), self.assertRaises(UnsupportedError):
                parse_one(f"SELECT {source} FROM t", read="duckdb").sql(
                    "sqlite", unsupported_level=ErrorLevel.RAISE
                )

    def test_unproved_shapes_keep_their_grouping_sets(self):
        for source in (
            "SELECT a FROM t GROUP BY ROLLUP (a)",
            "SELECT other, SUM(x) FROM t GROUP BY ROLLUP (a)",
            "SELECT a, SUM(x), RANK() OVER (ORDER BY a) FROM t GROUP BY ROLLUP (a) QUALIFY RANK() OVER (ORDER BY a) < 3",
            "SELECT a, SUM(x) FROM t GROUP BY ROLLUP (a) HAVING alias_total > 1",
            "SELECT a, SUM(x) FROM t GROUP BY ROLLUP (a) ORDER BY SUM(x) OVER ()",
        ):
            with self.subTest(source=source):
                self.assertIn(
                    "ROLLUP",
                    parse_one(source, read="duckdb").sql("sqlite").upper(),
                )

    def test_rewrite_leaves_its_input_alone(self):
        expression = parse_one(
            "SELECT * FROM (SELECT a, SUM(x) AS s FROM t GROUP BY ROLLUP (a)) AS q"
        )
        before = expression.sql()
        expand_grouping_sets(expression)
        self.assertEqual(expression.sql(), before)
        expand_grouping_sets(expression, copy=False)
        self.assertNotEqual(expression.sql(), before)


@requires_engine("sqlite")
@requires_engine("duckdb")
class TestGroupingSetsExecution(unittest.TestCase):
    def rows(self, engine, query, setup=SETUP):
        with engines.connect(engine) as connection:
            for statement in setup:
                connection.execute(statement)
            return connection.execute(parse_one(query, read="duckdb").sql(engine))

    def assert_same_rows(self, query, setup=SETUP):
        expected = self.rows("duckdb", query, setup)
        actual = self.rows("sqlite", query, setup)
        self.assertEqual(len(actual), len(expected))
        for position, (left, right) in enumerate(zip(actual, expected)):
            self.assertEqual(len(left), len(right))
            for a, b in zip(left, right):
                self.assertTrue(_same(a, b), f"row {position}: {left!r} != {right!r}")
        return expected

    def test_grouping_sets_match_duckdb(self):
        for query in ROLLUP_QUERIES:
            with self.subTest(query=query[:90]):
                self.assertTrue(self.assert_same_rows(query))

    def test_empty_input_keeps_the_grand_total_row(self):
        rows = self.assert_same_rows(
            "SELECT region, SUM(amt) AS total, GROUPING(region) AS g FROM empty_sales "
            "GROUP BY ROLLUP (region)"
        )
        self.assertEqual(rows, [(None, None, 1)])

    def test_null_group_values_stay_distinct_from_rolled_up_rows(self):
        rows = self.assert_same_rows(
            "SELECT region, GROUPING(region) AS g, SUM(amt) AS total FROM sales "
            "GROUP BY ROLLUP (region) ORDER BY g, region"
        )
        self.assertEqual([row[1] for row in rows].count(1), 1)
        self.assertEqual([row[0] for row in rows[:-1]].count(None), 1)

    def test_variance_matches_duckdb(self):
        rows = self.assert_same_rows(MOMENT_QUERY, MOMENT_SETUP)
        by_group = {row[0]: row for row in rows}
        self.assertIsNone(by_group[2][1])
        self.assertEqual(by_group[2][2], 0)
        self.assertIsNone(by_group[3][1])
        self.assertIsNone(by_group[3][2])
        self.assertEqual(by_group[4][1], 0)

    def test_variance_of_an_empty_table_is_null(self):
        rows = self.assert_same_rows(
            "SELECT STDDEV_SAMP(amt), VAR_POP(amt) FROM empty_sales", SETUP
        )
        self.assertEqual(rows, [(None, None)])


if __name__ == "__main__":
    unittest.main()
