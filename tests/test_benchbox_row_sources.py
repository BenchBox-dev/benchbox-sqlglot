import unittest

import sqlglot
from sqlglot.benchbox.compat import (
    add_row_source_to_fromless_selects,
    drop_literal_row_source,
    flatten_count_of_count,
)

DIALECTS = (
    "bigquery",
    "clickhouse",
    "databricks",
    "datafusion",
    "doris",
    "duckdb",
    "postgres",
    "snowflake",
    "spark",
    "sqlite",
    "starrocks",
    "trino",
)

HAVOC_Q17_V4 = (
    "SELECT SUM(revenue) / 7.0 AS avg_yearly FROM (SELECT l_extendedprice AS revenue FROM lineitem "
    "UNION ALL SELECT 0 AS revenue FROM (SELECT 1) AS dual WHERE FALSE) combined"
)
TPCDI_EQ3 = (
    "SELECT (SELECT COUNT(*) FROM (\n"
    "    -- Simulated source data count\n"
    "    SELECT COUNT(*) AS source_count FROM DimCustomer WHERE BatchID = 1\n"
    ")) AS source_records, COUNT(*) AS target_records FROM DimCustomer WHERE BatchID = 1"
)


def sql(rewrite, query, read, write=None, **kwargs):
    return rewrite(sqlglot.parse_one(query, read=read), **kwargs).sql(write or read)


class TestDropLiteralRowSource(unittest.TestCase):
    def test_dead_leg_becomes_fromless(self):
        self.assertEqual(
            sql(drop_literal_row_source, HAVOC_Q17_V4, "spark", alias="dual"),
            "SELECT SUM(revenue) / 7.0 AS avg_yearly FROM (SELECT l_extendedprice AS revenue FROM lineitem "
            "UNION ALL SELECT 0 AS revenue WHERE FALSE) AS combined",
        )

    def test_named_literal_column_is_dropped_when_unused(self):
        self.assertEqual(
            sql(
                drop_literal_row_source,
                "SELECT 0 AS r FROM (SELECT 1 AS c) AS dual WHERE FALSE",
                "postgres",
                alias="dual",
            ),
            "SELECT 0 AS r WHERE FALSE",
        )

    def test_other_aliases_and_shapes_are_kept(self):
        for query in (
            "SELECT 0 FROM (SELECT 1) AS other",
            "SELECT 0 FROM (SELECT 'a') AS dual",
            "SELECT 0 FROM (SELECT 1, 2) AS dual",
            "SELECT 0 FROM (SELECT x FROM t) AS dual",
            "SELECT dual.c FROM (SELECT 1 AS c) AS dual",
            "SELECT c FROM (SELECT 1 AS c) AS dual",
            "SELECT * FROM (SELECT 1) AS dual",
            "SELECT 0 FROM (SELECT 1) AS dual, t",
            "SELECT 0 FROM (SELECT 1) AS dual JOIN t ON TRUE",
            "SELECT 0 FROM (SELECT 1 FROM t) AS dual",
            "SELECT 0 FROM (SELECT 1 WHERE FALSE) AS dual",
            "SELECT 0 FROM (SELECT 1 LIMIT 0) AS dual",
            "SELECT COUNT(*) FROM (SELECT 1 FROM t) AS dual",
            "SELECT 0 FROM (SELECT DISTINCT 1) AS dual",
            "SELECT 0 FROM (SELECT 1 GROUP BY 1) AS dual",
            "SELECT 0 FROM (SELECT 1 HAVING FALSE) AS dual",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    sql(drop_literal_row_source, query, "postgres", alias="dual"), query
                )

    def test_copy(self):
        tree = sqlglot.parse_one("SELECT 0 FROM (SELECT 1) AS dual WHERE FALSE", read="postgres")
        drop_literal_row_source(tree, "dual")
        self.assertIn("FROM", tree.sql())
        drop_literal_row_source(tree, "dual", copy=False)
        self.assertEqual(tree.sql(), "SELECT 0 WHERE FALSE")

    def test_bigquery_gets_a_source_back(self):
        dropped = drop_literal_row_source(sqlglot.parse_one(HAVOC_Q17_V4, read="bigquery"), "dual")
        restored = add_row_source_to_fromless_selects(dropped, "UNNEST([1])", "bigquery").sql(
            "bigquery"
        )
        self.assertIn("SELECT 0 AS revenue FROM UNNEST([1]) WHERE FALSE", restored)

    def test_no_dialect_generates_a_values_source(self):
        for dialect in DIALECTS:
            with self.subTest(dialect=dialect):
                rewritten = drop_literal_row_source(
                    sqlglot.parse_one(HAVOC_Q17_V4, read=dialect), "dual"
                )
                self.assertNotIn("VALUES", rewritten.sql(dialect).upper())
                self.assertNotIn("DUAL", rewritten.sql(dialect).upper())


class TestFlattenCountOfCount(unittest.TestCase):
    def test_counts_the_source_rows(self):
        self.assertEqual(
            sql(flatten_count_of_count, TPCDI_EQ3, "netezza", "postgres"),
            "SELECT (/* Simulated source data count */ SELECT COUNT(*) FROM DimCustomer WHERE BatchID = 1) "
            "AS source_records, COUNT(*) AS target_records FROM DimCustomer WHERE BatchID = 1",
        )

    def test_outer_alias_and_joins_are_kept(self):
        self.assertEqual(
            sql(
                flatten_count_of_count,
                "SELECT COUNT(*) AS n FROM (SELECT COUNT(*) FROM a JOIN b ON a.k = b.k WHERE a.x > 1) AS q",
                "postgres",
            ),
            "SELECT COUNT(*) AS n FROM a JOIN b ON a.k = b.k WHERE a.x > 1",
        )

    def test_other_shapes_are_kept(self):
        for query in (
            "SELECT COUNT(*) FROM (SELECT k FROM t GROUP BY k) AS q",
            "SELECT COUNT(*) FROM (SELECT COUNT(*) FROM t GROUP BY k) AS q",
            "SELECT COUNT(*) FROM (SELECT COUNT(*) FROM t HAVING COUNT(*) > 1) AS q",
            "SELECT COUNT(*) FROM (SELECT COUNT(DISTINCT k) FROM t) AS q",
            "SELECT COUNT(*) FROM (SELECT COUNT(*) FROM t) AS q WHERE TRUE",
            "SELECT COUNT(*), 1 FROM (SELECT COUNT(*) FROM t) AS q",
            "SELECT SUM(c) FROM (SELECT COUNT(*) AS c FROM t) AS q",
            "SELECT COUNT(*) FROM (SELECT COUNT(*) AS a, 1 AS b FROM t) AS q",
            "SELECT COUNT(*) FROM (SELECT COUNT(*) FROM t LIMIT 1) AS q",
            "SELECT COUNT(*) FROM t",
        ):
            with self.subTest(query=query):
                self.assertEqual(sql(flatten_count_of_count, query, "postgres"), query)

    def test_copy(self):
        tree = sqlglot.parse_one(
            "SELECT COUNT(*) FROM (SELECT COUNT(*) FROM t) AS q", read="postgres"
        )
        flatten_count_of_count(tree)
        self.assertIn("(SELECT", tree.sql())
        self.assertEqual(flatten_count_of_count(tree, copy=False).sql(), "SELECT COUNT(*) FROM t")


if __name__ == "__main__":
    unittest.main()
