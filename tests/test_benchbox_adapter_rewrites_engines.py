"""Execute the adapter rewrites (division, aliases, tables, flags, Databricks keys) on real engines.

Run by the ``in-process`` and ``postgres`` jobs of ``benchbox-engines.yml``; locally each
class skips when its engine is not available. BigQuery, Snowflake and managed Databricks
have no engine in CI, so their rewrites run as the shape another engine executes (see
``downstream/ENGINE-GAPS.md``).
"""

import unittest
import uuid
from decimal import Decimal

import sqlglot
from sqlglot import exp
from sqlglot.benchbox.rewrites import (
    deduplicate_output_aliases,
    flag_comparisons_to_boolean,
    safe_division,
    unqualified_tables,
)
from tests import engines
from tests.engines import requires_engine

DIVISION_SETUP = (
    "CREATE TEMP TABLE ratios (id INT, a DOUBLE PRECISION, b DOUBLE PRECISION)",
    "INSERT INTO ratios VALUES (1, 6, 3), (2, 6, 0), (3, NULL, 2), (4, 9, 4)",
)
DIVISION_SQL = "SELECT id, a / b AS q, (a + 1) / (b - b) AS z FROM ratios ORDER BY id"
EXPECTED_QUOTIENTS = [(1, 2.0), (2, None), (3, None), (4, 2.25)]


def numbers(rows):
    return [
        tuple(
            float(v) if isinstance(v, (Decimal, float, int)) and not isinstance(v, bool) else v
            for v in row
        )
        for row in rows
    ]


def rewritten(read, write, sql=DIVISION_SQL):
    return safe_division(sqlglot.parse_one(sql, read=read), read).sql(write)


def run(engine, setup, *statements):
    with engines.connect(engine) as connection:
        for statement in setup:
            connection.execute(statement)
        return [connection.execute(statement) for statement in statements]


class DivisionOnRelationalEngines:
    ENGINE = ""

    def setup_statements(self):
        return DIVISION_SETUP

    def assert_quotients(self, rows):
        self.assertEqual([(r[0], r[1]) for r in numbers(rows)], EXPECTED_QUOTIENTS)
        self.assertEqual([r[2] for r in rows], [None] * 4)

    def test_bigquery_safe_divide_shape(self):
        sql = rewritten("bigquery", self.ENGINE)
        self.assertIn("CASE WHEN", sql)
        (rows,) = run(self.ENGINE, self.setup_statements(), sql)
        self.assert_quotients(rows)

    def test_databricks_try_divide_shape(self):
        sql = rewritten("databricks", self.ENGINE)
        (rows,) = run(self.ENGINE, self.setup_statements(), sql)
        self.assert_quotients(rows)

    def test_snowflake_nullif_shape(self):
        sql = rewritten("snowflake", self.ENGINE)
        self.assertIn("NULLIF(b, 0)", sql)
        (rows,) = run(self.ENGINE, self.setup_statements(), sql)
        self.assert_quotients(rows)

    def test_clickhouse_nullif_shape(self):
        sql = rewritten("clickhouse", self.ENGINE)
        (rows,) = run(self.ENGINE, self.setup_statements(), sql)
        self.assert_quotients(rows)


@requires_engine("postgres")
class TestSafeDivisionOnPostgres(DivisionOnRelationalEngines, unittest.TestCase):
    ENGINE = "postgres"

    def test_division_by_zero_fails_without_the_rewrite(self):
        with engines.connect("postgres") as connection:
            for statement in DIVISION_SETUP:
                connection.execute(statement)
            with self.assertRaises(Exception) as raised:
                connection.execute("SELECT a / b FROM ratios")
            self.assertIn("division by zero", str(raised.exception))


@requires_engine("duckdb")
class TestSafeDivisionOnDuckDB(DivisionOnRelationalEngines, unittest.TestCase):
    ENGINE = "duckdb"


