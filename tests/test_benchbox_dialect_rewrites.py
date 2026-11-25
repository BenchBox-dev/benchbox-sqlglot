"""Unit tests for the rewrites in ``sqlglot.benchbox.dialect_rewrites``.

The output table holds what BenchBox's own copies of these rewrites produced for the same
input. The rest ports BenchBox's ``test_qualify_order_by_rewrite.py`` and the ``apply_rewrite``,
``dialect_compat`` and TPC-Havoc cases for these rewrites.
"""

import logging
import unittest
from unittest import mock

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.benchbox import dialect_rewrites
from sqlglot.benchbox.dialect_rewrites import (
    alias_derived_tables,
    alias_outer_table_references,
    coalesce_count_subqueries,
    factor_common_conjuncts,
    hoist_qualify_outer_clauses,
    in_subquery_or_to_left_join,
    positive_count_subquery_to_exists,
    trim_char_case_comparisons,
)
from sqlglot.benchbox.engine_rewrites import correlated_exists_to_in

# Dialects whose generator has no QUALIFY; BenchBox hoists the outer clauses for these.
ELIMINATING = [
    "athena",
    "datafusion",
    "doris",
    "dune",
    "fabric",
    "greenplum",
    "hive",
    "materialize",
    "mysql",
    "netezza",
    "oracle",
    "postgres",
    "presto",
    "questdb",
    "risingwave",
    "singlestore",
    "spark",
    "spark2",
    "sqlite",
    "starrocks",
    "trino",
    "tsql",
    "vertica",
]

