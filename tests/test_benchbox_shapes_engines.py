"""Execute the query-shape and dialect rewrites on real engines.

Run by the ``in-process``, ``starrocks`` and ``doris`` jobs of ``benchbox-engines.yml``; each
class skips when its engine is not available. Most rewrites exist for MySQL-family engines
(SingleStore has no engine in CI, see ``downstream/ENGINE-GAPS.md``), so each is proved by
executing the original and the rewritten SQL on an engine that accepts both and comparing rows;
the rewrites that fix a Doris or StarRocks limitation also run on those engines. Data is created
inline or in a fresh in-memory database, so every class is self-contained.
"""

import sqlite3
import unittest

import sqlglot
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
from sqlglot.benchbox.shapes import (
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
from tests import engines
from tests.engines import requires_engine


def parse(sql, dialect="duckdb"):
    return sqlglot.parse_one(sql, read=dialect)


def rows(connection, sql):
    return [tuple(None if v is None else v for v in row) for row in connection.execute(sql)]


def duckdb_connection():
    connection = engines.connect("duckdb")
    for statement in (
        "CREATE TABLE items (id INT, grp VARCHAR, code VARCHAR, pk INT, name VARCHAR, kind VARCHAR)",
        "CREATE TABLE marks (code VARCHAR, flag INT)",
        "CREATE TABLE t (g INTEGER, v INTEGER, k INTEGER)",
        "CREATE TABLE u (k INTEGER, v INTEGER, flag VARCHAR)",
        "CREATE TABLE empty_t (a INTEGER, x INTEGER)",
        "CREATE TABLE some_t (a INTEGER, x INTEGER)",
        "INSERT INTO items VALUES (1, 'x', 'a', 1, 'p', 'a'), (2, 'z', 'a', 2, 'q', 'c'), "
        "(3, 'z', 'b', 3, 'r', 'c'), (4, 'z', NULL, 4, 's', 'b'), (5, NULL, 'c', 5, 't', 'a'), "
        "(6, 'z', 'd', 0, 'u', 'a')",
        "INSERT INTO marks VALUES ('a', 1), ('a', 1), ('c', 1), (NULL, 1), ('b', 0)",
        "INSERT INTO t VALUES (1, 10, 1), (1, 20, 1), (3, 5, 3), (3, 6, 3), (2, 7, 2), (2, 8, 2), (4, 1, 4)",
        "INSERT INTO u VALUES (1, 5, 'R'), (1, 6, 'N'), (2, 9, 'R'), (5, 1, 'R')",
        "INSERT INTO some_t VALUES (1, 10), (1, 20), (2, 5)",
    ):
        connection.execute(statement)
    return connection


@requires_engine("duckdb")
class TestEquivalenceOnDuckDB(unittest.TestCase):
    """The rewritten text returns the rows of the original text."""

    def setUp(self):
        self.connection = duckdb_connection()

    def tearDown(self):
        self.connection.close()

    def assert_same_rows(self, sql, rewritten, expected=None, ordered=True):
        original_rows = rows(self.connection, sql)
        if expected is not None:
            self.assertEqual(original_rows, expected)
        rewritten_rows = rows(self.connection, rewritten.sql("duckdb"))
        if ordered:
            self.assertEqual(rewritten_rows, original_rows)
        else:
            self.assertEqual(sorted(rewritten_rows, key=repr), sorted(original_rows, key=repr))

    def test_in_subquery_under_or_becomes_a_left_join_with_the_same_rows(self):
        sql = (
            "SELECT id FROM items WHERE grp IN ('x', 'y') OR code IN "
            "(SELECT code FROM marks WHERE flag = 1) ORDER BY id"
        )
        rewritten = in_subquery_or_to_left_join(parse(sql))
        self.assertIn("LEFT JOIN", rewritten.sql("duckdb"))
        self.assertNotIn(" IN (SELECT", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten, [(1,), (2,), (5,)])

    def test_positive_count_and_common_conjunct_chain_preserves_results(self):
        sql = (
            "SELECT DISTINCT o.name FROM items AS o WHERE o.pk > 0 AND "
            "(SELECT COUNT(*) AS n FROM items AS i WHERE (i.grp = o.grp AND i.kind = 'a') "
            "OR (i.grp = o.grp AND i.kind = 'b')) > 0 ORDER BY 1"
        )
        tree = factor_common_conjuncts(positive_count_subquery_to_exists(parse(sql)))
        tree = correlated_exists_to_in(tree, expected=1)
        self.assertNotIn("COUNT", tree.sql("duckdb"))
        self.assertIn(" IN (SELECT", tree.sql("duckdb"))
        self.assert_same_rows(sql, tree, [("p",), ("q",), ("r",), ("s",)])

    def test_exists_becomes_a_join_to_a_derived_table_with_the_same_rows(self):
        sql = (
            "SELECT u.k, u.v FROM u WHERE u.v > 0 AND EXISTS "
            "(SELECT 1 FROM u u2 WHERE u2.k = u.k AND u2.v = u.v AND u2.flag = 'R') ORDER BY u.k, u.v"
        )
        rewritten = exists_to_derived_join(parse(sql))
        self.assertNotIn("EXISTS", rewritten.sql("duckdb").upper())
        self.assert_same_rows(sql, rewritten, [(1, 5), (2, 9), (5, 1)])

    def test_char_case_comparison_trims_the_padding_a_char_column_carries(self):
        sql = "SELECT n FROM (SELECT 'ASIA ' AS name, 1 AS n) WHERE name = CASE WHEN n = 1 THEN 'ASIA' ELSE name END"
        self.assertEqual(rows(self.connection, sql), [])
        rewritten = trim_char_case_comparisons(parse(sql))
        self.assertEqual(rows(self.connection, rewritten.sql("duckdb")), [(1,)])

    def test_hoisted_order_defines_the_result_order(self):
        source = (
            "SELECT g, SUM(v) OVER (PARTITION BY g) AS total, COUNT(*) OVER (PARTITION BY g) AS n "
            "FROM t QUALIFY COUNT(*) OVER (PARTITION BY g) > 1 ORDER BY g DESC"
        )
        rewritten = hoist_qualify_outer_clauses(parse(source))
        rendered = rewritten.sql("duckdb")
        self.assertNotIn("QUALIFY", rendered.upper())
        self.assertEqual([row[0] for row in rows(self.connection, rendered)], [3, 3, 2, 2, 1, 1])

    def test_hoisted_distinct_order_limit_and_offset_match_native_qualify(self):
        source = (
            "SELECT DISTINCT g AS grp, v % 2 AS parity FROM t "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) <= 2 "
            "ORDER BY grp DESC, parity LIMIT 3 OFFSET 1"
        )
        rewritten = hoist_qualify_outer_clauses(parse(source))
        self.assertNotIn("QUALIFY", rewritten.sql("duckdb").upper())
        self.assert_same_rows(source, rewritten)
        self.assertEqual(len(rows(self.connection, source)), 3)

    def test_limit_applies_after_the_qualify_filter_not_before(self):
        source = "SELECT g FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) = 1 ORDER BY g LIMIT 2"
        self.assert_same_rows(source, hoist_qualify_outer_clauses(parse(source)), [(1,), (2,)])

    def test_aggregate_subqueries_are_hoisted_without_changing_the_result(self):
        sql = "SELECT SUM(CASE WHEN EXISTS(SELECT 1 FROM u WHERE u.k = t.k) THEN t.v ELSE 0 END) FROM t"
        rewritten = hoist_aggregate_subqueries(parse(sql))
        self.assertIn("hoisted_subquery_1", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten, [(45,)])

    def test_order_by_a_subquery_alias_runs_in_the_enclosing_query(self):
        sql = (
            "SELECT k, (SELECT SUM(v) FROM u WHERE u.k = t.k) AS total FROM t "
            "ORDER BY total DESC, k LIMIT 3"
        )
        rewritten = wrap_order_by_subquery_aliases(parse(sql))
        self.assertIn("ordered_rows", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten)

    def test_scalar_subquery_with_a_pinned_group_by_keeps_its_value(self):
        # u.k = t.g pins the grouping key u.k to a column of another name, so there is one group.
        sql = (
            "SELECT t.k FROM t WHERE t.v < (SELECT MAX(u.v) FROM u WHERE u.k = t.g AND u.v > 0 "
            "GROUP BY u.k HAVING MAX(u.v) > 5) ORDER BY t.k, t.v"
        )
        tree = drop_correlated_scalar_group_by(parse(sql), columns=["k"])
        tree = fold_scalar_having_into_case(tree)
        rendered = tree.sql("duckdb")
        self.assertNotIn("GROUP BY", rendered)
        self.assertNotIn("HAVING", rendered)
        self.assertIn("CASE WHEN", rendered)
        self.assert_same_rows(sql, tree)

    def test_scalar_subquery_grouped_by_an_unpinned_key_keeps_its_group_by(self):
        sql = (
            "SELECT t.k FROM t WHERE t.v < (SELECT MAX(u.v) FROM u WHERE u.k = 1 AND u.v > 0 "
            "GROUP BY u.k)"
        )
        tree = drop_correlated_scalar_group_by(parse(sql), columns=["k"])
        self.assertIn("GROUP BY", tree.sql("duckdb"))

    def test_aggregated_columns_are_qualified_without_changing_rows(self):
        sql = "SELECT g, SUM(v) AS v FROM t GROUP BY g ORDER BY v DESC, g"
        rewritten = qualify_aggregated_columns(parse(sql), tables={"v": "t"})
        self.assertIn("SUM(t.v)", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten)

    def test_order_by_the_projected_alias_keeps_the_order(self):
        sql = "SELECT g, RANK() OVER (ORDER BY v) AS r FROM t ORDER BY RANK() OVER (ORDER BY v), g"
        rewritten = alias_order_by_projections(parse(sql))
        self.assertIn("ORDER BY r, g", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten)

    def test_rollup_over_empty_input_returns_the_grand_total_row(self):
        for table in ("empty_t", "some_t"):
            with self.subTest(table=table):
                sql = (
                    f"SELECT a, COUNT(*) AS n, SUM(x) AS s FROM {table} "
                    "GROUP BY ROLLUP (a) ORDER BY a NULLS LAST LIMIT 5"
                )
                rewritten = rollup_empty_input_row(parse(sql))
                self.assertIn("NOT EXISTS", rewritten.sql("duckdb"))
                self.assert_same_rows(sql, rewritten)

    def test_empty_group_by_is_dropped_without_changing_the_row(self):
        sql = "SELECT SUM(v) AS s FROM t GROUP BY () HAVING SUM(v) IS NOT NULL"
        rewritten = drop_empty_group_by(parse(sql))
        self.assertNotIn("GROUP BY", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten, [(sum((10, 20, 5, 6, 7, 8, 1)),)])

    def test_single_grouping_set_is_a_plain_group_by(self):
        sql = "SELECT g, SUM(v) FROM t GROUP BY GROUPING SETS ((g)) ORDER BY g"
        rewritten = unwrap_single_grouping_set(parse(sql))
        self.assertNotIn("GROUPING SETS", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten)

    def test_sourceless_exists_is_its_predicate(self):
        sql = "SELECT id FROM items AS a WHERE EXISTS(SELECT 1 WHERE a.grp = 'x' OR a.pk = 5) ORDER BY id"
        rewritten = inline_sourceless_exists(parse(sql))
        self.assertNotIn("EXISTS", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten, [(1,), (5,)])

    def test_date_arithmetic_becomes_day_functions(self):
        sql = "SELECT d_date + 5 AS later, d_date - 3 AS earlier FROM (SELECT DATE '2000-02-01' AS d_date)"
        rewritten = date_column_integer_arithmetic(parse(sql), suffix="_date")
        self.assertNotIn("+ 5", rewritten.sql("duckdb"))
        # DuckDB types DATE + INTERVAL as a timestamp, so compare the day.
        self.assertEqual(
            [
                tuple(str(v)[:10] for v in row)
                for row in rows(self.connection, rewritten.sql("duckdb"))
            ],
            [tuple(str(v)[:10] for v in row) for row in rows(self.connection, sql)],
        )

    def test_count_subquery_is_coalesced_to_zero(self):
        sql = "SELECT g, (SELECT COUNT(u.k) FROM u WHERE u.k = t.g) AS n FROM t ORDER BY g, v"
        rewritten = coalesce_count_subqueries(parse(sql))
        self.assertIn("COALESCE", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten)

    def test_outer_table_alias_keeps_the_correlated_rows(self):
        sql = (
            "SELECT g, v FROM t WHERE NOT EXISTS (SELECT 1 FROM t t2 WHERE t2.g = t.g AND t2.v > t.v) "
            "ORDER BY g"
        )
        rewritten = alias_outer_table_references(parse(sql), table="t", alias="t_outer")
        self.assertIn("t AS t_outer", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten)

    def test_derived_tables_are_named_without_changing_rows(self):
        sql = (
            "SELECT * FROM (SELECT g FROM t WHERE v > 5) JOIN (SELECT k FROM u) ON g = k ORDER BY g"
        )
        rewritten = alias_derived_tables(parse(sql))
        self.assertIn("AS _dt1", rewritten.sql("duckdb"))
        self.assert_same_rows(sql, rewritten)