@requires_engine("pyspark")
class TestSafeDivisionOnSpark(unittest.TestCase):
    SETUP = (
        "SET spark.sql.ansi.enabled=true",
        "CREATE OR REPLACE TEMP VIEW ratios AS SELECT * FROM VALUES "
        "(1, 6.0D, 3.0D), (2, 6.0D, 0.0D), (3, CAST(NULL AS DOUBLE), 2.0D), (4, 9.0D, 4.0D) AS t(id, a, b)",
    )

    def test_try_divide_returns_null_where_ansi_division_fails(self):
        sql = rewritten("databricks", "databricks")
        self.assertIn("TRY_DIVIDE(a, b)", sql)
        with engines.connect("pyspark") as connection:
            for statement in self.SETUP:
                connection.execute(statement)
            try:
                with self.assertRaises(Exception) as raised:
                    connection.execute("SELECT a / b FROM ratios")
                self.assertIn("DIVIDE_BY_ZERO", str(raised.exception))
                rows = connection.execute(sql)
            finally:
                connection.execute("SET spark.sql.ansi.enabled=false")
        self.assertEqual([(r[0], r[1]) for r in rows], EXPECTED_QUOTIENTS)
        self.assertEqual([r[2] for r in rows], [None] * 4)


@requires_engine("chdb")
class TestSafeDivisionOnClickHouse(unittest.TestCase):
    NULL = "\\N"
    DECIMAL = (
        "SELECT x / CAST(y AS Nullable(Decimal(15, 2))) AS q FROM "
        "(SELECT 6 AS x, 0 AS y UNION ALL SELECT 6, 3 UNION ALL SELECT 9, 4) ORDER BY y"
    )

    def test_decimal_division_by_zero_fails_without_the_rewrite(self):
        with engines.connect("chdb") as connection:
            with self.assertRaises(Exception) as raised:
                connection.execute(self.DECIMAL)
            self.assertIn("Division by zero", str(raised.exception))

    def test_nullable_decimal_divisor_returns_null(self):
        sql = rewritten("clickhouse", "clickhouse", self.DECIMAL)
        self.assertIn("nullIf(CAST(y AS Nullable(Decimal(15, 2))), 0)", sql)
        (rows,) = run("chdb", (), sql)
        self.assertEqual(rows, [(self.NULL,), ("2",), ("2.25",)])

    def test_plain_integer_and_float_divisors(self):
        sql = rewritten(
            "clickhouse",
            "clickhouse",
            "SELECT x / y, x / (y - y), x / 2 FROM (SELECT 6 AS x, 3 AS y)",
        )
        (rows,) = run("chdb", (), sql)
        self.assertEqual(rows, [("2", self.NULL, "3")])


@requires_engine("duckdb")
class TestDeduplicateOutputAliasesOnDuckDB(unittest.TestCase):
    def test_rewritten_query_executes_and_names_stay_unique(self):
        sql = "SELECT a AS x, b AS x, c AS x, a, t.a FROM (SELECT 1 AS a, 2 AS b, 3 AS c) AS t"
        tree = deduplicate_output_aliases(sqlglot.parse_one(sql, read="databricks"))
        text = tree.sql("duckdb")
        self.assertIn("b AS x_2", text)
        _, columns, rows = run(
            "duckdb",
            (f"CREATE TABLE out_t AS {text}",),
            "SELECT 1",
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'out_t' ORDER BY ordinal_position",
            "SELECT * FROM out_t",
        )
        self.assertEqual(columns, [("x",), ("x_2",), ("x_3",), ("a",), ("a_2",)])
        self.assertEqual(rows, [(1, 2, 3, 1, 1)])


@requires_engine("postgres")
class TestDeduplicateOutputAliasesOnPostgres(unittest.TestCase):
    def test_duplicate_names_fail_without_the_rewrite(self):
        sql = "SELECT a AS x, b AS x FROM (SELECT 1 AS a, 2 AS b) AS t"
        text = deduplicate_output_aliases(sqlglot.parse_one(sql, read="databricks")).sql("postgres")
        with engines.connect("postgres") as connection:
            with self.assertRaises(Exception) as raised:
                connection.execute(f"CREATE TEMP TABLE dup_plain AS {sql}")
            self.assertIn("specified more than once", str(raised.exception))
            connection.execute(f"CREATE TEMP TABLE dup_fixed AS {text}")
            self.assertEqual(connection.execute("SELECT x, x_2 FROM dup_fixed"), [(1, 2)])


