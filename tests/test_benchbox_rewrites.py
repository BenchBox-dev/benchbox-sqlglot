import unittest
from decimal import Decimal

from sqlglot import exp, parse_one
from sqlglot.benchbox.rewrites import (
    date_integer_arithmetic_to_interval,
    deduplicate_output_aliases,
    flag_comparisons_to_boolean,
    fold_between_literal_arithmetic,
    mark_date_columns,
    safe_division,
    unqualified_tables,
)
from sqlglot.dialects.dialect import Dialect
from tests import engines
from tests.engines import requires_engine


def fold(sql, read="sqlite"):
    return fold_between_literal_arithmetic(parse_one(sql, read=read)).sql(read)


def canonical(sql):
    return parse_one(sql, read="sqlite").sql("sqlite")


class TestFoldBetweenLiteralArithmetic(unittest.TestCase):
    def test_nested_and_qualified_bounds(self):
        self.assertEqual(
            fold(
                "SELECT 0.06 + 0.01 FROM items AS l "
                "WHERE l.discount BETWEEN (0.06 - 0.02 + 0.01) AND (0.04 + 0.02 + 0.01)"
            ),
            "SELECT 0.06 + 0.01 FROM items AS l WHERE l.discount BETWEEN 0.05 AND 0.07",
        )

    def test_every_table_and_column_is_folded(self):
        for center, low, high in (("0.06", "0.05", "0.07"), ("0.07", "0.06", "0.07 + 0.01")):
            for table, column in (
                ("items", "discount"),
                ("sat_lineitem", "sl_discount"),
                ("t", "x"),
            ):
                with self.subTest(center=center, table=table):
                    self.assertEqual(
                        fold(
                            f"SELECT 0.06 + 0.01 AS untouched FROM {table} "
                            f"WHERE {column} BETWEEN {center} - 0.01 AND {center} + 0.01 AND quantity < 25"
                        ),
                        f"SELECT 0.06 + 0.01 AS untouched FROM {table} "
                        f"WHERE {column} BETWEEN {low} AND {high} AND quantity < 25",
                    )

    def test_each_bound_and_operand_is_folded_on_its_own(self):
        for sql, expected in (
            (
                "discount BETWEEN 0.06 - 0.01 AND center + 0.01",
                "discount BETWEEN 0.05 AND center + 0.01",
            ),
            (
                "discount BETWEEN center - 0.01 AND 0.06 + 0.01",
                "discount BETWEEN center - 0.01 AND 0.07",
            ),
            ("discount > 0.06 + 0.01", "discount > 0.07"),
            ("0.06 + 0.01 <= discount", "0.07 <= discount"),
            ("discount >= 0.06 - 0.01", "discount >= 0.05"),
            ("discount < (0.06 + 0.01)", "discount < 0.07"),
            ("discount = 0.1 + 0.2", "discount = 0.3"),
            ("discount <> 0.1 + 0.2", "discount <> 0.3"),
            ("discount BETWEEN 0.01 - 0.06 AND 0.06 + 0.01", "discount BETWEEN -0.05 AND 0.07"),
            ("discount BETWEEN 0.07 - 1 AND 0.07 + 1", "discount BETWEEN -0.93 AND 0.07 + 1"),
            (
                "discount BETWEEN 100.0 - 99.0 AND 0.06 + 0.01",
                "discount BETWEEN 100.0 - 99.0 AND 0.07",
            ),
        ):
            with self.subTest(predicate=sql):
                self.assertEqual(
                    fold(f"SELECT 1 FROM items WHERE {sql}"),
                    canonical(f"SELECT 1 FROM items WHERE {expected}"),
                )

    def test_unrelated_or_nonliteral_expressions_are_unchanged(self):
        for predicate in (
            "discount BETWEEN center - 0.01 AND center + 0.01",
            "discount BETWEEN '0.06' - 0.01 AND '0.06' + 0.01",
            "discount BETWEEN 0.06 * 0.01 AND 0.06 / 0.01",
            "discount BETWEEN 0.05 AND 0.07",
            "discount BETWEEN 1 - 1 AND 1 + 1",
            "discount BETWEEN 0.5 + 0.25 AND 0.5 - 0.25",
            "discount BETWEEN (9223372036854775807 + 1 - 9223372036854775807) AND (1 + 1)",
            "discount BETWEEN 6e-2 - 1e-2 AND 6e-2 + 1e-2",
            (
                "discount BETWEEN (10000000000000000000000000000.0 + 9.0 - 10000000000000000000000000000.0) "
                "AND (20.0 + 0.0)"
            ),
            "discount BETWEEN (100000000.0 + 0.0000000001 - 100000000.0) AND (20.0 + 0.0)",
            f"discount BETWEEN (1{'0' * 400}.0 - 1{'0' * 400}.0) AND (20.0 + 0.0)",
            "discount BETWEEN 0.01 + 0.01 + 0.01 + 0.01 + 0.01 AND 20.0 + 0.0",
            "discount BETWEEN 0.0000001 + 0.0000001 AND 20.0 + 0.0",
            "discount + 0.06 > 0.07 AND price * (0.06 + 0.01) > 1",
            "discount IN (0.06 + 0.01, 0.1 + 0.2)",
        ):
            with self.subTest(predicate=predicate):
                sql = f"SELECT 0.06 + 0.01 FROM items WHERE {predicate}"
                self.assertEqual(fold(sql), canonical(sql))

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
        folded = fold_between_literal_arithmetic(tree)
        self.assertEqual(tree.sql("sqlite"), before)
        self.assertIsNot(folded, tree)
        in_place = fold_between_literal_arithmetic(tree, copy=False)
        self.assertIs(in_place, tree)
        between = tree.find(exp.Between)
        assert between is not None
        self.assertIsInstance(between.args["low"], exp.Literal)
        self.assertIsInstance(between.args["high"], exp.Literal)

    def test_other_dialect_trees_are_rewritten_the_same(self):
        for dialect in ("duckdb", "postgres", "mysql", "clickhouse"):
            with self.subTest(dialect=dialect):
                self.assertIn(
                    "BETWEEN 0.05 AND 0.07",
                    fold(
                        "SELECT * FROM items WHERE discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01",
                        read=dialect,
                    ),
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
                "(20.0 + 0.0)",
                [0.0, 0.05],
            ),
            ((0.05,), f"(1{'0' * 400}.0 - 1{'0' * 400}.0)", "(20.0 + 0.0)", []),
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