OUTPUT_CASES = [
    (
        "alias_outer_table_references",
        "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM t t2 WHERE t2.k = t.k AND t2.v < t.v)",
        "clickhouse",
        {"table": "t", "alias": "t_outer"},
        "SELECT a FROM t AS t_outer WHERE NOT EXISTS(SELECT 1 FROM t AS t2 WHERE t2.k = t_outer.k AND t2.v < t_outer.v)",
    ),
    (
        "alias_outer_table_references",
        "SELECT a FROM u",
        "clickhouse",
        {"table": "t", "alias": "t_outer"},
        "SELECT a FROM u",
    ),
    (
        "alias_outer_table_references",
        "SELECT t.a FROM t JOIN u ON t.k = u.k WHERE EXISTS (SELECT 1 FROM t WHERE t.k = 1)",
        "clickhouse",
        {"table": "t", "alias": "o"},
        "SELECT o.a FROM t AS o JOIN u ON o.k = u.k WHERE EXISTS(SELECT 1 FROM t WHERE o.k = 1)",
    ),
    (
        "alias_derived_tables",
        "SELECT * FROM (SELECT 1 AS a) JOIN (SELECT 2 AS b) ON TRUE JOIN (SELECT 3 AS c) AS _dt2 ON TRUE",
        "doris",
        {},
        "SELECT * FROM (SELECT 1 AS a) AS _dt1 JOIN (SELECT 2 AS b) AS _dt3 ON TRUE JOIN (SELECT 3 AS c) AS _dt2 ON TRUE",
    ),
    (
        "alias_derived_tables",
        "SELECT * FROM (SELECT * FROM (SELECT 1 AS a)) WHERE a IN (SELECT 1)",
        "doris",
        {},
        "SELECT * FROM (SELECT * FROM (SELECT 1 AS a) AS _dt2) AS _dt1 WHERE a IN (SELECT 1)",
    ),
    (
        "alias_derived_tables",
        "SELECT * FROM (SELECT 1 AS a) UNION ALL SELECT * FROM (SELECT 2 AS a)",
        "doris",
        {"prefix": "dt"},
        "SELECT * FROM (SELECT 1 AS a) AS dt1 UNION ALL SELECT * FROM (SELECT 2 AS a) AS dt2",
    ),
    (
        "coalesce_count_subqueries",
        "SELECT (SELECT COUNT(o2.k) FROM o o2 WHERE o2.k = o.k) AS n FROM o",
        "clickhouse",
        {},
        "SELECT COALESCE((SELECT COUNT(o2.k) FROM o AS o2 WHERE o2.k = o.k), 0) AS n FROM o",
    ),
    (
        "coalesce_count_subqueries",
        "SELECT (SELECT COUNT(*) FROM o2 GROUP BY k) AS n, (SELECT MAX(x) FROM o2) FROM o",
        "clickhouse",
        {},
        "SELECT (SELECT COUNT(*) FROM o2 GROUP BY k) AS n, (SELECT MAX(x) FROM o2) FROM o",
    ),
    (
        "coalesce_count_subqueries",
        "SELECT a FROM t WHERE x > (SELECT COUNT(*) FROM u)",
        "clickhouse",
        {},
        "SELECT a FROM t WHERE x > (SELECT COUNT(*) FROM u)",
    ),
    (
        "trim_char_case_comparisons",
        "SELECT 1 FROM r WHERE r_name = CASE WHEN 'EUROPE' IS NOT NULL THEN 'EUROPE' ELSE r_name END",
        "spark",
        {},
        "SELECT 1 FROM r WHERE RTRIM(r_name) = RTRIM(CASE WHEN NOT 'EUROPE' IS NULL THEN 'EUROPE' ELSE r_name END)",
    ),
    (
        "trim_char_case_comparisons",
        "SELECT 1 FROM r WHERE r_name = CASE WHEN a = 1 THEN 'EUROPE' ELSE 'ASIA' END",
        "spark",
        {},
        "SELECT 1 FROM r WHERE r_name = CASE WHEN a = 1 THEN 'EUROPE' ELSE 'ASIA' END",
    ),
    (
        "trim_char_case_comparisons",
        "SELECT 1 FROM r WHERE CASE WHEN r_name = 'ASIA' THEN r_name ELSE NULL END = 'ASIA'",
        "spark",
        {},
        "SELECT 1 FROM r WHERE RTRIM('ASIA') = RTRIM(CASE WHEN r_name = 'ASIA' THEN r_name ELSE NULL END)",
    ),
    (
        "trim_char_case_comparisons",
        "SELECT 1 FROM r WHERE CASE WHEN a = b THEN a ELSE NULL END = b",
        "spark",
        {},
        "SELECT 1 FROM r WHERE CASE WHEN a = b THEN a ELSE NULL END = b",
    ),
    (
        "in_subquery_or_to_left_join",
        "SELECT id FROM items WHERE grp IN ('x', 'y') OR code IN (SELECT code FROM marks WHERE flag = 1) ORDER BY id",
        "duckdb",
        {},
        "SELECT id FROM items LEFT JOIN (SELECT DISTINCT code AS in_value_1 FROM marks WHERE flag = 1) AS in_subquery_1 ON code = in_subquery_1.in_value_1 WHERE grp IN ('x', 'y') OR NOT in_subquery_1.in_value_1 IS NULL ORDER BY id",
    ),
    (
        "in_subquery_or_to_left_join",
        "SELECT id FROM items WHERE grp = 'x' OR code IN (SELECT code FROM marks) OR k IN (SELECT k FROM m2)",
        "duckdb",
        {},
        "SELECT id FROM items LEFT JOIN (SELECT DISTINCT k AS in_value_1 FROM m2) AS in_subquery_1 ON k = in_subquery_1.in_value_1 LEFT JOIN (SELECT DISTINCT code AS in_value_2 FROM marks) AS in_subquery_2 ON code = in_subquery_2.in_value_2 WHERE grp = 'x' OR NOT in_subquery_2.in_value_2 IS NULL OR NOT in_subquery_1.in_value_1 IS NULL",
    ),
    (
        "in_subquery_or_to_left_join",
        "SELECT id FROM items WHERE code IN (SELECT code FROM marks)",
        "duckdb",
        {},
        "SELECT id FROM items WHERE code IN (SELECT code FROM marks)",
    ),
    (
        "in_subquery_or_to_left_join",
        "SELECT id FROM items WHERE NOT (grp = 'x' OR code IN (SELECT code FROM marks))",
        "duckdb",
        {},
        "SELECT id FROM items WHERE NOT (grp = 'x' OR code IN (SELECT code FROM marks))",
    ),
    (
        "in_subquery_or_to_left_join",
        "SELECT id FROM items AS i WHERE grp = 'x' OR code IN (SELECT code FROM marks WHERE marks.code = i.code)",
        "duckdb",
        {},
        "SELECT id FROM items AS i WHERE grp = 'x' OR code IN (SELECT code FROM marks WHERE marks.code = i.code)",
    ),
    (
        "in_subquery_or_to_left_join",
        "SELECT id FROM items, marks WHERE grp = 'x' OR items.code IN (SELECT code FROM marks)",
        "duckdb",
        {},
        "SELECT id FROM items, marks WHERE grp = 'x' OR items.code IN (SELECT code FROM marks)",
    ),
    (
        "factor_common_conjuncts",
        "SELECT 1 FROM t WHERE a = 1 AND b = 2 OR c = 3 AND d = 4",
        "duckdb",
        {},
        "SELECT 1 FROM t WHERE a = 1 AND b = 2 OR c = 3 AND d = 4",
    ),
    (
        "factor_common_conjuncts",
        "SELECT 1 FROM t WHERE (i.g = o.g AND i.k = 'a') OR (i.g = o.g AND i.k = 'b')",
        "duckdb",
        {},
        "SELECT 1 FROM t WHERE i.g = o.g AND ((i.k = 'a') OR (i.k = 'b'))",
    ),
    (
        "factor_common_conjuncts",
        "SELECT 1 FROM t WHERE (a = 1 AND b = 2) OR (a = 1 AND b = 2 AND c = 3)",
        "duckdb",
        {},
        "SELECT 1 FROM t WHERE a = 1 AND b = 2",
    ),
    (
        "factor_common_conjuncts",
        "SELECT 1 FROM t WHERE x = 1 AND ((a = 1 AND b = 2) OR (a = 1 AND c = 3) OR (a = 1 AND d = 4))",
        "duckdb",
        {},
        "SELECT 1 FROM t WHERE x = 1 AND (a = 1 AND ((b = 2) OR (c = 3) OR (d = 4)))",
    ),
    (
        "positive_count_subquery_to_exists",
        "SELECT 1 FROM t WHERE (SELECT COUNT(*) AS n FROM u WHERE u.k = t.k) > 0",
        "duckdb",
        {},
        "SELECT 1 FROM t WHERE EXISTS(SELECT 1 FROM u WHERE u.k = t.k)",
    ),
    (
        "positive_count_subquery_to_exists",
        "SELECT 1 FROM t WHERE (SELECT COUNT(*) FROM u GROUP BY k) > 0 AND (SELECT COUNT(x) FROM u) > 0 AND (SELECT COUNT(*) FROM u) > 1",
        "duckdb",
        {},
        "SELECT 1 FROM t WHERE (SELECT COUNT(*) FROM u GROUP BY k) > 0 AND (SELECT COUNT(x) FROM u) > 0 AND (SELECT COUNT(*) FROM u) > 1",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT a AS x, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY x DESC NULLS LAST, b",
        "duckdb",
        {},
        "SELECT x, b FROM (SELECT a AS x, b, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _w FROM t) AS _t WHERE _w = 1 ORDER BY x DESC, b",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT t.a AS x, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY t.a",
        "duckdb",
        {},
        "SELECT x, b FROM (SELECT t.a AS x, b, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _w FROM t) AS _t WHERE _w = 1 ORDER BY x",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY 2, 1",
        "duckdb",
        {},
        "SELECT a, b FROM (SELECT a, b, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _w FROM t) AS _t WHERE _w = 1 ORDER BY 2, 1",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1",
        "duckdb",
        {},
        "SELECT a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY b",
        "duckdb",
        {},
        "SELECT a FROM (SELECT a, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _w FROM t ORDER BY b) AS _t WHERE _w = 1",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT * FROM (SELECT a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY a) q ORDER BY a",
        "duckdb",
        {},
        "SELECT * FROM (SELECT a FROM (SELECT a, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _w FROM t) AS _t WHERE _w = 1 ORDER BY a) AS q ORDER BY a",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT DISTINCT g AS grp, v FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) <= 2 ORDER BY grp DESC, v LIMIT 3 OFFSET 1",
        "duckdb",
        {},
        "SELECT DISTINCT grp, v FROM (SELECT g AS grp, v, ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) AS _w FROM t) AS _t WHERE _w <= 2 ORDER BY grp DESC, v LIMIT 3 OFFSET 1",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT DISTINCT ON (a) a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY a",
        "duckdb",
        {},
        "SELECT a, b FROM (SELECT DISTINCT ON (a) a, b, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _w FROM t ORDER BY a) AS _t WHERE _w = 1 ORDER BY a",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY b LIMIT 4",
        "duckdb",
        {},
        "SELECT a FROM (SELECT a, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _w FROM t ORDER BY b LIMIT 4) AS _t WHERE _w = 1",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT l.a AS x, r.a FROM l JOIN r ON l.k = r.k QUALIFY ROW_NUMBER() OVER (PARTITION BY l.k ORDER BY r.a) = 1 ORDER BY l.a",
        "duckdb",
        {},
        "SELECT x, a FROM (SELECT l.a AS x, r.a, ROW_NUMBER() OVER (PARTITION BY l.k ORDER BY r.a) AS _w FROM l JOIN r ON l.k = r.k) AS _t WHERE _w = 1 ORDER BY x",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT b AS a, c FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY c ORDER BY b) = 1 ORDER BY t.a",
        "duckdb",
        {},
        "SELECT a, c FROM (SELECT b AS a, c, ROW_NUMBER() OVER (PARTITION BY c ORDER BY b) AS _w FROM t ORDER BY t.a) AS _t WHERE _w = 1",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY COUNT(*) OVER (PARTITION BY a)",
        "duckdb",
        {},
        "SELECT a, b FROM (SELECT a, b, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _w FROM t ORDER BY COUNT(*) OVER (PARTITION BY a)) AS _t WHERE _w = 1",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY SUM(a)",
        "duckdb",
        {},
        "SELECT a, b FROM (SELECT a, b, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _w FROM t ORDER BY SUM(a)) AS _t WHERE _w = 1",
    ),
    (
        "hoist_qualify_outer_clauses",
        "SELECT a AS b, b AS c FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY b + 0",
        "duckdb",
        {},
        "SELECT b, c FROM (SELECT a AS b, b AS c, ROW_NUMBER() OVER (PARTITION BY a ORDER BY a) AS _w FROM t) AS _t WHERE _w = 1 ORDER BY c + 0",
    ),
]


