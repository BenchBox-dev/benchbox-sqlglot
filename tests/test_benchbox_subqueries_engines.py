"""Execute the subquery rewrites on real engines and compare their rows with the original query.

Run by the ``in-process`` and ``postgres`` jobs of ``benchbox-engines.yml``; each class skips when its
engine is not available.
"""

import unittest
import uuid
from decimal import Decimal

import sqlglot
from sqlglot.benchbox.subqueries import (
    decorrelate_subqueries,
    in_aggregate_subquery_to_exists,
    not_in_subquery_to_not_exists,
)
from tests import engines
from tests.engines import requires_engine

TABLES = {
    "items": (
        {"i_id": "int", "i_supplier": "int", "i_qty": "int", "i_name": "text"},
        [
            (1, 10, 5, "forest oak"),
            (2, 10, 40, "forest pine"),
            (3, 20, 7, "desert"),
            (4, 30, 3, "forest elm"),
            (5, 40, 9, "marsh"),
        ],
    ),
    "suppliers": (
        {"s_id": "int", "s_region": "text", "s_comment": "text"},
        [
            (10, "north", "fine"),
            (20, "north", "bad service"),
            (30, "south", "fine"),
            (40, "south", "very bad"),
            (50, "north", "fine"),
        ],
    ),
    "shipments": (
        {"h_item": "int", "h_supplier": "int", "h_qty": "int"},
        [
            (1, 10, 4),
            (1, 10, 2),
            (2, 10, 30),
            (2, 10, 50),
            (3, 20, 3),
            (4, 30, 1),
            (4, 30, 1),
            (5, 40, 9),
            (5, 50, 100),
        ],
    ),
    "orders": (
        {"o_id": "int", "o_customer": "text", "o_total": "int"},
        [(1, "ann", 100), (2, "bob", 50), (3, "cy", 75), (4, "dee", 20)],
    ),
    "order_lines": (
        {"l_order": "int", "l_qty": "int"},
        [(1, 3), (1, 4), (2, 1), (3, 6), (3, 1), (4, 2), (4, 2)],
    ),
}
SCHEMA = {name: columns for name, (columns, _) in TABLES.items()}
NON_NULL = ["i_supplier", "s_id", "o_id", "l_order"]

QUERIES = {
    "having_scalar": (
        "SELECT i_supplier, SUM(i_qty) AS v FROM items, suppliers WHERE i_supplier = s_id "
        "GROUP BY i_supplier HAVING SUM(i_qty) > (SELECT SUM(i_qty) * 0.3 FROM items, suppliers "
        "WHERE i_supplier = s_id AND s_region = 'north')"
    ),
    "not_in": (
        "SELECT i_id FROM items WHERE i_qty > 1 AND i_supplier NOT IN "
        "(SELECT s_id FROM suppliers WHERE s_comment LIKE '%bad%')"
    ),
    "in_having": (
        "SELECT o_customer, o_total FROM orders, suppliers WHERE o_id = s_id / 10 AND o_id IN "
        "(SELECT l_order FROM order_lines GROUP BY l_order HAVING SUM(l_qty) > 2) "
        "UNION ALL SELECT o_customer, o_total FROM orders WHERE o_id IN "
        "(SELECT l_order FROM order_lines GROUP BY l_order HAVING SUM(l_qty) > 4)"
    ),
    "nested_correlated": (
        "SELECT s_id FROM suppliers WHERE s_id IN (SELECT h1.h_supplier FROM shipments AS h1 WHERE h1.h_item IN "
        "(SELECT i_id FROM items WHERE i_name LIKE 'forest%') AND h1.h_qty > "
        "(SELECT 0.5 * SUM(h2.h_qty) FROM shipments AS h2 "
        "WHERE h2.h_item = h1.h_item AND h2.h_supplier = h1.h_supplier))"
    ),
    "correlated_scalar": (
        "SELECT o_id FROM orders WHERE o_total > (SELECT 10 * SUM(l_qty) FROM order_lines WHERE l_order = o_id)"
    ),
    "correlated_exists_with_inequality": (
        "SELECT o_id FROM orders WHERE EXISTS(SELECT 1 FROM order_lines WHERE l_order = o_id AND l_qty <> o_id)"
    ),
}


def values(rows):
    return sorted(
        (
            tuple(
                float(v) if isinstance(v, (Decimal, float)) and not isinstance(v, bool) else v
                for v in row
            )
            for row in rows
        ),
        key=repr,
    )


