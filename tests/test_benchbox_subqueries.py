import unittest

import sqlglot
from sqlglot import exp
from sqlglot.benchbox.subqueries import (
    decorrelate_subqueries,
    in_aggregate_subquery_to_exists,
    not_in_subquery_to_not_exists,
)

SCHEMA = {
    "items": {"i_id": "int", "i_supplier": "int", "i_qty": "int", "i_name": "text"},
    "suppliers": {"s_id": "int", "s_region": "text", "s_comment": "text"},
    "orders": {"o_id": "int", "o_customer": "text", "o_total": "int"},
    "order_lines": {"l_order": "int", "l_qty": "int"},
}


def sql(rewrite, query, read="postgres", write=None, **kwargs):
    return rewrite(sqlglot.parse_one(query, read=read), **kwargs).sql(write or read)


class TestNotInSubqueryToNotExists(unittest.TestCase):
    QUERY = (
        "SELECT i_id FROM items WHERE i_qty > 1 AND i_supplier NOT IN "
        "(SELECT s_id FROM suppliers WHERE s_comment LIKE '%bad%')"
    )

    def test_non_null_columns(self):
        self.assertEqual(
            sql(not_in_subquery_to_not_exists, self.QUERY, non_null_columns=["i_supplier", "s_id"]),
            "SELECT i_id FROM items WHERE i_qty > 1 AND NOT EXISTS(SELECT 1 FROM suppliers WHERE "
            "s_comment LIKE '%bad%' AND s_id = i_supplier)",
        )

    def test_case_insensitive_and_qualified_names(self):
        self.assertEqual(
            sql(
                not_in_subquery_to_not_exists,
                "SELECT i_id FROM items AS i WHERE i.i_supplier NOT IN (SELECT s.s_id FROM suppliers AS s)",
                non_null_columns=["I_SUPPLIER", "S_ID"],
            ),
            "SELECT i_id FROM items AS i WHERE NOT EXISTS(SELECT 1 FROM suppliers AS s WHERE s.s_id = i.i_supplier)",
        )

    def test_unknown_nullability_is_left_alone(self):
        for columns in ([], ["i_supplier"], ["s_id"]):
            with self.subTest(columns=columns):
                self.assertEqual(
                    sql(not_in_subquery_to_not_exists, self.QUERY, non_null_columns=columns),
                    sqlglot.parse_one(self.QUERY, read="postgres").sql("postgres"),
                )

    def test_other_shapes_are_left_alone(self):
        for query in (
            "SELECT 1 FROM items WHERE i_supplier IN (SELECT s_id FROM suppliers)",
            "SELECT 1 FROM items WHERE i_supplier NOT IN (1, 2)",
            "SELECT 1 FROM items WHERE i_supplier + 1 NOT IN (SELECT s_id FROM suppliers)",
            "SELECT 1 FROM items WHERE i_supplier NOT IN (SELECT s_id, s_region FROM suppliers)",
            "SELECT 1 FROM items WHERE i_supplier NOT IN (SELECT s_id + 1 FROM suppliers)",
            "SELECT 1 FROM items WHERE i_supplier NOT IN (SELECT s_id FROM suppliers LIMIT 3)",
            "SELECT 1 FROM items WHERE i_supplier NOT IN (SELECT s_id FROM suppliers GROUP BY s_id)",
            "SELECT 1 FROM items WHERE i_supplier NOT IN (SELECT s_id FROM suppliers UNION SELECT 1)",
            "SELECT 1 FROM items WHERE i_supplier NOT IN (SELECT 1)",
            "SELECT 1 FROM items WHERE i_supplier NOT IN (WITH w AS (SELECT s_id FROM suppliers) SELECT s_id FROM w)",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    sql(
                        not_in_subquery_to_not_exists,
                        query,
                        non_null_columns=["i_supplier", "s_id"],
                    ),
                    sqlglot.parse_one(query, read="postgres").sql("postgres"),
                )

    def test_same_unqualified_name_is_skipped(self):
        query = "SELECT 1 FROM items WHERE NOT s_id IN (SELECT s_id FROM suppliers)"
        self.assertEqual(
            sql(not_in_subquery_to_not_exists, query, non_null_columns=["s_id"]), query
        )
        self.assertEqual(
            sql(
                not_in_subquery_to_not_exists,
                "SELECT 1 FROM items AS i WHERE i.s_id NOT IN (SELECT s_id FROM suppliers)",
                non_null_columns=["s_id"],
            ),
            "SELECT 1 FROM items AS i WHERE NOT EXISTS(SELECT 1 FROM suppliers WHERE s_id = i.s_id)",
        )

    def test_nested_and_root(self):
        self.assertEqual(
            sql(
                not_in_subquery_to_not_exists,
                "SELECT 1 FROM items WHERE EXISTS(SELECT 1 FROM orders WHERE o_id NOT IN (SELECT l_order FROM order_lines))",
                non_null_columns=["o_id", "l_order"],
            ),
            "SELECT 1 FROM items WHERE EXISTS(SELECT 1 FROM orders WHERE NOT EXISTS(SELECT 1 FROM order_lines WHERE l_order = o_id))",
        )
        tree = sqlglot.parse_one("a NOT IN (SELECT b FROM u)", read="postgres")
        self.assertEqual(
            not_in_subquery_to_not_exists(tree, ["a", "b"]).sql("postgres"),
            "NOT EXISTS(SELECT 1 FROM u WHERE b = a)",
        )

    def test_copy(self):
        tree = sqlglot.parse_one(self.QUERY, read="postgres")
        not_in_subquery_to_not_exists(tree, ["i_supplier", "s_id"])
        self.assertIn("NOT i_supplier IN", tree.sql("postgres"))
        not_in_subquery_to_not_exists(tree, ["i_supplier", "s_id"], copy=False)
        self.assertNotIn("NOT i_supplier IN", tree.sql("postgres"))


