import unittest

import sqlglot
from sqlglot import parse_one
from sqlglot.benchbox.engine_rewrites import (
    add_derived_table_aliases,
    add_query_settings,
    correlated_exists_to_in,
    decimal_zero_literals,
    lowercase_identifiers,
    qualify_order_by_columns,
)
from sqlglot.benchbox.rewrites import safe_division

UPPERCASE_NAMES = (r"[A-Z][A-Z_]+_SK", r"[A-Z][A-Z_]+_AMT", "D_YEAR", "D_MOY")


def clickhouse(sql, func, *args, **kwargs):
    return func(parse_one(sql, read="clickhouse"), *args, **kwargs).sql("clickhouse")


class TestLowercaseIdentifiers(unittest.TestCase):
    def test_matching_identifiers_are_lowercased(self):
        self.assertEqual(
            clickhouse(
                "SELECT SR_RETURN_AMT, t.D_YEAR AS D_MOY FROM t WHERE SS_ITEM_SK = 1",
                lowercase_identifiers,
                UPPERCASE_NAMES,
            ),
            "SELECT sr_return_amt, t.d_year AS d_moy FROM t WHERE ss_item_sk = 1",
        )

    def test_literals_functions_and_other_names_are_untouched(self):
        sql = "SELECT 'D_YEAR', COUNT(XY_SK), Mixed_Case_SK, OTHER_NAME FROM T"
        self.assertEqual(
            clickhouse(sql, lowercase_identifiers, UPPERCASE_NAMES),
            "SELECT 'D_YEAR', COUNT(xy_sk), Mixed_Case_SK, OTHER_NAME FROM T",
        )

    def test_quoted_identifier_is_lowercased_and_stays_quoted(self):
        self.assertEqual(
            clickhouse('SELECT "D_YEAR" FROM t', lowercase_identifiers, UPPERCASE_NAMES),
            'SELECT "d_year" FROM t',
        )

    def test_compiled_patterns_and_no_patterns(self):
        import re

        sql = "SELECT D_YEAR FROM t"
        self.assertEqual(
            clickhouse(sql, lowercase_identifiers, [re.compile("D_YEAR")]), "SELECT d_year FROM t"
        )
        self.assertEqual(clickhouse(sql, lowercase_identifiers, []), sql)

    def test_copy_flag(self):
        tree = parse_one("SELECT D_YEAR FROM t", read="clickhouse")
        lowercase_identifiers(tree, ["D_YEAR"])
        self.assertEqual(tree.sql("clickhouse"), "SELECT D_YEAR FROM t")
        lowercase_identifiers(tree, ["D_YEAR"], copy=False)
        self.assertEqual(tree.sql("clickhouse"), "SELECT d_year FROM t")


class TestDecimalZeroLiterals(unittest.TestCase):
    def test_coalesce_arithmetic_and_case_results(self):
        self.assertEqual(
            clickhouse(
                "SELECT COALESCE(x, 0.0), y - 0.00, 0.0 + z, CASE WHEN a > 1 THEN 0.0 ELSE 0.0 END, IF(a, 0.0, b) FROM t",
                decimal_zero_literals,
            ),
            "SELECT COALESCE(x, CAST(0 AS Decimal(15, 2))), y - CAST(0 AS Decimal(15, 2)), "
            "CAST(0 AS Decimal(15, 2)) + z, CASE WHEN a > 1 THEN CAST(0 AS Decimal(15, 2)) "
            "ELSE CAST(0 AS Decimal(15, 2)) END, CASE WHEN a THEN CAST(0 AS Decimal(15, 2)) ELSE b END FROM t",
        )

    def test_other_literals_are_untouched(self):
        sql = "SELECT COALESCE(x, 0), y - 0.05, z * 1.0, 0.0, f(0.0), (1 + 0.0) FROM t"
        self.assertEqual(clickhouse(sql, decimal_zero_literals), sql)

    def test_precision_and_scale(self):
        self.assertEqual(
            clickhouse("SELECT COALESCE(x, 0.0) FROM t", decimal_zero_literals, 7, 3),
            "SELECT COALESCE(x, CAST(0 AS Decimal(7, 3))) FROM t",
        )

    def test_other_dialects_write_their_own_decimal(self):
        tree = decimal_zero_literals(parse_one("SELECT COALESCE(x, 0.0) FROM t"))
        self.assertEqual(tree.sql("duckdb"), "SELECT COALESCE(x, CAST(0 AS DECIMAL(15, 2))) FROM t")


