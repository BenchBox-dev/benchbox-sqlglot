import math
import unittest
from decimal import Decimal

from sqlglot import ErrorLevel, UnsupportedError, exp, parse_one
from sqlglot.benchbox.pivot import expand_order_by_all, lower_pivots
from tests import engines
from tests.engines import requires_engine

SETUP = (
    "CREATE TABLE sales (region VARCHAR, mode VARCHAR, qty INTEGER, price DOUBLE)",
    "INSERT INTO sales VALUES "
    "('east', 'AIR', 10, 1.5), ('east', 'AIR', 5, 2.5), ('east', 'RAIL', 7, 3.0), "
    "('east', 'SHIP', 1, 4.0), ('west', 'RAIL', 2, 5.0), ('west', 'TRUCK', 9, NULL), "
    "('north', 'SHIP', 4, 6.0), ('north', 'SHIP', NULL, 7.0), (NULL, 'AIR', 3, 8.0), "
    "(NULL, NULL, 8, 9.0), ('south', 'MAIL', 11, 10.0)",
    "CREATE TABLE wide (id INTEGER, width INTEGER, height DOUBLE, depth DOUBLE, label VARCHAR)",
    "INSERT INTO wide VALUES (1, 10, 2.5, NULL, 'a'), (2, NULL, 3.5, 4.5, 'b'), "
    "(3, 30, NULL, NULL, NULL), (4, 40, 5.5, 6.5, 'd')",
    "CREATE TABLE tags (id INTEGER, name VARCHAR, hits INTEGER)",
    "INSERT INTO tags VALUES (1, 'x', 3), (1, 'y', NULL), (2, 'x', 4), (3, 'y', 6), (3, 'y', 1)",
)

PIVOT_QUERIES = (
    "SELECT * FROM (SELECT region, mode, qty FROM sales) PIVOT (SUM(qty) FOR mode IN ('AIR', 'RAIL', 'SHIP', 'TRUCK')) "
    "ORDER BY region",
    "SELECT * FROM (SELECT region, mode, qty FROM sales WHERE price > 2) "
    "PIVOT (COUNT(qty) FOR mode IN ('AIR', 'SHIP')) ORDER BY region",
    "SELECT * FROM (SELECT mode, qty FROM sales) PIVOT (MAX(qty) FOR mode IN ('AIR', 'RAIL', 'MAIL', 'NONE'))",
    "SELECT * FROM (SELECT region, mode, price FROM sales) PIVOT (AVG(price) FOR mode IN ('AIR', 'SHIP')) "
    "ORDER BY region",
    "SELECT * FROM (SELECT region, mode, qty FROM sales) PIVOT (MIN(qty) FOR mode IN ('AIR' AS a, 'RAIL' AS r)) "
    "ORDER BY region",
    "SELECT * FROM (SELECT region, mode, qty FROM sales) PIVOT (SUM(qty) AS total FOR mode IN ('AIR', 'RAIL')) "
    "ORDER BY region",
    "SELECT region, AIR FROM (SELECT region, mode, qty FROM sales) PIVOT (SUM(qty) FOR mode IN ('AIR')) AS p "
    "WHERE AIR > 5 ORDER BY region",
    "SELECT * FROM (SELECT region, mode, qty * 2 AS doubled FROM sales) "
    "PIVOT (SUM(doubled) FOR mode IN ('AIR', 'RAIL')) ORDER BY region",
    "WITH s AS (SELECT region, mode, qty FROM sales) SELECT * FROM "
    "(SELECT region, mode, qty FROM s) PIVOT (SUM(qty) FOR mode IN ('SHIP')) ORDER BY region",
    "SELECT * FROM (SELECT id, name, hits FROM tags) PIVOT (SUM(hits) FOR name IN ('x', 'y')) ORDER BY id",
)

