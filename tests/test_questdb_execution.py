"""Execute the QuestDB dialect's output on a real QuestDB and compare the results.

Run by the ``questdb`` job of ``benchbox-engines.yml``; skipped locally unless
``BENCHBOX_ENGINE_QUESTDB=host:port`` points at a running QuestDB. Each rewrite is
shown with the SQL it generates, the rows it returns, and, where the source SQL also
runs on QuestDB, that the rows are the same.
"""

import datetime
import unittest

import sqlglot
from sqlglot import exp
from tests import engines
from tests.engines import requires_engine


def questdb_sql(sql, read="postgres", **kwargs):
    return sqlglot.transpile(sql, read=read, write="questdb", **kwargs)[0]


def ts(*args):
    return datetime.datetime(*args)


def rejected(connection, sql):
    """The error QuestDB raises for ``sql``, or ``None`` if it ran."""
    try:
        connection.execute(sql)
    except Exception as e:  # noqa: BLE001 - the driver's error types are engine specific
        return str(e)
    return None


@requires_engine("questdb")
class QuestDBExecutionCase(unittest.TestCase):
    TABLES = {
        "qx_a": "(id INT, k INT, v DOUBLE)",
        "qx_b": "(id INT, k INT, m INT)",
        "qx_c": "(id INT, m INT, z INT)",
        "qx_s": "(id INT, s STRING)",
        "qx_d": "(id INT, d DATE)",
        "qx_p": "(p_id INT, p_k INT)",
        "qx_q": "(q_id INT, q_k INT, q_m INT)",
        "qx_r": "(r_id INT, r_m INT, r_z INT)",
    }
    ROWS = {
        "qx_a": "(1, 10, 1.5), (2, 20, 2.5), (3, 30, 3.5), (4, 40, 4.5)",
        "qx_b": "(1, 10, 100), (2, 20, 200), (3, 20, 300), (4, 50, 400)",
        "qx_c": "(1, 100, 7), (2, 200, 8), (3, 999, 9)",
        "qx_s": "(1, 'abcdef'), (2, NULL), (3, ''), (4, 'ab')",
        "qx_d": "(1, CAST('2000-01-31' AS DATE)), (2, CAST('2000-02-29' AS DATE)), "
        "(3, CAST('1998-12-01' AS DATE))",
        "qx_p": "(1, 10), (2, 20), (3, 30)",
        "qx_q": "(1, 10, 100), (2, 20, 200), (3, 20, 300)",
        "qx_r": "(1, 100, 7), (2, 200, 8), (3, 999, 9)",
    }

    def setUp(self):
        super().setUp()
        self.connection = engines.connect("questdb")
        self.addCleanup(self.connection.close)
        for table, columns in self.TABLES.items():
            self.connection.execute(f"DROP TABLE IF EXISTS {table}")
            self.connection.execute(f"CREATE TABLE {table} {columns}")
            self.connection.execute(f"INSERT INTO {table} VALUES {self.ROWS[table]}")

    def rows(self, sql):
        return self.connection.execute(sql)

    def run_rewritten(self, sql, **kwargs):
        """Generate QuestDB SQL from PostgreSQL ``sql``, run it and return (sql, rows)."""
        generated = questdb_sql(sql, **kwargs)
        return generated, self.rows(generated)