def render(function, sql, dialect, **kwargs):
    return ";\n".join(
        function(tree, **kwargs).sql(dialect, identify=False)
        for tree in sqlglot.parse(sql, read=dialect)
    )


def translate(sql, dialect):
    """BenchBox's translation of DuckDB text to a dialect without QUALIFY."""
    tree = hoist_qualify_outer_clauses(sqlglot.parse_one(sql, read="duckdb"))
    return tree.sql(dialect, identify=True, unsupported_level=ErrorLevel.IGNORE)


def sort_columns(order):
    names = [column.name for item in order.expressions for column in item.this.find_all(exp.Column)]
    return [name for index, name in enumerate(names) if index == 0 or name != names[index - 1]]


def outer_order_names(sql, dialect):
    order = sqlglot.parse_one(sql, read=dialect).args.get("order")
    assert order is not None, f"outermost SELECT has no ORDER BY: {sql}"
    return sort_columns(order)


class TestOutputsMatchBenchBox(unittest.TestCase):
    def test_each_rewrite_writes_the_text_benchbox_wrote(self):
        for name, sql, dialect, kwargs, expected in OUTPUT_CASES:
            with self.subTest(rewrite=name, sql=sql):
                with mock.patch.object(dialect_rewrites.logger, "warning"):
                    actual = render(getattr(dialect_rewrites, name), sql, dialect, **kwargs)
                self.assertEqual(actual, expected)

    def test_rewrites_copy_by_default_and_mutate_on_request(self):
        for name, sql, dialect, kwargs, _ in OUTPUT_CASES:
            with self.subTest(rewrite=name, sql=sql):
                tree = sqlglot.parse_one(sql, read=dialect)
                before = tree.sql(dialect)
                with mock.patch.object(dialect_rewrites.logger, "warning"):
                    getattr(dialect_rewrites, name)(tree, **kwargs)
                self.assertEqual(tree.sql(dialect), before)