class TestClickHouseTransformComposition(unittest.TestCase):
    def test_division_by_a_decimal_cast_and_by_zero_literals(self):
        tree = parse_one(
            "SELECT a / b, a / 2, 1 / CAST(c AS Nullable(Decimal(7, 2))), a / NULLIF(d, 0), a / 0.0 FROM t",
            read="clickhouse",
        )
        tree = safe_division(decimal_zero_literals(tree), "clickhouse")
        self.assertEqual(
            tree.sql("clickhouse"),
            "SELECT a / nullIf(b, 0), a / 2, 1 / nullIf(CAST(c AS Nullable(Decimal(7, 2))), 0), "
            "a / nullIf(d, 0), a / nullIf(CAST(0 AS Decimal(15, 2)), 0) FROM t",
        )


class TestAddQuerySettings(unittest.TestCase):
    def test_select_and_with(self):
        for sql in ("SELECT 1", "WITH c AS (SELECT 1) SELECT * FROM c"):
            with self.subTest(sql=sql):
                self.assertEqual(
                    clickhouse(sql, add_query_settings, {"joined_subquery_requires_alias": 0}),
                    sql + " SETTINGS joined_subquery_requires_alias = 0"
                    if sql == "SELECT 1"
                    else "WITH c AS (SELECT 1) SELECT * FROM c SETTINGS joined_subquery_requires_alias = 0",
                )

    def test_values_are_literals(self):
        self.assertEqual(
            clickhouse(
                "SELECT 1",
                add_query_settings,
                {
                    "join_algorithm": "grace_hash",
                    "grace_hash_join_initial_buckets": 8,
                    "flag": True,
                    "ratio": 0.5,
                },
            ),
            "SELECT 1 SETTINGS join_algorithm = 'grace_hash', grace_hash_join_initial_buckets = 8, flag = 1, ratio = 0.5",
        )

    def test_string_value_escapes_quotes(self):
        self.assertEqual(
            clickhouse("SELECT 1", add_query_settings, {"name": "it's"}),
            "SELECT 1 SETTINGS name = 'it''s'",
        )

    def test_existing_setting_is_replaced_and_others_kept(self):
        self.assertEqual(
            clickhouse("SELECT 1 SETTINGS a = 1, b = 2", add_query_settings, {"A": 3}),
            "SELECT 1 SETTINGS b = 2, A = 3",
        )

    def test_set_operation_settings_go_on_the_last_select(self):
        self.assertEqual(
            clickhouse("SELECT 1 UNION ALL SELECT 2", add_query_settings, {"a": 1}),
            "SELECT 1 UNION ALL SELECT 2 SETTINGS a = 1",
        )

    def test_non_query_and_empty_settings_are_unchanged(self):
        self.assertEqual(
            clickhouse("DROP TABLE t", add_query_settings, {"a": 1}),
            "DROP TABLE t",
        )
        self.assertEqual(clickhouse("SELECT 1", add_query_settings, {}), "SELECT 1")


Q10_SHAPE = (
    "SELECT c.c_customer_sk FROM customer AS c, customer_address AS ca "
    "WHERE c.c_current_addr_sk = ca.ca_address_sk "
    "AND EXISTS(SELECT * FROM store_sales, date_dim WHERE c.c_customer_sk = ss_customer_sk "
    "AND ss_sold_date_sk = d_date_sk AND d_year = 2002 AND d_moy BETWEEN 1 AND 1 + 3) "
    "AND (EXISTS(SELECT * FROM web_sales, date_dim WHERE c.c_customer_sk = ws_bill_customer_sk "
    "AND ws_sold_date_sk = d_date_sk AND d_year = 2002 AND d_moy BETWEEN 1 AND 1 + 3) "
    "OR EXISTS(SELECT * FROM catalog_sales, date_dim WHERE c.c_customer_sk = cs_ship_customer_sk "
    "AND cs_sold_date_sk = d_date_sk AND d_year = 2002 AND d_moy BETWEEN 1 AND 1 + 3))"
)


