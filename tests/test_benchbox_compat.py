import unittest

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
    strip_window_null_ordering,
    wrap_grouped_scalar_subqueries,
)


def sql(rewrite, query, read, write=None, **kwargs):
    return rewrite(sqlglot.parse_one(query, read=read), **kwargs).sql(write or read)


class TestInlineSelectAliases(unittest.TestCase):
    def test_having_and_where(self):
        self.assertEqual(
            sql(
                inline_select_aliases,
                "SELECT k, SUM(x) AS total FROM t WHERE x > 0 GROUP BY k HAVING total > 2 ORDER BY total",
                "postgres",
            ),
            "SELECT k, SUM(x) AS total FROM t WHERE x > 0 GROUP BY k HAVING SUM(x) > 2 ORDER BY total",
        )
        self.assertEqual(
            sql(inline_select_aliases, "SELECT x * 2 AS y FROM t WHERE y > 1", "trino"),
            "SELECT x * 2 AS y FROM t WHERE x * 2 > 1",
        )

    def test_clauses_argument(self):
        self.assertEqual(
            sql(
                inline_select_aliases,
                "SELECT x * 2 AS y FROM t WHERE y > 1 HAVING y > 1",
                "postgres",
                clauses=("where",),
            ),
            "SELECT x * 2 AS y FROM t WHERE x * 2 > 1 HAVING y > 1",
        )

    def test_qualified_and_foreign_columns_are_kept(self):
        query = "SELECT x AS y FROM t WHERE t.y > 1 AND z IN (SELECT y FROM u WHERE y > 1)"
        self.assertEqual(sql(inline_select_aliases, query, "postgres"), query)

    def test_nested_select_resolves_its_own_aliases(self):
        self.assertEqual(
            sql(
                inline_select_aliases,
                "SELECT a AS y FROM (SELECT b AS y FROM u WHERE y > 1) AS s WHERE y > 2",
                "postgres",
            ),
            "SELECT a AS y FROM (SELECT b AS y FROM u WHERE b > 1) AS s WHERE a > 2",
        )

    def test_copy(self):
        tree = sqlglot.parse_one("SELECT x AS y FROM t WHERE y > 1", read="postgres")
        inline_select_aliases(tree)
        self.assertEqual(tree.sql(), "SELECT x AS y FROM t WHERE y > 1")
        inline_select_aliases(tree, copy=False)
        self.assertEqual(tree.sql(), "SELECT x AS y FROM t WHERE x > 1")


class TestQualifyColumnsByName(unittest.TestCase):
    def test_qualifies_only_unqualified_columns(self):
        self.assertEqual(
            sql(
                qualify_columns_by_name,
                "SELECT k, b.k FROM a, b WHERE a.id = b.id AND k > 1",
                "postgres",
                tables={"k": "a"},
            ),
            "SELECT a.k, b.k FROM a, b WHERE a.id = b.id AND a.k > 1",
        )

    def test_unlisted_columns_and_copy(self):
        tree = sqlglot.parse_one("SELECT k, j FROM a", read="postgres")
        self.assertEqual(qualify_columns_by_name(tree, {"k": "a"}).sql(), "SELECT a.k, j FROM a")
        self.assertEqual(tree.sql(), "SELECT k, j FROM a")


