"""Doris and StarRocks generators: named windows are inlined and ``GROUP BY ()`` is dropped.

The TPC-Havoc cases hold the query text BenchBox translates (Netezza source) and the text
BenchBox wrote for it before these generator changes (local rewrite ``inline_named_windows`` for
1_v8, and the text strip of ``" GROUP BY ()"`` for 6_v2 and 14_v2), so the carried patch is proved
byte for byte. The pipeline is the one BenchBox runs for the target.
"""

import unittest

import sqlglot
from sqlglot import ErrorLevel
from sqlglot.benchbox.dialect_rewrites import alias_derived_tables, hoist_qualify_outer_clauses
from sqlglot.benchbox.shapes import date_column_integer_arithmetic, rollup_empty_input_row
from tests import engines
from tests.engines import requires_engine

BENCHBOX_OUTPUT = [
    (
        "1_v8",
        "doris",
        "\nselect distinct\n    l_returnflag,\n    l_linestatus,\n    sum(l_quantity) over w as sum_qty,\n    sum(l_extendedprice) over w as sum_base_price,\n    sum(l_extendedprice * (1 - l_discount)) over w as sum_disc_price,\n    sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) over w as sum_charge,\n    avg(l_quantity) over w as avg_qty,\n    avg(l_extendedprice) over w as avg_price,\n    avg(l_discount) over w as avg_disc,\n    count(*) over w as count_order\nfrom\n    lineitem\nwhere\n    l_shipdate <= date '1998-12-01' - interval '90' day\nwindow w as (partition by l_returnflag, l_linestatus)\nqualify count(*) over w > 0\norder by\n    l_returnflag,\n    l_linestatus\n",
        "SELECT DISTINCT `l_returnflag`, `l_linestatus`, `sum_qty`, `sum_base_price`, `sum_disc_price`, `sum_charge`, `avg_qty`, `avg_price`, `avg_disc`, `count_order` FROM (SELECT `l_returnflag`, `l_linestatus`, SUM(`l_quantity`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `sum_qty`, SUM(`l_extendedprice`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `sum_base_price`, SUM(`l_extendedprice` * (1 - `l_discount`)) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `sum_disc_price`, SUM(`l_extendedprice` * (1 - `l_discount`) * (1 + `l_tax`)) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `sum_charge`, AVG(`l_quantity`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `avg_qty`, AVG(`l_extendedprice`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `avg_price`, AVG(`l_discount`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `avg_disc`, COUNT(*) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `count_order`, COUNT(*) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `_w` FROM `lineitem` WHERE `l_shipdate` <= CAST('1998-12-01' AS DATE) - INTERVAL '90' DAY) AS `_t` WHERE `_w` > 0 ORDER BY CASE WHEN `l_returnflag` IS NULL THEN 1 ELSE 0 END, `l_returnflag`, CASE WHEN `l_linestatus` IS NULL THEN 1 ELSE 0 END, `l_linestatus`",
    ),
    (
        "6_v2",
        "doris",
        "\nselect\n    sum(l_extendedprice * l_discount) as revenue\nfrom\n    lineitem\nwhere\n    l_shipdate >= date '1994-01-01'\n    and l_shipdate < date '1994-01-01' + interval '1' year\n    and l_discount between 0.06 - 0.01 and 0.06 + 0.01\n    and l_quantity < 24\ngroup by\n    ()\nhaving\n    sum(l_extendedprice * l_discount) is not null\n",
        "SELECT SUM(`l_extendedprice` * `l_discount`) AS `revenue` FROM `lineitem` WHERE `l_shipdate` >= CAST('1994-01-01' AS DATE) AND `l_shipdate` < CAST('1994-01-01' AS DATE) + INTERVAL '1' YEAR AND `l_discount` BETWEEN 0.06 - 0.01 AND 0.06 + 0.01 AND `l_quantity` < 24 HAVING SUM(`l_extendedprice` * `l_discount`) IS NOT NULL",
    ),
    (
        "14_v2",
        "doris",
        "\nselect\n    100.00 * sum(case\n        when p_type like 'PROMO%'\n            then l_extendedprice * (1 - l_discount)\n        else 0\n    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue\nfrom\n    lineitem,\n    part\nwhere\n    l_partkey = p_partkey\n    and l_shipdate >= date '1995-09-01'\n    and l_shipdate < date '1995-09-01' + interval '1' month\ngroup by\n    ()\nhaving\n    sum(l_extendedprice * (1 - l_discount)) > 0\n",
        "SELECT 100.00 * SUM(CASE WHEN `p_type` LIKE 'PROMO%' THEN `l_extendedprice` * (1 - `l_discount`) ELSE 0 END) / SUM(`l_extendedprice` * (1 - `l_discount`)) AS `promo_revenue` FROM `lineitem`, `part` WHERE `l_partkey` = `p_partkey` AND `l_shipdate` >= CAST('1995-09-01' AS DATE) AND `l_shipdate` < CAST('1995-09-01' AS DATE) + INTERVAL '1' MONTH HAVING SUM(`l_extendedprice` * (1 - `l_discount`)) > 0",
    ),
    (
        "1_v8",
        "starrocks",
        "\nselect distinct\n    l_returnflag,\n    l_linestatus,\n    sum(l_quantity) over w as sum_qty,\n    sum(l_extendedprice) over w as sum_base_price,\n    sum(l_extendedprice * (1 - l_discount)) over w as sum_disc_price,\n    sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) over w as sum_charge,\n    avg(l_quantity) over w as avg_qty,\n    avg(l_extendedprice) over w as avg_price,\n    avg(l_discount) over w as avg_disc,\n    count(*) over w as count_order\nfrom\n    lineitem\nwhere\n    l_shipdate <= date '1998-12-01' - interval '90' day\nwindow w as (partition by l_returnflag, l_linestatus)\nqualify count(*) over w > 0\norder by\n    l_returnflag,\n    l_linestatus\n",
        "SELECT DISTINCT `l_returnflag`, `l_linestatus`, `sum_qty`, `sum_base_price`, `sum_disc_price`, `sum_charge`, `avg_qty`, `avg_price`, `avg_disc`, `count_order` FROM (SELECT `l_returnflag`, `l_linestatus`, SUM(`l_quantity`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `sum_qty`, SUM(`l_extendedprice`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `sum_base_price`, SUM(`l_extendedprice` * (1 - `l_discount`)) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `sum_disc_price`, SUM(`l_extendedprice` * (1 - `l_discount`) * (1 + `l_tax`)) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `sum_charge`, AVG(`l_quantity`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `avg_qty`, AVG(`l_extendedprice`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `avg_price`, AVG(`l_discount`) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `avg_disc`, COUNT(*) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `count_order`, COUNT(*) OVER (PARTITION BY `l_returnflag`, `l_linestatus`) AS `_w` FROM `lineitem` WHERE `l_shipdate` <= CAST('1998-12-01' AS DATE) - INTERVAL '90' DAY) AS `_t` WHERE `_w` > 0 ORDER BY CASE WHEN `l_returnflag` IS NULL THEN 1 ELSE 0 END, `l_returnflag`, CASE WHEN `l_linestatus` IS NULL THEN 1 ELSE 0 END, `l_linestatus`",
    ),
    (
        "6_v2",
        "starrocks",
        "\nselect\n    sum(l_extendedprice * l_discount) as revenue\nfrom\n    lineitem\nwhere\n    l_shipdate >= date '1994-01-01'\n    and l_shipdate < date '1994-01-01' + interval '1' year\n    and l_discount between 0.06 - 0.01 and 0.06 + 0.01\n    and l_quantity < 24\ngroup by\n    ()\nhaving\n    sum(l_extendedprice * l_discount) is not null\n",
        "SELECT SUM(`l_extendedprice` * `l_discount`) AS `revenue` FROM `lineitem` WHERE `l_shipdate` >= CAST('1994-01-01' AS DATE) AND `l_shipdate` < CAST('1994-01-01' AS DATE) + INTERVAL '1' YEAR AND `l_discount` BETWEEN 0.06 - 0.01 AND 0.06 + 0.01 AND `l_quantity` < 24 HAVING SUM(`l_extendedprice` * `l_discount`) IS NOT NULL",
    ),
    (
        "14_v2",
        "starrocks",
        "\nselect\n    100.00 * sum(case\n        when p_type like 'PROMO%'\n            then l_extendedprice * (1 - l_discount)\n        else 0\n    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue\nfrom\n    lineitem,\n    part\nwhere\n    l_partkey = p_partkey\n    and l_shipdate >= date '1995-09-01'\n    and l_shipdate < date '1995-09-01' + interval '1' month\ngroup by\n    ()\nhaving\n    sum(l_extendedprice * (1 - l_discount)) > 0\n",
        "SELECT 100.00 * SUM(CASE WHEN `p_type` LIKE 'PROMO%' THEN `l_extendedprice` * (1 - `l_discount`) ELSE 0 END) / SUM(`l_extendedprice` * (1 - `l_discount`)) AS `promo_revenue` FROM `lineitem`, `part` WHERE `l_partkey` = `p_partkey` AND `l_shipdate` >= CAST('1995-09-01' AS DATE) AND `l_shipdate` < CAST('1995-09-01' AS DATE) + INTERVAL '1' MONTH HAVING SUM(`l_extendedprice` * (1 - `l_discount`)) > 0",
    ),
]