class TestCorrelatedExistsToIn(unittest.TestCase):
    def rewrite(self, sql, **kwargs):
        return correlated_exists_to_in(parse_one(sql, read="clickhouse"), **kwargs).sql(
            "clickhouse"
        )

    def test_three_semi_joins_under_and_or(self):
        self.assertEqual(
            self.rewrite(Q10_SHAPE, expected=3),
            "SELECT c.c_customer_sk FROM customer AS c CROSS JOIN customer_address AS ca "
            "WHERE c.c_current_addr_sk = ca.ca_address_sk "
            "AND c.c_customer_sk IN (SELECT ss_customer_sk FROM store_sales CROSS JOIN date_dim "
            "WHERE ss_sold_date_sk = d_date_sk AND d_year = 2002 AND d_moy BETWEEN 1 AND 1 + 3) "
            "AND (c.c_customer_sk IN (SELECT ws_bill_customer_sk FROM web_sales CROSS JOIN date_dim "
            "WHERE ws_sold_date_sk = d_date_sk AND d_year = 2002 AND d_moy BETWEEN 1 AND 1 + 3) "
            "OR c.c_customer_sk IN (SELECT cs_ship_customer_sk FROM catalog_sales CROSS JOIN date_dim "
            "WHERE cs_sold_date_sk = d_date_sk AND d_year = 2002 AND d_moy BETWEEN 1 AND 1 + 3))",
        )

    def test_unexpected_count_fails_closed(self):
        for expected in (0, 2, 4):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                self.rewrite(Q10_SHAPE, expected=expected)
        with self.assertRaises(ValueError):
            self.rewrite("SELECT 1 FROM c WHERE c.a = 1", expected=1)

    def test_correlated_equality_on_either_side_and_sole_predicate(self):
        self.assertEqual(
            self.rewrite("SELECT 1 FROM c WHERE EXISTS(SELECT 1 FROM s WHERE s.k = c.k)"),
            "SELECT 1 FROM c WHERE c.k IN (SELECT s.k FROM s)",
        )

    def test_not_exists_is_left_alone(self):
        sql = "SELECT 1 FROM c WHERE NOT EXISTS(SELECT 1 FROM s WHERE s.k = c.k)"
        self.assertEqual(self.rewrite(sql), sql)
        sql = "SELECT 1 FROM c WHERE NOT (EXISTS(SELECT 1 FROM s WHERE s.k = c.k) OR c.a = 1)"
        self.assertEqual(self.rewrite(sql), sql)

    def test_exists_outside_where_is_left_alone(self):
        sql = "SELECT EXISTS(SELECT 1 FROM s WHERE s.k = c.k) FROM c"
        self.assertEqual(self.rewrite(sql), sql)

    def test_unsupported_inner_shapes_are_left_alone(self):
        for inner in (
            "SELECT 1 FROM s WHERE s.k = c.k AND s.v > c.v",
            "SELECT 1 FROM s WHERE s.k > c.k",
            "SELECT 1 FROM s WHERE s.k = c.k GROUP BY s.k",
            "SELECT 1 FROM s WHERE s.k = c.k LIMIT 1",
            "SELECT DISTINCT 1 FROM s WHERE s.k = c.k",
            "SELECT 1 FROM s WHERE s.k = c.k AND s.v > (SELECT AVG(v) FROM s)",
            "SELECT 1 FROM s WHERE c.k = c.j",
            "SELECT 1 FROM s",
        ):
            with self.subTest(inner=inner):
                sql = f"SELECT 1 FROM c WHERE EXISTS({inner})"
                self.assertEqual(self.rewrite(sql), sql)

    def test_uncorrelated_exists_is_left_alone(self):
        sql = "SELECT 1 FROM c WHERE EXISTS(SELECT 1 FROM s WHERE s.v = 1)"
        self.assertEqual(self.rewrite(sql), sql)

    def test_copy_flag(self):
        tree = parse_one("SELECT 1 FROM c WHERE EXISTS(SELECT 1 FROM s WHERE s.k = c.k)")
        correlated_exists_to_in(tree)
        self.assertIn("EXISTS", tree.sql())
        correlated_exists_to_in(tree, copy=False)
        self.assertNotIn("EXISTS", tree.sql())