class TestInAggregateSubqueryToExists(unittest.TestCase):
    QUERY = (
        "SELECT o_customer FROM orders WHERE o_id IN "
        "(SELECT l_order FROM order_lines GROUP BY l_order HAVING SUM(l_qty) > 5) AND o_total > 1"
    )

    def test_having_subquery(self):
        self.assertEqual(
            sql(in_aggregate_subquery_to_exists, self.QUERY),
            "SELECT o_customer FROM orders WHERE EXISTS(SELECT 1 FROM (SELECT l_order FROM order_lines "
            "GROUP BY l_order HAVING SUM(l_qty) > 5) AS _in_subquery WHERE _in_subquery.l_order = o_id) "
            "AND o_total > 1",
        )

    def test_alias_and_projection_alias(self):
        self.assertEqual(
            sql(
                in_aggregate_subquery_to_exists,
                "SELECT 1 FROM orders AS o WHERE o.o_id IN "
                "(SELECT l_order AS k FROM order_lines GROUP BY l_order HAVING COUNT(*) > 1)",
                alias="t",
            ),
            "SELECT 1 FROM orders AS o WHERE EXISTS(SELECT 1 FROM (SELECT l_order AS k FROM order_lines "
            "GROUP BY l_order HAVING COUNT(*) > 1) AS t WHERE t.k = o.o_id)",
        )

    def test_group_by_without_having_and_having_clause_context(self):
        self.assertEqual(
            sql(
                in_aggregate_subquery_to_exists,
                "SELECT k FROM t GROUP BY k HAVING k IN (SELECT l_order FROM order_lines GROUP BY l_order)",
            ),
            "SELECT k FROM t GROUP BY k HAVING EXISTS(SELECT 1 FROM (SELECT l_order FROM order_lines "
            "GROUP BY l_order) AS _in_subquery WHERE _in_subquery.l_order = k)",
        )

    def test_or_and_parentheses_are_positive_context(self):
        self.assertEqual(
            sql(
                in_aggregate_subquery_to_exists,
                "SELECT 1 FROM orders WHERE (o_id IN (SELECT l_order FROM order_lines GROUP BY l_order) OR o_total > 1)",
            ),
            "SELECT 1 FROM orders WHERE (EXISTS(SELECT 1 FROM (SELECT l_order FROM order_lines GROUP BY "
            "l_order) AS _in_subquery WHERE _in_subquery.l_order = o_id) OR o_total > 1)",
        )

    def test_other_shapes_are_left_alone(self):
        for query in (
            "SELECT 1 FROM orders WHERE o_id IN (SELECT l_order FROM order_lines)",
            "SELECT 1 FROM orders WHERE o_id NOT IN (SELECT l_order FROM order_lines GROUP BY l_order)",
            "SELECT 1 FROM orders WHERE NOT (o_id IN (SELECT l_order FROM order_lines GROUP BY l_order))",
            "SELECT 1 FROM orders WHERE o_id IN (1, 2)",
            "SELECT CASE WHEN o_id IN (SELECT l_order FROM order_lines GROUP BY l_order) THEN 1 END FROM orders",
            "SELECT o_id IN (SELECT l_order FROM order_lines GROUP BY l_order) FROM orders",
            "SELECT 1 FROM orders WHERE o_id IN (SELECT l_order, l_qty FROM order_lines GROUP BY l_order, l_qty)",
            "SELECT 1 FROM orders WHERE o_id IN (SELECT SUM(l_qty) FROM order_lines GROUP BY l_order)",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    sql(in_aggregate_subquery_to_exists, query),
                    sqlglot.parse_one(query, read="postgres").sql("postgres"),
                )

    def test_copy_and_root(self):
        tree = sqlglot.parse_one(self.QUERY, read="postgres")
        in_aggregate_subquery_to_exists(tree)
        self.assertIsNotNone(tree.find(exp.In))
        root = sqlglot.parse_one(
            "a IN (SELECT l_order FROM order_lines GROUP BY l_order)", read="postgres"
        )
        self.assertIsInstance(in_aggregate_subquery_to_exists(root), exp.In)