def translate(sql, dialect):
    """BenchBox's translation of Netezza text to Doris or StarRocks, without runtime generator edits."""
    tree = hoist_qualify_outer_clauses(sqlglot.parse_one(sql, read="netezza"))
    if dialect == "doris":
        tree = alias_derived_tables(tree)
        tree = date_column_integer_arithmetic(tree, suffix="_date")
        tree = rollup_empty_input_row(tree)
    return tree.sql(dialect, identify=True)


class TestByteIdenticalToBenchBox(unittest.TestCase):
    def test_havoc_variants_match_the_text_benchbox_wrote(self):
        for query_id, dialect, sql, expected in BENCHBOX_OUTPUT:
            with self.subTest(query=query_id, dialect=dialect):
                self.assertEqual(translate(sql, dialect), expected)


class TestNamedWindows(unittest.TestCase):
    def test_named_window_is_inlined(self):
        sql = "SELECT SUM(x) OVER w AS s, COUNT(*) OVER w AS n FROM t WINDOW w AS (PARTITION BY k ORDER BY v)"
        for dialect in ("doris", "starrocks"):
            with self.subTest(dialect=dialect):
                self.assertEqual(
                    sqlglot.transpile(sql, read=dialect, write=dialect)[0],
                    "SELECT SUM(x) OVER (PARTITION BY k ORDER BY v) AS s, "
                    "COUNT(*) OVER (PARTITION BY k ORDER BY v) AS n FROM t",
                )

    def test_a_window_that_extends_another_is_resolved(self):
        sql = "SELECT SUM(x) OVER w2 FROM t WINDOW w1 AS (PARTITION BY k), w2 AS (w1 ORDER BY v)"
        for dialect in ("doris", "starrocks"):
            with self.subTest(dialect=dialect):
                self.assertEqual(
                    sqlglot.transpile(sql, read=dialect, write=dialect)[0],
                    "SELECT SUM(x) OVER (PARTITION BY k ORDER BY v) FROM t",
                )

    def test_inner_selects_keep_their_own_windows(self):
        sql = (
            "SELECT * FROM (SELECT SUM(x) OVER w AS s FROM t WINDOW w AS (PARTITION BY a)) "
            "q, (SELECT MAX(x) OVER w AS m FROM u WINDOW w AS (PARTITION BY b)) r"
        )
        for dialect in ("doris", "starrocks"):
            with self.subTest(dialect=dialect):
                result = sqlglot.transpile(sql, read=dialect, write=dialect)[0]
                self.assertNotIn("WINDOW", result)
                self.assertIn("OVER (PARTITION BY a)", result)
                self.assertIn("OVER (PARTITION BY b)", result)

    def test_a_query_without_a_window_clause_is_unchanged(self):
        sql = "SELECT SUM(x) OVER (PARTITION BY k) FROM t"
        for dialect in ("doris", "starrocks"):
            with self.subTest(dialect=dialect):
                self.assertEqual(sqlglot.transpile(sql, read=dialect, write=dialect)[0], sql)