class TestAddDerivedTableAliases(unittest.TestCase):
    def alias(self, sql, **kwargs):
        return add_derived_table_aliases(parse_one(sql, read="starrocks"), **kwargs).sql(
            "starrocks"
        )

    def test_from_subquery_gets_alias(self):
        self.assertEqual(
            self.alias("SELECT * FROM (SELECT 1 AS x)"), "SELECT * FROM (SELECT 1 AS x) AS `_sq0`"
        )

    def test_existing_aliases_are_respected(self):
        for sql in (
            "SELECT * FROM (SELECT 1 AS x) AS sq",
            "SELECT * FROM (SELECT 1 AS x) sq",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.alias(sql), "SELECT * FROM (SELECT 1 AS x) AS sq")

    def test_in_and_scalar_subqueries_are_not_aliased(self):
        sql = "SELECT (SELECT 1) FROM t WHERE id IN (SELECT id FROM s) AND EXISTS(SELECT 1)"
        self.assertEqual(self.alias(sql), sql)

    def test_comma_and_explicit_joins_with_distinct_aliases(self):
        self.assertEqual(
            self.alias(
                "SELECT * FROM t1, (SELECT id FROM t2), (SELECT 2) JOIN (SELECT 3) ON 1 = 1"
            ),
            "SELECT * FROM t1, (SELECT id FROM t2) AS `_sq0`, (SELECT 2) AS `_sq1` "
            "JOIN (SELECT 3) AS `_sq2` ON 1 = 1",
        )

    def test_cte_body_and_with_inside_subquery(self):
        self.assertEqual(
            self.alias("SELECT * FROM (WITH cte AS (SELECT 1) SELECT * FROM cte)"),
            "SELECT * FROM (WITH cte AS (SELECT 1) SELECT * FROM cte) AS `_sq0`",
        )
        self.assertEqual(
            self.alias("WITH c AS (SELECT * FROM (SELECT 1)) SELECT * FROM c"),
            "WITH c AS (SELECT * FROM (SELECT 1) AS `_sq0`) SELECT * FROM c",
        )

    def test_union_after_subquery_and_nested_subqueries(self):
        self.assertEqual(
            self.alias("SELECT * FROM (SELECT 1) UNION SELECT 2"),
            "SELECT * FROM (SELECT 1) AS `_sq0` UNION SELECT 2",
        )
        self.assertEqual(
            self.alias("SELECT * FROM (SELECT * FROM (SELECT 2))"),
            "SELECT * FROM (SELECT * FROM (SELECT 2) AS `_sq1`) AS `_sq0`",
        )

    def test_prefix_and_quoting(self):
        self.assertEqual(
            self.alias("SELECT * FROM (SELECT 1)", prefix="dt", quoted=False),
            "SELECT * FROM (SELECT 1) AS dt0",
        )

    def test_copy_flag(self):
        tree = parse_one("SELECT * FROM (SELECT 1)")
        add_derived_table_aliases(tree)
        self.assertEqual(tree.sql(), "SELECT * FROM (SELECT 1)")
        add_derived_table_aliases(tree, copy=False)
        self.assertEqual(tree.sql(), 'SELECT * FROM (SELECT 1) AS "_sq0"')