UNPIVOT_QUERIES = (
    "SELECT * FROM (SELECT id, width, height FROM wide) UNPIVOT (measure FOR kind IN (width, height)) "
    "ORDER BY id, kind",
    "SELECT id, kind, measure FROM (SELECT id, width, height, depth FROM wide) "
    "UNPIVOT (measure FOR kind IN (width, height, depth)) ORDER BY id, kind",
    "SELECT * FROM (SELECT id, width, height FROM wide) "
    "UNPIVOT INCLUDE NULLS (measure FOR kind IN (width, height)) ORDER BY id, kind",
    "SELECT * FROM (SELECT id, width, height FROM wide) "
    "UNPIVOT (measure FOR kind IN (width AS w, height AS h)) ORDER BY id, kind",
    "SELECT kind, COUNT(*) AS n, SUM(measure) AS total FROM (SELECT id, width, height, depth FROM wide) "
    "UNPIVOT (measure FOR kind IN (height, depth)) GROUP BY kind ORDER BY kind",
)

ORDER_BY_ALL_QUERIES = (
    "SELECT region, mode, COUNT(*) AS n FROM sales GROUP BY region, mode ORDER BY ALL",
    "SELECT region, mode, SUM(qty) AS total FROM sales GROUP BY region, mode ORDER BY ALL DESC",
    "SELECT region, mode, SUM(qty) AS total FROM sales GROUP BY region, mode ORDER BY ALL DESC NULLS LAST LIMIT 4",
    "SELECT region, qty FROM sales ORDER BY ALL ASC NULLS FIRST",
    "SELECT mode, qty FROM sales WHERE qty IS NOT NULL ORDER BY ALL DESC LIMIT 5 OFFSET 1",
    "SELECT id FROM wide UNION ALL SELECT id + 10 FROM wide ORDER BY ALL DESC",
)


def _cell(value):
    return float(value) if isinstance(value, (int, float, Decimal)) else value


def _same(left, right):
    left, right = _cell(left), _cell(right)
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    return left == right