@requires_engine("pyspark")
class TestDeduplicateOutputAliasesOnSpark(unittest.TestCase):
    SQL = "SELECT a AS x, b AS x FROM VALUES (1, 2) AS t(a, b)"

    def test_ambiguous_reference_fails_without_the_rewrite(self):
        with engines.connect("pyspark") as connection:
            with self.assertRaises(Exception) as raised:
                connection.execute(f"SELECT x FROM ({self.SQL})")
            self.assertIn("AMBIGUOUS_REFERENCE", str(raised.exception))
            with self.assertRaises(Exception) as raised:
                connection.execute(f"CREATE TABLE dup_spark_plain USING parquet AS {self.SQL}")
            self.assertIn("COLUMN_ALREADY_EXISTS", str(raised.exception))

    def test_nested_duplicates_are_renamed_where_an_outer_query_reads_them(self):
        sql = f"SELECT x, x_2 FROM ({self.SQL})"
        broken = f"SELECT x FROM ({self.SQL})"
        text = deduplicate_output_aliases(sqlglot.parse_one(broken, read="databricks")).sql(
            "databricks"
        )
        self.assertIn("b AS x_2", text)
        self.assertEqual(run("pyspark", (), text), [[(1,)]])
        text = deduplicate_output_aliases(sqlglot.parse_one(sql, read="databricks")).sql(
            "databricks"
        )
        self.assertEqual(run("pyspark", (), text), [[(1, 2)]])

    def test_rewritten_query_is_usable_from_an_outer_query_and_ctas(self):
        text = deduplicate_output_aliases(sqlglot.parse_one(self.SQL, read="databricks")).sql(
            "databricks"
        )
        table = f"dup_spark_{uuid.uuid4().hex[:8]}"
        with engines.connect("pyspark") as connection:
            try:
                self.assertEqual(connection.execute(f"SELECT x, x_2 FROM ({text})"), [(1, 2)])
                connection.execute(f"CREATE TABLE {table} USING parquet AS {text}")
                self.assertEqual(connection.execute(f"SELECT x, x_2 FROM {table}"), [(1, 2)])
            finally:
                connection.execute(f"DROP TABLE IF EXISTS {table}")


class QualifyTables:
    ENGINE = ""
    DIALECT = ""
    QUERY = (
        "WITH orders AS (SELECT 1 AS k, 10 AS v), "
        "totals AS (SELECT SUM(l_qty) AS q FROM lineitem) "
        "SELECT o.k, t.q FROM orders AS o CROSS JOIN totals AS t "
        "WHERE o.v IN (SELECT n FROM nation)"
    )

    def prepare(self, connection, schema):
        connection.execute(f"CREATE SCHEMA {schema}")
        connection.execute(f"CREATE TABLE {schema}.lineitem (l_qty INT)")
        connection.execute(f"INSERT INTO {schema}.lineitem VALUES (4), (6)")
        connection.execute(f"CREATE TABLE {schema}.nation (n INT)")
        connection.execute(f"INSERT INTO {schema}.nation VALUES (10), (20)")

    def test_qualified_query_runs_where_unqualified_fails(self):
        schema = f"bench_{uuid.uuid4().hex[:8]}"
        tree = sqlglot.parse_one(self.QUERY, read=self.DIALECT)
        nodes = unqualified_tables(tree)
        self.assertEqual(sorted(node.name for node in nodes), ["lineitem", "nation"])
        for node in nodes:
            node.set("db", exp.to_identifier(schema))
        with engines.connect(self.ENGINE) as connection:
            try:
                self.prepare(connection, schema)
                with self.assertRaises(Exception):
                    connection.execute(self.QUERY)
                self.assertEqual(connection.execute(tree.sql(self.DIALECT)), [(1, 10)])
            finally:
                connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@requires_engine("duckdb")
