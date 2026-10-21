"""Execute the compatibility rewrites and the generator behavior they depend on on real engines.

Run by the ``in-process`` and ``postgres`` jobs of ``benchbox-engines.yml``; each class skips
when its engine is not available. BigQuery, Snowflake and Trino have no engine in
``in-process``/``postgres``, so their rewrites run as the shape another engine executes (see
``downstream/ENGINE-GAPS.md``).
"""

import unittest
from decimal import Decimal

import sqlglot
from sqlglot import exp
from sqlglot.benchbox.compat import (
    add_row_source_to_fromless_selects,
    filtered_aggregates_to_conditional,
    inline_select_aliases,
    literal_subquery_to_values,
    name_literal_subquery_columns,
    order_list_aggregates,
    qualify_columns_by_name,
    wrap_grouped_scalar_subqueries,
)
from tests import engines
from tests.engines import requires_engine

ROWS = (
    "(SELECT 1 AS id, 'a' AS k, 1 AS x, 1 AS y"
    " UNION ALL SELECT 2, 'a', 2, 5"
    " UNION ALL SELECT 3, 'a', CAST(NULL AS INT), 5"
    " UNION ALL SELECT 4, 'b', 3, 5"
    " UNION ALL SELECT 5, 'b', 3, 0"
    " UNION ALL SELECT 6, 'c', 1, 0)"
)


def values(rows):
    return [
        tuple(
            float(v) if isinstance(v, (Decimal, float)) and not isinstance(v, bool) else v
            for v in row
        )
        for row in rows
    ]


def sort(rows):
    return sorted(values(rows), key=repr)


