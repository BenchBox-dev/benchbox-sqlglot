"""Pin the SQL BenchBox gets from the ``athena`` and ``singlestore`` dialects.

BenchBox generates Athena workload queries in the ``athena`` dialect and SingleStore workload queries in
the ``singlestore`` dialect. The text checks run everywhere. ``TestAthenaQueriesOnTrino`` executes the
Athena output on Trino (``trino`` job of ``benchbox-engines.yml``; Athena v3 is Trino based). SingleStore
has no engine in CI, so its output is golden text only (``downstream/ENGINE-GAPS.md``).
"""

import unittest
from decimal import Decimal

import sqlglot
from tests import engines
from tests.engines import requires_engine

TPCH_Q1_SHAPE = """
select l_returnflag, l_linestatus, sum(l_quantity) as sum_qty, count(*) as count_order
from lineitem
where l_shipdate <= date '1998-12-01' - interval '90' day
group by l_returnflag, l_linestatus
order by l_returnflag, l_linestatus
"""

TPCH_Q2_SHAPE = """
select s_name, count(*) as numwait
from supplier
where s_acctbal > 0
group by s_name
order by numwait desc, s_name
limit 100
"""

DATE_WINDOW = """
select d_date_sk from date_dim
where d_date between cast('2000-02-01' as date) and (cast('2000-02-01' as date) + interval '60' day)
order by d_date_sk
"""


def transpile(sql, write, read="netezza"):
    return sqlglot.transpile(sql, read=read, write=write, identify=True)[0]


class TestSingleStoreNativeDialect(unittest.TestCase):
    def test_date_cast_uses_the_native_cast_operator(self):
        self.assertEqual(
            transpile(TPCH_Q1_SHAPE, "singlestore"),
            "SELECT `l_returnflag`, `l_linestatus`, SUM(`l_quantity`) AS `sum_qty`, COUNT(*) AS `count_order` "
            "FROM `lineitem` WHERE `l_shipdate` <= '1998-12-01' :> DATE - INTERVAL '90' DAY "
            "GROUP BY `l_returnflag`, `l_linestatus` "
            "ORDER BY `l_returnflag` NULLS LAST, `l_linestatus` NULLS LAST",
        )

    def test_null_ordering_is_written_not_emulated(self):
        self.assertEqual(
            transpile(TPCH_Q2_SHAPE, "singlestore"),
            "SELECT `s_name`, COUNT(*) AS `numwait` FROM `supplier` WHERE `s_acctbal` > 0 GROUP BY `s_name` "
            "ORDER BY `numwait` DESC NULLS FIRST, `s_name` NULLS LAST LIMIT 100",
        )

    def test_interval_bounds_keep_the_native_cast(self):
        sql = transpile(DATE_WINDOW, "singlestore")
        self.assertIn("'2000-02-01' :> DATE", sql)
        self.assertNotIn("CAST(", sql)
        self.assertNotIn("CASE WHEN", sql)

    def test_json_array_indexes_are_integers(self):
        self.assertEqual(
            transpile("SELECT JSON_EXTRACT(c, '$.a.preferences[0]') FROM t", "singlestore", read="mysql"),
            "SELECT JSON_EXTRACT_JSON(`c`, 'a', 'preferences', 0) FROM `t`",
        )
        self.assertEqual(
            transpile("SELECT c->'$.a[1].b' FROM t", "singlestore", read="mysql"),
            "SELECT JSON_EXTRACT_JSON(`c`, 'a', 1, 'b') FROM `t`",
        )

    def test_mysql_dialect_still_emulates_what_singlestore_writes_natively(self):
        sql = transpile(TPCH_Q2_SHAPE, "mysql")
        self.assertIn("CASE WHEN", sql)
        self.assertNotIn("NULLS", sql)


