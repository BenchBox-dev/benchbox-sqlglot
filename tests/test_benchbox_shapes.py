"""Unit tests for the query-shape rewrites in ``sqlglot.benchbox.shapes``.

The output table holds what BenchBox's own copies of these rewrites produced for the same
input, so a move into the fork is proved byte for byte. The rest of the file ports BenchBox's
rewrite and translation tests: the rewrites BenchBox's Doris and SingleStore translation runs
(order and gates as BenchBox applies them), the TPC-Havoc variant chains, and the semantic
guarantees of each rewrite.
"""

import unittest

import sqlglot
from sqlglot import exp
from sqlglot.benchbox import shapes
from sqlglot.benchbox.dialect_rewrites import hoist_qualify_outer_clauses
from sqlglot.benchbox.shapes import (
    add_dual_source_to_filtered_selects,
    alias_order_by_projections,
    date_column_integer_arithmetic,
    drop_correlated_scalar_group_by,
    drop_empty_group_by,
    exists_to_derived_join,
    fold_scalar_having_into_case,
    hoist_aggregate_subqueries,
    inline_sourceless_exists,
    qualify_aggregated_columns,
    rollup_empty_input_row,
    unwrap_single_grouping_set,
    wrap_order_by_subquery_aliases,
)

OUTPUT_CASES = [
    (
        "drop_empty_group_by",
        "SELECT SUM(a) FROM t GROUP BY ()",
        "singlestore",
        {},
        "SELECT SUM(a) FROM t",
    ),
    (
        "drop_empty_group_by",
        "SELECT a, SUM(b) FROM t GROUP BY a, ()",
        "singlestore",
        {},
        "SELECT a, SUM(b) FROM t GROUP BY a",
    ),
    (
        "drop_empty_group_by",
        "SELECT a FROM t GROUP BY ROLLUP (a), ()",
        "singlestore",
        {},
        "SELECT a FROM t GROUP BY ROLLUP (a)",
    ),
    (
        "drop_empty_group_by",
        "SELECT SUM(a) FROM t GROUP BY () HAVING SUM(a) > 1",
        "singlestore",
        {},
        "SELECT SUM(a) FROM t HAVING SUM(a) > 1",
    ),
    (
        "drop_empty_group_by",
        "SELECT SUM(a) FROM t GROUP BY a",
        "singlestore",
        {},
        "SELECT SUM(a) FROM t GROUP BY a",
    ),
    (
        "unwrap_single_grouping_set",
        "SELECT a, b, SUM(c) FROM t GROUP BY GROUPING SETS ((a, b))",
        "singlestore",
        {},
        "SELECT a, b, SUM(c) FROM t GROUP BY a, b",
    ),
    (
        "unwrap_single_grouping_set",
        "SELECT a, SUM(c) FROM t GROUP BY GROUPING SETS (a)",
        "singlestore",
        {},
        "SELECT a, SUM(c) FROM t GROUP BY a",
    ),
    (
        "unwrap_single_grouping_set",
        "SELECT a, SUM(c) FROM t GROUP BY GROUPING SETS ((a), ())",
        "singlestore",
        {},
        "SELECT a, SUM(c) FROM t GROUP BY GROUPING SETS ((a), ())",
    ),
    (
        "add_dual_source_to_filtered_selects",
        "SELECT 1 WHERE 1 = 1",
        "singlestore",
        {},
        "SELECT 1 FROM DUAL WHERE 1 = 1",
    ),
    (
        "add_dual_source_to_filtered_selects",
        "SELECT (SELECT 1 WHERE x = 1) FROM t",
        "singlestore",
        {},
        "SELECT (SELECT 1 FROM DUAL WHERE x = 1) FROM t",
    ),
    ("add_dual_source_to_filtered_selects", "SELECT 1", "singlestore", {}, "SELECT 1"),
    (
        "inline_sourceless_exists",
        "SELECT 1 FROM a WHERE EXISTS(SELECT 1 WHERE a.x = 1 OR a.y = 2)",
        "starrocks",
        {},
        "SELECT 1 FROM a WHERE (a.x = 1 OR a.y = 2)",
    ),
    (
        "inline_sourceless_exists",
        "SELECT 1 FROM a WHERE EXISTS(SELECT 1 FROM b WHERE b.k = a.k)",
        "starrocks",
        {},
        "SELECT 1 FROM a WHERE EXISTS(SELECT 1 FROM b WHERE b.k = a.k)",
    ),
    (
        "inline_sourceless_exists",
        "SELECT 1 FROM a WHERE NOT EXISTS(SELECT 1 WHERE a.x = 1 LIMIT 1)",
        "starrocks",
        {},
        "SELECT 1 FROM a WHERE NOT EXISTS(SELECT 1 WHERE a.x = 1 LIMIT 1)",
    ),
    (
        "exists_to_derived_join",
        "SELECT t.k, t.v FROM t WHERE t.v > 0 AND EXISTS (SELECT 1 FROM t t2 WHERE t2.k = t.k AND t2.v = t.v AND t2.flag = 'R') ORDER BY t.k, t.v",
        "sqlite",
        {},
        "SELECT t.k, t.v FROM t, (SELECT DISTINCT t2.k AS t2_k, t2.v AS t2_v FROM t AS t2 WHERE t2.flag = 'R') AS t2 WHERE t.v > 0 AND t2.t2_k = t.k AND t2.t2_v = t.v ORDER BY t.k, t.v",
    ),
    (
        "exists_to_derived_join",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM t t2 WHERE t2.v < t.v)",
        "sqlite",
        {},
        "SELECT a FROM t WHERE EXISTS(SELECT 1 FROM t AS t2 WHERE t2.v < t.v)",
    ),
    (
        "exists_to_derived_join",
        "SELECT o.o_orderkey FROM orders o WHERE o.o_orderdate > 1 AND EXISTS (SELECT * FROM lineitem l2 WHERE l2.l_orderkey = o.o_orderkey AND l2.l_commitdate < l2.l_receiptdate) AND o.x = 3",
        "singlestore",
        {},
        "SELECT o.o_orderkey FROM orders AS o, (SELECT DISTINCT l2.l_orderkey AS l2_l_orderkey FROM lineitem AS l2 WHERE l2.l_commitdate < l2.l_receiptdate) AS l2 WHERE o.o_orderdate > 1 AND o.x = 3 AND l2.l2_l_orderkey = o.o_orderkey",
    ),
    (
        "exists_to_derived_join",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.k OR u.j = 1)",
        "singlestore",
        {},
        "SELECT a FROM t WHERE EXISTS(SELECT 1 FROM u WHERE u.k = t.k OR u.j = 1)",
    ),
    (
        "exists_to_derived_join",
        "SELECT a FROM t WHERE a = 1 OR EXISTS (SELECT 1 FROM u WHERE u.k = t.k)",
        "singlestore",
        {},
        "SELECT a FROM t WHERE a = 1 OR EXISTS(SELECT 1 FROM u WHERE u.k = t.k)",
    ),
    (
        "drop_correlated_scalar_group_by",
        "SELECT 1 FROM p WHERE p.c = (SELECT MIN(ps_supplycost) FROM ps WHERE ps_partkey = p_partkey GROUP BY ps_partkey)",
        "doris",
        {"columns": ["ps_partkey"]},
        "SELECT 1 FROM p WHERE p.c = (SELECT MIN(ps_supplycost) FROM ps WHERE ps_partkey = p_partkey GROUP BY ps_partkey)",
    ),
    (
        "drop_correlated_scalar_group_by",
        "SELECT 1 FROM p WHERE p.c = (SELECT MIN(x) FROM ps WHERE ps_partkey = 1 GROUP BY ps_partkey)",
        "doris",
        {"columns": ["PS_PARTKEY"]},
        "SELECT 1 FROM p WHERE p.c = (SELECT MIN(x) FROM ps WHERE ps_partkey = 1 GROUP BY ps_partkey)",
    ),
    (
        "drop_correlated_scalar_group_by",
        "SELECT 1 FROM p WHERE p.c = (SELECT MIN(x) FROM ps WHERE ps_partkey = p_partkey GROUP BY ps_partkey, ps_x)",
        "doris",
        {"columns": ["ps_partkey"]},
        "SELECT 1 FROM p WHERE p.c = (SELECT MIN(x) FROM ps WHERE ps_partkey = p_partkey GROUP BY ps_partkey, ps_x)",
    ),
    (
        "drop_correlated_scalar_group_by",
        "SELECT 1 FROM p WHERE EXISTS (SELECT MIN(x) FROM ps WHERE ps_partkey = p_partkey GROUP BY ps_partkey)",
        "doris",
        {"columns": ["ps_partkey"]},
        "SELECT 1 FROM p WHERE EXISTS(SELECT MIN(x) FROM ps WHERE ps_partkey = p_partkey GROUP BY ps_partkey)",
    ),
    (
        "fold_scalar_having_into_case",
        "SELECT 1 FROM p WHERE p.c = (SELECT SUM(x) AS s FROM ps WHERE ps_partkey = p_partkey HAVING SUM(x) > 1)",
        "doris",
        {},
        "SELECT 1 FROM p WHERE p.c = (SELECT CASE WHEN SUM(x) > 1 THEN SUM(x) END AS s FROM ps WHERE ps_partkey = p_partkey)",
    ),
    (
        "fold_scalar_having_into_case",
        "SELECT 1 FROM p WHERE p.c = (SELECT SUM(x) FROM ps GROUP BY k HAVING SUM(x) > 1)",
        "doris",
        {},
        "SELECT 1 FROM p WHERE p.c = (SELECT SUM(x) FROM ps GROUP BY k HAVING SUM(x) > 1)",
    ),
    (
        "qualify_aggregated_columns",
        "SELECT SUM(revenue) AS revenue FROM combined, t WHERE a = 1 GROUP BY k ORDER BY revenue",
        "singlestore",
        {"tables": {"revenue": "combined"}},
        "SELECT SUM(combined.revenue) AS revenue FROM combined, t WHERE a = 1 GROUP BY k ORDER BY revenue",
    ),
    (
        "qualify_aggregated_columns",
        "SELECT SUM(Revenue), MAX(x.revenue), revenue FROM c",
        "singlestore",
        {"tables": {"REVENUE": "c"}},
        "SELECT SUM(c.Revenue), MAX(x.revenue), revenue FROM c",
    ),
    (
        "wrap_order_by_subquery_aliases",
        "SELECT k, (SELECT SUM(v) FROM u WHERE u.k = t.k) AS total FROM t ORDER BY total DESC LIMIT 5",
        "singlestore",
        {},
        "SELECT * FROM (SELECT k, (SELECT SUM(v) FROM u WHERE u.k = t.k) AS total FROM t) AS ordered_rows ORDER BY total DESC LIMIT 5",
    ),
    (
        "wrap_order_by_subquery_aliases",
        "SELECT k, (SELECT SUM(v) FROM u WHERE u.k = t.k) AS total FROM t ORDER BY k LIMIT 5 OFFSET 2",
        "singlestore",
        {},
        "SELECT k, (SELECT SUM(v) FROM u WHERE u.k = t.k) AS total FROM t ORDER BY k LIMIT 5 OFFSET 2",
    ),
    (
        "wrap_order_by_subquery_aliases",
        "SELECT k, (SELECT SUM(v) FROM u) AS total FROM t ORDER BY total, k",
        "singlestore",
        {},
        "SELECT * FROM (SELECT k, (SELECT SUM(v) FROM u) AS total FROM t) AS ordered_rows ORDER BY total, k",
    ),
    (
        "alias_order_by_projections",
        "SELECT k, (SELECT SUM(v) FROM u WHERE u.k = t.k) AS total FROM t ORDER BY (SELECT SUM(v) FROM u WHERE u.k = t.k) DESC",
        "doris",
        {},
        "SELECT k, (SELECT SUM(v) FROM u WHERE u.k = t.k) AS total FROM t ORDER BY total DESC",
    ),
    (
        "alias_order_by_projections",
        "SELECT k, RANK() OVER (ORDER BY v) AS r FROM t ORDER BY RANK() OVER (ORDER BY v), k",
        "starrocks",
        {},
        "SELECT k, RANK() OVER (ORDER BY v) AS r FROM t ORDER BY r, k",
    ),
    (
        "alias_order_by_projections",
        "SELECT k, v + 1 AS w FROM t ORDER BY v + 1",
        "starrocks",
        {},
        "SELECT k, v + 1 AS w FROM t ORDER BY v + 1",
    ),
    (
        "date_column_integer_arithmetic",
        "SELECT 1 FROM t WHERE d1.d_date + 5 > d2.d_date - 3",
        "doris",
        {"suffix": "_date"},
        "SELECT 1 FROM t WHERE DATE_ADD(d1.d_date, INTERVAL 5 DAY) > DATE_SUB(d2.d_date, INTERVAL 3 DAY)",
    ),
    (
        "date_column_integer_arithmetic",
        "SELECT d_date + 5.5, D_DATE + '5', d_date + x, x + 5, o_x - 2, d_date - 3 FROM t",
        "doris",
        {"suffix": "_date"},
        "SELECT d_date + 5.5, D_DATE + '5', d_date + x, x + 5, o_x - 2, DATE_SUB(d_date, INTERVAL 3 DAY) FROM t",
    ),
    (
        "rollup_empty_input_row",
        "WITH c AS (SELECT 1 AS a, 2 AS x) SELECT a, AVG(x) AS m FROM c GROUP BY ROLLUP (a) ORDER BY a LIMIT 5",
        "doris",
        {},
        "WITH c AS (SELECT 1 AS a, 2 AS x), rollup_rows AS (SELECT a, AVG(x) AS m FROM c GROUP BY ROLLUP (a)) SELECT * FROM (SELECT * FROM rollup_rows UNION ALL SELECT NULL AS a, NULL AS m FROM (SELECT 1) AS rollup_dual WHERE NOT EXISTS(SELECT 1 FROM rollup_rows)) AS rollup_union ORDER BY a LIMIT 5",
    ),
    (
        "rollup_empty_input_row",
        "SELECT a, GROUPING(a) AS g, SUM(x) FROM t GROUP BY ROLLUP (a)",
        "doris",
        {},
        "SELECT a, GROUPING(a) AS g, SUM(x) FROM t GROUP BY ROLLUP (a)",
    ),
    (
        "rollup_empty_input_row",
        "SELECT a, b, COUNT(*) AS n, SUM(x) + 1 AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY a, b LIMIT 3 OFFSET 1",
        "singlestore",
        {},
        "WITH rollup_rows AS (SELECT a, b, COUNT(*) AS n, SUM(x) + 1 AS s FROM t GROUP BY ROLLUP (a, b)) SELECT * FROM (SELECT * FROM rollup_rows UNION ALL SELECT NULL AS a, NULL AS b, 0 :> BIGINT AS n, NULL + 1 AS s FROM (SELECT 1) AS rollup_dual WHERE NOT EXISTS(SELECT 1 FROM rollup_rows)) AS rollup_union ORDER BY a, b LIMIT 3 OFFSET 1",
    ),
    (
        "rollup_empty_input_row",
        "SELECT a, SUM(x) FROM t GROUP BY ROLLUP (a) HAVING SUM(x) > 1",
        "doris",
        {},
        "SELECT a, SUM(x) FROM t GROUP BY ROLLUP (a) HAVING SUM(x) > 1",
    ),
    (
        "rollup_empty_input_row",
        "SELECT t.a, SUM(x) FROM t GROUP BY ROLLUP (t.a) ORDER BY t.a",
        "doris",
        {},
        "SELECT t.a, SUM(x) FROM t GROUP BY ROLLUP (t.a) ORDER BY t.a",
    ),
    (
        "rollup_empty_input_row",
        "SELECT * FROM (SELECT a, SUM(x) AS s FROM t GROUP BY ROLLUP (a)) q JOIN (SELECT b, MIN(y) AS m FROM u GROUP BY ROLLUP (b)) r ON TRUE",
        "doris",
        {},
        "SELECT * FROM (WITH rollup_rows_ AS (SELECT a, SUM(x) AS s FROM t GROUP BY ROLLUP (a)) SELECT * FROM (SELECT * FROM rollup_rows_ UNION ALL SELECT NULL AS a, NULL AS s FROM (SELECT 1) AS rollup_dual WHERE NOT EXISTS(SELECT 1 FROM rollup_rows_)) AS rollup_union) AS q JOIN (WITH rollup_rows AS (SELECT b, MIN(y) AS m FROM u GROUP BY ROLLUP (b)) SELECT * FROM (SELECT * FROM rollup_rows UNION ALL SELECT NULL AS b, NULL AS m FROM (SELECT 1) AS rollup_dual WHERE NOT EXISTS(SELECT 1 FROM rollup_rows)) AS rollup_union) AS r ON TRUE",
    ),
    (
        "rollup_empty_input_row",
        "SELECT a AS k, MAX(x) FROM rollup_rows GROUP BY ROLLUP (a)",
        "doris",
        {},
        "WITH rollup_rows_ AS (SELECT a AS k, MAX(x) FROM rollup_rows GROUP BY ROLLUP (a)) SELECT * FROM (SELECT * FROM rollup_rows_ UNION ALL SELECT NULL AS k, NULL FROM (SELECT 1) AS rollup_dual WHERE NOT EXISTS(SELECT 1 FROM rollup_rows_)) AS rollup_union",
    ),
    (
        "hoist_aggregate_subqueries",
        "SELECT SUM(CASE WHEN EXISTS(SELECT 1 FROM u WHERE u.k = a.k) THEN a.v ELSE 0 END) FROM a",
        "singlestore",
        {},
        "SELECT SUM(CASE WHEN hoisted_subquery_1 THEN a.v ELSE 0 END) FROM (SELECT *, EXISTS(SELECT 1 FROM u WHERE u.k = a.k) AS hoisted_subquery_1 FROM a) AS a",
    ),
    (
        "hoist_aggregate_subqueries",
        "SELECT SUM(CASE WHEN EXISTS(SELECT 1 FROM u WHERE u.k = a) THEN v ELSE 0 END), AVG((SELECT MAX(z) FROM u)), SUM(CASE WHEN EXISTS(SELECT 1 FROM u WHERE u.k = a) THEN 1 END) FROM a JOIN b ON a.k = b.k WHERE a > 1",
        "singlestore",
        {},
        "SELECT SUM(CASE WHEN hoisted_subquery_1 THEN v ELSE 0 END), AVG(hoisted_subquery_2), SUM(CASE WHEN hoisted_subquery_1 THEN 1 END) FROM (SELECT *, EXISTS(SELECT 1 FROM u WHERE u.k = a) AS hoisted_subquery_1, (SELECT MAX(z) FROM u) AS hoisted_subquery_2 FROM a JOIN b ON a.k = b.k WHERE a > 1) AS hoisted_source",
    ),
    (
        "hoist_aggregate_subqueries",
        "SELECT SUM(x IN (SELECT y FROM u)) FROM a WHERE a.q = 1",
        "singlestore",
        {},
        "SELECT SUM(hoisted_subquery_1) FROM (SELECT *, x IN (SELECT y FROM u) AS hoisted_subquery_1 FROM a WHERE a.q = 1) AS a",
    ),
    (
        "hoist_aggregate_subqueries",
        "SELECT SUM(x) FROM a",
        "singlestore",
        {},
        "SELECT SUM(x) FROM a",
    ),
]