class TestUnqualifiedTablesOnDuckDB(QualifyTables, unittest.TestCase):
    ENGINE = "duckdb"
    DIALECT = "duckdb"


@requires_engine("postgres")
class TestUnqualifiedTablesOnPostgres(QualifyTables, unittest.TestCase):
    ENGINE = "postgres"
    DIALECT = "postgres"


class FlagColumns:
    ENGINE = ""
    SETUP = (
        "CREATE TEMP TABLE dim (id INT, IsCurrent BOOLEAN, HolidayFlag BOOLEAN)",
        "INSERT INTO dim VALUES (1, TRUE, FALSE), (2, FALSE, FALSE), (3, TRUE, TRUE)",
    )
    QUERY = (
        "SELECT id FROM dim d WHERE d.IsCurrent = 1 AND HolidayFlag = 0 "
        "OR CASE WHEN IsCurrent = 0 THEN 1 ELSE 0 END = 1 ORDER BY id"
    )

    def rewrite(self, use_is):
        tree = sqlglot.parse_one(self.QUERY, read="postgres")
        return flag_comparisons_to_boolean(tree, ["IsCurrent", "HolidayFlag"], use_is=use_is).sql(
            self.ENGINE
        )

    def test_equals_and_is_forms_select_the_same_rows(self):
        equals, is_form = self.rewrite(False), self.rewrite(True)
        self.assertIn("d.IsCurrent = TRUE", equals)
        self.assertIn("HolidayFlag IS FALSE", is_form)
        equals_rows, is_rows = run(self.ENGINE, self.SETUP, equals, is_form)
        self.assertEqual(equals_rows, [(1,), (2,)])
        self.assertEqual(is_rows, equals_rows)


@requires_engine("postgres")
class TestFlagColumnsOnPostgres(FlagColumns, unittest.TestCase):
    ENGINE = "postgres"

    def test_integer_comparison_is_rejected_without_the_rewrite(self):
        with engines.connect("postgres") as connection:
            for statement in self.SETUP:
                connection.execute(statement)
            with self.assertRaises(Exception) as raised:
                connection.execute(self.QUERY)
            self.assertIn("boolean = integer", str(raised.exception))


@requires_engine("duckdb")
class TestFlagColumnsOnDuckDB(FlagColumns, unittest.TestCase):
    ENGINE = "duckdb"


@requires_engine("pyspark")
class TestDatabricksPrimaryKeyOnSpark(unittest.TestCase):
    def create(self, dialect, suffix):
        tree = sqlglot.parse_one(f"CREATE TABLE pk_{suffix} (a INT NOT NULL, b INT NOT NULL)")
        tree.this.append(
            "expressions",
            exp.PrimaryKey(
                expressions=[
                    exp.Ordered(this=exp.column("a"), desc=False, nulls_first=False),
                    exp.Ordered(this=exp.column("b"), desc=True, nulls_first=False),
                ]
            ),
        )
        return tree.sql(dialect)

    def parse_error(self, sql):
        with engines.connect("pyspark") as connection:
            try:
                connection.execute(sql)
            except Exception as e:
                return "PARSE_SYNTAX_ERROR" in str(e)
        return False

    def test_primary_key_without_ordering_is_valid_syntax(self):
        suffix = uuid.uuid4().hex[:8]
        databricks = self.create("databricks", suffix)
        self.assertEqual(
            databricks,
            f"CREATE TABLE pk_{suffix} (a INT NOT NULL, b INT NOT NULL, PRIMARY KEY (a, b))",
        )
        self.assertFalse(self.parse_error(databricks))
        self.addCleanup(lambda: run("pyspark", (), f"DROP TABLE IF EXISTS pk_{suffix}"))

    def test_ordering_in_a_primary_key_is_a_syntax_error(self):
        spark = self.create("spark", uuid.uuid4().hex[:8])
        self.assertIn("NULLS LAST", spark)
        self.assertTrue(self.parse_error(spark))