def literal(value):
    return "NULL" if value is None else repr(value) if isinstance(value, str) else str(value)


class SubqueriesOnEngine:
    ENGINE = ""
    DIALECT = ""
    PREFIX = "CREATE TEMPORARY VIEW"

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:8]
        self.connection = engines.connect(self.ENGINE)
        self.names = {name: f"{name}_{self.suffix}" for name in TABLES}
        for name, (columns, rows) in TABLES.items():
            select = " UNION ALL ".join(
                "SELECT "
                + ", ".join(
                    f"CAST({literal(value)} AS {'INT' if kind == 'int' else 'VARCHAR(40)'}) AS {column}"
                    for (column, kind), value in zip(columns.items(), row)
                )
                for row in rows
            )
            self.connection.execute(f"{self.PREFIX} {self.names[name]} AS {select}")

    def tearDown(self):
        self.connection.close()

    def parse(self, query):
        tree = sqlglot.parse_one(query, read=self.DIALECT)
        for table in tree.find_all(sqlglot.exp.Table):
            if table.name in self.names:
                table.set("this", sqlglot.exp.to_identifier(self.names[table.name]))
        return tree

    def schema(self):
        return {self.names[name]: columns for name, columns in SCHEMA.items()}

    def rows(self, sql):
        return values(self.connection.execute(sql))

    def assert_equivalent(self, name, rewrite):
        tree = self.parse(QUERIES[name])
        original = tree.sql(self.DIALECT)
        rewritten = rewrite(tree).sql(self.DIALECT)
        self.assertNotEqual(rewritten, original)
        expected = self.rows(original)
        self.assertTrue(expected)
        self.assertEqual(self.rows(rewritten), expected)
        return rewritten

    def decorrelate(self, tree):
        return decorrelate_subqueries(tree, self.schema(), self.DIALECT)

    def test_decorrelated_having_scalar(self):
        rewritten = self.assert_equivalent("having_scalar", self.decorrelate)
        self.assertIn("CROSS JOIN", rewritten)

    def test_decorrelated_in_having_with_comma_joins(self):
        rewritten = self.assert_equivalent("in_having", self.decorrelate)
        self.assertIn("LEFT JOIN", rewritten)

    def test_decorrelated_nested_correlated(self):
        rewritten = self.assert_equivalent("nested_correlated", self.decorrelate)
        self.assertIn("LEFT JOIN", rewritten)

    def test_decorrelated_correlated_scalar(self):
        self.assert_equivalent("correlated_scalar", self.decorrelate)

    def test_decorrelation_leaves_inequality_correlation_correct(self):
        tree = self.parse(QUERIES["correlated_exists_with_inequality"])
        original = tree.sql(self.DIALECT)
        rewritten = self.decorrelate(tree).sql(self.DIALECT)
        self.assertNotIn("ARRAY", rewritten)
        self.assertEqual(self.rows(rewritten), self.rows(original))

    def test_not_in_to_not_exists(self):
        rewritten = self.assert_equivalent(
            "not_in",
            lambda tree: not_in_subquery_to_not_exists(tree, NON_NULL),
        )
        self.assertIn("NOT EXISTS", rewritten)

    def test_in_aggregate_to_exists(self):
        rewritten = self.assert_equivalent("in_having", in_aggregate_subquery_to_exists)
        self.assertIn("EXISTS", rewritten)


@requires_engine("postgres")
class TestOnPostgres(SubqueriesOnEngine, unittest.TestCase):
    ENGINE = "postgres"
    DIALECT = "postgres"
    PREFIX = "CREATE TEMPORARY VIEW"


@requires_engine("duckdb")
class TestOnDuckDB(SubqueriesOnEngine, unittest.TestCase):
    ENGINE = "duckdb"
    DIALECT = "duckdb"


@requires_engine("datafusion")
class TestOnDataFusion(SubqueriesOnEngine, unittest.TestCase):
    ENGINE = "datafusion"
    DIALECT = "datafusion"
    PREFIX = "CREATE VIEW"


@requires_engine("pyspark")
class TestOnPySpark(SubqueriesOnEngine, unittest.TestCase):
    ENGINE = "pyspark"
    DIALECT = "spark"


if __name__ == "__main__":
    unittest.main()