class OnEngine:
    ENGINE = ""
    DIALECT = ""

    def query(self, sql):
        return values(engines.execute(self.ENGINE, sql))

    def parse(self, sql):
        return sqlglot.parse_one(sql, read=self.DIALECT)

    def generate(self, expression):
        return expression.sql(self.DIALECT)

    def assert_same_rows(self, rewritten, original):
        self.assertEqual(sort(self.query(rewritten)), sort(self.query(original)))

    def test_inline_select_aliases(self):
        sql = f"SELECT k, SUM(x) AS total FROM {ROWS} AS t WHERE y > 0 GROUP BY k HAVING total > 2"
        rewritten = self.generate(inline_select_aliases(self.parse(sql)))
        expected = (
            f"SELECT k, SUM(x) AS total FROM {ROWS} AS t WHERE y > 0 GROUP BY k HAVING SUM(x) > 2"
        )
        self.assertEqual(sort(self.query(rewritten)), sort(self.query(expected)))
        self.assertEqual(sort(self.query(rewritten)), [("a", 3), ("b", 3)])

        where = f"SELECT id, x * 2 AS dx FROM {ROWS} AS t WHERE dx > 2 ORDER BY id"
        rewritten = self.generate(inline_select_aliases(self.parse(where)))
        self.assertEqual(self.query(rewritten), [(2, 4), (4, 6), (5, 6)])

    def test_qualify_columns_by_name(self):
        sql = (
            f"SELECT k FROM {ROWS} AS a, (SELECT id AS bid, 'z' AS k FROM {ROWS} AS s) AS b "
            "WHERE a.id = b.bid AND a.x > 1 ORDER BY a.id"
        )
        rewritten = self.generate(qualify_columns_by_name(self.parse(sql), {"k": "a"}))
        self.assertEqual(self.query(rewritten), [("a",), ("b",), ("b",)])

    def test_filtered_aggregates_generic_form(self):
        sql = (
            "SELECT k, SUM(x) FILTER (WHERE y > 0) AS s, COUNT(*) FILTER (WHERE y > 0) AS c, "
            "COUNT(x) FILTER (WHERE y > 0) AS cx, COUNT(DISTINCT x) FILTER (WHERE y > 0) AS d, "
            "MIN(x) FILTER (WHERE y > 0) AS lo, MAX(x) FILTER (WHERE y > 0) AS hi, "
            f"AVG(x) FILTER (WHERE y > 0) AS a FROM {ROWS} AS t GROUP BY k"
        )
        rewritten = self.generate(filtered_aggregates_to_conditional(self.parse(sql), self.DIALECT))
        self.assertNotIn("FILTER", rewritten)
        self.assertEqual(
            sort(self.query(rewritten)),
            sort(
                [
                    ("a", 3, 3, 2, 2, 1, 2, 1.5),
                    ("b", 3, 1, 1, 1, 3, 3, 3.0),
                    ("c", None, 0, 0, 0, None, None, None),
                ]
            ),
        )

    def test_filtered_aggregates_in_windows(self):
        sql = (
            "SELECT id, SUM(x) FILTER (WHERE y > 0) OVER (PARTITION BY k) AS s, "
            f"COUNT(*) FILTER (WHERE y > 0) OVER (PARTITION BY k) AS c FROM {ROWS} AS t ORDER BY id"
        )
        rewritten = self.generate(
            filtered_aggregates_to_conditional(self.parse(sql), self.DIALECT, windows_only=True)
        )
        self.assertNotIn("FILTER", rewritten)
        self.assertEqual(
            self.query(rewritten),
            [(1, 3, 3), (2, 3, 3), (3, 3, 3), (4, 3, 1), (5, 3, 1), (6, None, 0)],
        )

    def test_bigquery_filtered_aggregates_shape(self):
        sql = (
            "SELECT k, SUM(x) FILTER (WHERE y > 0) AS s, COUNT(*) FILTER (WHERE y > 0) AS c, "
            f"COUNT(DISTINCT x) FILTER (WHERE y > 0) AS d FROM {ROWS} AS t GROUP BY k"
        )
        bigquery = filtered_aggregates_to_conditional(self.parse(sql), "bigquery").sql("bigquery")
        self.assertIn("COUNTIF(y > 0)", bigquery)
        runnable = sqlglot.transpile(bigquery, read="bigquery", write=self.DIALECT)[0]
        self.assertEqual(
            sort(self.query(runnable)), sort([("a", 3, 3, 2), ("b", 3, 1, 1), ("c", None, 0, 0)])
        )

    def test_add_row_source_to_fromless_selects(self):
        fromless = f"SELECT id FROM {ROWS} AS t WHERE EXISTS(SELECT 1 WHERE t.k = 'a' OR t.y = 0) ORDER BY id"
        rewritten = self.generate(
            add_row_source_to_fromless_selects(
                self.parse(fromless), "(VALUES (1)) AS one(c)", self.DIALECT
            )
        )
        self.assertIn("AS one(c) WHERE", rewritten)
        self.assertEqual(self.query(rewritten), [(i,) for i in (1, 2, 3, 5, 6)])

    def test_literal_subquery_to_values(self):
        sql = (
            f"SELECT SUM(r) FROM (SELECT x AS r FROM {ROWS} AS t "
            "UNION ALL SELECT 0 AS r FROM (SELECT 1) AS dual WHERE FALSE) AS c"
        )
        rewritten = self.generate(literal_subquery_to_values(self.parse(sql), "dual", "dual_col"))
        self.assertIn("dual(dual_col)", rewritten)
        self.assertEqual(self.query(rewritten), [(10,)])

    def test_name_literal_subquery_columns(self):
        sql = (
            f"SELECT SUM(r) FROM (SELECT x AS r FROM {ROWS} AS t "
            "UNION ALL SELECT 0 AS r FROM (SELECT 1) AS dual WHERE FALSE) AS c"
        )
        rewritten = self.generate(name_literal_subquery_columns(self.parse(sql), "dual_col"))
        self.assertIn("SELECT 1 AS dual_col", rewritten)
        self.assertEqual(self.query(rewritten), [(10,)])

    def wrapped_scalar_subqueries(self, **kwargs):
        sql = (
            f"SELECT k, (SELECT MAX(x) FROM {ROWS} AS u) AS m, SUM(x) AS s "
            f"FROM {ROWS} AS t GROUP BY k"
        )
        rewritten = self.generate(wrap_grouped_scalar_subqueries(self.parse(sql), **kwargs))
        self.assertEqual(sort(self.query(rewritten)), sort([("a", 3, 3), ("b", 3, 6), ("c", 3, 1)]))
        return rewritten

    def test_wrap_grouped_scalar_subqueries(self):
        self.assertIn("MAX((SELECT", self.wrapped_scalar_subqueries(aggregate=exp.Max))

    def test_order_list_aggregates(self):
        sql = f"SELECT k, LIST(x) AS xs FROM {ROWS} AS t GROUP BY k"
        tree = sqlglot.parse_one(sql, read="duckdb")
        rewritten = order_list_aggregates(tree, ["id"], self.DIALECT).sql(self.DIALECT)
        rows = {k: list(xs) for k, xs in self.query(rewritten)}
        self.assertEqual(rows, {"a": [1, 2, None], "b": [3, 3], "c": [1]})

    def test_order_list_aggregates_with_several_keys(self):
        sql = f"SELECT LIST(id) AS ids FROM {ROWS} AS t"
        tree = sqlglot.parse_one(sql, read="duckdb")
        rewritten = order_list_aggregates(tree, ["y", "id"], self.DIALECT).sql(self.DIALECT)
        ((ids,),) = self.query(rewritten)
        self.assertEqual(list(ids), [5, 6, 1, 2, 3, 4])