class TestAliasOuterTableReferences(unittest.TestCase):
    def test_inner_aliased_table_is_untouched(self):
        tree = sqlglot.parse_one(
            "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM t t2 WHERE t2.k = t.k AND t2.v < t.v)",
            read="clickhouse",
        )
        self.assertEqual(
            alias_outer_table_references(tree, table="t", alias="t_outer").sql("clickhouse"),
            "SELECT a FROM t AS t_outer WHERE NOT EXISTS(SELECT 1 FROM t AS t2 "
            "WHERE t2.k = t_outer.k AND t2.v < t_outer.v)",
        )

    def test_a_query_without_the_outer_table_is_ignored(self):
        tree = sqlglot.parse_one("SELECT a FROM u", read="clickhouse")
        self.assertEqual(
            alias_outer_table_references(tree, table="t", alias="t_outer").sql("clickhouse"),
            "SELECT a FROM u",
        )


class TestAliasDerivedTables(unittest.TestCase):
    def test_numbering_starts_at_one_and_skips_taken_names(self):
        tree = sqlglot.parse_one(
            "SELECT * FROM (SELECT 1 AS a) JOIN (SELECT 2 AS b) ON TRUE "
            "JOIN (SELECT 3 AS c) AS _dt2 ON TRUE",
            read="doris",
        )
        self.assertEqual(
            alias_derived_tables(tree).sql("doris"),
            "SELECT * FROM (SELECT 1 AS a) AS _dt1 JOIN (SELECT 2 AS b) AS _dt3 ON TRUE "
            "JOIN (SELECT 3 AS c) AS _dt2 ON TRUE",
        )

    def test_prefix_is_an_argument(self):
        tree = sqlglot.parse_one("SELECT * FROM (SELECT 1 AS a)", read="doris")
        self.assertEqual(
            alias_derived_tables(tree, prefix="dt").sql("doris"),
            "SELECT * FROM (SELECT 1 AS a) AS dt1",
        )