class TestPivotLowering(unittest.TestCase):
    def sqlite(self, sql, read="duckdb"):
        return parse_one(sql, read=read).sql("sqlite")

    def test_pivot_is_conditional_aggregation(self):
        self.assertEqual(
            self.sqlite("SELECT * FROM (SELECT a, m, q FROM t) PIVOT (SUM(q) FOR m IN ('x', 'y'))"),
            "SELECT * FROM (SELECT a, SUM(CASE WHEN m = 'x' THEN q END) AS x, "
            "SUM(CASE WHEN m = 'y' THEN q END) AS y FROM (SELECT a, m, q FROM t) AS _pivot_src GROUP BY a)",
        )

    def test_pivot_without_group_keys_has_no_group_by(self):
        sql = self.sqlite("SELECT * FROM (SELECT m, q FROM t) PIVOT (SUM(q) FOR m IN ('x'))")
        self.assertNotIn("GROUP BY", sql)

    def test_unpivot_is_a_union_all(self):
        self.assertEqual(
            self.sqlite("SELECT * FROM (SELECT id, a, b FROM t) UNPIVOT (v FOR n IN (a, b))"),
            "SELECT * FROM (SELECT id, 'a' AS n, a AS v FROM (SELECT id, a, b FROM t) AS _pivot_src "
            "WHERE NOT a IS NULL UNION ALL SELECT id, 'b' AS n, b AS v FROM (SELECT id, a, b FROM t) "
            "AS _pivot_src WHERE NOT b IS NULL)",
        )

    def test_pivot_alias_names_the_derived_table(self):
        sql = self.sqlite(
            "SELECT p.x FROM (SELECT a, m, q FROM t) PIVOT (SUM(q) FOR m IN ('x')) AS p"
        )
        self.assertTrue(sql.endswith("GROUP BY a) AS p"))

    def test_netezza_source_is_lowered_too(self):
        sql = self.sqlite(
            "SELECT * FROM (SELECT a, m, q FROM t) PIVOT (SUM(q) FOR m IN ('x'))", read="netezza"
        )
        self.assertIn("SUM(CASE WHEN m = 'x' THEN q END) AS x", sql)

    def test_rewrite_leaves_its_input_alone(self):
        expression = parse_one(
            "SELECT * FROM (SELECT a, m, q FROM t) PIVOT (SUM(q) FOR m IN ('x'))", read="duckdb"
        )
        before = expression.sql("duckdb")
        lowered = lower_pivots(expression)
        self.assertEqual(expression.sql("duckdb"), before)
        self.assertFalse(list(lowered.find_all(exp.Pivot)))

    def test_unproved_shapes_raise_instead_of_dropping_the_pivot(self):
        for source in (
            "SELECT * FROM t PIVOT (SUM(q) FOR m IN ('x'))",
            "SELECT * FROM t UNPIVOT (v FOR n IN (a, b))",
            "SELECT * FROM (SELECT * FROM t) PIVOT (SUM(q) FOR m IN ('x'))",
            "SELECT * FROM (SELECT a, m, q FROM t) PIVOT (SUM(q) FOR m IN (SELECT DISTINCT m FROM t))",
            "SELECT * FROM (SELECT a, m, q FROM t) PIVOT (SUM(q), MAX(q) FOR m IN ('x'))",
            "SELECT * FROM (SELECT a, m, q FROM t) PIVOT (SUM(DISTINCT q) FOR m IN ('x'))",
            "SELECT * FROM (SELECT a, m, q FROM t) PIVOT (COUNT(*) FOR m IN ('x'))",
            "SELECT * FROM (SELECT a, m, q FROM t) PIVOT (FIRST(q) FOR m IN ('x'))",
            "SELECT * FROM (SELECT a, m, q FROM t) PIVOT (SUM(q) FOR m IN ('x') GROUP BY a)",
            "SELECT * FROM (SELECT a, m, q FROM t) UNPIVOT ((v1, v2) FOR n IN ((a, b), (c, d)))",
            "SELECT * FROM (SELECT a, 1 + b FROM t) UNPIVOT (v FOR n IN (a))",
            "SELECT * FROM (SELECT a, m, q FROM t) UNPIVOT (v FOR n IN (missing))",
        ):
            for level in (ErrorLevel.IGNORE, ErrorLevel.WARN, ErrorLevel.RAISE):
                for dialect in ("sqlite", "datafusion"):
                    with self.subTest(source=source, level=level, dialect=dialect):
                        with self.assertRaises(UnsupportedError):
                            parse_one(source, read="duckdb").sql(dialect, unsupported_level=level)

    def test_dialects_with_a_pivot_keep_theirs(self):
        sql = parse_one(
            "SELECT * FROM (SELECT a, m, q FROM t) PIVOT (SUM(q) FOR m IN ('x'))", read="duckdb"
        ).sql("snowflake")
        self.assertIn("PIVOT", sql)


class TestOrderByAll(unittest.TestCase):
    def test_every_selected_column_is_a_key_with_the_direction(self):
        for dialect, expected in (
            ("sqlite", "ORDER BY 1 DESC, 2 DESC, 3 DESC LIMIT 100"),
            (
                "datafusion",
                "ORDER BY 1 DESC NULLS LAST, 2 DESC NULLS LAST, 3 DESC NULLS LAST LIMIT 100",
            ),
        ):
            with self.subTest(dialect=dialect):
                sql = parse_one(
                    "SELECT a, b, SUM(c) AS s FROM t GROUP BY a, b ORDER BY ALL DESC LIMIT 100",
                    read="duckdb",
                ).sql(dialect)
                self.assertTrue(sql.endswith(expected), sql)

    def test_null_placement_is_kept(self):
        sql = parse_one("SELECT a, b FROM t ORDER BY ALL ASC NULLS FIRST", read="duckdb").sql(
            "sqlite"
        )
        self.assertTrue(sql.endswith("ORDER BY 1 ASC, 2 ASC"), sql)

    def test_netezza_reads_all_as_the_keyword(self):
        sql = parse_one("SELECT a, b FROM t ORDER BY ALL DESC", read="netezza").sql("sqlite")
        self.assertTrue(sql.startswith("SELECT a, b FROM t ORDER BY 1 DESC"), sql)
        self.assertNotIn("ALL", sql)

    def test_quoted_all_is_a_column(self):
        sql = parse_one('SELECT "all", b FROM t ORDER BY "all" DESC', read="netezza").sql("sqlite")
        self.assertIn('ORDER BY "all" DESC', sql)
        self.assertNotIn("ORDER BY 1", sql)

    def test_unknown_select_list_raises(self):
        for source in (
            "SELECT * FROM t ORDER BY ALL DESC",
            "SELECT a, b FROM t ORDER BY ALL, a",
        ):
            for level in (ErrorLevel.IGNORE, ErrorLevel.WARN):
                with self.subTest(source=source, level=level), self.assertRaises(UnsupportedError):
                    parse_one(source, read="duckdb").sql("sqlite", unsupported_level=level)

    def test_rewrite_leaves_its_input_alone(self):
        expression = parse_one("SELECT a, b FROM t ORDER BY ALL DESC", read="duckdb")
        before = expression.sql("duckdb")
        expand_order_by_all(expression)
        self.assertEqual(expression.sql("duckdb"), before)


