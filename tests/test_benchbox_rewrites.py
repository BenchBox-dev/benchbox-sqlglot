import unittest
from decimal import Decimal

from sqlglot import exp, parse_one
from sqlglot.benchbox.rewrites import (
    date_integer_arithmetic_to_interval,
    fold_between_literal_arithmetic,
)
from tests import engines
from tests.engines import requires_engine


def fold(sql, columns=("discount",), tables=("items",)):
    return fold_between_literal_arithmetic(parse_one(sql, read="sqlite"), columns, tables).sql(
        "sqlite"
    )


def canonical(sql):
    return parse_one(sql, read="sqlite").sql("sqlite")


class TestFoldBetweenLiteralArithmetic(unittest.TestCase):
    def test_nested_and_qualified_bounds(self):
        self.assertEqual(
            fold(
                "SELECT 0.06 + 0.01 FROM items AS l "
                "WHERE l.discount BETWEEN (0.06 - 0.02 + 0.01) AND (0.06 + 0.02 - 0.01)"
            ),
            "SELECT 0.06 + 0.01 FROM items AS l WHERE l.discount BETWEEN 0.05 AND 0.07",
        )

    def test_only_gated_column_bounds_are_folded(self):
        for center, low, high in (("0.06", "0.05", "0.07"), ("0.07", "0.06", "0.08")):
            with self.subTest(center=center):
                self.assertEqual(
                    fold(
                        f"SELECT 0.06 + 0.01 AS untouched FROM items "
                        f"WHERE discount BETWEEN {center} - 0.01 AND {center} + 0.01 AND quantity < 25"
                    ),
                    f"SELECT 0.06 + 0.01 AS untouched FROM items "
                    f"WHERE discount BETWEEN {low} AND {high} AND quantity < 25",
                )

    def test_unrelated_or_nonliteral_expressions_are_unchanged(self):
        for predicate in (
            "discount BETWEEN center - 0.01 AND 0.06 + 0.01",
            "discount BETWEEN 0.06 - 0.01 AND center + 0.01",
            "other_col BETWEEN 0.06 - 0.01 AND 0.06 + 0.01",
            "discount > 0.06 + 0.01",
            "discount BETWEEN '0.06' - 0.01 AND '0.06' + 0.01",
            "discount BETWEEN 0.06 * 0.01 AND 0.06 + 0.01",
            "discount BETWEEN 0.05 AND 0.06 + 0.01",
            "discount BETWEEN 0.06 - 0.01 AND 0.07",
            "discount BETWEEN 1 - 1 AND 1 + 1",
            "discount BETWEEN 0.06 - 1 AND 0.06 + 1",
            "discount BETWEEN (9223372036854775807 + 1 - 9223372036854775807) AND (1 + 1)",
            "discount BETWEEN 0.01 - 0.06 AND 0.06 + 0.01",
            "discount BETWEEN 6e-2 - 1e-2 AND 6e-2 + 1e-2",
            "discount BETWEEN (10000000000000000000000000000.0 + 9.0 - 10000000000000000000000000000.0) "
            "AND (20.0 + 0.0)",
            "discount BETWEEN (100000000.0 + 0.0000000001 - 100000000.0) AND (0.06 + 0.01)",
            f"discount BETWEEN (1{'0' * 400}.0 - 1{'0' * 400}.0) AND (0.06 + 0.01)",
            "discount BETWEEN 0.01 + 0.01 + 0.01 + 0.01 + 0.01 AND 0.06 + 0.01",
            "discount BETWEEN 0.0000001 + 0.0000001 AND 0.06 + 0.01",
            "discount BETWEEN 100.0 - 99.0 AND 0.06 + 0.01",
        ):
            with self.subTest(predicate=predicate):
                sql = f"SELECT 0.06 + 0.01 FROM items WHERE {predicate}"
                self.assertEqual(fold(sql), canonical(sql))

    def test_table_gate(self):
        for sql in (
            "SELECT * FROM orders WHERE discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01",
            "SELECT 'items' AS note FROM t WHERE discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01",
            "SELECT * FROM t /* items */ WHERE discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(fold(sql), canonical(sql))
        folded = "BETWEEN 0.05 AND 0.07"
        self.assertIn(
            folded,
            fold(
                "SELECT 1 FROM orders o JOIN items i ON i.id = o.id "
                "WHERE i.discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01"
            ),
        )
        self.assertIn(
            folded,
            fold(
                "SELECT * FROM (SELECT discount FROM items) "
                "WHERE discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01"
            ),
        )
        # The table gate is case-insensitive and optional.
        sql = "SELECT * FROM Items WHERE Discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01"
        self.assertIn(folded, fold(sql))
        self.assertIn(folded, fold(sql.replace("Items", "orders"), tables=None))
        self.assertNotIn(folded, fold(sql, tables=()))
        self.assertNotIn(folded, fold(sql, columns=()))

    def test_bare_dot_decimal_bounds(self):
        self.assertIn(
            "BETWEEN 0.05 AND 0.07",
            fold("SELECT discount FROM items WHERE discount BETWEEN .06 - .01 AND .06 + .01"),
        )

    def test_copy(self):
        tree = parse_one(
            "SELECT * FROM items WHERE discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01", read="sqlite"
        )
        before = tree.sql("sqlite")
        folded = fold_between_literal_arithmetic(tree, ["discount"])
        self.assertEqual(tree.sql("sqlite"), before)
        self.assertIsNot(folded, tree)
        in_place = fold_between_literal_arithmetic(tree, ["discount"], copy=False)
        self.assertIs(in_place, tree)
        between = tree.find(exp.Between)
        assert between is not None
        self.assertIsInstance(between.args["low"], exp.Literal)
        self.assertIsInstance(between.args["high"], exp.Literal)

    def test_other_dialect_trees_are_rewritten_the_same(self):
        for dialect in ("duckdb", "postgres", "mysql"):
            with self.subTest(dialect=dialect):
                tree = parse_one(
                    "SELECT * FROM items WHERE discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01",
                    read=dialect,
                )
                self.assertIn(
                    "BETWEEN 0.05 AND 0.07",
                    fold_between_literal_arithmetic(tree, ["discount"], ["items"]).sql(dialect),
                )


@requires_engine("sqlite")
class TestFoldBetweenExecution(unittest.TestCase):
    def test_integer_overflow_bounds_keep_sqlite_semantics(self):
        sql = (
            "WITH items AS (SELECT 0 AS discount) SELECT * FROM items "
            "WHERE discount BETWEEN (9223372036854775807 + 1 - 9223372036854775807) AND (1 + 1)"
        )
        self.assertEqual(engines.execute("sqlite", sql), [(0,)])
        self.assertEqual(engines.execute("sqlite", fold(sql)), [(0,)])

    def test_bare_dot_decimal_bounds_follow_exact_decimals(self):
        sql = "SELECT discount FROM items WHERE discount BETWEEN .06 - .01 AND .06 + .01 ORDER BY discount"
        with engines.connect("sqlite") as connection:
            connection.execute("CREATE TABLE items (discount DECIMAL(15,2))")
            for value in ("0.04", "0.05", "0.06", "0.07"):
                connection.execute(f"INSERT INTO items VALUES ('{value}')")
            self.assertEqual(connection.execute(sql), [(0.05,), (0.06,)])
            rows = connection.execute(fold(sql))
            self.assertEqual(rows, [(0.05,), (0.06,), (0.07,)])
            self.assertEqual([type(row[0]) for row in rows], [float] * 3)

    def test_two_decimal_domain_matches_exact_decimal_semantics(self):
        values = [f"{cents / 100:.2f}" for cents in range(101)]
        with engines.connect("sqlite") as connection:
            connection.execute("CREATE TABLE items (discount DECIMAL(15,2))")
            for value in values:
                connection.execute(f"INSERT INTO items VALUES ('{value}')")
            unfolded_wrong = folded_wrong = checked = 0
            for cents in range(101):
                for delta in ("0.01", "0.02", "0.05"):
                    center = f"{cents / 100:.2f}"
                    low, high = Decimal(center) - Decimal(delta), Decimal(center) + Decimal(delta)
                    if low < 0:
                        continue
                    sql = (
                        f"SELECT discount FROM items WHERE discount BETWEEN {center} - {delta} "
                        f"AND {center} + {delta} ORDER BY discount"
                    )
                    expected = [float(v) for v in values if low <= Decimal(v) <= high]
                    checked += 1
                    unfolded_wrong += [r[0] for r in connection.execute(sql)] != expected
                    folded_wrong += [r[0] for r in connection.execute(fold(sql))] != expected
            self.assertGreater(checked, 250)
            self.assertGreater(
                unfolded_wrong, 0, "the sweep must exercise the float boundary defect"
            )
            self.assertEqual(folded_wrong, 0)

    def test_boundary_membership(self):
        for center, discounts, expected_sum in (
            ("0.06", ("0.04", "0.05", "0.06", "0.07", "0.08"), 18),
            ("0.07", ("0.05", "0.06", "0.07", "0.08", "0.09"), 21),
        ):
            membership = (
                f"SELECT discount BETWEEN {center} - 0.01 AND {center} + 0.01 FROM items "
                "ORDER BY discount"
            )
            aggregate = (
                f"SELECT SUM(100 * discount) FROM items "
                f"WHERE discount BETWEEN {center} - 0.01 AND {center} + 0.01"
            )
            with engines.connect("sqlite") as connection, self.subTest(center=center):
                connection.execute("CREATE TABLE items (discount DECIMAL(15,2))")
                for value in discounts:
                    connection.execute(f"INSERT INTO items VALUES ('{value}')")
                self.assertEqual(
                    connection.execute(fold(membership)), [(0,), (1,), (1,), (1,), (0,)]
                )
                self.assertEqual(connection.execute(fold(aggregate)), [(expected_sum,)])

    def test_value_inside_the_double_noise_band_follows_exact_decimals(self):
        sql = (
            "WITH items AS (SELECT 0.00000000000000001 AS discount) SELECT discount FROM items "
            "WHERE discount BETWEEN (0.0 + 0.0) AND (0.1 + 0.2 - 0.3)"
        )
        self.assertEqual(engines.execute("sqlite", sql), [(1e-17,)])
        self.assertEqual(engines.execute("sqlite", fold(sql)), [])

    def test_cancellation_and_overflow_bounds_keep_sqlite_semantics(self):
        for rows, low, high, expected in (
            (
                (8.0, 9.0),
                "(10000000000000000000000000000.0 + 9.0 - 10000000000000000000000000000.0)",
                "(20.0 + 0.0)",
                [8.0, 9.0],
            ),
            (
                (0.0, 0.05),
                "(100000000.0 + 0.0000000001 - 100000000.0)",
                "(0.06 + 0.01)",
                [0.0, 0.05],
            ),
            ((0.05,), f"(1{'0' * 400}.0 - 1{'0' * 400}.0)", "(0.06 + 0.01)", []),
        ):
            values = " UNION ALL ".join(f"SELECT {value} AS discount" for value in rows)
            sql = (
                f"WITH items AS ({values}) SELECT discount FROM items "
                f"WHERE discount BETWEEN {low} AND {high} ORDER BY discount"
            )
            with self.subTest(low=low):
                self.assertEqual(fold(sql), canonical(sql))
                self.assertEqual([v for (v,) in engines.execute("sqlite", sql)], expected)
                self.assertEqual([v for (v,) in engines.execute("sqlite", fold(sql))], expected)


class TestDateIntegerArithmeticToInterval(unittest.TestCase):
    def rewrite(self, sql, columns=("d_date",), read="netezza", write="postgres"):
        return date_integer_arithmetic_to_interval(parse_one(sql, read=read), columns).sql(write)

    def test_integer_days_become_day_intervals(self):
        self.assertEqual(
            self.rewrite("SELECT d_date + 5, d_date - 30 FROM t"),
            "SELECT d_date + INTERVAL '5 DAY', d_date - INTERVAL '30 DAY' FROM t",
        )

    def test_qualified_and_case_insensitive_columns(self):
        self.assertEqual(
            self.rewrite("SELECT D1.D_DATE + 7 FROM date_dim AS d1 WHERE d1.d_date - 1 > x"),
            "SELECT D1.D_DATE + INTERVAL '7 DAY' FROM date_dim AS d1 "
            "WHERE d1.d_date - INTERVAL '1 DAY' > x",
        )

    def test_the_column_set_is_the_gate(self):
        sql = "SELECT d_date + 5, d_date_sk + 5, qty + 5 FROM t"
        self.assertEqual(
            self.rewrite(sql),
            "SELECT d_date + INTERVAL '5 DAY', d_date_sk + 5, qty + 5 FROM t",
        )
        self.assertEqual(
            self.rewrite(sql, columns=()), "SELECT d_date + 5, d_date_sk + 5, qty + 5 FROM t"
        )
        self.assertEqual(
            self.rewrite(sql, columns=["qty"]),
            "SELECT d_date + 5, d_date_sk + 5, qty + INTERVAL '5 DAY' FROM t",
        )

    def test_only_a_whole_integer_literal_operand_is_rewritten(self):
        for sql in (
            "SELECT d_date + 5 * 2 FROM t",
            "SELECT d_date + n FROM t",
            "SELECT d_date + 1.5 FROM t",
            "SELECT d_date + '5' FROM t",
            "SELECT 5 + d_date FROM t",
            "SELECT d_date + d_date FROM t",
            "SELECT d_date + INTERVAL '5' DAY FROM t",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.rewrite(sql), parse_one(sql, read="netezza").sql("postgres"))

    def test_copy_flag(self):
        tree = parse_one("SELECT d_date + 5", read="netezza")
        rewritten = date_integer_arithmetic_to_interval(tree, ["d_date"])
        self.assertIsNot(rewritten, tree)
        self.assertEqual(tree.sql("postgres"), "SELECT d_date + 5")
        same = date_integer_arithmetic_to_interval(tree, ["d_date"], copy=False)
        self.assertIs(same, tree)
        self.assertIsNotNone(tree.find(exp.Interval))

    def test_other_write_dialects(self):
        self.assertEqual(
            self.rewrite("SELECT d_date + 5", write="duckdb"),
            "SELECT d_date + INTERVAL '5' DAY",
        )


if __name__ == "__main__":
    unittest.main()