class TestInSubqueryOrToLeftJoin(unittest.TestCase):
    def test_unsupported_shapes_are_left_alone(self):
        for sql in (
            "SELECT id FROM items WHERE code IN (SELECT code FROM marks)",
            "SELECT id FROM items WHERE NOT (grp = 'x' OR code IN (SELECT code FROM marks))",
            "SELECT id FROM items AS i WHERE grp = 'x' OR code IN "
            "(SELECT code FROM marks WHERE marks.code = i.code)",
            "SELECT id FROM items, marks WHERE grp = 'x' OR items.code IN (SELECT code FROM marks)",
        ):
            with self.subTest(sql=sql):
                tree = sqlglot.parse_one(sql, read="duckdb")
                self.assertEqual(
                    in_subquery_or_to_left_join(tree).sql("duckdb"), tree.sql("duckdb")
                )


class TestTrimCharCaseComparisons(unittest.TestCase):
    def test_self_defaulting_case_trims_both_sides(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM r WHERE r_name = CASE WHEN 'EUROPE' IS NOT NULL THEN 'EUROPE' ELSE r_name END",
            read="spark",
        )
        self.assertEqual(
            trim_char_case_comparisons(tree).sql("spark"),
            "SELECT 1 FROM r WHERE RTRIM(r_name) = "
            "RTRIM(CASE WHEN NOT 'EUROPE' IS NULL THEN 'EUROPE' ELSE r_name END)",
        )

    def test_other_cases_are_untouched(self):
        for sql in (
            "SELECT 1 FROM r WHERE r_name = CASE WHEN a = 1 THEN 'EUROPE' ELSE 'ASIA' END",
            "SELECT 1 FROM r WHERE CASE WHEN a = b THEN a ELSE NULL END = b",
        ):
            with self.subTest(sql=sql):
                tree = sqlglot.parse_one(sql, read="spark")
                self.assertEqual(trim_char_case_comparisons(tree).sql("spark"), tree.sql("spark"))

    def test_a_column_guarded_case_compared_to_a_string_literal_is_trimmed(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM r WHERE CASE WHEN r_name = 'ASIA' THEN r_name ELSE NULL END = 'ASIA'",
            read="spark",
        )
        self.assertEqual(
            trim_char_case_comparisons(tree).sql("spark"),
            "SELECT 1 FROM r WHERE RTRIM('ASIA') = "
            "RTRIM(CASE WHEN r_name = 'ASIA' THEN r_name ELSE NULL END)",
        )