class TestDecorrelateSubqueries(unittest.TestCase):
    def rewrite(self, query):
        return sql(decorrelate_subqueries, query, schema=SCHEMA, dialect="postgres")

    def test_scalar_subquery_in_having_becomes_a_cross_join(self):
        self.assertEqual(
            self.rewrite(
                "SELECT i_supplier, SUM(i_qty) AS v FROM items GROUP BY i_supplier "
                "HAVING SUM(i_qty) > (SELECT SUM(i_qty) * 0.3 FROM items)"
            ),
            "SELECT items.i_supplier AS i_supplier, SUM(items.i_qty) AS v FROM items AS items "
            "CROSS JOIN (SELECT SUM(items.i_qty) * 0.3 AS _col_0 FROM items AS items) AS _u_0 "
            "GROUP BY items.i_supplier HAVING SUM(items.i_qty) > MAX(_u_0._col_0)",
        )

    def test_comma_joins_become_cross_joins_so_the_left_join_binds_correctly(self):
        rewritten = self.rewrite(
            "SELECT o_customer FROM orders, suppliers WHERE o_id = s_id AND o_id IN "
            "(SELECT l_order FROM order_lines GROUP BY l_order HAVING SUM(l_qty) > 5)"
        )
        self.assertIn(
            "FROM orders AS orders CROSS JOIN suppliers AS suppliers LEFT JOIN (", rewritten
        )
        self.assertNotIn(" IN (", rewritten)

    def test_selects_without_subqueries_keep_their_comma_joins(self):
        self.assertEqual(
            self.rewrite("SELECT o_customer FROM orders, suppliers WHERE o_id = s_id"),
            "SELECT orders.o_customer AS o_customer FROM orders AS orders, suppliers AS suppliers "
            "WHERE orders.o_id = suppliers.s_id",
        )

    def test_correlated_scalar_subquery_becomes_a_left_join(self):
        rewritten = self.rewrite(
            "SELECT o_id FROM orders WHERE o_total > (SELECT 0.5 * SUM(l_qty) FROM order_lines WHERE l_order = o_id)"
        )
        self.assertIn("LEFT JOIN (SELECT 0.5 * SUM(order_lines.l_qty) AS _col_0", rewritten)
        self.assertIn("GROUP BY order_lines.l_order) AS _u_0 ON _u_0._u_1 = orders.o_id", rewritten)

    def test_non_equality_correlation_is_left_in_place(self):
        query = (
            "SELECT o_id FROM orders WHERE EXISTS(SELECT 1 FROM order_lines "
            "WHERE l_order = o_id AND l_qty <> o_total)"
        )
        rewritten = self.rewrite(query)
        self.assertIn('EXISTS(SELECT 1 AS "1" FROM order_lines AS order_lines WHERE', rewritten)
        self.assertNotIn("ARRAY", rewritten)
        self.assertNotIn("_u_", rewritten)

    def test_not_in_is_left_in_place(self):
        rewritten = self.rewrite(
            "SELECT i_id FROM items WHERE i_supplier NOT IN (SELECT s_id FROM suppliers)"
        )
        self.assertIn(
            "NOT items.i_supplier IN (SELECT suppliers.s_id AS s_id FROM suppliers AS suppliers)",
            rewritten,
        )

    def test_does_not_mutate_the_input(self):
        tree = sqlglot.parse_one(
            "SELECT o_id FROM orders WHERE o_id IN (SELECT l_order FROM order_lines GROUP BY l_order)",
            read="postgres",
        )
        before = tree.sql("postgres")
        decorrelate_subqueries(tree, SCHEMA, "postgres")
        self.assertEqual(tree.sql("postgres"), before)


if __name__ == "__main__":
    unittest.main()