@requires_engine("postgres")
class TestOnPostgres(OnEngine, unittest.TestCase):
    ENGINE = "postgres"
    DIALECT = "postgres"

    def test_native_forms_still_work(self):
        sql = f"SELECT SUM(x) FILTER (WHERE y > 0) FROM {ROWS} AS t"
        self.assertEqual(self.query(sql), [(6,)])

    def test_alias_in_having_is_rejected_without_the_rewrite(self):
        with self.assertRaises(Exception):
            self.query(f"SELECT k, SUM(x) AS total FROM {ROWS} AS t GROUP BY k HAVING total > 2")

    def test_ambiguous_column_is_rejected_without_the_rewrite(self):
        with self.assertRaises(Exception):
            self.query(f"SELECT k FROM {ROWS} AS a, {ROWS} AS b WHERE a.id = b.id")


@requires_engine("duckdb")
class TestOnDuckDB(OnEngine, unittest.TestCase):
    ENGINE = "duckdb"
    DIALECT = "duckdb"

    def test_first_wraps_scalar_subqueries(self):
        self.assertIn("FIRST((SELECT", self.wrapped_scalar_subqueries())

    def test_native_filter_matches_the_rewrite(self):
        sql = (
            "SELECT k, SUM(x) FILTER (WHERE y > 0) AS s, COUNT(*) FILTER (WHERE y > 0) AS c, "
            "COUNT(x) FILTER (WHERE y > 0) AS cx, COUNT(DISTINCT x) FILTER (WHERE y > 0) AS d, "
            "MIN(x) FILTER (WHERE y > 0) AS lo, MAX(x) FILTER (WHERE y > 0) AS hi, "
            f"AVG(x) FILTER (WHERE y > 0) AS a FROM {ROWS} AS t GROUP BY k"
        )
        rewritten = filtered_aggregates_to_conditional(self.parse(sql), "duckdb").sql("duckdb")
        self.assert_same_rows(rewritten, sql)


@requires_engine("datafusion")
class TestOnDataFusion(OnEngine, unittest.TestCase):
    ENGINE = "datafusion"
    DIALECT = "datafusion"


@requires_engine("pyspark")
class TestOnPySpark(OnEngine, unittest.TestCase):
    ENGINE = "pyspark"
    DIALECT = "spark"

    def test_first_wraps_scalar_subqueries(self):
        self.assertIn("FIRST((SELECT", self.wrapped_scalar_subqueries())

    def test_databricks_filtered_aggregates_shape(self):
        sql = (
            f"SELECT k, SUM(x) FILTER (WHERE y > 0) AS s, COUNT(DISTINCT x) FILTER (WHERE y > 0) AS d "
            f"FROM {ROWS} AS t GROUP BY k"
        )
        rewritten = filtered_aggregates_to_conditional(self.parse(sql), "databricks").sql(
            "databricks"
        )
        self.assertEqual(
            sort(self.query(rewritten)), sort([("a", 3, 2), ("b", 3, 1), ("c", None, 0)])
        )


@requires_engine("chdb")
class TestFilteredAggregatesOnClickHouse(unittest.TestCase):
    def run_rows(self, sql):
        return engines.execute("chdb", sql)

    def test_conditional_combinators(self):
        source = (
            "SELECT k, SUM(x) FILTER (WHERE y > 0) AS s, COUNT(*) FILTER (WHERE y > 0) AS c, "
            "COUNT(x) FILTER (WHERE y > 0) AS cx, COUNT(DISTINCT x) FILTER (WHERE y > 0) AS d, "
            "MIN(x) FILTER (WHERE y > 0) AS lo, MAX(x) FILTER (WHERE y > 0) AS hi, "
            f"AVG(x) FILTER (WHERE y > 0) AS a FROM {ROWS.replace('CAST(NULL AS INT)', 'CAST(NULL AS Nullable(Int32))')} AS t "
            "GROUP BY k ORDER BY k"
        )
        rewritten = filtered_aggregates_to_conditional(
            sqlglot.parse_one(source, read="postgres"), "clickhouse"
        ).sql("clickhouse")
        self.assertNotIn("FILTER", rewritten)
        self.assertEqual(
            self.run_rows(rewritten),
            [
                ("a", "3", "3", "2", "2", "1", "2", "1.5"),
                ("b", "3", "1", "1", "1", "3", "3", "3"),
                ("c", "\\N", "0", "0", "0", "\\N", "\\N", "\\N"),
            ],
        )