class TestCommaJoins(QuestDBExecutionCase):
    def joins_of(self, generated):
        return list(sqlglot.parse_one(generated, read="questdb").find_all(exp.Join))

    def test_two_tables_match_the_comma_join(self):
        sql = (
            "SELECT a.id, b.m FROM qx_a a, qx_b b WHERE a.k = b.k AND a.v > 1.5 "
            "ORDER BY a.id, b.m"
        )
        generated, rows = self.run_rewritten(sql)
        self.assertIn("JOIN qx_b AS b ON a.k = b.k", generated)
        self.assertNotIn(" a, ", generated)
        self.assertEqual(rows, [(2, 200), (2, 300)])
        self.assertEqual(rows, self.rows(sql))

    def test_three_tables(self):
        sql = (
            "SELECT a.id, c.z FROM qx_a a, qx_b b, qx_c c "
            "WHERE a.k = b.k AND b.m = c.m ORDER BY a.id"
        )
        generated, rows = self.run_rewritten(sql)
        self.assertEqual([j.args.get("kind") for j in self.joins_of(generated)], [None, None])
        self.assertEqual(rows, [(1, 7), (2, 8)])
        self.assertEqual(rows, self.rows(sql))

    def test_join_order_does_not_matter(self):
        sql = (
            "SELECT a.id, c.z FROM qx_a a, qx_c c, qx_b b "
            "WHERE b.m = c.m AND a.k = b.k ORDER BY a.id"
        )
        generated, rows = self.run_rewritten(sql)
        self.assertEqual(rows, [(1, 7), (2, 8)])
        self.assertEqual(rows, self.rows(sql))

    def test_comma_join_without_a_predicate_is_a_cross_join(self):
        sql = "SELECT count(*) FROM qx_a, qx_b"
        generated, rows = self.run_rewritten(sql)
        self.assertIn("CROSS JOIN", generated)
        self.assertEqual(rows, [(16,)])
        self.assertEqual(rows, self.rows(sql))

    def test_filter_only_leaves_a_cross_join_with_the_filter(self):
        sql = "SELECT count(*) FROM qx_a a, qx_b b WHERE a.v > 3 AND b.m < 300"
        generated, rows = self.run_rewritten(sql)
        self.assertIn("CROSS JOIN qx_b AS b WHERE a.v > 3 AND b.m < 300", generated)
        self.assertEqual(rows, [(4,)])
        self.assertEqual(rows, self.rows(sql))

    def test_columns_without_a_qualifier(self):
        sql = (
            "SELECT p_id, r_z FROM qx_p, qx_q, qx_r WHERE q_m = r_m AND p_k = q_k ORDER BY p_id"
        )
        generated, rows = self.run_rewritten(sql)
        self.assertIn("JOIN qx_q ON q_m = r_m JOIN qx_r ON p_k = q_k", generated)
        self.assertEqual(rows, [(1, 7), (2, 8)])
        self.assertEqual(rows, self.rows(sql))

    def test_join_inside_cte_and_derived_table(self):
        sql = (
            "WITH t AS (SELECT a.id AS aid, b.m FROM qx_a a, qx_b b WHERE a.k = b.k) "
            "SELECT s.aid, s.m FROM (SELECT t.aid, t.m FROM t, qx_c c WHERE t.m = c.m) s "
            "ORDER BY s.aid"
        )
        generated, rows = self.run_rewritten(sql)
        self.assertEqual(len(self.joins_of(generated)), 2)
        self.assertEqual(rows, [(1, 100), (2, 200)])
        self.assertEqual(rows, self.rows(sql))

    def test_join_with_an_explicit_join_in_between(self):
        sql = (
            "SELECT a.id, c.z FROM qx_a a JOIN qx_b b ON a.k = b.k, qx_c c "
            "WHERE b.m = c.m ORDER BY a.id"
        )
        generated, rows = self.run_rewritten(sql)
        self.assertNotIn(", qx_c", generated)
        self.assertEqual(rows, [(1, 7), (2, 8)])
        self.assertEqual(rows, self.rows(sql))


class TestIntervalArithmetic(QuestDBExecutionCase):
    def test_interval_arithmetic_is_not_accepted_as_written(self):
        # The reason the generator emits DATEADD.
        error = rejected(self.connection, "SELECT d + INTERVAL '5' DAY FROM qx_d")
        self.assertIsNotNone(error)
        self.assertIn("impossible type cast", error)

    def test_literal_dates(self):
        generated, rows = self.run_rewritten(
            "SELECT DATE '1998-12-01' - INTERVAL '90' DAY, DATE '1995-03-15' + INTERVAL '30' DAY, "
            "DATE '1994-01-01' + INTERVAL '3' MONTH, DATE '2000-02-29' + INTERVAL '1' YEAR"
        )
        self.assertNotIn("INTERVAL", generated)
        # The values PostgreSQL returns for the same expressions.
        self.assertEqual(rows, [(ts(1998, 9, 2), ts(1995, 4, 14), ts(1994, 4, 1), ts(2001, 2, 28))])

    def test_columns_with_month_and_year_ends(self):
        generated, rows = self.run_rewritten(
            "SELECT id, d + INTERVAL '1' MONTH, d - INTERVAL '1' YEAR, d + INTERVAL '45' DAY "
            "FROM qx_d ORDER BY id"
        )
        self.assertEqual(generated.count("DATEADD"), 3)
        self.assertEqual(
            rows,
            [
                (1, ts(2000, 2, 29), ts(1999, 1, 31), ts(2000, 3, 16)),
                (2, ts(2000, 3, 29), ts(1999, 2, 28), ts(2000, 4, 14)),
                (3, ts(1999, 1, 1), ts(1997, 12, 1), ts(1999, 1, 15)),
            ],
        )

    def test_predicate_on_a_shifted_date(self):
        sql = (
            "SELECT id FROM qx_d WHERE d <= CAST('2000-02-29' AS DATE) - INTERVAL '30' DAY "
            "OR d > CAST('2000-01-31' AS DATE) + INTERVAL '1' DAY ORDER BY id"
        )
        generated, rows = self.run_rewritten(sql)
        self.assertEqual(rows, [(2,), (3,)])

    def test_shifts_nest(self):
        generated, rows = self.run_rewritten(
            "SELECT id, d + INTERVAL '1' DAY - INTERVAL '1' DAY FROM qx_d ORDER BY id"
        )
        self.assertIn("DATEADD('d', -1, DATEADD('d', 1, d))", generated)
        self.assertEqual(rows, [(1, ts(2000, 1, 31)), (2, ts(2000, 2, 29)), (3, ts(1998, 12, 1))])