class TestQualifyColumnsClauses(unittest.TestCase):
    QUERY = "SELECT SUM(v) FROM c WHERE v > 0 GROUP BY k HAVING SUM(v) > (SELECT MAX(v) FROM u WHERE v > 1) ORDER BY v"

    def test_clauses_limit_the_columns_qualified(self):
        self.assertEqual(
            sql(
                qualify_columns_by_name,
                self.QUERY,
                "postgres",
                tables={"v": "c"},
                clauses=("having",),
            ),
            "SELECT SUM(v) FROM c WHERE v > 0 GROUP BY k HAVING SUM(c.v) > (SELECT MAX(c.v) FROM u WHERE c.v > 1) "
            "ORDER BY v",
        )
        self.assertEqual(
            sql(
                qualify_columns_by_name,
                self.QUERY,
                "postgres",
                tables={"v": "c"},
                clauses=("where", "order"),
            ),
            "SELECT SUM(v) FROM c WHERE c.v > 0 GROUP BY k HAVING SUM(v) > (SELECT MAX(v) FROM u WHERE c.v > 1) "
            "ORDER BY c.v",
        )

    def test_quoted_qualifier(self):
        self.assertEqual(
            sql(
                qualify_columns_by_name,
                "SELECT SUM(v) FROM c GROUP BY k HAVING SUM(v) > 1",
                "bigquery",
                tables={"v": "combined"},
                clauses=("having",),
                quoted=True,
            ),
            "SELECT SUM(v) FROM c GROUP BY k HAVING SUM(`combined`.v) > 1",
        )


class TestAddRowSourceToFromlessSelects(unittest.TestCase):
    def test_where_without_from_gets_the_source(self):
        self.assertEqual(
            sql(
                add_row_source_to_fromless_selects,
                "SELECT a FROM t WHERE EXISTS(SELECT 1 WHERE (t.a = 1 AND t.b = 2) OR t.c = 3) "
                "AND NOT EXISTS(SELECT 1 FROM u WHERE u.k = t.k) AND EXISTS(SELECT 1)",
                "bigquery",
                source="UNNEST([1])",
                dialect="bigquery",
            ),
            "SELECT a FROM t WHERE EXISTS(SELECT 1 FROM UNNEST([1]) WHERE (t.a = 1 AND t.b = 2) OR t.c = 3) "
            "AND NOT EXISTS(SELECT 1 FROM u WHERE u.k = t.k) AND EXISTS(SELECT 1)",
        )

    def test_copy(self):
        tree = sqlglot.parse_one("SELECT 1 WHERE FALSE", read="bigquery")
        add_row_source_to_fromless_selects(tree, "UNNEST([1])", "bigquery")
        self.assertEqual(tree.sql("bigquery"), "SELECT 1 WHERE FALSE")
        add_row_source_to_fromless_selects(tree, "UNNEST([1])", "bigquery", copy=False)
        self.assertEqual(tree.sql("bigquery"), "SELECT 1 FROM UNNEST([1]) WHERE FALSE")