@requires_engine("sqlite")
class TestEquivalenceOnSQLite(unittest.TestCase):
    def test_exists_to_derived_join_keeps_matching_rows_identical(self):
        source = (
            "SELECT t.k, t.v FROM t WHERE t.v > 0 AND EXISTS "
            "(SELECT 1 FROM t t2 WHERE t2.k = t.k AND t2.v = t.v AND t2.flag = 'R') ORDER BY t.k, t.v"
        )
        rewritten = exists_to_derived_join(parse(source, "sqlite")).sql("sqlite")
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE t (k INT, v INT, flag TEXT)")
        connection.executemany(
            "INSERT INTO t VALUES (?, ?, ?)",
            [(1, 1, "R"), (1, 1, "R"), (2, 5, "N"), (3, 7, "R"), (4, 0, "R")],
        )
        self.assertNotIn("EXISTS", rewritten.upper())
        self.assertEqual(
            connection.execute(rewritten).fetchall(), connection.execute(source).fetchall()
        )

    def test_a_global_aggregate_runs_once_its_empty_group_by_is_dropped(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE t (v INT)")
        connection.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
        sql = "SELECT SUM(v) AS s FROM t GROUP BY ()"
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute(sql)
        rewritten = drop_empty_group_by(parse(sql, "sqlite")).sql("sqlite")
        self.assertEqual(connection.execute(rewritten).fetchall(), [(3,)])


@requires_engine("datafusion")
class TestEquivalenceOnDataFusion(unittest.TestCase):
    def test_hoisted_qualify_runs_where_the_engine_has_no_qualify(self):
        source = (
            "SELECT g, v FROM (VALUES (1, 10), (1, 20), (2, 5), (3, 1), (3, 2)) AS t(g, v) "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY v DESC) = 1 ORDER BY g DESC"
        )
        rewritten = hoist_qualify_outer_clauses(parse(source)).sql("datafusion")
        self.assertNotIn("QUALIFY", rewritten.upper())
        self.assertEqual(
            [tuple(int(v) for v in row) for row in engines.execute("datafusion", rewritten)],
            [(3, 2), (2, 5), (1, 20)],
        )


