"""``SELECT unnest(generate_series(lo, hi)) AS n`` on engines that cannot run it as written.

``lower_series_selects`` is exercised on the transactional scripts' shape (an ``INSERT ... SELECT`` over a
derived table that counts) for every dialect it supports. The text assertions pin each form; SQLite,
DuckDB (the reference), PostgreSQL, Doris, StarRocks and QuestDB execute it and return the same rows.
Snowflake, BigQuery and SingleStore have no engine in CI (``downstream/ENGINE-GAPS.md``), so their
forms are asserted as text, and SingleStore's digit cross join is also executed on DuckDB.
"""

import unittest

import sqlglot
from sqlglot import exp
from sqlglot.benchbox.series import SERIES_DIALECTS, lower_series_selects
from tests import engines
from tests.engines import requires_engine

SERIES = "SELECT unnest(generate_series(1, 5)) AS n"
INSERT = (
    "INSERT INTO target SELECT 100 + n, 'x' || CAST(n AS VARCHAR) FROM "
    "(SELECT unnest(generate_series(1, 5)) AS n) t"
)
EXPECTED_ROWS = [(101, "x1"), (102, "x2"), (103, "x3"), (104, "x4"), (105, "x5")]


def lowered(sql, dialect, read="duckdb"):
    return lower_series_selects(sqlglot.parse_one(sql, read=read), dialect).sql(dialect)