class TestFilteredAggregates(unittest.TestCase):
    QUERY = (
        "SELECT SUM(x) FILTER (WHERE y > 1) AS s, COUNT(*) FILTER (WHERE y > 1) AS c, "
        "COUNT(x) FILTER (WHERE y > 1) AS cx, COUNT(DISTINCT x) FILTER (WHERE y > 1) AS d, "
        "MIN(x) FILTER (WHERE y > 1) AS lo, MAX(x) FILTER (WHERE y > 1) AS hi, "
        "AVG(x) FILTER (WHERE y > 1) AS a FROM t"
    )

    def test_bigquery(self):
        self.assertEqual(
            sql(
                filtered_aggregates_to_conditional,
                self.QUERY,
                "postgres",
                "bigquery",
                dialect="bigquery",
            ),
            "SELECT SUM(IF(y > 1, x, NULL)) AS s, COUNTIF(y > 1) AS c, COUNT(IF(y > 1, x, NULL)) AS cx, "
            "COUNT(DISTINCT IF(y > 1, x, NULL)) AS d, MIN(IF(y > 1, x, NULL)) AS lo, "
            "MAX(IF(y > 1, x, NULL)) AS hi, AVG(IF(y > 1, x, NULL)) AS a FROM t",
        )

    def test_clickhouse(self):
        self.assertEqual(
            sql(
                filtered_aggregates_to_conditional,
                self.QUERY,
                "postgres",
                "clickhouse",
                dialect="clickhouse",
            ),
            "SELECT sumOrNullIf(x, y > 1) AS s, countIf(y > 1) AS c, countIf(NOT (x IS NULL) AND y > 1) AS cx, "
            "uniqExactIf(x, y > 1) AS d, minOrNullIf(x, y > 1) AS lo, maxOrNullIf(x, y > 1) AS hi, "
            "avgOrNullIf(x, y > 1) AS a FROM t",
        )

    def test_clickhouse_rejects_aggregates_without_a_conditional_form(self):
        for aggregate in ("MEDIAN(x)", "SUM(DISTINCT x)", "COUNT(DISTINCT x, y)"):
            with self.assertRaises(NotImplementedError, msg=aggregate):
                sql(
                    filtered_aggregates_to_conditional,
                    f"SELECT {aggregate} FILTER (WHERE y > 1) FROM t",
                    "postgres",
                    dialect="clickhouse",
                )

    def test_other_dialects_use_if(self):
        for dialect in ("trino", "presto", "athena", "spark", "databricks"):
            with self.subTest(dialect=dialect):
                self.assertEqual(
                    sql(
                        filtered_aggregates_to_conditional,
                        "SELECT SUM(x) FILTER (WHERE y > 1), COUNT(*) FILTER (WHERE y > 1), "
                        "COUNT(DISTINCT x) FILTER (WHERE y > 1) FROM t",
                        "postgres",
                        dialect,
                        dialect=dialect,
                    ),
                    "SELECT SUM(IF(y > 1, x, NULL)), COUNT(IF(y > 1, 1, NULL)), "
                    "COUNT(DISTINCT IF(y > 1, x, NULL)) FROM t",
                )

    def test_windows_only(self):
        query = "SELECT SUM(x) FILTER (WHERE y > 1) OVER (PARTITION BY k), SUM(x) FILTER (WHERE y > 1) FROM t"
        self.assertEqual(
            sql(
                filtered_aggregates_to_conditional,
                query,
                "postgres",
                "trino",
                dialect="trino",
                windows_only=True,
            ),
            "SELECT SUM(IF(y > 1, x, NULL)) OVER (PARTITION BY k), SUM(x) FILTER(WHERE y > 1) FROM t",
        )
        self.assertEqual(
            sql(filtered_aggregates_to_conditional, query, "postgres", "trino", dialect="trino"),
            "SELECT SUM(IF(y > 1, x, NULL)) OVER (PARTITION BY k), SUM(IF(y > 1, x, NULL)) FROM t",
        )

    def test_filter_without_aggregate_and_copy(self):
        tree = sqlglot.parse_one("SELECT SUM(x) FILTER (WHERE y > 1) FROM t", read="postgres")
        filtered_aggregates_to_conditional(tree, "trino")
        self.assertIsNotNone(tree.find(exp.Filter))
        filtered_aggregates_to_conditional(tree, "trino", copy=False)
        self.assertIsNone(tree.find(exp.Filter))

    def test_root_filter(self):
        tree = (
            sqlglot.parse_one("SELECT SUM(x) FILTER (WHERE y > 1)", read="postgres")
            .selects[0]
            .copy()
        )
        self.assertEqual(
            filtered_aggregates_to_conditional(tree, "trino").sql("trino"),
            "SUM(IF(y > 1, x, NULL))",
        )