class TestMarkDateColumns(unittest.TestCase):
    def test_named_columns_are_typed_date(self):
        tree = mark_date_columns(
            parse_one("SELECT D1.D_DATE, d_date_sk, qty FROM t AS d1", read="netezza"),
            ["d_date"],
        )
        types = {column.name: column.type for column in tree.find_all(exp.Column)}
        self.assertTrue(types["D_DATE"].is_type(exp.DType.DATE))
        self.assertIsNone(types["d_date_sk"])
        self.assertIsNone(types["qty"])

    def test_no_columns_marks_nothing(self):
        tree = mark_date_columns(parse_one("SELECT d_date FROM t"), [])
        self.assertIsNone(tree.find(exp.Column).type)

    def test_copy_flag(self):
        tree = parse_one("SELECT d_date")
        marked = mark_date_columns(tree, ["d_date"])
        self.assertIsNot(marked, tree)
        self.assertIsNone(tree.find(exp.Column).type)
        self.assertIs(mark_date_columns(tree, ["d_date"], copy=False), tree)
        self.assertTrue(tree.find(exp.Column).type.is_type(exp.DType.DATE))

    def test_marking_does_not_change_other_dialects(self):
        sql = "SELECT d_date + 5 FROM t"
        marked = mark_date_columns(parse_one(sql, read="netezza"), ["d_date"])
        self.assertEqual(marked.sql("postgres"), sql)

    def test_sqlite_counts_whole_days(self):
        marked = mark_date_columns(
            parse_one(
                "SELECT d3.d_date - d1.d_date, d1.d_date + 5 FROM t AS d1, t AS d3 "
                "WHERE d3.d_date > d1.d_date + 5",
                read="netezza",
            ),
            ["d_date"],
        )
        self.assertEqual(
            marked.sql("sqlite"),
            "SELECT CAST((JULIANDAY(d3.d_date) - JULIANDAY(d1.d_date)) AS INTEGER), "
            "DATE(d1.d_date, '5 DAY') FROM t AS d1, t AS d3 "
            "WHERE d3.d_date > DATE(d1.d_date, '5 DAY')",
        )

    @requires_engine("sqlite")
    @requires_engine("duckdb")
    def test_sqlite_matches_duckdb_on_a_tpcds_q72_predicate(self):
        sql = (
            "SELECT d1.d_date FROM date_dim AS d1 JOIN date_dim AS d3 ON d3.d_date > d1.d_date + 5 "
            "WHERE d1.d_date < '2000-01-03' ORDER BY d1.d_date, d3.d_date"
        )
        days = ("2000-01-01", "2000-01-05", "2000-01-06", "2000-01-07", "2000-01-08")
        results = {}
        for engine in ("sqlite", "duckdb"):
            with engines.connect(engine) as connection:
                connection.execute(
                    f"CREATE TABLE date_dim (d_date {'TEXT' if engine == 'sqlite' else 'DATE'})"
                )
                for day in days:
                    connection.execute(f"INSERT INTO date_dim VALUES ('{day}')")
                tree = mark_date_columns(parse_one(sql, read="netezza"), ["d_date"])
                results[engine] = [(str(d),) for (d,) in connection.execute(tree.sql(engine))]
        self.assertEqual(results["duckdb"], [("2000-01-01",), ("2000-01-01",)])
        self.assertEqual(results["sqlite"], results["duckdb"])