def _rows(engine, query, read="duckdb"):
    with engines.connect(engine) as connection:
        for statement in SETUP:
            connection.execute(statement)
        return connection.execute(parse_one(query, read=read).sql(engine))


def _reference(query):
    with engines.connect("duckdb") as connection:
        for statement in SETUP:
            connection.execute(statement)
        return connection.execute(query)


class _ExecutionCase(unittest.TestCase):
    engine = ""

    def assert_same_rows(self, query, read="duckdb"):
        expected = _reference(query)
        actual = _rows(self.engine, query, read)
        self.assertEqual(len(actual), len(expected), query)
        for position, (left, right) in enumerate(zip(actual, expected)):
            self.assertEqual(len(left), len(right), query)
            for a, b in zip(left, right):
                self.assertTrue(_same(a, b), f"{query}\nrow {position}: {left!r} != {right!r}")
        return expected

    def run_group(self, queries, read="duckdb"):
        for query in queries:
            with self.subTest(query=query):
                rows = self.assert_same_rows(query, read)
                self.assertTrue(rows, query)


@requires_engine("sqlite")
@requires_engine("duckdb")
class TestSqliteExecution(_ExecutionCase):
    engine = "sqlite"

    def test_pivot_rows_equal_duckdb(self):
        self.run_group(PIVOT_QUERIES)

    def test_unpivot_rows_equal_duckdb(self):
        self.run_group(UNPIVOT_QUERIES)

    def test_order_by_all_rows_equal_duckdb(self):
        self.run_group(ORDER_BY_ALL_QUERIES)

    def test_netezza_source_rows_equal_duckdb(self):
        self.run_group(PIVOT_QUERIES[:3] + UNPIVOT_QUERIES[:2], read="netezza")

    def test_order_by_all_desc_is_descending(self):
        rows = _rows(
            "sqlite", "SELECT mode, SUM(qty) AS total FROM sales GROUP BY mode ORDER BY ALL DESC"
        )
        self.assertEqual([row[0] for row in rows], ["TRUCK", "SHIP", "RAIL", "MAIL", "AIR", None])

    def test_netezza_order_by_all_desc_is_descending(self):
        rows = _rows(
            "sqlite",
            "SELECT mode, SUM(qty) AS total FROM sales WHERE mode IS NOT NULL GROUP BY mode ORDER BY ALL DESC",
            read="netezza",
        )
        self.assertEqual([row[0] for row in rows], ["TRUCK", "SHIP", "RAIL", "MAIL", "AIR"])


@requires_engine("datafusion")
@requires_engine("duckdb")
class TestDataFusionExecution(_ExecutionCase):
    engine = "datafusion"

    def test_pivot_rows_equal_duckdb(self):
        self.run_group(PIVOT_QUERIES)

    def test_unpivot_rows_equal_duckdb(self):
        self.run_group(UNPIVOT_QUERIES)

    def test_order_by_all_rows_equal_duckdb(self):
        self.run_group(ORDER_BY_ALL_QUERIES[:5])


if __name__ == "__main__":
    unittest.main()