class TestSubstring(QuestDBExecutionCase):
    def test_from_for(self):
        generated, rows = self.run_rewritten(
            "SELECT id, SUBSTRING(s FROM 2 FOR 3) FROM qx_s ORDER BY id"
        )
        self.assertNotIn(" FROM 2", generated)
        # The values PostgreSQL returns, including NULL and the empty string.
        self.assertEqual(rows, [(1, "bcd"), (2, None), (3, ""), (4, "b")])

    def test_from_without_for_uses_the_length_of_the_string(self):
        sql = "SELECT id, SUBSTRING(s FROM 3) FROM qx_s ORDER BY id"
        generated, rows = self.run_rewritten(sql)
        self.assertIn("SUBSTRING(s, 3, LENGTH(s))", generated)
        self.assertEqual(rows, [(1, "cdef"), (2, None), (3, ""), (4, "")])
        # Without the length QuestDB does not know the function.
        self.assertIsNotNone(rejected(self.connection, "SELECT SUBSTRING(s, 3) FROM qx_s"))

    def test_start_before_the_string(self):
        generated, rows = self.run_rewritten(
            "SELECT id, SUBSTRING(s FROM 0 FOR 2) FROM qx_s ORDER BY id"
        )
        self.assertEqual(rows, [(1, "a"), (2, None), (3, ""), (4, "a")])

    def test_in_a_predicate_and_with_an_expression_argument(self):
        generated, rows = self.run_rewritten(
            "SELECT id FROM qx_s WHERE SUBSTRING(s FROM 1 FOR 2) IN ('ab', 'zz') "
            "AND SUBSTRING(UPPER(s) FROM 2) = 'BCDEF' ORDER BY id"
        )
        self.assertEqual(rows, [(1,)])


class TestCteColumnList(QuestDBExecutionCase):
    def test_column_list_is_not_accepted_as_written(self):
        error = rejected(self.connection, "WITH r (p, q) AS (SELECT k, v FROM qx_a) SELECT * FROM r")
        self.assertIsNotNone(error)
        self.assertIn("'as' expected", error)

    def test_names_come_from_the_column_list(self):
        generated, rows = self.run_rewritten(
            "WITH r (p, q) AS (SELECT k, SUM(v) FROM qx_a GROUP BY k) "
            "SELECT p, q FROM r WHERE q > 2 ORDER BY p"
        )
        self.assertNotIn("r(", generated)
        self.assertEqual(rows, [(20, 2.5), (30, 3.5), (40, 4.5)])

    def test_a_query_over_a_renamed_join_column(self):
        # The names must reach the outer query, not just disappear.
        generated, rows = self.run_rewritten(
            "WITH rev (supplier, total) AS (SELECT k, SUM(v) FROM qx_a GROUP BY k) "
            "SELECT b.id, rev.total FROM qx_b b, rev WHERE b.k = rev.supplier ORDER BY b.id"
        )
        self.assertEqual(rows, [(1, 1.5), (2, 2.5), (3, 2.5)])

    def test_nested_parentheses_and_later_ctes(self):
        generated, rows = self.run_rewritten(
            "WITH a (x) AS (SELECT (k + (v * 2)) FROM qx_a WHERE id = 1), "
            "b (p, q) AS (SELECT SUM(x * (1 - (x / 100))), (COUNT(*)) FROM a), "
            "c (r) AS (SELECT p FROM b) SELECT r FROM c"
        )
        self.assertNotIn("(x)", generated)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][0], 13 * (1 - 13 / 100))

    def test_union_names_come_from_the_first_branch(self):
        generated, rows = self.run_rewritten(
            "WITH r (p, q) AS (SELECT id, k FROM qx_a WHERE id < 2 UNION ALL "
            "SELECT id, k FROM qx_b WHERE id > 3) SELECT p, q FROM r ORDER BY p"
        )
        self.assertEqual(rows, [(1, 10), (4, 50)])


class TestComposition(QuestDBExecutionCase):
    def test_all_rewrites_in_one_query(self):
        generated, rows = self.run_rewritten(
            "WITH rev (sid, total) AS (SELECT k, SUM(v) FROM qx_a GROUP BY k) "
            "SELECT SUBSTRING(s.s FROM 1 FOR 2), rev.total, d.d + INTERVAL '1' MONTH "
            "FROM qx_s s, rev, qx_d d "
            "WHERE s.id = rev.sid / 10 AND d.id = s.id AND s.s IS NOT NULL ORDER BY s.id"
        )
        self.assertNotIn("INTERVAL", generated)
        self.assertEqual(generated.count("JOIN"), 2)
        self.assertEqual(rows, [("ab", 1.5, ts(2000, 2, 29)), ("", 3.5, ts(1999, 1, 1))])


if __name__ == "__main__":
    unittest.main()