def render(function, sql, dialect, **kwargs):
    return ";\n".join(
        function(tree, **kwargs).sql(dialect, identify=False)
        for tree in sqlglot.parse(sql, read=dialect)
    )


def doris(sql):
    """The rewrites BenchBox runs on every Doris translation, in its order."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    tree = hoist_qualify_outer_clauses(tree)
    tree = shapes_alias_derived_tables(tree)
    tree = date_column_integer_arithmetic(tree, suffix="_date")
    tree = rollup_empty_input_row(tree)
    return tree.sql("doris", identify=True)


def singlestore(sql):
    tree = sqlglot.parse_one(sql, read="duckdb")
    tree = hoist_qualify_outer_clauses(tree)
    tree = rollup_empty_input_row(tree)
    return tree.sql("singlestore", identify=True)


def shapes_alias_derived_tables(tree):
    from sqlglot.benchbox.dialect_rewrites import alias_derived_tables

    return alias_derived_tables(tree)


class TestOutputsMatchBenchBox(unittest.TestCase):
    def test_each_rewrite_writes_the_text_benchbox_wrote(self):
        for name, sql, dialect, kwargs, expected in OUTPUT_CASES:
            with self.subTest(rewrite=name, sql=sql):
                self.assertEqual(render(getattr(shapes, name), sql, dialect, **kwargs), expected)

    def test_rewrites_copy_by_default_and_mutate_on_request(self):
        for name, sql, dialect, kwargs, _ in OUTPUT_CASES:
            with self.subTest(rewrite=name, sql=sql):
                tree = sqlglot.parse_one(sql, read=dialect)
                before = tree.sql(dialect)
                getattr(shapes, name)(tree, **kwargs)
                self.assertEqual(tree.sql(dialect), before)


class TestDorisTranslation(unittest.TestCase):
    def test_date_column_plus_integer_becomes_date_add(self):
        sql = doris("SELECT 1 FROM t WHERE d1.d_date + 5 > d2.d_date - 3")
        self.assertIn("DATE_ADD(`d1`.`d_date`, INTERVAL 5 DAY)", sql)
        self.assertIn("DATE_SUB(`d2`.`d_date`, INTERVAL 3 DAY)", sql)

    def test_the_date_column_gate_is_the_suffix_argument(self):
        tree = sqlglot.parse_one(
            "SELECT d_date + 1, ship_dt + 1, d_date_sk + 1 FROM t", read="doris"
        )
        self.assertEqual(
            date_column_integer_arithmetic(tree, suffix="_dt").sql("doris"),
            "SELECT d_date + 1, DATE_ADD(ship_dt, INTERVAL 1 DAY), d_date_sk + 1 FROM t",
        )
        self.assertEqual(
            date_column_integer_arithmetic(tree, suffix="_DATE").sql("doris"),
            "SELECT DATE_ADD(d_date, INTERVAL 1 DAY), ship_dt + 1, d_date_sk + 1 FROM t",
        )

    def test_rollup_over_empty_input_still_emits_the_grand_total_row(self):
        sql = doris(
            "WITH c AS (SELECT 1 AS a, 2 AS x) "
            "SELECT a, AVG(x) AS m FROM c GROUP BY ROLLUP (a) ORDER BY a LIMIT 5"
        )
        self.assertTrue(sql.startswith("WITH `c` AS"))
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("UNION ALL", sql)

    def test_rollup_with_grouping_function_is_left_alone(self):
        self.assertNotIn(
            "UNION", doris("SELECT a, GROUPING(a) AS g, SUM(x) FROM t GROUP BY ROLLUP (a)")
        )

    def test_derived_tables_are_named_in_the_translation(self):
        sql = doris("SELECT * FROM (SELECT 1 AS a) JOIN (SELECT 2 AS b) ON TRUE")
        self.assertEqual(sql.count("AS `_dt"), 2)

    def test_sourceless_exists_is_inlined_as_its_predicate(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM a WHERE EXISTS(SELECT 1 WHERE a.x = 1 OR a.y = 2)", read="starrocks"
        )
        rewritten = inline_sourceless_exists(tree).sql("starrocks")
        self.assertNotIn("EXISTS", rewritten)
        self.assertIn("(a.x = 1 OR a.y = 2)", rewritten)
        kept = sqlglot.parse_one(
            "SELECT 1 FROM a WHERE EXISTS(SELECT 1 FROM b WHERE b.k = a.k)", read="starrocks"
        )
        self.assertIn("EXISTS", inline_sourceless_exists(kept).sql("starrocks"))


class TestSingleStoreTranslation(unittest.TestCase):
    def test_rollup_over_empty_input_still_emits_the_grand_total_row(self):
        sql = singlestore(
            "WITH c AS (SELECT 1 AS a, 2 AS x) "
            "SELECT a, AVG(x) AS m FROM c GROUP BY ROLLUP (a) ORDER BY a LIMIT 5"
        )
        self.assertTrue(sql.startswith("WITH `c` AS"))
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("UNION ALL", sql)

    def test_rollup_with_grouping_function_is_left_alone(self):
        self.assertNotIn(
            "UNION", singlestore("SELECT a, GROUPING(a) AS g, SUM(x) FROM t GROUP BY ROLLUP (a)")
        )

    def test_qualify_is_eliminated(self):
        sql = singlestore(
            "SELECT x FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY k ORDER BY x) = 1"
        )
        self.assertNotIn("QUALIFY", sql.upper())
        self.assertIn("ROW_NUMBER", sql.upper())


class TestVariantChains(unittest.TestCase):
    """The rewrite lists BenchBox's TPC-Havoc variants select, in their order."""

    def test_correlated_scalar_group_by_is_a_single_group_guarded_by_case(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM part WHERE ps_supplycost = (SELECT MIN(ps_supplycost) FROM partsupp "
            "WHERE p_partkey = ps_partkey AND ps_supplycost > 0 GROUP BY ps_partkey "
            "HAVING MIN(ps_supplycost) > 1)",
            read="doris",
        )
        tree = drop_correlated_scalar_group_by(tree, columns=["ps_partkey"])
        tree = fold_scalar_having_into_case(tree)
        scalar = next(
            subquery.this
            for subquery in tree.find_all(exp.Subquery)
            if isinstance(subquery.parent, exp.Binary) and isinstance(subquery.this, exp.Select)
        )
        self.assertIsNone(scalar.args.get("group"))
        self.assertIsNone(scalar.args.get("having"))
        self.assertIsNotNone(scalar.find(exp.Case))

    def test_the_having_fold_needs_the_group_by_dropped_first(self):
        sql = (
            "SELECT 1 FROM part WHERE c = (SELECT MIN(x) FROM partsupp "
            "WHERE p_partkey = ps_partkey AND x > 0 GROUP BY ps_partkey HAVING MIN(x) > 1)"
        )
        folded_first = fold_scalar_having_into_case(sqlglot.parse_one(sql, read="doris"))
        self.assertIn("HAVING", folded_first.sql("doris"))
        right = fold_scalar_having_into_case(
            drop_correlated_scalar_group_by(
                sqlglot.parse_one(sql, read="doris"), columns=["ps_partkey"]
            )
        )
        self.assertNotIn("HAVING", right.sql("doris"))
        self.assertNotIn("GROUP BY", right.sql("doris"))

    def test_group_by_is_kept_when_a_key_is_not_pinned_to_another_column(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM p WHERE c = (SELECT MIN(x) FROM ps WHERE ps_partkey = 1 GROUP BY ps_partkey)",
            read="doris",
        )
        self.assertIn(
            "GROUP BY", drop_correlated_scalar_group_by(tree, columns=["ps_partkey"]).sql("doris")
        )

    def test_empty_group_by_is_dropped_and_having_kept(self):
        tree = sqlglot.parse_one(
            "SELECT SUM(a) AS s FROM t GROUP BY () HAVING SUM(a) > 1", read="singlestore"
        )
        self.assertEqual(
            drop_empty_group_by(tree).sql("singlestore"),
            "SELECT SUM(a) AS s FROM t HAVING SUM(a) > 1",
        )

    def test_a_group_by_with_other_keys_loses_only_the_empty_key(self):
        tree = sqlglot.parse_one("SELECT a, SUM(b) FROM t GROUP BY a, ()", read="singlestore")
        self.assertEqual(
            drop_empty_group_by(tree).sql("singlestore"), "SELECT a, SUM(b) FROM t GROUP BY a"
        )

    def test_single_grouping_set_becomes_plain_group_by(self):
        tree = sqlglot.parse_one(
            "SELECT a, b, SUM(c) FROM t GROUP BY GROUPING SETS ((a, b))", read="singlestore"
        )
        self.assertEqual(
            unwrap_single_grouping_set(tree).sql("singlestore"),
            "SELECT a, b, SUM(c) FROM t GROUP BY a, b",
        )

    def test_filtered_selects_without_source_read_from_dual_as_a_keyword(self):
        tree = sqlglot.parse_one("SELECT 1 WHERE 1 = 1", read="singlestore")
        self.assertEqual(
            add_dual_source_to_filtered_selects(tree).sql("singlestore"),
            "SELECT 1 FROM DUAL WHERE 1 = 1",
        )

    def test_aggregated_columns_are_qualified_but_order_by_is_not(self):
        tree = sqlglot.parse_one(
            "SELECT SUM(revenue) AS revenue FROM combined GROUP BY k ORDER BY revenue",
            read="singlestore",
        )
        self.assertEqual(
            qualify_aggregated_columns(tree, tables={"revenue": "combined"}).sql("singlestore"),
            "SELECT SUM(combined.revenue) AS revenue FROM combined GROUP BY k ORDER BY revenue",
        )

    def test_order_by_references_the_projected_alias(self):
        tree = sqlglot.parse_one(
            "SELECT k, RANK() OVER (ORDER BY v) AS r FROM t ORDER BY RANK() OVER (ORDER BY v), k",
            read="starrocks",
        )
        self.assertEqual(
            alias_order_by_projections(tree).sql("starrocks"),
            "SELECT k, RANK() OVER (ORDER BY v) AS r FROM t ORDER BY r, k",
        )

    def test_hoist_aggregate_subqueries_rejects_qualified_multi_source_references(self):
        tree = sqlglot.parse_one(
            "SELECT SUM(CASE WHEN EXISTS(SELECT 1 FROM u WHERE u.k = a.k) THEN a.v ELSE 0 END) FROM a, b",
            read="singlestore",
        )
        with self.assertRaisesRegex(ValueError, "qualified column references"):
            hoist_aggregate_subqueries(tree)

    def test_wrap_order_by_subquery_aliases_keeps_limit_outside(self):
        tree = sqlglot.parse_one(
            "SELECT k, (SELECT SUM(v) FROM u WHERE u.k = t.k) AS total FROM t ORDER BY total DESC LIMIT 5",
            read="singlestore",
        )
        self.assertTrue(
            wrap_order_by_subquery_aliases(tree)
            .sql("singlestore")
            .endswith("AS ordered_rows ORDER BY total DESC LIMIT 5")
        )

    def test_wrap_order_by_subquery_aliases_rejects_an_unprojected_sort_column(self):
        tree = sqlglot.parse_one(
            "SELECT (SELECT 1) AS total FROM t ORDER BY total, missing", read="singlestore"
        )
        with self.assertRaisesRegex(ValueError, "not projected"):
            wrap_order_by_subquery_aliases(tree)

    def test_exists_with_non_equality_correlation_is_untouched(self):
        sql = "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM t t2 WHERE t2.v < t.v)"
        tree = sqlglot.parse_one(sql, read="sqlite")
        self.assertEqual(
            exists_to_derived_join(tree).sql("sqlite"),
            "SELECT a FROM t WHERE EXISTS(SELECT 1 FROM t AS t2 WHERE t2.v < t.v)",
        )

    def test_rollup_rewrite_avoids_names_the_query_uses(self):
        tree = sqlglot.parse_one(
            "SELECT a, MAX(x) FROM rollup_rows GROUP BY ROLLUP (a)", read="doris"
        )
        sql = rollup_empty_input_row(tree).sql("doris")
        self.assertTrue(sql.startswith("WITH rollup_rows_ AS ("))


if __name__ == "__main__":
    unittest.main()