class TestLiteralSubqueries(unittest.TestCase):
    def test_literal_subquery_to_values(self):
        self.assertEqual(
            sql(
                literal_subquery_to_values,
                "SELECT 0 AS r FROM (SELECT 1) AS dual WHERE FALSE UNION ALL SELECT 2 FROM (SELECT 1) AS other",
                "postgres",
                alias="dual",
                column="dual_col",
            ),
            "SELECT 0 AS r FROM (VALUES (1)) AS dual(dual_col) WHERE FALSE "
            "UNION ALL SELECT 2 FROM (SELECT 1) AS other",
        )

    def test_literal_subquery_to_values_keeps_other_shapes(self):
        for query in (
            "SELECT 0 FROM (SELECT 'a') AS dual",
            "SELECT 0 FROM (SELECT 1 AS one) AS dual",
            "SELECT 0 FROM (SELECT 1, 2) AS dual",
            "SELECT 0 FROM (SELECT x FROM t) AS dual",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    sql(literal_subquery_to_values, query, "postgres", alias="dual", column="c"),
                    query,
                )

    def test_name_literal_subquery_columns(self):
        self.assertEqual(
            sql(
                name_literal_subquery_columns,
                "SELECT 0 FROM (SELECT 1) AS dual UNION ALL SELECT 0 FROM (SELECT 1 AS one) AS named "
                "UNION ALL SELECT 0 FROM (SELECT 'a') AS text",
                "spark",
                column="dual_col",
            ),
            "SELECT 0 FROM (SELECT 1 AS dual_col) AS dual UNION ALL SELECT 0 FROM (SELECT 1 AS one) AS named "
            "UNION ALL SELECT 0 FROM (SELECT 'a') AS text",
        )

    def test_name_literal_subquery_columns_is_idempotent(self):
        once = sql(
            name_literal_subquery_columns, "SELECT 0 FROM (SELECT 1) AS dual", "spark", column="c"
        )
        self.assertEqual(sql(name_literal_subquery_columns, once, "spark", column="c"), once)


class TestWrapGroupedScalarSubqueries(unittest.TestCase):
    def test_wraps_projected_subqueries_of_grouped_selects(self):
        self.assertEqual(
            sql(
                wrap_grouped_scalar_subqueries,
                "SELECT k, (SELECT MAX(y) FROM u) AS m, (SELECT MAX(y) FROM u) + 1, SUM(x) "
                "FROM t GROUP BY k",
                "spark",
            ),
            "SELECT k, FIRST((SELECT MAX(y) FROM u)) AS m, FIRST((SELECT MAX(y) FROM u)) + 1, SUM(x) "
            "FROM t GROUP BY k",
        )

    def test_leaves_other_positions_alone(self):
        for query in (
            "SELECT (SELECT MAX(y) FROM u) FROM t",
            "SELECT k, SUM((SELECT MAX(y) FROM u)) FROM t GROUP BY k",
            "SELECT k, EXISTS(SELECT 1 FROM u) FROM t GROUP BY k",
            "SELECT k, k IN (SELECT y FROM u) FROM t GROUP BY k",
            "SELECT k FROM t WHERE x > (SELECT MAX(y) FROM u) GROUP BY k",
            "SELECT k, FIRST((SELECT MAX(y) FROM u)) FROM t GROUP BY k",
        ):
            with self.subTest(query=query):
                self.assertEqual(sql(wrap_grouped_scalar_subqueries, query, "spark"), query)

    def test_nested_grouped_select(self):
        self.assertEqual(
            sql(
                wrap_grouped_scalar_subqueries,
                "SELECT m FROM (SELECT k, (SELECT MAX(y) FROM u) AS m FROM t GROUP BY k) AS s",
                "spark",
            ),
            "SELECT m FROM (SELECT k, FIRST((SELECT MAX(y) FROM u)) AS m FROM t GROUP BY k) AS s",
        )

    def test_aggregate_argument(self):
        self.assertEqual(
            sql(
                wrap_grouped_scalar_subqueries,
                "SELECT k, (SELECT MAX(y) FROM u) FROM t GROUP BY k",
                "spark",
                aggregate=exp.AnyValue,
            ),
            "SELECT k, ANY_VALUE((SELECT MAX(y) FROM u)) FROM t GROUP BY k",
        )