class TestEmptyGroupBy(unittest.TestCase):
    def test_a_global_aggregate_drops_group_by_and_keeps_having(self):
        sql = "SELECT SUM(a) AS s FROM t GROUP BY () HAVING SUM(a) > 1"
        for dialect in ("doris", "starrocks"):
            with self.subTest(dialect=dialect):
                self.assertEqual(
                    sqlglot.transpile(sql, read="duckdb", write=dialect)[0],
                    "SELECT SUM(a) AS s FROM t HAVING SUM(a) > 1",
                )

    def test_group_by_with_keys_is_untouched(self):
        sql = "SELECT a, SUM(b) FROM t GROUP BY a"
        for dialect in ("doris", "starrocks"):
            with self.subTest(dialect=dialect):
                self.assertEqual(sqlglot.transpile(sql, read="duckdb", write=dialect)[0], sql)

    def test_an_empty_group_by_that_is_not_a_global_aggregate_is_reported(self):
        sql = "SELECT a FROM t GROUP BY ()"
        for dialect in ("doris", "starrocks"):
            with self.subTest(dialect=dialect):
                with self.assertRaises(sqlglot.errors.UnsupportedError):
                    sqlglot.transpile(
                        sql, read="duckdb", write=dialect, unsupported_level=ErrorLevel.RAISE
                    )

    def test_other_mysql_family_generators_keep_the_clause(self):
        self.assertEqual(
            sqlglot.transpile("SELECT SUM(a) FROM t GROUP BY ()", read="duckdb", write="mysql")[0],
            "SELECT SUM(a) FROM t GROUP BY ()",
        )