GLOBAL_AGGREGATE_SQL = (
    "SELECT SUM(x) AS s FROM {rows} AS t WHERE y > {bound} GROUP BY () HAVING SUM(x) IS NOT NULL"
)


class GeneratorsOnEngine:
    ENGINE = ""
    DIALECT = ""

    def translated(self, sql):
        return sqlglot.transpile(sql, read="postgres", write=self.DIALECT)[0]

    def test_empty_group_by(self):
        for bound, expected in ((0, [(6,)]), (100, [])):
            sql = GLOBAL_AGGREGATE_SQL.format(rows=ROWS, bound=bound)
            self.assertEqual(values(engines.execute(self.ENGINE, self.translated(sql))), expected)


@requires_engine("postgres")
class TestGeneratorsOnPostgres(GeneratorsOnEngine, unittest.TestCase):
    ENGINE = "postgres"
    DIALECT = "postgres"


@requires_engine("duckdb")
class TestGeneratorsOnDuckDB(GeneratorsOnEngine, unittest.TestCase):
    ENGINE = "duckdb"
    DIALECT = "duckdb"


@requires_engine("datafusion")
class TestGeneratorsOnDataFusion(GeneratorsOnEngine, unittest.TestCase):
    ENGINE = "datafusion"
    DIALECT = "datafusion"

    def test_empty_group_by_is_rejected_by_the_engine(self):
        with self.assertRaises(Exception):
            engines.execute("datafusion", GLOBAL_AGGREGATE_SQL.format(rows=ROWS, bound=0))


@requires_engine("pyspark")
class TestGeneratorsOnPySpark(GeneratorsOnEngine, unittest.TestCase):
    ENGINE = "pyspark"
    DIALECT = "spark"

    def test_empty_group_by_is_rejected_by_the_engine(self):
        with self.assertRaises(Exception):
            engines.execute("pyspark", GLOBAL_AGGREGATE_SQL.format(rows=ROWS, bound=0))


@requires_engine("duckdb")
class TestBigQueryValueWindowNullOrderingOnDuckDB(unittest.TestCase):
    """BigQuery has no engine in CI, so the SQL its generator writes runs on DuckDB, which parses it."""

    def bigquery_text(self, sql):
        return sqlglot.parse_one(sql, read="duckdb").sql(
            "bigquery", unsupported_level=sqlglot.ErrorLevel.RAISE
        )

    def assert_same_rows(self, sql, expected):
        bigquery = self.bigquery_text(sql)
        self.assertEqual(sort(engines.execute("duckdb", sql)), sort(expected))
        self.assertEqual(sort(engines.execute("duckdb", bigquery)), sort(expected))
        return bigquery

    def test_null_key_is_ordered_by_an_is_null_key(self):
        bigquery = self.assert_same_rows(
            f"SELECT k, FIRST_VALUE(x) OVER (PARTITION BY k ORDER BY x) FROM {ROWS} WHERE k = 'a'",
            [("a", 1), ("a", 1), ("a", 1)],
        )
        self.assertIn("ORDER BY x IS NULL, x", bigquery)
        bigquery = self.assert_same_rows(
            f"SELECT k, FIRST_VALUE(x) OVER (PARTITION BY k ORDER BY x DESC NULLS FIRST) FROM {ROWS} WHERE k = 'a'",
            [("a", None), ("a", None), ("a", None)],
        )
        self.assertIn("ORDER BY x IS NULL DESC, x DESC", bigquery)

    def test_partition_key_clause_is_dropped(self):
        bigquery = self.assert_same_rows(
            f"SELECT k, FIRST_VALUE(x) OVER (PARTITION BY x ORDER BY x) FROM {ROWS} WHERE k = 'a'",
            [("a", 1), ("a", 2), ("a", None)],
        )
        self.assertNotIn("NULLS", bigquery)
        self.assertNotIn("IS NULL", bigquery)


if __name__ == "__main__":
    unittest.main()