class TestOrderListAggregates(unittest.TestCase):
    QUERY = "SELECT k, LIST(x) AS xs, LIST(y) AS ys FROM t GROUP BY k"

    def test_forms_per_dialect(self):
        expected = {
            "bigquery": "SELECT k, ARRAY_AGG(x ORDER BY id, n) AS xs, ARRAY_AGG(y ORDER BY id, n) AS ys "
            "FROM t GROUP BY k",
            "trino": "SELECT k, ARRAY_AGG(x ORDER BY id, n) AS xs, ARRAY_AGG(y ORDER BY id, n) AS ys "
            "FROM t GROUP BY k",
            "postgres": "SELECT k, ARRAY_AGG(x ORDER BY id, n) AS xs, ARRAY_AGG(y ORDER BY id, n) AS ys "
            "FROM t GROUP BY k",
            "spark": "SELECT k, TRANSFORM(SORT_ARRAY(COLLECT_LIST(STRUCT(id AS _k0, n AS _k1, x AS _v))), "
            "r -> r._v) AS xs, TRANSFORM(SORT_ARRAY(COLLECT_LIST(STRUCT(id AS _k0, n AS _k1, y AS _v))), "
            "r -> r._v) AS ys FROM t GROUP BY k",
            "databricks": "SELECT k, TRANSFORM(SORT_ARRAY(COLLECT_LIST(STRUCT(id AS _k0, n AS _k1, x AS _v))), "
            "r -> r._v) AS xs, TRANSFORM(SORT_ARRAY(COLLECT_LIST(STRUCT(id AS _k0, n AS _k1, y AS _v))), "
            "r -> r._v) AS ys FROM t GROUP BY k",
        }
        for dialect, sql_text in expected.items():
            with self.subTest(dialect=dialect):
                self.assertEqual(
                    sql(
                        order_list_aggregates,
                        self.QUERY,
                        "duckdb",
                        dialect,
                        order_by=["id", "n"],
                        dialect=dialect,
                    ),
                    sql_text,
                )

    def test_orders_unordered_array_agg_and_keeps_ordered_one(self):
        self.assertEqual(
            sql(
                order_list_aggregates,
                "SELECT ARRAY_AGG(x), ARRAY_AGG(y ORDER BY z DESC) FROM t",
                "postgres",
                order_by=["id"],
                dialect="postgres",
            ),
            "SELECT ARRAY_AGG(x ORDER BY id), ARRAY_AGG(y ORDER BY z DESC) FROM t",
        )

    def test_root_and_copy(self):
        tree = exp.List(expressions=[exp.column("x")])
        self.assertEqual(
            order_list_aggregates(tree, ["id"], "postgres").sql("postgres"),
            "ARRAY_AGG(x ORDER BY id)",
        )
        self.assertIsInstance(tree, exp.List)


class TestStripWindowNullOrdering(unittest.TestCase):
    QUERY = (
        "SELECT FIRST_VALUE(a) OVER (PARTITION BY b ORDER BY b), "
        "FIRST_VALUE(a) OVER (PARTITION BY b ORDER BY c) FROM t ORDER BY b"
    )

    def test_named_columns_only(self):
        self.assertEqual(
            sql(
                strip_window_null_ordering,
                self.QUERY,
                "duckdb",
                "bigquery",
                dialect="bigquery",
                columns=["B"],
            ),
            "SELECT FIRST_VALUE(a) OVER (PARTITION BY b ORDER BY b), "
            "FIRST_VALUE(a) OVER (PARTITION BY b ORDER BY c NULLS LAST) FROM t ORDER BY b NULLS LAST",
        )
        self.assertEqual(
            sql(
                strip_window_null_ordering,
                self.QUERY,
                "duckdb",
                "bigquery",
                dialect="bigquery",
                columns=[],
            ),
            "SELECT FIRST_VALUE(a) OVER (PARTITION BY b ORDER BY b NULLS LAST), "
            "FIRST_VALUE(a) OVER (PARTITION BY b ORDER BY c NULLS LAST) FROM t ORDER BY b NULLS LAST",
        )

    def test_descending_order_uses_its_default(self):
        self.assertEqual(
            sql(
                strip_window_null_ordering,
                "SELECT FIRST_VALUE(a) OVER (ORDER BY b DESC) FROM t",
                "duckdb",
                "bigquery",
                dialect="bigquery",
                columns=["b"],
            ),
            "SELECT FIRST_VALUE(a) OVER (ORDER BY b DESC) FROM t",
        )


if __name__ == "__main__":
    unittest.main()
