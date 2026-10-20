from sqlglot import ErrorLevel, exp, parse_one
from sqlglot.dialects.dialect import Dialect, Dialects
from sqlglot.dialects.postgres import Postgres
from tests.dialects.test_dialect import Validator


class TestNetezza(Validator):
    dialect = "netezza"

    def test_netezza(self):
        self.validate_identity("SELECT CAST(x AS INT) FROM t")
        self.validate_identity("SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b")
        self.validate_identity("SELECT d_date + 5, d_date - 30 FROM t")

        self.validate_all(
            "SELECT CAST(x AS TEXT) || 'a'",
            read={
                "netezza": "SELECT x::TEXT || 'a'",
                "postgres": "SELECT x::TEXT || 'a'",
            },
            write={
                "netezza": "SELECT CAST(x AS TEXT) || 'a'",
                "postgres": "SELECT CAST(x AS TEXT) || 'a'",
                "duckdb": "SELECT CAST(x AS TEXT) || 'a'",
                "spark": "SELECT CAST(x AS STRING) || 'a'",
            },
        )
        self.validate_all(
            "SELECT a FROM t ORDER BY a LIMIT 5 OFFSET 2",
            write={
                "netezza": "SELECT a FROM t ORDER BY a LIMIT 5 OFFSET 2",
                "postgres": "SELECT a FROM t ORDER BY a LIMIT 5 OFFSET 2",
                "sqlite": "SELECT a FROM t ORDER BY a NULLS LAST LIMIT 5 OFFSET 2",
            },
        )

    def test_dialect_is_registered(self):
        dialect = Dialect.get_or_raise("netezza")
        self.assertEqual(type(dialect).__name__, "Netezza")
        self.assertEqual(Dialects.NETEZZA.value, "netezza")
        self.assertIsInstance(dialect, Postgres)
        self.assertIs(Dialect.get_or_raise(Dialects.NETEZZA).__class__, type(dialect))
        self.assertIsInstance(parse_one("x::INT", read="netezza"), exp.Cast)

    def check(self, sql, writes):
        tree = parse_one(sql, read="netezza")
        for dialect, expected in writes.items():
            with self.subTest(sql=sql, dialect=dialect):
                self.assertEqual(tree.sql(dialect, unsupported_level=ErrorLevel.RAISE), expected)

    def test_sqlite_date_idioms(self):
        for sql in ("SELECT DATE('now')", "SELECT DATE('NOW')"):
            self.check(
                sql,
                {
                    "netezza": "SELECT CURRENT_DATE",
                    "sqlite": "SELECT CURRENT_DATE",
                    "duckdb": "SELECT CURRENT_DATE",
                    "postgres": "SELECT CURRENT_DATE",
                    "clickhouse": "SELECT CURRENT_DATE()",
                    "bigquery": "SELECT CURRENT_DATE",
                    "snowflake": "SELECT CURRENT_DATE",
                    "databricks": "SELECT CURRENT_DATE",
                    "doris": "SELECT CURRENT_DATE()",
                },
            )
        self.check(
            "SELECT DATE('now', '-90 days'), DATE('now', ' - 90 DAY ')",
            {
                "netezza": "SELECT CURRENT_DATE - INTERVAL '90 DAY', CURRENT_DATE - INTERVAL '90 DAY'",
                "sqlite": "SELECT DATE(CURRENT_DATE, '-90 DAY'), DATE(CURRENT_DATE, '-90 DAY')",
                "duckdb": "SELECT CURRENT_DATE - INTERVAL 90 DAY, CURRENT_DATE - INTERVAL 90 DAY",
                "clickhouse": "SELECT DATE_SUB(DAY, 90, CURRENT_DATE()), DATE_SUB(DAY, 90, CURRENT_DATE())",
                "bigquery": "SELECT DATE_SUB(CURRENT_DATE, INTERVAL 90 DAY), DATE_SUB(CURRENT_DATE, INTERVAL 90 DAY)",
                "snowflake": "SELECT DATEADD(DAY, 90 * -1, CURRENT_DATE), DATEADD(DAY, 90 * -1, CURRENT_DATE)",
                "databricks": "SELECT DATE_ADD(CURRENT_DATE, -90), DATE_ADD(CURRENT_DATE, -90)",
                "starrocks": "SELECT DATE_SUB(CURRENT_DATE, INTERVAL 90 DAY), DATE_SUB(CURRENT_DATE, INTERVAL 90 DAY)",
                "doris": "SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY), DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)",
            },
        )
        self.check(
            "SELECT DATE('now', '+1 month'), DATE('now', '2 years')",
            {
                "postgres": "SELECT CURRENT_DATE + INTERVAL '1 MONTH', CURRENT_DATE + INTERVAL '2 YEAR'",
                "sqlite": "SELECT DATE(CURRENT_DATE, '1 MONTH'), DATE(CURRENT_DATE, '2 YEAR')",
                "clickhouse": "SELECT DATE_ADD(MONTH, 1, CURRENT_DATE()), DATE_ADD(YEAR, 2, CURRENT_DATE())",
                "snowflake": "SELECT DATEADD(MONTH, 1, CURRENT_DATE), DATEADD(YEAR, 2, CURRENT_DATE)",
                "databricks": "SELECT DATEADD(MONTH, 1, CURRENT_DATE), DATEADD(YEAR, 2, CURRENT_DATE)",
            },
        )
        self.validate_identity(
            "SELECT DATE('now', 'weekday 0'), DATE(x), DATE('2024-01-01'), DATE('now', '-3 hours')"
        )

    def test_julianday_idiom(self):
        self.check(
            "SELECT JULIANDAY(a) - JULIANDAY(b)",
            {
                "sqlite": "SELECT CAST((JULIANDAY(a) - JULIANDAY(b)) AS INTEGER)",
                "duckdb": "SELECT DATE_DIFF('DAY', b, a)",
                "clickhouse": "SELECT DATE_DIFF(DAY, b, a)",
                "bigquery": "SELECT DATE_DIFF(a, b, DAY)",
                "snowflake": "SELECT DATEDIFF(DAY, b, a)",
                "databricks": "SELECT DATEDIFF(DAY, b, a)",
                "starrocks": "SELECT DATE_DIFF('DAY', a, b)",
                "doris": "SELECT DATEDIFF(a, b)",
                "postgres": "SELECT CAST(EXTRACT(epoch FROM CAST(a AS TIMESTAMP) - CAST(b AS TIMESTAMP)) / 86400 AS BIGINT)",
            },
        )
        self.check(
            "SELECT ABS(JULIANDAY(a) - JULIANDAY(b)) <= 30, "
            "JULIANDAY(DATE('now')) - JULIANDAY(MIN(d)), "
            "(JULIANDAY(a)) - (JULIANDAY(b)) - 1",
            {
                "duckdb": "SELECT ABS(DATE_DIFF('DAY', b, a)) <= 30, DATE_DIFF('DAY', MIN(d), CURRENT_DATE), DATE_DIFF('DAY', b, a) - 1",
                "clickhouse": "SELECT ABS(DATE_DIFF(DAY, b, a)) <= 30, DATE_DIFF(DAY, MIN(d), CURRENT_DATE()), DATE_DIFF(DAY, b, a) - 1",
            },
        )
        self.check(
            "SELECT JULIANDAY(x)",
            {
                "duckdb": "SELECT (DATE_DIFF('DAY', CAST('1970-01-01' AS DATE), x) + 2440588)",
                "sqlite": "SELECT (CAST((JULIANDAY(x) - JULIANDAY(DATE('1970-01-01'))) AS INTEGER) + 2440588)",
                "snowflake": "SELECT (DATEDIFF(DAY, CAST('1970-01-01' AS DATE), x) + 2440588)",
                "bigquery": "SELECT (DATE_DIFF(x, CAST('1970-01-01' AS DATE), DAY) + 2440588)",
            },
        )

        mixed = parse_one(
            "SELECT JULIANDAY(a) + 1 - JULIANDAY(b), JULIANDAY(a) * 2", read="netezza"
        )
        self.assertEqual(len(list(mixed.find_all(exp.DateDiff))), 3)
        self.assertEqual(
            mixed.sql("duckdb"),
            "SELECT (DATE_DIFF('DAY', CAST('1970-01-01' AS DATE), a) + 2440588) + 1 - "
            "(DATE_DIFF('DAY', CAST('1970-01-01' AS DATE), b) + 2440588), "
            "(DATE_DIFF('DAY', CAST('1970-01-01' AS DATE), a) + 2440588) * 2",
        )

    def test_other_dialects_keep_the_sqlite_idioms_as_functions(self):
        self.assertEqual(
            parse_one("SELECT DATE('now'), JULIANDAY(x) - JULIANDAY(y)", read="postgres").sql(
                "postgres"
            ),
            "SELECT DATE('now'), JULIANDAY(x) - JULIANDAY(y)",
        )