class TestForms(unittest.TestCase):
    def test_sqlite_recursive_cte(self):
        self.assertEqual(
            lowered(SERIES, "sqlite"),
            "WITH RECURSIVE _series(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM _series "
            "WHERE n < 5) SELECT n FROM _series",
        )

    def test_doris_numbers(self):
        self.assertEqual(
            lowered(SERIES, "doris"), "SELECT number + 1 AS n FROM NUMBERS('number' = 5)"
        )
        self.assertEqual(
            lowered("SELECT unnest(generate_series(10, 109)) AS k", "doris"),
            "SELECT number + 10 AS k FROM NUMBERS('number' = 100)",
        )
        self.assertEqual(
            lowered("SELECT unnest(generate_series(-2, 2)) AS k", "doris"),
            "SELECT number + -2 AS k FROM NUMBERS('number' = 5)",
        )

    def test_doris_needs_integer_literal_bounds(self):
        for sql in (
            "SELECT unnest(generate_series(1, x)) AS n",
            "SELECT unnest(generate_series(1.5, 3)) AS n",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                lowered(sql, "doris")

    def test_singlestore_digit_cross_join(self):
        sql = lowered(SERIES, "singlestore")
        self.assertTrue(
            sql.startswith(
                "SELECT n AS n FROM (SELECT a.d + 10 * b.d + 100 * c.d + 1000 * e.d AS n FROM (SELECT 0 AS d UNION ALL"
            ),
            sql,
        )
        self.assertTrue(sql.endswith(") AS e) AS _digits WHERE n BETWEEN 1 AND 5"), sql)
        self.assertEqual(sql.count("UNION ALL"), 36)
        self.assertNotIn("GENERATE_SERIES", sql.upper())

    def test_table_function_forms(self):
        for dialect, expected in {
            "snowflake": "SELECT CAST(value AS INT) AS n FROM TABLE(FLATTEN(ARRAY_GENERATE_RANGE(1, 5 + 1)))",
            "bigquery": "SELECT n FROM UNNEST(GENERATE_ARRAY(1, 5)) AS n",
            "questdb": "SELECT generate_series AS n FROM GENERATE_SERIES(1, 5)",
            "starrocks": "SELECT n FROM TABLE(GENERATE_SERIES(1, 5)) AS series(n)",
        }.items():
            with self.subTest(dialect=dialect):
                self.assertEqual(lowered(SERIES, dialect), expected)

    def test_postgres_drops_the_unnest(self):
        self.assertEqual(lowered(SERIES, "postgres"), "SELECT GENERATE_SERIES(1, 5) AS n")
        self.assertEqual(
            lowered(
                "SELECT UNNEST(GENERATE_SERIES(1, 5)) AS n, 'a' AS c FROM t", "postgres", "postgres"
            ),
            "SELECT GENERATE_SERIES(1, 5) AS n, 'a' AS c FROM t",
        )

    def test_postgres_expression_bounds_and_naked_unnest(self):
        self.assertEqual(
            lowered("SELECT unnest(generate_series(1, n + 1)) FROM t", "postgres"),
            "SELECT GENERATE_SERIES(1, n + 1) FROM t",
        )

    def test_the_alias_and_bounds_come_from_the_query(self):
        self.assertEqual(
            lowered("SELECT unnest(generate_series(3, 7)) AS idx", "sqlite"),
            "WITH RECURSIVE _series(idx) AS (SELECT 3 UNION ALL SELECT idx + 1 FROM _series "
            "WHERE idx < 7) SELECT idx FROM _series",
        )
        self.assertEqual(
            lowered("SELECT unnest(generate_series(lo, hi - 1)) AS idx", "bigquery"),
            "SELECT idx FROM UNNEST(GENERATE_ARRAY(lo, hi - 1)) AS idx",
        )
        self.assertEqual(
            lowered("SELECT unnest(generate_series(1, a + b)) AS i", "snowflake"),
            "SELECT CAST(value AS INT) AS i FROM TABLE(FLATTEN(ARRAY_GENERATE_RANGE(1, a + b + 1)))",
        )

    def test_inside_a_statement(self):
        self.assertEqual(
            lowered(INSERT, "sqlite"),
            "INSERT INTO target SELECT 100 + n, 'x' || CAST(n AS TEXT) FROM (WITH RECURSIVE _series(n) AS "
            "(SELECT 1 UNION ALL SELECT n + 1 FROM _series WHERE n < 5) SELECT n FROM _series) AS t",
        )

    def test_two_series_in_one_statement(self):
        sql = lowered(
            "SELECT a.n, b.m FROM (SELECT unnest(generate_series(1, 2)) AS n) a, "
            "(SELECT unnest(generate_series(5, 6)) AS m) b",
            "sqlite",
        )
        self.assertEqual(sql.count("WITH RECURSIVE"), 2)
        self.assertIn("_series(n)", sql)
        self.assertIn("_series(m)", sql)

    def test_in_a_cte_and_a_set_operation(self):
        sql = lowered(
            "WITH s AS (SELECT unnest(generate_series(1, 3)) AS n) SELECT n FROM s UNION ALL "
            "SELECT * FROM (SELECT unnest(generate_series(7, 8)) AS n)",
            "bigquery",
        )
        self.assertNotIn("EXPLODE", sql.upper())
        self.assertEqual(sql.count("GENERATE_ARRAY"), 2)

    def test_other_shapes_are_left_alone(self):
        for sql in (
            "SELECT unnest(generate_series(1, 5, 2)) AS n",
            "SELECT unnest(generate_series(1, 5))",
            "SELECT unnest(generate_series(1, 5)) AS n FROM t",
            "SELECT unnest(generate_series(1, 5)) AS n WHERE n > 2",
            "SELECT unnest(generate_series(1, 5)) AS n, 2 AS m",
            "SELECT unnest(range(1, 5)) AS n",
            "SELECT n FROM generate_series(1, 5) AS t(n)",
        ):
            with self.subTest(sql=sql):
                tree = sqlglot.parse_one(sql, read="duckdb")
                self.assertEqual(lower_series_selects(tree, "sqlite"), tree)

    def test_copy_and_in_place(self):
        tree = sqlglot.parse_one(INSERT, read="duckdb")
        before = tree.sql("duckdb")
        lower_series_selects(tree, "sqlite")
        self.assertEqual(tree.sql("duckdb"), before)
        self.assertIs(lower_series_selects(tree, "sqlite", copy=False), tree)
        self.assertNotEqual(tree.sql("duckdb"), before)

    def test_a_dialect_without_a_form_raises(self):
        for dialect in ("duckdb", "spark", "mysql", "clickhouse", "netezza"):
            with self.subTest(dialect=dialect), self.assertRaises(ValueError):
                lowered(SERIES, dialect)

    def test_dialect_objects_are_accepted(self):
        from sqlglot.dialects.dialect import Dialect

        self.assertEqual(
            lower_series_selects(
                sqlglot.parse_one(SERIES, read="duckdb"), Dialect.get_or_raise("sqlite")
            ).sql("sqlite"),
            lowered(SERIES, "sqlite"),
        )

    def test_supported_dialects(self):
        self.assertEqual(
            SERIES_DIALECTS,
            {
                "sqlite",
                "doris",
                "starrocks",
                "singlestore",
                "snowflake",
                "bigquery",
                "questdb",
                "postgres",
            },
        )

    def test_current_output_is_not_a_form_the_engines_run(self):
        # Without the rewrite these dialects write a function the engine does not have
        for dialect in ("sqlite", "doris", "starrocks", "singlestore"):
            with self.subTest(dialect=dialect):
                self.assertIn(
                    "EXPLODE(GENERATE_SERIES(1, 5))",
                    sqlglot.transpile(SERIES, read="duckdb", write=dialect)[0],
                )


class TestStandardTranslation(unittest.TestCase):
    """The rest of a transactional script needs no rewrite: the fork's own output is what the engines run."""

    def statements(self, sql, dialect):
        return [e.sql(dialect) for e in sqlglot.parse(sql, read="duckdb") if e is not None]

    def test_begin_and_commit(self):
        script = "BEGIN TRANSACTION; DELETE FROM t WHERE a = 1; COMMIT;"
        for dialect in ("doris", "starrocks", "singlestore"):
            with self.subTest(dialect=dialect):
                self.assertEqual(
                    self.statements(script, dialect),
                    ["BEGIN", "DELETE FROM t WHERE a = 1", "COMMIT"],
                )

    def test_singlestore_temporary_table_and_typed_literal(self):
        self.assertEqual(
            self.statements(
                "CREATE TEMP TABLE tmp AS SELECT DATE '1998-01-01' AS d", "singlestore"
            ),
            ["CREATE TEMPORARY TABLE tmp AS SELECT '1998-01-01' :> DATE AS d"],
        )

    def test_sqlite_typed_literals(self):
        self.assertEqual(
            self.statements("SELECT DATE '1998-01-01', TIMESTAMP '1998-01-01 10:00:00'", "sqlite"),
            ["SELECT DATE('1998-01-01'), DATETIME('1998-01-01 10:00:00')"],
        )


class SeriesExecution:
    ENGINE = ""
    DIALECT = ""
    CREATE = "CREATE TABLE target (k INT, v VARCHAR(10))"
    SETUP = ()

    def connect(self):
        connection = engines.connect(self.ENGINE)
        for statement in self.SETUP:
            connection.execute(statement)
        return connection

    def lowered_insert(self, sql=INSERT):
        tree = lower_series_selects(sqlglot.parse_one(sql, read="duckdb"), self.DIALECT)
        return tree.sql(self.DIALECT)

    def test_insert_from_a_series(self):
        with self.connect() as connection:
            connection.execute("DROP TABLE IF EXISTS target")
            connection.execute(self.CREATE)
            connection.execute(self.lowered_insert())
            rows = connection.execute("SELECT k, v FROM target ORDER BY k")
        self.assertEqual([(int(k), v) for k, v in rows], EXPECTED_ROWS)

    def test_larger_and_offset_series(self):
        with self.connect() as connection:
            connection.execute("DROP TABLE IF EXISTS target")
            connection.execute(self.CREATE)
            connection.execute(
                self.lowered_insert(
                    "INSERT INTO target SELECT n, 'v' FROM (SELECT unnest(generate_series(1, 1000)) AS n) t"
                )
            )
            connection.execute(
                self.lowered_insert(
                    "INSERT INTO target SELECT n, 'w' FROM (SELECT unnest(generate_series(5001, 5010)) AS n) t"
                )
            )
            ((count, low, high, total),) = connection.execute(
                "SELECT COUNT(*), MIN(k), MAX(k), SUM(k) FROM target"
            )
        self.assertEqual(
            (int(count), int(low), int(high), int(total)),
            (1010, 1, 5010, 500500 + sum(range(5001, 5011))),
        )

    def test_a_single_row_series(self):
        with self.connect() as connection:
            rows = connection.execute(
                self.lowered_insert("SELECT n FROM (SELECT unnest(generate_series(7, 7)) AS n) t")
            )
        self.assertEqual([int(r[0]) for r in rows], [7])


@requires_engine("sqlite")
class TestSQLiteExecution(SeriesExecution, unittest.TestCase):
    ENGINE = DIALECT = "sqlite"

    def test_the_source_returns_the_same_rows(self):
        # DuckDB, the source dialect, is the reference when it is available
        if not engines.is_available("duckdb"):
            self.skipTest("duckdb is not available")
        with (
            engines.connect("duckdb") as duckdb_connection,
            engines.connect("sqlite") as connection,
        ):
            expected = duckdb_connection.execute(
                "SELECT n FROM (SELECT unnest(generate_series(3, 40)) AS n) t ORDER BY n"
            )
            rows = connection.execute(
                lowered(
                    "SELECT n FROM (SELECT unnest(generate_series(3, 40)) AS n) t ORDER BY n",
                    "sqlite",
                )
            )
        self.assertEqual([int(r[0]) for r in rows], [int(r[0]) for r in expected])


@requires_engine("postgres")
class TestPostgresExecution(SeriesExecution, unittest.TestCase):
    ENGINE = DIALECT = "postgres"


@requires_engine("doris")
class TestDorisExecution(SeriesExecution, unittest.TestCase):
    ENGINE = DIALECT = "doris"
    SETUP = ("CREATE DATABASE IF NOT EXISTS series_tests", "USE series_tests")
    CREATE = (
        "CREATE TABLE target (k INT, v VARCHAR(10)) DUPLICATE KEY(k) DISTRIBUTED BY HASH(k) "
        "BUCKETS 1 PROPERTIES ('replication_num' = '1')"
    )


@requires_engine("starrocks")
class TestStarRocksExecution(SeriesExecution, unittest.TestCase):
    ENGINE = DIALECT = "starrocks"
    SETUP = ("CREATE DATABASE IF NOT EXISTS series_tests", "USE series_tests")
    CREATE = (
        "CREATE TABLE target (k INT, v VARCHAR(10)) DUPLICATE KEY(k) DISTRIBUTED BY HASH(k) "
        "BUCKETS 1 PROPERTIES ('replication_num' = '1')"
    )


@requires_engine("questdb")
class TestQuestDBExecution(unittest.TestCase):
    def test_series_select(self):
        with engines.connect("questdb") as connection:
            rows = connection.execute(
                lowered(
                    "SELECT n FROM (SELECT unnest(generate_series(1, 5)) AS n) t ORDER BY n",
                    "questdb",
                )
            )
        self.assertEqual([int(r[0]) for r in rows], [1, 2, 3, 4, 5])


@requires_engine("duckdb")
class TestSingleStoreDigitJoinOnDuckDB(unittest.TestCase):
    """SingleStore has no engine in CI: the digit cross join is plain SQL, so DuckDB runs it."""

    def test_rows_equal_the_source_series(self):
        sql = "SELECT n FROM (SELECT unnest(generate_series({lo}, {hi})) AS n) t ORDER BY n"
        with engines.connect("duckdb") as connection:
            for lo, hi in ((1, 100), (0, 0), (250, 1500), (9990, 9999)):
                with self.subTest(lo=lo, hi=hi):
                    expected = connection.execute(sql.format(lo=lo, hi=hi))
                    rows = connection.execute(lowered(sql.format(lo=lo, hi=hi), "singlestore"))
                    self.assertEqual(rows, expected)
                    self.assertEqual(len(rows), hi - lo + 1)

    def test_lowered_text_is_valid_for_the_dialect(self):
        sql = lowered(SERIES, "singlestore")
        self.assertEqual(
            sqlglot.parse_one(sql, read="singlestore").find(exp.Between).sql(), "n BETWEEN 1 AND 5"
        )


if __name__ == "__main__":
    unittest.main()