class TestAthenaNativeDialect(unittest.TestCase):
    def test_query_text_equals_trino(self):
        for sql in (TPCH_Q1_SHAPE, TPCH_Q2_SHAPE, DATE_WINDOW):
            with self.subTest(sql=sql.strip().splitlines()[0]):
                self.assertEqual(transpile(sql, "athena"), transpile(sql, "trino"))

    def test_query_text(self):
        self.assertEqual(
            transpile(TPCH_Q2_SHAPE, "athena"),
            'SELECT "s_name", COUNT(*) AS "numwait" FROM "supplier" WHERE "s_acctbal" > 0 GROUP BY "s_name" '
            'ORDER BY "numwait" DESC NULLS FIRST, "s_name" LIMIT 100',
        )

    def test_ddl_is_hive_not_trino(self):
        ddl = "CREATE TABLE t (a INT NOT NULL, b VARCHAR(10))"
        self.assertEqual(
            sqlglot.transpile(ddl, read="postgres", write="athena", identify=True)[0],
            "CREATE TABLE `t` (`a` INT NOT NULL, `b` VARCHAR(10))",
        )
        self.assertEqual(
            sqlglot.transpile(ddl, read="postgres", write="trino", identify=True)[0],
            'CREATE TABLE "t" ("a" INTEGER NOT NULL, "b" VARCHAR(10))',
        )


LINEITEM = """
(SELECT * FROM (VALUES
  ('A', 'F', CAST(17.00 AS DECIMAL(15,2)), CAST('1996-03-13' AS DATE)),
  ('A', 'F', CAST(36.00 AS DECIMAL(15,2)), CAST('1996-04-12' AS DATE)),
  ('N', 'O', CAST(45.00 AS DECIMAL(15,2)), CAST('1997-11-30' AS DATE)),
  ('N', 'O', CAST(30.00 AS DECIMAL(15,2)), CAST('1998-08-19' AS DATE)),
  ('R', 'F', CAST(3.00 AS DECIMAL(15,2)), CAST('1995-06-01' AS DATE)),
  ('R', 'F', CAST(9.00 AS DECIMAL(15,2)), CAST('1998-12-01' AS DATE))
) AS v(l_returnflag, l_linestatus, l_quantity, l_shipdate))
"""

SUPPLIER = """
(SELECT * FROM (VALUES
  ('s1', CAST(5.0 AS DECIMAL(15,2))), ('s1', CAST(7.0 AS DECIMAL(15,2))),
  ('s2', CAST(1.0 AS DECIMAL(15,2))), ('s3', CAST(-4.0 AS DECIMAL(15,2))),
  ('s3', CAST(2.0 AS DECIMAL(15,2))), ('s4', CAST(9.0 AS DECIMAL(15,2)))
) AS v(s_name, s_acctbal))
"""

DATE_DIM = """
(SELECT * FROM (VALUES
  (1, CAST('2000-01-31' AS DATE)), (2, CAST('2000-02-01' AS DATE)),
  (3, CAST('2000-03-31' AS DATE)), (4, CAST('2000-04-01' AS DATE)), (5, CAST('2000-04-02' AS DATE))
) AS v(d_date_sk, d_date))
"""


def with_tables(sql):
    return f'WITH "lineitem" AS {LINEITEM}, "supplier" AS {SUPPLIER}, "date_dim" AS {DATE_DIM} {sql}'


def normalize(rows):
    return [tuple(float(v) if isinstance(v, Decimal) else v for v in row) for row in rows]


@requires_engine("trino")
class TestAthenaQueriesOnTrino(unittest.TestCase):
    def run_athena(self, sql):
        return normalize(engines.execute("trino", with_tables(transpile(sql, "athena"))))

    def test_date_interval_aggregate(self):
        self.assertEqual(
            self.run_athena(TPCH_Q1_SHAPE),
            [("A", "F", 53.0, 2), ("N", "O", 75.0, 2), ("R", "F", 3.0, 1)],
        )

    def test_null_ordering_and_limit(self):
        self.assertEqual(
            self.run_athena(TPCH_Q2_SHAPE),
            [("s1", 2), ("s2", 1), ("s3", 1), ("s4", 1)],
        )

    def test_date_window(self):
        self.assertEqual(self.run_athena(DATE_WINDOW), [(2,), (3,), (4,)])

    def test_athena_and_trino_text_execute_to_the_same_rows(self):
        for sql in (TPCH_Q1_SHAPE, TPCH_Q2_SHAPE, DATE_WINDOW):
            with self.subTest(sql=sql.strip().splitlines()[0]):
                self.assertEqual(
                    self.run_athena(sql),
                    normalize(engines.execute("trino", with_tables(transpile(sql, "trino")))),
                )


if __name__ == "__main__":
    unittest.main()