@requires_engine("chdb")
class TestEquivalenceOnClickHouse(unittest.TestCase):
    def test_hoisted_qualify_runs_where_the_engine_has_no_qualify(self):
        source = (
            "SELECT g, v FROM (SELECT 1 AS g, 10 AS v UNION ALL SELECT 1, 20 UNION ALL SELECT 2, 5) "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY v DESC) = 1 ORDER BY g DESC"
        )
        rewritten = hoist_qualify_outer_clauses(parse(source)).sql("clickhouse")
        self.assertNotIn("QUALIFY", rewritten.upper())
        self.assertEqual(
            [tuple(int(v) for v in row) for row in engines.execute("chdb", rewritten)],
            [(2, 5), (1, 20)],
        )


ROLLUP_DATA = "(SELECT 1 AS a, 2 AS x UNION ALL SELECT 1, 4 UNION ALL SELECT 2, 6) AS t"


class MySqlFamilyShapes:
    ENGINE = ""

    def write(self, tree):
        return tree.sql(self.ENGINE)

    def values(self, sql):
        return [
            tuple(None if v is None else float(v) for v in row)
            for row in engines.execute(self.ENGINE, sql)
        ]

    def test_rollup_over_empty_input_emits_the_grand_total_row(self):
        for where, expected in (
            ("WHERE x > 100", [(None, 0.0, None)]),
            ("", [(1.0, 2.0, 6.0), (2.0, 1.0, 6.0), (None, 3.0, 12.0)]),
        ):
            with self.subTest(where=where):
                sql = (
                    f"SELECT a, COUNT(*) AS n, SUM(x) AS s FROM {ROLLUP_DATA} {where} "
                    "GROUP BY ROLLUP (a) ORDER BY a NULLS LAST"
                )
                rewritten = rollup_empty_input_row(parse(sql))
                self.assertEqual(self.values(self.write(rewritten)), expected)

    def test_date_arithmetic_on_a_date_column_runs(self):
        sql = "SELECT d_date + 5 AS later, d_date - 3 AS earlier FROM (SELECT DATE '2000-02-01' AS d_date) AS d"
        rewritten = date_column_integer_arithmetic(parse(sql), suffix="_date")
        (row,) = engines.execute(self.ENGINE, self.write(rewritten))
        self.assertEqual([str(v)[:10] for v in row], ["2000-02-06", "2000-01-29"])

    def test_derived_tables_get_names_the_engine_requires(self):
        sql = "SELECT * FROM (SELECT 1 AS x) JOIN (SELECT 2 AS y) ON TRUE"
        rewritten = alias_derived_tables(parse(sql))
        (row,) = engines.execute(self.ENGINE, self.write(rewritten))
        self.assertEqual([int(v) for v in row], [1, 2])


@requires_engine("starrocks")
class TestShapesOnStarRocks(MySqlFamilyShapes, unittest.TestCase):
    ENGINE = "starrocks"


@requires_engine("doris")
class TestShapesOnDoris(MySqlFamilyShapes, unittest.TestCase):
    ENGINE = "doris"


if __name__ == "__main__":
    unittest.main()
