import unittest

import sqlglot
from sqlglot.benchbox.compat import order_list_aggregates, reduce_list_aggregates

Q1_V7 = """
with aggregated as (
    select l_returnflag, l_linestatus,
        list(l_quantity) as quantities, list(l_extendedprice) as prices,
        list(l_discount) as discounts, list(l_tax) as taxes
    from lineitem
    group by l_returnflag, l_linestatus
)
select l_returnflag, l_linestatus,
    list_sum(quantities) as sum_qty,
    list_sum(list_transform(list_zip(prices, discounts), x -> x[1] * (1 - x[2]))) as sum_disc_price,
    list_sum(list_transform(list_zip(prices, discounts, taxes), x -> x[1] * (1 - x[2]) * (1 + x[3]))) as sum_charge,
    list_avg(prices) as avg_price,
    len(quantities) as count_order
from aggregated
"""


def rewrite(sql: str, dialect: str) -> str:
    tree = sqlglot.parse_one(sql, read="duckdb")
    tree = order_list_aggregates(tree, ["l_orderkey", "l_linenumber"], dialect)
    return reduce_list_aggregates(tree, dialect).sql(dialect)


def outer(sql: str) -> str:
    return sql.split("FROM aggregated")[0].rsplit("SELECT l_returnflag", 1)[1]


class TestReduceListAggregates(unittest.TestCase):
    def test_presto_family(self):
        for dialect in ("presto", "trino", "athena"):
            with self.subTest(dialect):
                out = rewrite(Q1_V7, dialect)
                self.assertNotIn("LIST_", out)
                self.assertIn("ZIP_WITH(prices, discounts, (_v0, _v1) -> _v0 * (1 - _v1))", out)
                self.assertIn(
                    "TRANSFORM(ZIP(prices, discounts, taxes), _r -> _r[1] * (1 - _r[2]) * (1 + _r[3]))",
                    out,
                )
                self.assertIn("REDUCE(quantities, CAST(NULL AS DOUBLE)", out)
                self.assertIn("CARDINALITY(quantities) AS count_order", out)

    def test_spark_family(self):
        for dialect in ("spark", "databricks"):
            with self.subTest(dialect):
                out = rewrite(Q1_V7, dialect)
                self.assertNotIn("LIST_", out)
                self.assertIn("ZIP_WITH(prices, discounts, (_v0, _v1) -> _v0 * (1 - _v1))", out)
                self.assertIn(
                    "ZIP_WITH(prices, ZIP_WITH(discounts, taxes, (_v1, _v2) -> STRUCT(_v1 AS _f1, _v2 AS _f2)), "
                    "(_v0, _r) -> _v0 * (1 - _r._f1) * (1 + _r._f2))",
                    out,
                )
                self.assertIn("AGGREGATE(quantities, CAST(NULL AS DOUBLE)", out)
                self.assertIn("SIZE(quantities) AS count_order", out)

    def test_bigquery(self):
        out = rewrite(Q1_V7, "bigquery")
        self.assertNotIn("LIST_", out)
        self.assertIn("(SELECT SUM(_item) FROM UNNEST(quantities) AS _item) AS sum_qty", out)
        self.assertIn(
            "(SELECT SUM(prices[SAFE_OFFSET(_pos)] * (1 - discounts[SAFE_OFFSET(_pos)])) "
            "FROM UNNEST(GENERATE_ARRAY(0, GREATEST(ARRAY_LENGTH(prices), ARRAY_LENGTH(discounts)) - 1)) AS _pos) "
            "AS sum_disc_price",
            out,
        )
        self.assertIn("(SELECT AVG(_item) FROM UNNEST(prices) AS _item) AS avg_price", out)
        self.assertIn("ARRAY_LENGTH(quantities) AS count_order", out)

    def test_bigquery_standalone_transform_keeps_order(self):
        tree = sqlglot.parse_one(
            "SELECT list_transform(xs, v -> v + 1) AS ys FROM (SELECT list(x) AS xs FROM t)",
            read="duckdb",
        )
        self.assertEqual(
            reduce_list_aggregates(tree, "bigquery").sql("bigquery"),
            "SELECT ARRAY(SELECT _item + 1 FROM UNNEST(xs) AS _item WITH OFFSET AS _offset ORDER BY _offset) AS ys "
            "FROM (SELECT ARRAY_AGG(x) AS xs FROM t)",
        )

    def test_length_only_of_list_columns(self):
        tree = sqlglot.parse_one(
            "WITH a AS (SELECT k, list(x) AS xs, name FROM t GROUP BY k, name) "
            "SELECT len(xs), len(name), len(list(1)) FROM a",
            read="duckdb",
        )
        self.assertEqual(
            reduce_list_aggregates(tree, "trino").sql("trino"),
            "WITH a AS (SELECT k, ARRAY_AGG(x) AS xs, name FROM t GROUP BY k, name) "
            "SELECT CARDINALITY(xs), LENGTH(name), CARDINALITY(ARRAY_AGG(1)) FROM a",
        )

    def test_unsupported_lambda_shapes_are_left_alone(self):
        sql = "SELECT list_sum(list_transform(list_zip(a, b), x -> x[1] + len(x))) FROM t"
        tree = sqlglot.parse_one(sql, read="duckdb")
        out = reduce_list_aggregates(tree, "trino").sql("trino")
        self.assertIn("REDUCE(TRANSFORM(ZIP(a, b), x -> x[1] + LENGTH(x))", out)

    def test_other_dialects_unchanged(self):
        tree = sqlglot.parse_one(Q1_V7, read="duckdb")
        for dialect in ("duckdb", "postgres", "snowflake"):
            with self.subTest(dialect):
                self.assertEqual(
                    reduce_list_aggregates(tree, dialect).sql(dialect), tree.sql(dialect)
                )

    def test_copy_flag(self):
        tree = sqlglot.parse_one("SELECT list_sum(xs) FROM t", read="duckdb")
        reduce_list_aggregates(tree, "trino")
        self.assertIn("list_sum", tree.sql("duckdb").lower())
        reduce_list_aggregates(tree, "trino", copy=False)
        self.assertNotIn("list_sum", tree.sql("trino").lower())

    def test_root_aggregate_and_idempotent(self):
        tree = sqlglot.parse_one("list_sum(xs)", read="duckdb")
        once = reduce_list_aggregates(tree, "spark")
        self.assertTrue(once.sql("spark").startswith("AGGREGATE(xs"))
        self.assertEqual(reduce_list_aggregates(once, "spark").sql("spark"), once.sql("spark"))


if __name__ == "__main__":
    unittest.main()