class TestFactorCommonConjuncts(unittest.TestCase):
    def test_disjunctions_without_shared_terms_are_left_alone(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM t WHERE a = 1 AND b = 2 OR c = 3 AND d = 4", read="duckdb"
        )
        self.assertEqual(factor_common_conjuncts(tree).sql("duckdb"), tree.sql("duckdb"))


class TestChainsBenchBoxRuns(unittest.TestCase):
    def test_positive_count_then_common_conjuncts_then_exists_to_in(self):
        """The ClickHouse OBT Q41 chain; each step needs the one before it."""
        sql = (
            "SELECT DISTINCT o.name FROM items AS o WHERE o.pk > 0 AND "
            "(SELECT COUNT(*) AS n FROM items AS i WHERE (i.grp = o.grp AND i.kind = 'a') "
            "OR (i.grp = o.grp AND i.kind = 'b')) > 0 ORDER BY 1"
        )
        tree = sqlglot.parse_one(sql, read="duckdb")
        tree = positive_count_subquery_to_exists(tree)
        self.assertIn("EXISTS(", tree.sql("duckdb"))
        tree = factor_common_conjuncts(tree)
        tree = correlated_exists_to_in(tree, expected=1)
        rewritten = tree.sql("duckdb")
        self.assertNotIn("COUNT", rewritten)
        self.assertIn(" IN (SELECT", rewritten)

    def test_the_chain_out_of_order_does_not_reach_the_in_form(self):
        sql = (
            "SELECT o.name FROM items AS o WHERE "
            "(SELECT COUNT(*) FROM items AS i WHERE (i.grp = o.grp AND i.kind = 'a') "
            "OR (i.grp = o.grp AND i.kind = 'b')) > 0"
        )
        tree = factor_common_conjuncts(sqlglot.parse_one(sql, read="duckdb"))
        tree = positive_count_subquery_to_exists(tree)
        self.assertIn("EXISTS(", tree.sql("duckdb"))
        self.assertIn("i.grp = o.grp AND (", tree.sql("duckdb"))


class TestCoalesceCountSubqueries(unittest.TestCase):
    def test_correlated_count_is_coalesced_to_zero(self):
        tree = sqlglot.parse_one(
            "SELECT (SELECT COUNT(o2.k) FROM o o2 WHERE o2.k = o.k) AS n FROM o", read="clickhouse"
        )
        self.assertIn(
            "COALESCE((SELECT COUNT(o2.k) FROM o AS o2 WHERE o2.k = o.k), 0)",
            coalesce_count_subqueries(tree).sql("clickhouse"),
        )