if __name__ == "__main__":
    unittest.main()


def rewrite_division(sql, dialect):
    return safe_division(parse_one(sql, read=dialect), dialect).sql(dialect)


class TestSafeDivision(unittest.TestCase):
    def test_bigquery_uses_safe_divide(self):
        self.assertEqual(
            rewrite_division("SELECT a / b, (x + 1) / (y - 1) / z FROM t", "bigquery"),
            "SELECT SAFE_DIVIDE(a, b), SAFE_DIVIDE(SAFE_DIVIDE((x + 1), (y - 1)), z) FROM t",
        )

    def test_databricks_uses_try_divide(self):
        self.assertEqual(
            rewrite_division("SELECT a / b FROM t WHERE x / y > 1", "databricks"),
            "SELECT TRY_DIVIDE(a, b) FROM t WHERE TRY_DIVIDE(x, y) > 1",
        )

    def test_snowflake_wraps_divisor_in_nullif(self):
        self.assertEqual(
            rewrite_division("SELECT a / b, a / 2, c / NULLIF(d, 0) FROM t", "snowflake"),
            "SELECT a / NULLIF(b, 0), a / NULLIF(2, 0), c / NULLIF(d, 0) FROM t",
        )

    def test_clickhouse_wraps_non_literal_divisors(self):
        self.assertEqual(
            rewrite_division(
                "SELECT a / b, a / 2, a / 0, a / NULLIF(b, 0), "
                "a / CAST(b AS Nullable(Decimal(15, 2))), a / (b - c) FROM t",
                "clickhouse",
            ),
            "SELECT a / nullIf(b, 0), a / 2, a / nullIf(0, 0), a / nullIf(b, 0), "
            "a / nullIf(CAST(b AS Nullable(Decimal(15, 2))), 0), a / nullIf((b - c), 0) FROM t",
        )

    def test_nested_division_in_every_position(self):
        self.assertEqual(
            rewrite_division("SELECT SUM(a / b) / SUM(c / d) FROM t", "snowflake"),
            "SELECT SUM(a / NULLIF(b, 0)) / NULLIF(SUM(c / NULLIF(d, 0)), 0) FROM t",
        )

    def test_rewrite_is_idempotent(self):
        for dialect in ("bigquery", "databricks", "snowflake", "clickhouse"):
            with self.subTest(dialect=dialect):
                once = rewrite_division("SELECT a / b FROM t", dialect)
                self.assertEqual(rewrite_division(once, dialect), once)

    def test_copy_flag(self):
        tree = parse_one("SELECT a / b FROM t", read="snowflake")
        safe_division(tree, "snowflake")
        self.assertEqual(tree.sql("snowflake"), "SELECT a / b FROM t")
        safe_division(tree, "snowflake", copy=False)
        self.assertEqual(tree.sql("snowflake"), "SELECT a / NULLIF(b, 0) FROM t")

    def test_unsupported_dialect_raises(self):
        with self.assertRaises(ValueError):
            safe_division(parse_one("SELECT a / b"), "postgres")
        with self.assertRaises(ValueError):
            safe_division(parse_one("SELECT a / b"), "no_such_dialect")

    def test_dialect_object_is_accepted(self):
        self.assertEqual(
            safe_division(parse_one("SELECT a / b"), Dialect.get_or_raise("bigquery")).sql(
                "bigquery"
            ),
            "SELECT SAFE_DIVIDE(a, b)",
        )

    def test_other_dialects_render_the_bigquery_form(self):
        tree = safe_division(parse_one("SELECT a / b FROM t", read="bigquery"), "bigquery")
        self.assertEqual(
            tree.sql("duckdb"), "SELECT CASE WHEN b <> 0 THEN a / b ELSE NULL END FROM t"
        )
        self.assertEqual(
            tree.sql("postgres"),
            "SELECT CASE WHEN b <> 0 THEN CAST(a AS DOUBLE PRECISION) / b ELSE NULL END FROM t",
        )