class TestQualifyOrderByColumns(unittest.TestCase):
    CTES = (
        "WITH ss_items AS (SELECT 1 AS item_id), cs_items AS (SELECT 1 AS item_id), "
        "ws_items AS (SELECT 1 AS item_id) "
    )

    def qualify(self, sql, qualifiers=None):
        if qualifiers is None:
            qualifiers = {"item_id": "ss_items"}
        return qualify_order_by_columns(parse_one(sql), qualifiers).sql()

    def test_unqualified_and_quoted_order_by_columns(self):
        body = "SELECT ss_items.item_id FROM ss_items, cs_items, ws_items ORDER BY {}, item_rev"
        self.assertEqual(
            self.qualify(self.CTES + body.format("item_id")),
            self.CTES + body.format("ss_items.item_id"),
        )
        self.assertEqual(
            self.qualify(self.CTES + body.format('"item_id"')),
            self.CTES + body.format('"ss_items"."item_id"'),
        )

    def test_qualified_reference_and_other_columns_are_untouched(self):
        sql = self.CTES + "SELECT 1 FROM ss_items, cs_items ORDER BY cs_items.item_id, other"
        self.assertEqual(self.qualify(sql), sql)

    def test_select_without_the_source_is_untouched(self):
        sql = "SELECT item_id FROM cs_items ORDER BY item_id"
        self.assertEqual(self.qualify(sql), sql)

    def test_alias_that_redefines_the_name_is_untouched(self):
        sql = "SELECT a + b AS item_id FROM ss_items ORDER BY item_id"
        self.assertEqual(self.qualify(sql), sql)
        self.assertEqual(
            self.qualify("SELECT ss_items.item_id AS item_id FROM ss_items ORDER BY item_id"),
            "SELECT ss_items.item_id AS item_id FROM ss_items ORDER BY ss_items.item_id",
        )

    def test_source_alias_and_case_insensitivity(self):
        self.assertEqual(
            self.qualify("SELECT 1 FROM t AS Ss_Items ORDER BY ITEM_ID", {"item_id": "ss_items"}),
            "SELECT 1 FROM t AS Ss_Items ORDER BY ss_items.ITEM_ID",
        )

    def test_no_order_by_or_qualifiers(self):
        sql = "SELECT 1 FROM ss_items"
        self.assertEqual(self.qualify(sql), sql)
        sql = "SELECT 1 FROM ss_items ORDER BY item_id"
        self.assertEqual(self.qualify(sql, {}), sql)


class TestNetezzaDayDurations(unittest.TestCase):
    def test_plus_and_minus_n_days_are_day_intervals(self):
        tree = parse_one(
            "SELECT CAST('2000-02-01' AS DATE) + 30 days, d - 14 DAY, (d + 5 days) AS x FROM t "
            "WHERE d BETWEEN CAST('2000-02-01' AS DATE) AND (CAST('2000-02-01' AS DATE) + 60 days)",
            read="netezza",
        )
        self.assertEqual(
            tree.sql("duckdb"),
            "SELECT CAST('2000-02-01' AS DATE) + INTERVAL '30' DAY, d - INTERVAL '14' DAY, "
            "(d + INTERVAL '5' DAY) AS x FROM t WHERE d BETWEEN CAST('2000-02-01' AS DATE) "
            "AND (CAST('2000-02-01' AS DATE) + INTERVAL '60' DAY)",
        )
        self.assertEqual(
            parse_one("SELECT d + 60 days", read="netezza"),
            parse_one("SELECT d + INTERVAL 60 DAY", read="netezza"),
        )

    def test_other_shapes_are_unchanged(self):
        for sql in (
            "SELECT a + 3 * 2 FROM t",
            "SELECT a + 3 AS days FROM t",
            "SELECT a + 1.5 days FROM t",
            "SELECT days FROM t",
        ):
            with self.subTest(sql=sql):
                try:
                    expected = parse_one(sql, read="postgres")
                except sqlglot.errors.ParseError:
                    continue
                self.assertEqual(parse_one(sql, read="netezza"), expected)


if __name__ == "__main__":
    unittest.main()