class TestHoistQualifyOuterClauses(unittest.TestCase):
    def test_1_v8_shape_keeps_the_sort_keys_on_the_outermost_select(self):
        sql = (
            "SELECT DISTINCT l_returnflag, l_linestatus, SUM(l_quantity) OVER w AS sum_qty "
            "FROM lineitem WHERE l_shipdate <= DATE '1998-09-02' "
            "WINDOW w AS (PARTITION BY l_returnflag, l_linestatus) "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY l_returnflag, l_linestatus ORDER BY l_returnflag) = 1 "
            "ORDER BY l_returnflag, l_linestatus"
        )
        for dialect in ELIMINATING:
            with self.subTest(dialect=dialect):
                rendered = translate(sql, dialect)
                self.assertEqual(
                    outer_order_names(rendered, dialect), ["l_returnflag", "l_linestatus"]
                )
                derived = sqlglot.parse_one(rendered, read=dialect).args["from_"].this
                self.assertIsInstance(derived, exp.Subquery)
                self.assertEqual(derived.alias, "_t")

    def test_qualify_with_order_by_is_ordered_outermost(self):
        sql = (
            "SELECT a AS x, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 "
            "ORDER BY x DESC NULLS LAST, b"
        )
        for dialect in ELIMINATING:
            with self.subTest(dialect=dialect):
                order = sqlglot.parse_one(translate(sql, dialect), read=dialect).args["order"]
                self.assertEqual(sort_columns(order), ["x", "b"])
                self.assertTrue(any(item.args.get("desc") is True for item in order.expressions))

    def test_order_by_on_an_aliased_source_column_resolves_to_the_projected_alias(self):
        sql = (
            "SELECT t.a AS x, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 "
            "ORDER BY t.a"
        )
        for dialect in ELIMINATING:
            with self.subTest(dialect=dialect):
                self.assertEqual(outer_order_names(translate(sql, dialect), dialect), ["x"])

    def test_positional_order_by_is_hoisted(self):
        sql = "SELECT a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY 2, 1"
        for dialect in ELIMINATING:
            with self.subTest(dialect=dialect):
                tree = sqlglot.parse_one(translate(sql, dialect), read=dialect)
                keys = [
                    item.this.sql()
                    for item in tree.args["order"].expressions
                    if isinstance(item.this, exp.Literal)
                ]
                self.assertEqual(keys, ["2", "1"])

    def test_qualify_without_order_by_is_left_for_the_generator(self):
        tree = sqlglot.parse_one(
            "SELECT a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1",
            read="duckdb",
        )
        rewritten = hoist_qualify_outer_clauses(tree)
        self.assertEqual(rewritten.sql("duckdb"), tree.sql("duckdb"))
        self.assertIsNotNone(rewritten.args.get("qualify"))

    def test_unprojected_sort_key_is_reported_rather_than_silently_dropped(self):
        sql = "SELECT a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 ORDER BY b"
        with self.assertLogs("sqlglot", level=logging.WARNING) as logs:
            rendered = translate(sql, "trino")
        self.assertIn("cannot hoist", "\n".join(logs.output))
        self.assertIsNone(sqlglot.parse_one(rendered, read="trino").args.get("order"))

    def test_nested_qualify_selects_each_get_their_own_outer_order(self):
        sql = (
            "SELECT * FROM (SELECT a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 "
            "ORDER BY a) q ORDER BY a"
        )
        tree = sqlglot.parse_one(translate(sql, "trino"), read="trino")
        self.assertIsNotNone(tree.args["order"])
        inner = next(s for s in tree.find_all(exp.Select) if s.args.get("where") is not None)
        self.assertEqual([item.this.name for item in inner.args["order"].expressions], ["a"])

    def test_order_distinct_limit_and_offset_land_on_the_outer_select_after_the_filter(self):
        sql = (
            "SELECT DISTINCT g AS grp, v FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) <= 2 "
            "ORDER BY grp DESC, v LIMIT 3 OFFSET 1"
        )
        for dialect in ELIMINATING:
            with self.subTest(dialect=dialect):
                tree = sqlglot.parse_one(translate(sql, dialect), read=dialect)
                self.assertIsNone(tree.args.get("qualify"))
                self.assertIsNotNone(tree.args["where"])
                for clause in ("distinct", "limit", "order"):
                    self.assertIsNotNone(tree.args.get(clause), f"{dialect}: {clause}")
                inner = tree.args["from_"].this.this
                self.assertIsInstance(inner, exp.Select)
                for clause in ("distinct", "limit", "offset", "order"):
                    self.assertIsNone(inner.args.get(clause), f"{dialect}: {clause} stayed inside")

    def test_distinct_on_stays_with_its_own_elimination(self):
        tree = sqlglot.parse_one(
            "SELECT DISTINCT ON (a) a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 "
            "ORDER BY a",
            read="duckdb",
        )
        rewritten = hoist_qualify_outer_clauses(tree)
        inner = rewritten.args["from_"].this.this
        self.assertIsNotNone(inner.args["distinct"].args["on"])
        self.assertIsNotNone(inner.args["order"])
        self.assertIsNotNone(rewritten.args["order"])

    def test_limit_stays_with_its_sort_key_when_the_key_cannot_be_hoisted(self):
        sql = (
            "SELECT a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 "
            "ORDER BY b LIMIT 4"
        )
        with self.assertLogs("sqlglot", level=logging.WARNING) as logs:
            rendered = translate(sql, "postgres")
        tree = sqlglot.parse_one(rendered, read="postgres")
        inner = tree.args["from_"].this.this
        self.assertIn("cannot hoist", "\n".join(logs.output))
        self.assertIsNone(tree.args.get("limit"))
        self.assertIsNotNone(inner.args["limit"])
        self.assertIsNotNone(inner.args["order"])

    def test_qualified_order_by_resolves_by_source_not_by_bare_name(self):
        sql = (
            "SELECT l.a AS x, r.a FROM l JOIN r ON l.k = r.k "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY l.k ORDER BY r.a) = 1 ORDER BY l.a"
        )
        self.assertEqual(outer_order_names(translate(sql, "trino"), "trino"), ["x"])

    def test_qualified_order_by_does_not_bind_to_an_alias_holding_another_column(self):
        sql = (
            "SELECT b AS a, c FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY c ORDER BY b) = 1 "
            "ORDER BY t.a"
        )
        with self.assertLogs("sqlglot", level=logging.WARNING):
            rendered = translate(sql, "trino")
        self.assertIsNone(sqlglot.parse_one(rendered, read="trino").args.get("order"))

    def test_aggregate_and_window_sort_keys_are_not_hoisted(self):
        for key in ("COUNT(*) OVER (PARTITION BY a)", "SUM(a)"):
            with self.subTest(key=key):
                sql = (
                    "SELECT a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 "
                    f"ORDER BY {key}"
                )
                with self.assertLogs("sqlglot", level=logging.WARNING) as logs:
                    rendered = translate(sql, "trino")
                self.assertIn("cannot hoist", "\n".join(logs.output))
                self.assertIsNone(sqlglot.parse_one(rendered, read="trino").args.get("order"))

    def test_columns_inside_an_order_by_expression_bind_to_input_columns_not_aliases(self):
        sql = (
            "SELECT a AS b, b AS c FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 "
            "ORDER BY b + 0"
        )
        order = sqlglot.parse_one(translate(sql, "trino"), read="trino").args["order"]
        self.assertEqual(order.expressions[0].this.sql(), '"c" + 0')

    def test_dialects_with_native_qualify_are_not_in_the_eliminating_set(self):
        sql = "SELECT a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY a) = 1"
        eliminating = set()
        for name in sqlglot.Dialect.classes:
            if not name or name == "starrocks":
                continue
            try:
                rendered = sqlglot.parse_one(sql, read="duckdb").sql(name)
            except Exception:
                continue
            if "QUALIFY" not in rendered.upper():
                eliminating.add(name)
        self.assertLessEqual(eliminating - {"starrocks"}, set(ELIMINATING))

    def test_a_generator_with_native_qualify_keeps_it(self):
        sql = (
            "SELECT DISTINCT a, b FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1 "
            "ORDER BY a LIMIT 3"
        )
        for dialect in ("duckdb", "snowflake", "bigquery", "databricks", "clickhouse", "redshift"):
            with self.subTest(dialect=dialect):
                rendered = sqlglot.transpile(sql, read="duckdb", write=dialect)[0]
                self.assertIn("QUALIFY", rendered.upper())


if __name__ == "__main__":
    unittest.main()