# The rows of this query; the window runs over each group, so the inlined text must give the
# same answers as the named window on an engine that supports both.
WINDOW_DATA = (
    "(SELECT 1 AS k, 10 AS v UNION ALL SELECT 1, 20 UNION ALL SELECT 2, 5 UNION ALL SELECT 2, 7"
    " UNION ALL SELECT 3, 1)"
)
WINDOW_QUERY = (
    "SELECT k, v, SUM(v) OVER w AS s, COUNT(*) OVER w AS n FROM {data} AS t "
    "WINDOW w AS (PARTITION BY k) ORDER BY k, v"
)


@requires_engine("duckdb")
class TestNamedWindowsExecute(unittest.TestCase):
    def test_the_text_written_for_doris_returns_the_named_window_rows(self):
        sql = WINDOW_QUERY.format(data=WINDOW_DATA)
        expected = engines.execute("duckdb", sql)
        for dialect in ("doris", "starrocks"):
            with self.subTest(dialect=dialect):
                written = sqlglot.transpile(sql, read="duckdb", write=dialect)[0]
                self.assertNotIn("WINDOW", written)
                # The MySQL family quotes with backticks and has no QUALIFY; the rest is standard.
                standard = sqlglot.transpile(written, read=dialect, write="duckdb")[0]
                self.assertEqual(engines.execute("duckdb", standard), expected)


class MySqlFamilyWindows:
    ENGINE = ""

    def test_inlined_window_runs_and_matches_duckdb(self):
        sql = WINDOW_QUERY.format(data=WINDOW_DATA)
        expected = engines.execute("duckdb", sql)
        written = sqlglot.transpile(sql, read="duckdb", write=self.ENGINE)[0]
        self.assertNotIn("WINDOW", written)
        rows = engines.execute(self.ENGINE, written)
        self.assertEqual(
            [tuple(int(v) for v in row) for row in rows],
            [tuple(int(v) for v in row) for row in expected],
        )

    def test_global_aggregate_without_group_by_returns_its_row(self):
        sql = (
            "SELECT SUM(v) AS s FROM " + WINDOW_DATA + " AS t GROUP BY () HAVING SUM(v) IS NOT NULL"
        )
        written = sqlglot.transpile(sql, read="duckdb", write=self.ENGINE)[0]
        self.assertNotIn("GROUP BY", written)
        self.assertEqual([int(row[0]) for row in engines.execute(self.ENGINE, written)], [43])


@requires_engine("starrocks")
class TestStarRocksGenerator(MySqlFamilyWindows, unittest.TestCase):
    ENGINE = "starrocks"


@requires_engine("doris")
class TestDorisGenerator(MySqlFamilyWindows, unittest.TestCase):
    ENGINE = "doris"


if __name__ == "__main__":
    unittest.main()
