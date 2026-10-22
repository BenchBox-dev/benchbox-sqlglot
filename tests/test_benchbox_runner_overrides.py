"""Text checks for the rewrites that replace the TPC-DS runner overrides in BenchBox.

``tests/test_benchbox_runner_overrides_engines.py`` executes the same queries on real engines; these
tests pin the written SQL (``downstream/ENGINE-GAPS.md``).
"""

import unittest

import sqlglot
from sqlglot import exp
from sqlglot.benchbox.compat import inline_order_by_aliases
from sqlglot.benchbox.engine_rewrites import (
    alias_qualified_projections,
    decimal_division_to_double,
    qualify_alias_shadowed_columns,
    use_nulls_for_rollups,
)
from sqlglot.benchbox.rewrites import nullif_zero_divisors, safe_division
from tests.tpcds_queries import Q36, Q47, Q57, Q66, Q70, Q86, Q90


def parse(sql, dialect="netezza"):
    return sqlglot.parse_one(sql, read=dialect)


class TestNullifZeroDivisors(unittest.TestCase):
    def test_q90_divisor_is_guarded_for_every_dialect(self):
        for dialect in (
            "postgres",
            "duckdb",
            "spark",
            "clickhouse",
            "tsql",
            "bigquery",
            "snowflake",
            "sqlite",
        ):
            with self.subTest(dialect=dialect):
                sql = nullif_zero_divisors(parse(Q90), ["pmc"]).sql(dialect)
                self.assertRegex(sql, r"(?i)nullif\(CAST\(\W?pmc\W? AS [\w(), ]+\), 0\)")
                self.assertNotRegex(sql, r"(?i)nullif\((CAST\()?\W?amc")

    def test_postgres_text(self):
        sql = nullif_zero_divisors(parse(Q90), ["pmc"]).sql("postgres")
        self.assertTrue(
            sql.startswith(
                "SELECT CAST(amc AS DECIMAL(15, 4)) / NULLIF(CAST(pmc AS DECIMAL(15, 4)), 0) AS am_pm_ratio FROM"
            ),
            sql,
        )

    def test_columns_are_case_insensitive_and_may_be_qualified(self):
        tree = parse("SELECT a / (CAST(t.PMC AS DECIMAL(15, 4))), b / c FROM t", "postgres")
        self.assertEqual(
            nullif_zero_divisors(tree, ["pmc"]).sql("postgres"),
            "SELECT a / NULLIF((CAST(t.PMC AS DECIMAL(15, 4))), 0), b / c FROM t",
        )

    def test_guarded_and_unnamed_divisors_are_left_alone(self):
        for sql in (
            "SELECT a / NULLIF(pmc, 0) FROM t",
            "SELECT a / CAST(NULLIF(pmc, 0) AS DECIMAL(15, 4)) FROM t",
            "SELECT a / (b + pmc) FROM t",
            "SELECT a / 2 FROM t",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(
                    nullif_zero_divisors(parse(sql, "postgres"), ["pmc"]).sql("postgres"),
                    parse(sql, "postgres").sql("postgres"),
                )

    def test_input_is_not_mutated_by_default(self):
        tree = parse(Q90)
        before = tree.sql("postgres")
        nullif_zero_divisors(tree, ["pmc"])
        self.assertEqual(tree.sql("postgres"), before)
        nullif_zero_divisors(tree, ["pmc"], copy=False)
        self.assertNotEqual(tree.sql("postgres"), before)

    def test_safe_division_still_skips_the_guarded_divisor_on_clickhouse(self):
        tree = safe_division(nullif_zero_divisors(parse(Q90), ["pmc"]), "clickhouse")
        self.assertEqual(tree.sql("clickhouse").upper().count("NULLIF("), 1)


class TestInlineOrderByAliases(unittest.TestCase):
    def order_by(self, sql):
        return inline_order_by_aliases(parse(sql)).sql("postgres").split(" ORDER BY ")[-1]

    def test_q36(self):
        self.assertEqual(
            self.order_by(Q36),
            "lochierarchy DESC, CASE WHEN (GROUPING(i_category) + GROUPING(i_class)) = 0 THEN i_category END, "
            "rank_within_parent LIMIT 100",
        )

    def test_q70(self):
        self.assertEqual(
            self.order_by(Q70),
            "lochierarchy DESC, CASE WHEN (GROUPING(s_state) + GROUPING(s_county)) = 0 THEN s_state END, "
            "rank_within_parent LIMIT 100",
        )

    def test_q86(self):
        self.assertEqual(
            self.order_by(Q86),
            "lochierarchy DESC, CASE WHEN (GROUPING(i_category) + GROUPING(i_class)) = 0 THEN i_category END, "
            "rank_within_parent LIMIT 100",
        )

    def test_only_the_selected_names_are_inlined(self):
        sql = "SELECT a + 1 AS x, b AS y FROM t ORDER BY x * 2, y + 1"
        self.assertEqual(
            inline_order_by_aliases(parse(sql), names=["Y"]).sql("postgres"),
            "SELECT a + 1 AS x, b AS y FROM t ORDER BY x * 2, b + 1",
        )
        self.assertEqual(
            inline_order_by_aliases(parse(sql)).sql("postgres"),
            "SELECT a + 1 AS x, b AS y FROM t ORDER BY (a + 1) * 2, b + 1",
        )

    def test_whole_key_aliases_qualified_columns_and_other_scopes_are_kept(self):
        for sql in (
            "SELECT a AS x FROM t ORDER BY x DESC",
            "SELECT a AS x FROM t ORDER BY t.x + 1",
            "SELECT a AS x FROM t ORDER BY (SELECT MAX(x) FROM u)",
            "SELECT a AS x FROM t GROUP BY x HAVING x > 1",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(
                    inline_order_by_aliases(parse(sql)).sql("postgres"), parse(sql).sql("postgres")
                )

    def test_nested_selects_are_rewritten_in_their_own_scope(self):
        sql = "SELECT * FROM (SELECT a + 1 AS x FROM t ORDER BY x * 2 LIMIT 3) AS s"
        self.assertEqual(
            inline_order_by_aliases(parse(sql)).sql("postgres"),
            "SELECT * FROM (SELECT a + 1 AS x FROM t ORDER BY (a + 1) * 2 LIMIT 3) AS s",
        )


class TestAliasQualifiedProjections(unittest.TestCase):
    def test_q47_neighbour_projection_gets_its_own_names(self):
        sql = alias_qualified_projections(parse(Q47)).sql("clickhouse")
        self.assertIn(
            "v2 AS (SELECT v1.i_category AS i_category, v1.i_brand AS i_brand, "
            "v1.d_year AS d_year, v1.d_moy AS d_moy, "
            "v1.avg_monthly_sales AS avg_monthly_sales, v1.sum_sales AS sum_sales, v1_lag.sum_sales AS psum, "
            "v1_lead.sum_sales AS nsum FROM v1, v1 AS v1_lag, v1 AS v1_lead",
            sql,
        )

    def test_q57_neighbour_projection_gets_its_own_names(self):
        sql = alias_qualified_projections(parse(Q57)).sql("clickhouse")
        self.assertIn(
            "v2 AS (SELECT v1.cc_name AS cc_name, v1.d_year AS d_year, v1.d_moy AS d_moy, "
            "v1.avg_monthly_sales AS avg_monthly_sales, v1.sum_sales AS sum_sales, v1_lag.sum_sales AS psum, "
            "v1_lead.sum_sales AS nsum FROM v1, v1 AS v1_lag, v1 AS v1_lead",
            sql,
        )

    def test_top_level_is_opt_in(self):
        sql = "SELECT a.x, a.y AS z FROM a"
        self.assertEqual(
            alias_qualified_projections(parse(sql)).sql("clickhouse"), "SELECT a.x, a.y AS z FROM a"
        )
        self.assertEqual(
            alias_qualified_projections(parse(sql), top_level=True).sql("clickhouse"),
            "SELECT a.x AS x, a.y AS z FROM a",
        )

    def test_repeated_names_and_stars_are_kept(self):
        sql = "SELECT * FROM (SELECT a.x, b.x, c.*, d.y, 1 AS y FROM a, b, c, d) AS s"
        self.assertEqual(
            alias_qualified_projections(parse(sql)).sql("clickhouse"),
            "SELECT * FROM (SELECT a.x, b.x, c.*, d.y, 1 AS y FROM a, b, c, d) AS s",
        )

    def test_quoting_of_the_column_is_kept(self):
        tree = alias_qualified_projections(
            parse('WITH v AS (SELECT t."D_Year" FROM t) SELECT * FROM v')
        )
        self.assertEqual(
            tree.sql("clickhouse"),
            'WITH v AS (SELECT t."D_Year" AS "D_Year" FROM t) SELECT * FROM v',
        )


class TestQualifyAliasShadowedColumns(unittest.TestCase):
    def test_q66_outer_aggregates_read_the_derived_table(self):
        sql = qualify_alias_shadowed_columns(parse(Q66)).sql("clickhouse")
        self.assertNotRegex(sql, r"SUM\((jan|dec)_sales")
        for month in ("jan", "jun", "dec"):
            self.assertIn(f"SUM(x.{month}_sales) AS {month}_sales", sql)
            self.assertIn(
                f"SUM(x.{month}_sales / w_warehouse_sq_ft) AS {month}_sales_per_sq_foot", sql
            )
            self.assertIn(f"SUM(x.{month}_net) AS {month}_net", sql)
        # The inner aggregates and the dimension columns, which no alias hides, are untouched.
        self.assertIn(
            "SUM(CASE WHEN d_moy = 1 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS jan_sales",
            sql,
        )
        self.assertIn("SELECT w_warehouse_name, w_warehouse_sq_ft, w_city", sql)
        self.assertTrue(sql.endswith("ORDER BY w_warehouse_name LIMIT 100"), sql[-80:])

    def test_order_by_keeps_the_alias(self):
        sql = "SELECT SUM(total) AS total FROM (SELECT 1 AS total) AS s ORDER BY total"
        self.assertEqual(
            qualify_alias_shadowed_columns(parse(sql)).sql("clickhouse"),
            "SELECT SUM(s.total) AS total FROM (SELECT 1 AS total) AS s ORDER BY total",
        )

    def test_where_and_group_by_read_the_source(self):
        sql = "SELECT k + 1 AS k, SUM(v) AS v FROM (SELECT 1 AS k, 2 AS v) AS s WHERE k > 0 GROUP BY k"
        self.assertEqual(
            qualify_alias_shadowed_columns(parse(sql)).sql("clickhouse"),
            "SELECT s.k + 1 AS k, SUM(s.v) AS v FROM (SELECT 1 AS k, 2 AS v) AS s WHERE s.k > 0 GROUP BY s.k",
        )

    def test_with_entries_are_sources_too(self):
        sql = "WITH c AS (SELECT 1 AS n) SELECT SUM(n) AS n, SUM(n * 2) AS twice FROM c"
        self.assertEqual(
            qualify_alias_shadowed_columns(parse(sql)).sql("clickhouse"),
            "WITH c AS (SELECT 1 AS n) SELECT SUM(c.n) AS n, SUM(c.n * 2) AS twice FROM c",
        )

    def test_unknown_sources_and_ambiguous_names_are_left_alone(self):
        for sql in (
            "SELECT SUM(n) AS n FROM t",
            "SELECT SUM(n) AS n FROM (SELECT 1 AS n) AS a, t",
            "SELECT SUM(n) AS n FROM (SELECT 1 AS n) AS a, (SELECT 2 AS n) AS b",
            "SELECT SUM(n) AS n FROM (SELECT * FROM t) AS a",
            "SELECT n AS n FROM (SELECT 1 AS n) AS a",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(
                    qualify_alias_shadowed_columns(parse(sql)).sql("clickhouse"),
                    parse(sql).sql("clickhouse"),
                )

    def test_union_derived_table_reads_the_first_branch_names(self):
        sql = "SELECT SUM(a) AS a FROM (SELECT 1 AS a UNION ALL SELECT 2) AS u"
        self.assertEqual(
            qualify_alias_shadowed_columns(parse(sql)).sql("clickhouse"),
            "SELECT SUM(u.a) AS a FROM (SELECT 1 AS a UNION ALL SELECT 2) AS u",
        )

    def test_inner_scope_columns_are_not_confused_with_the_outer_alias(self):
        sql = "SELECT SUM(n) AS n, (SELECT MAX(n) FROM t) AS m FROM (SELECT 1 AS n) AS s"
        out = qualify_alias_shadowed_columns(parse(sql)).find(exp.Subquery).sql("clickhouse")
        self.assertEqual(out, "(SELECT MAX(n) FROM t)")


class TestDecimalDivisionToDouble(unittest.TestCase):
    def rewrite(self, sql):
        return decimal_division_to_double(parse(sql, "clickhouse")).sql("clickhouse")

    def test_dividend_is_cast_and_keeps_null(self):
        self.assertEqual(
            self.rewrite("SELECT SUM(a) / SUM(b), (a + 1) / 2 FROM t"),
            "SELECT CAST(SUM(a) AS Nullable(Float64)) / SUM(b), CAST((a + 1) AS Nullable(Float64)) / 2 FROM t",
        )

    def test_real_dividends_and_chained_divisions_are_left_alone(self):
        for sql in (
            "SELECT 5.0 / 3 FROM t",
            "SELECT 1e3 / 3 FROM t",
            "SELECT CAST(a AS Float64) / b FROM t",
            "SELECT CAST(a AS Nullable(Float64)) / b FROM t",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.rewrite(sql), parse(sql, "clickhouse").sql("clickhouse"))
        self.assertEqual(
            self.rewrite("SELECT a / b / c FROM t"),
            "SELECT CAST(a AS Nullable(Float64)) / b / c FROM t",
        )

    def test_applying_twice_changes_nothing(self):
        once = decimal_division_to_double(parse(Q66, "clickhouse"))
        self.assertEqual(decimal_division_to_double(once).sql("clickhouse"), once.sql("clickhouse"))

    def test_q66_per_square_foot_columns_are_cast(self):
        sql = self.rewrite(Q66)
        self.assertIn("sum(CAST(jan_sales AS Nullable(Float64)) / w_warehouse_sq_ft)", sql)
        self.assertNotIn("sum(jan_sales / w_warehouse_sq_ft)", sql)

    def test_input_is_not_mutated_by_default(self):
        tree = parse("SELECT a / b FROM t", "clickhouse")
        decimal_division_to_double(tree)
        self.assertEqual(tree.sql("clickhouse"), "SELECT a / b FROM t")


class TestUseNullsForRollups(unittest.TestCase):
    def rewrite(self, sql):
        return use_nulls_for_rollups(parse(sql, "clickhouse")).sql("clickhouse")

    def test_rollup_cube_and_grouping_sets_get_the_setting(self):
        for grouping in ("ROLLUP (a, b)", "CUBE (a, b)", "GROUPING SETS ((a), ())"):
            with self.subTest(grouping=grouping):
                self.assertEqual(
                    self.rewrite(f"SELECT a, b, SUM(c) FROM t GROUP BY {grouping}"),
                    f"SELECT a, b, SUM(c) FROM t GROUP BY {grouping} SETTINGS group_by_use_nulls = 1",
                )

    def test_plain_grouping_is_unchanged(self):
        self.assertEqual(
            self.rewrite("SELECT a, SUM(c) FROM t GROUP BY a"), "SELECT a, SUM(c) FROM t GROUP BY a"
        )

    def test_existing_setting_is_replaced_not_repeated(self):
        self.assertEqual(
            self.rewrite("SELECT a FROM t GROUP BY ROLLUP (a) SETTINGS group_by_use_nulls = 0"),
            "SELECT a FROM t GROUP BY ROLLUP (a) SETTINGS group_by_use_nulls = 1",
        )


if __name__ == "__main__":
    unittest.main()