def dedupe(sql, dialect="databricks"):
    return deduplicate_output_aliases(parse_one(sql, read=dialect)).sql(dialect)


class TestDeduplicateOutputAliases(unittest.TestCase):
    def test_repeated_aliases_and_columns_are_suffixed(self):
        self.assertEqual(
            dedupe("SELECT a AS x, b AS x, c AS x, a, t.a, d FROM t"),
            "SELECT a AS x, b AS x_2, c AS x_3, a, t.a AS a_2, d FROM t",
        )

    def test_names_compare_case_insensitively_and_keep_quoting(self):
        self.assertEqual(
            dedupe("SELECT a AS `Total`, b AS TOTAL, c AS `total` FROM t"),
            "SELECT a AS `Total`, b AS TOTAL_2, c AS `total_3` FROM t",
        )

    def test_unnamed_expressions_are_not_counted(self):
        self.assertEqual(
            dedupe("SELECT a + 1, b + 1, c AS c, d AS c FROM t"),
            "SELECT a + 1, b + 1, c AS c, d AS c_2 FROM t",
        )

    def test_unique_projection_is_unchanged(self):
        sql = "SELECT a, b AS c FROM t"
        self.assertEqual(dedupe(sql), sql)

    def test_star_projection_is_unchanged(self):
        sql = "SELECT *, a AS x, b AS x FROM t"
        self.assertEqual(dedupe(sql), sql)

    def test_order_or_group_by_a_repeated_name_is_unchanged(self):
        for tail in ("ORDER BY x", "GROUP BY x", "GROUP BY 1 ORDER BY X DESC"):
            with self.subTest(tail=tail):
                sql = f"SELECT a AS x, b AS x FROM t {tail}"
                self.assertEqual(dedupe(sql), sql)

    def test_qualified_order_by_does_not_block_the_rename(self):
        self.assertEqual(
            dedupe("SELECT a AS x, b AS x FROM t ORDER BY t.a"),
            "SELECT a AS x, b AS x_2 FROM t ORDER BY t.a",
        )

    def test_with_clause_select_is_deduplicated(self):
        self.assertEqual(
            dedupe("WITH c AS (SELECT 1 AS k) SELECT k, k FROM c"),
            "WITH c AS (SELECT 1 AS k) SELECT k, k AS k_2 FROM c",
        )

    def test_set_operation_operands_are_unchanged(self):
        for sql in (
            "SELECT a AS x, b AS x UNION SELECT 1, 2",
            "(SELECT a AS x, b AS x) UNION ALL (SELECT 1, 2)",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(dedupe(sql), parse_one(sql, read="databricks").sql("databricks"))

    def test_nested_selects_are_deduplicated(self):
        self.assertEqual(
            dedupe(
                "WITH c AS (SELECT a AS x, b AS x FROM t) "
                "SELECT s.x, s.k FROM (SELECT x, x AS k, x AS k FROM c) AS s "
                "WHERE s.x IN (SELECT a AS y, b AS y FROM u)"
            ),
            "WITH c AS (SELECT a AS x, b AS x_2 FROM t) "
            "SELECT s.x, s.k FROM (SELECT x, x AS k, x AS k_2 FROM c) AS s "
            "WHERE s.x IN (SELECT a AS y, b AS y_2 FROM u)",
        )

    def test_nested_select_skip_rules_apply_per_select(self):
        self.assertEqual(
            dedupe(
                "SELECT s.x FROM (SELECT a AS x, b AS x FROM t ORDER BY x) AS s CROSS JOIN "
                "(SELECT c AS y, d AS y FROM t) AS r"
            ),
            "SELECT s.x FROM (SELECT a AS x, b AS x FROM t ORDER BY x) AS s CROSS JOIN "
            "(SELECT c AS y, d AS y_2 FROM t) AS r",
        )

    def test_dml_source_is_deduplicated(self):
        self.assertEqual(
            dedupe("INSERT INTO t SELECT 1 AS x, 2 AS x"), "INSERT INTO t SELECT 1 AS x, 2 AS x_2"
        )

    def test_only_the_top_level_select_without_nested(self):
        tree = parse_one(
            "SELECT s.x FROM (SELECT a AS x, b AS x FROM t) AS s, "
            "(SELECT 1) AS r WHERE 1 = 1 UNION SELECT 1",
            read="databricks",
        )
        self.assertEqual(
            deduplicate_output_aliases(tree, nested=False).sql("databricks"),
            tree.sql("databricks"),
        )
        plain = parse_one("SELECT a AS x, b AS x FROM (SELECT c AS y, d AS y FROM t)")
        self.assertEqual(
            deduplicate_output_aliases(plain, nested=False).sql(),
            "SELECT a AS x, b AS x_2 FROM (SELECT c AS y, d AS y FROM t)",
        )

    def test_copy_flag(self):
        tree = parse_one("SELECT a AS x, b AS x", read="databricks")
        deduplicate_output_aliases(tree)
        self.assertEqual(tree.sql("databricks"), "SELECT a AS x, b AS x")
        deduplicate_output_aliases(tree, copy=False)
        self.assertEqual(tree.sql("databricks"), "SELECT a AS x, b AS x_2")


def names(sql, dialect="bigquery"):
    return [table.sql(dialect) for table in unqualified_tables(parse_one(sql, read=dialect))]


class TestUnqualifiedTables(unittest.TestCase):
    def test_returns_nodes_of_the_expression(self):
        tree = parse_one("SELECT * FROM a JOIN ds.b ON TRUE JOIN p.ds.c ON TRUE", read="bigquery")
        (node,) = unqualified_tables(tree)
        self.assertIs(node, tree.find(exp.Table))
        node.set("db", exp.to_identifier("ds"))
        self.assertEqual(
            tree.sql("bigquery"), "SELECT * FROM ds.a JOIN ds.b ON TRUE JOIN p.ds.c ON TRUE"
        )

    def test_every_reference_is_returned_depth_first(self):
        self.assertEqual(
            names("SELECT * FROM a WHERE x IN (SELECT y FROM b) UNION ALL SELECT * FROM a"),
            ["a", "b", "a"],
        )

    def test_cte_references_are_skipped(self):
        self.assertEqual(
            names("WITH c AS (SELECT * FROM real_t) SELECT * FROM c JOIN other ON TRUE"),
            ["other", "real_t"],
        )

    def test_later_cte_does_not_shadow_an_earlier_body(self):
        self.assertEqual(
            names("WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS k) SELECT * FROM a, b"),
            ["b"],
        )

    def test_recursive_cte_may_reference_itself(self):
        self.assertEqual(
            names("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n FROM r) SELECT * FROM r"),
            [],
        )

    def test_nested_cte_scopes(self):
        self.assertEqual(
            names(
                "SELECT * FROM (WITH inner_c AS (SELECT 1 AS k) SELECT * FROM inner_c) AS s "
                "JOIN inner_c ON TRUE"
            ),
            ["inner_c"],
        )

    def test_dml_targets_and_table_functions(self):
        self.assertEqual(names("INSERT INTO target SELECT * FROM source"), ["target", "source"])
        self.assertEqual(names("SELECT * FROM UNNEST([1, 2]) AS u"), [])

    def test_catalog_only_and_schema_qualified_are_skipped(self):
        self.assertEqual(names("SELECT * FROM `p.d.t` JOIN d.u ON TRUE"), [])


class TestFlagComparisonsToBoolean(unittest.TestCase):
    COLUMNS = ("IsCurrent", "holidayflag")

    def rewrite(self, sql, **kwargs):
        return flag_comparisons_to_boolean(
            parse_one(sql, read="sqlite"), self.COLUMNS, **kwargs
        ).sql("sqlite")

    def test_equals_form(self):
        self.assertEqual(
            self.rewrite(
                "SELECT * FROM d WHERE IsCurrent = 1 AND d.HolidayFlag = 0 AND 1 = iscurrent"
            ),
            "SELECT * FROM d WHERE IsCurrent = TRUE AND d.HolidayFlag = FALSE AND iscurrent = TRUE",
        )

    def test_is_form(self):
        self.assertEqual(
            self.rewrite(
                "SELECT CASE WHEN IsCurrent = 1 THEN 1 END FROM d WHERE HolidayFlag = 0",
                use_is=True,
            ),
            "SELECT CASE WHEN IsCurrent IS TRUE THEN 1 END FROM d WHERE HolidayFlag IS FALSE",
        )

    def test_other_comparisons_are_unchanged(self):
        sql = (
            "SELECT * FROM d WHERE IsCurrent = 2 AND IsCurrent = 1.5 AND IsCurrent <> 1 "
            "AND other = 1 AND IsCurrent = flag2 AND IsCurrent IN (0, 1)"
        )
        self.assertEqual(self.rewrite(sql), parse_one(sql, read="sqlite").sql("sqlite"))

    def test_no_columns_is_a_no_op(self):
        tree = parse_one("SELECT * FROM d WHERE IsCurrent = 1")
        self.assertEqual(flag_comparisons_to_boolean(tree, []).sql(), tree.sql())

    def test_copy_flag(self):
        tree = parse_one("SELECT * FROM d WHERE IsCurrent = 1")
        flag_comparisons_to_boolean(tree, self.COLUMNS)
        self.assertEqual(tree.sql(), "SELECT * FROM d WHERE IsCurrent = 1")
        flag_comparisons_to_boolean(tree, self.COLUMNS, copy=False)
        self.assertEqual(tree.sql(), "SELECT * FROM d WHERE IsCurrent = TRUE")
