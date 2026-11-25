import sqlite3
import unittest
from contextlib import closing

from sqlglot import ErrorLevel, ParseError, UnsupportedError, exp, parse_one
from tests import engines
from tests.engines import requires_engine
from sqlglot.generator import logger as generator_logger
from sqlglot.helper import logger as helper_logger
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.parser import logger as parser_logger
from tests.dialects.test_dialect import Validator


class TestSQLite(Validator):
    dialect = "sqlite"

    def test_variadic_iif(self):
        for name in ("IIF", "IF"):
            self.validate_all(
                f"SELECT {name}(a, b, c, d)",
                write={
                    "sqlite": "SELECT CASE WHEN a THEN b WHEN c THEN d END",
                    "duckdb": "SELECT CASE WHEN a THEN b WHEN c THEN d END",
                    "postgres": "SELECT CASE WHEN a THEN b WHEN c THEN d END",
                },
            )
            self.validate_all(
                f"SELECT {name}(a, b, c, d, e)",
                write={"sqlite": "SELECT CASE WHEN a THEN b WHEN c THEN d ELSE e END"},
            )

        for args in ("0, 1", "0, 1, 2"):
            with self.subTest(args=args):
                sql = f"IIF({args})"
                call = self.validate_identity(f"SELECT\n  {sql}", f"SELECT {sql}").expressions[0]
                self.assertEqual(call.meta, {"line": 2, "col": 5, "start": 9, "end": 11})

    @unittest.skipIf(sqlite3.sqlite_version_info < (3, 49), "Requires variadic SQLite IIF")
    def test_variadic_iif_execution(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            for name in ("IIF", "IF"):
                for args in (
                    "1, 10, 1, 20",
                    "0, 10, 1, 20",
                    "0, 10, 0, 20",
                    "NULL, 10, 1, 20",
                    "0, 10, 0, 20, 30",
                    "0, 10, NULL, 20, 1, 30, 40",
                    "1, IIF(0, 10, 1, 20), 0, 30, 40",
                ):
                    with self.subTest(name=name, args=args):
                        sql = f"SELECT {name}({args})"
                        generated = parse_one(sql, dialect="sqlite").sql("sqlite")
                        self.assertEqual(
                            connection.execute(generated).fetchall(),
                            connection.execute(sql).fetchall(),
                        )

    def test_quoted_variadic_iif(self):
        for name in ('"IF"', '"IIF"'):
            with self.subTest(name=name):
                call = self.validate_identity(
                    f"SELECT {name}(0, 1, 1, 2)", "SELECT CASE WHEN 0 THEN 1 WHEN 1 THEN 2 END"
                ).expressions[0]
                end = 6 + len(name)
                self.assertEqual(call.meta, {"line": 1, "col": end + 1, "start": 7, "end": end})
                with self.assertRaisesRegex(ParseError, r"Expecting \)"):
                    self.parse_one(f"SELECT {name}(0, 1")

    def test_iif_anonymous_marker(self):
        for name, args in (
            ("IIF", "0, 1, 2"),
            ("iF", "0, 1, 2"),
            ('"iF"', "0, 1, 1, 2"),
        ):
            with self.subTest(name=name):
                sql = f"SELECT {name}({args})"
                query = self.parse_one(f"{sql} /* sqlglot.anonymous */")
                call = query.expressions[0]
                self.assertIsInstance(call, exp.Anonymous)
                self.assertEqual(call.comments, [" sqlglot.anonymous "])
                for dialect in ("postgres", "duckdb"):
                    self.assertEqual(
                        query.sql(dialect, comments=False, normalize_functions=False), sql
                    )

    def test_length_null_terminated_text(self):
        for arg, duckdb_arg in (
            ("''", "''"),
            ("'abc'", "'abc'"),
            ("'a' || CHAR(0) || 'b'", "'a' || CHR(0) || 'b'"),
            ("CHAR(0)", "CHR(0)"),
            ("CHAR(0) || 'ab'", "CHR(0) || 'ab'"),
            ("'ab' || CHAR(0)", "'ab' || CHR(0)"),
            ("'é😊' || CHAR(0) || 'x'", "'é😊' || CHR(0) || 'x'"),
        ):
            sql = f"SELECT LENGTH({arg})"
            self.validate_all(
                sql,
                write={
                    "sqlite": sql,
                    "duckdb": f"SELECT LENGTH(SPLIT_PART({duckdb_arg}, CHR(0), 1))",
                },
            )

        for arg, duckdb_arg in (
            ("NULL", "NULL"),
            ("123", "123"),
            ("x'610062'", "UNHEX('610062')"),
            ("x''", "UNHEX('')"),
        ):
            sql = f"SELECT LENGTH({arg})"
            self.validate_all(
                sql,
                write={
                    "sqlite": sql,
                    "duckdb": f"SELECT CASE TYPEOF({duckdb_arg}) "
                    f"WHEN 'BLOB' THEN OCTET_LENGTH(CAST({duckdb_arg} AS BLOB)) "
                    f"ELSE LENGTH(SPLIT_PART(CAST({duckdb_arg} AS TEXT), CHR(0), 1)) END",
                },
            )

        for dtype, expected in (
            ("TEXT", "SELECT LENGTH(SPLIT_PART(t.x, CHR(0), 1)) FROM t"),
            (
                "BLOB",
                "SELECT CASE TYPEOF(t.x) WHEN 'BLOB' THEN OCTET_LENGTH(CAST(t.x AS BLOB)) "
                "ELSE LENGTH(SPLIT_PART(CAST(t.x AS TEXT), CHR(0), 1)) END FROM t",
            ),
            (
                "INT",
                "SELECT CASE TYPEOF(t.x) WHEN 'BLOB' THEN OCTET_LENGTH(CAST(t.x AS BLOB)) "
                "ELSE LENGTH(SPLIT_PART(CAST(t.x AS TEXT), CHR(0), 1)) END FROM t",
            ),
        ):
            expression = annotate_types(
                self.parse_one("SELECT LENGTH(t.x) FROM t"),
                schema={"t": {"x": dtype}},
                dialect="sqlite",
            )
            self.assertEqual(expression.sql("duckdb"), expected)

        self.validate_all(
            "SELECT LENGTH('é')",
            write={"mysql": "SELECT CHAR_LENGTH('é')"},
        )

        expression = self.validate_identity("SELECT LENGTH(x)")
        original = expression.copy()
        sql = expression.sql("duckdb", copy=False)
        self.assertEqual(
            sql,
            "SELECT CASE TYPEOF(x) WHEN 'BLOB' THEN OCTET_LENGTH(CAST(x AS BLOB)) "
            "ELSE LENGTH(SPLIT_PART(CAST(x AS TEXT), CHR(0), 1)) END",
        )
        self.assertEqual(expression, original)
        length = expression.find(exp.Length)
        self.assertTrue(length.args.get("null_terminated"))
        self.assertIs(length.this.parent, length)
        self.assertEqual(expression.sql("sqlite", copy=False), "SELECT LENGTH(x)")
        self.assertEqual(
            parse_one("SELECT LENGTH(x)", dialect="duckdb").sql("duckdb"),
            "SELECT LENGTH(x)",
        )

    def test_round(self):
        for sql, duckdb_sql in (
            ("ROUND(123.45)", "ROUND(123.45)"),
            ("ROUND(123.45, -1)", "ROUND(123.45, 0)"),
            ("ROUND(123.45, -2)", "ROUND(123.45, 0)"),
            ("ROUND(-123.45, -1)", "ROUND(-123.45, 0)"),
            ("ROUND(123.45, 0)", "ROUND(123.45, 0)"),
            ("ROUND(123.45, 1)", "ROUND(123.45, 1)"),
            ("ROUND(NULL, -1)", "ROUND(NULL, 0)"),
            (
                "ROUND(123.45, NULL)",
                "ROUND(CAST(123.45 AS DOUBLE), CASE WHEN NULL < 0 THEN 0 ELSE NULL END)",
            ),
            ("ROUND(x, n)", "ROUND(CAST(x AS DOUBLE), CASE WHEN n < 0 THEN 0 ELSE n END)"),
            (
                "ROUND(123.45, n)",
                "ROUND(CAST(123.45 AS DOUBLE), CASE WHEN n < 0 THEN 0 ELSE n END)",
            ),
            (
                "ROUND(x, n - 1)",
                "ROUND(CAST(x AS DOUBLE), CASE WHEN (n - 1) < 0 THEN 0 ELSE n - 1 END)",
            ),
        ):
            with self.subTest(sql=sql):
                self.validate_all(sql, write={"sqlite": sql, "duckdb": duckdb_sql})

        sql = "SELECT ROUND(x, n) FROM t"
        expression = self.validate_identity(sql)
        original = expression.copy()
        for _ in range(3):
            self.assertEqual(
                expression.sql("duckdb", copy=False),
                "SELECT ROUND(CAST(x AS DOUBLE), CASE WHEN n < 0 THEN 0 ELSE n END) FROM t",
            )
            self.assertEqual(expression, original)
            round_expression = expression.find(exp.Round)
            self.assertIs(round_expression.args["decimals"].parent, round_expression)
            self.assertEqual(expression.sql("sqlite", copy=False), sql)
            expression = self.parse_one(expression.sql("sqlite"))

        for dialect in ("duckdb", "postgres", "snowflake"):
            with self.subTest(dialect=dialect):
                self.assertEqual(
                    parse_one("ROUND(123.45, -1)", dialect=dialect).sql("duckdb"),
                    "ROUND(123.45, -1)",
                )

    def test_week_date_arithmetic(self):
        self.validate_all(
            "DATE(d, (n * 7) || ' DAY')",
            read={"mysql": "DATE_ADD(d, INTERVAL n WEEK)"},
        )
        connection = sqlite3.connect(":memory:")
        try:
            for operation, amount, expected in (
                ("ADD", "2", "2024-01-15"),
                ("SUB", "2", "2023-12-18"),
                ("ADD", "-2", "2023-12-18"),
                ("SUB", "-2", "2024-01-15"),
                ("ADD", "0", "2024-01-01"),
            ):
                with self.subTest(operation=operation, amount=amount):
                    source = f"SELECT DATE_{operation}('2024-01-01', INTERVAL {amount} WEEK)"
                    sql = parse_one(source, read="mysql").sql("sqlite")
                    self.assertEqual(connection.execute(sql).fetchone()[0], expected)

            for operation, expected in (("ADD", "2024-01-15"), ("SUB", "2023-12-18")):
                source = f"SELECT DATE_{operation}(d, INTERVAL n WEEK) FROM (SELECT '2024-01-01' AS d, 2 AS n) AS t"
                sql = parse_one(source, read="mysql").sql("sqlite")
                self.assertEqual(connection.execute(sql).fetchone()[0], expected)
            source = (
                "SELECT DATE_ADD('2024-01-01', INTERVAL (n + 1) WEEK) FROM (SELECT 1 AS n) AS t"
            )
            sql = parse_one(source, read="mysql").sql("sqlite")
            self.assertEqual(connection.execute(sql).fetchone()[0], "2024-01-15")
            sql = parse_one("SELECT DATE_ADD(NULL, INTERVAL 2 WEEK)", read="mysql").sql("sqlite")
            self.assertIsNone(connection.execute(sql).fetchone()[0])
        finally:
            connection.close()

    def test_random(self):
        unit = "(CAST(RANDOM() & 9007199254740991 AS REAL) / 9007199254740992)"
        self.validate_all(
            f"SELECT {unit} < 0.1",
            read={
                "bigquery": "SELECT RAND() < 0.1",
                "mysql": "SELECT RAND() < 0.1",
                "postgres": "SELECT RANDOM() < 0.1",
            },
        )
        with closing(sqlite3.connect(":memory:")) as conn:
            for raw, expected in ((0, 0.0), (-1, 1 - 2**-53), (2**63 - 1, 1 - 2**-53)):
                conn.create_function("RANDOM", 0, lambda: raw)
                value, reciprocal = conn.execute(
                    parse_one("SELECT RAND(), 1.0 / RAND()", read="mysql").sql("sqlite")
                ).fetchone()
                self.assertIsInstance(value, float)
                self.assertEqual(value, expected)
                self.assertEqual(reciprocal, 1 / expected if expected else None)

        duckdb_random = "CAST((((CAST(FLOOR(RANDOM() * 4294967296) AS INT128) * 4294967296) + CAST(FLOOR(RANDOM() * 4294967296) AS INT128)) % ((CAST(9223372036854775807 AS INT128) - CAST(-9223372036854775807 AS INT128)) + 1)) + CAST(-9223372036854775807 AS INT128) AS BIGINT)"
        self.validate_all(
            "SELECT RANDOM()",
            write={
                "sqlite": "SELECT RANDOM()",
                "duckdb": f"SELECT {duckdb_random}",
            },
        )
        self.validate_all(
            "SELECT ABS(RANDOM()) % 2 WHERE RANDOM() < 0",
            write={
                "sqlite": "SELECT ABS(RANDOM()) % 2 WHERE RANDOM() < 0",
                "duckdb": f"SELECT ABS({duckdb_random}) % NULLIF(2, 0) WHERE {duckdb_random} < 0",
            },
        )
        expression = annotate_types(self.parse_one("RANDOM()"), dialect="sqlite")
        self.assertTrue(expression.is_type(exp.DType.BIGINT))
        self.assertEqual(expression.args["lower"].to_py(), -9223372036854775807)
        self.assertEqual(expression.args["upper"].to_py(), 9223372036854775807)

    def test_empty_group_by(self):
        self.validate_all(
            "SELECT COUNT(*) FROM t",
            read={"duckdb": "SELECT COUNT(*) FROM t GROUP BY ()"},
        )
        self.validate_all(
            "SELECT SUM(COUNT(*)) OVER () FROM t",
            read={"duckdb": "SELECT SUM(COUNT(*)) OVER () FROM t GROUP BY ()"},
        )
        self.validate_all(
            "SELECT _col_0 FROM (SELECT COALESCE(SUM(x), 0) AS _col_0, COUNT(*) > 1 AS _having FROM t) AS _global WHERE _having",
            read={"duckdb": "SELECT COALESCE(SUM(x), 0) FROM t GROUP BY () HAVING COUNT(*) > 1"},
        )
        self.validate_all(
            "SELECT _col_0 FROM (SELECT 1 AS _col_0, COUNT(*) > 0 AS _having FROM t) AS _global WHERE _having",
            read={"duckdb": "SELECT 1 FROM t GROUP BY () HAVING COUNT(*) > 0"},
        )

        for sql in (
            "SELECT 1 FROM t GROUP BY ()",
            "SELECT COUNT(*) FILTER(WHERE TRUE) OVER () FROM t GROUP BY ()",
            "SELECT SUM(COUNT(*)) OVER () FROM t GROUP BY () HAVING COUNT(*) > 1",
            "SELECT (SELECT COUNT(*) FROM u) FROM t GROUP BY ()",
            "SELECT COUNT(*) FROM t GROUP BY (), x",
        ):
            with self.subTest(sql=sql):
                query = parse_one(sql, read="duckdb")
                self.assertEqual(query.sql("sqlite", unsupported_level=ErrorLevel.IGNORE), sql)
                with self.assertRaisesRegex(UnsupportedError, r"GROUP BY \(\)"):
                    query.sql("sqlite", unsupported_level=ErrorLevel.RAISE)

    def test_empty_group_by_min_max(self):
        for sql in (
            "SELECT MIN(x), MAX(y) FROM t GROUP BY ()",
            "SELECT MIN(COUNT(*), 7) FROM t GROUP BY ()",
            "SELECT ROW_NUMBER() OVER w FROM t GROUP BY () WINDOW w AS (ORDER BY MAX(0, SUM(x)))",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(
                    parse_one(sql).sql("sqlite", unsupported_level=ErrorLevel.RAISE),
                    sql.replace(" GROUP BY ()", ""),
                )

        for dialect, sql in (
            ("", "SELECT MIN(x, y) FROM t GROUP BY ()"),
            ("", "SELECT ROW_NUMBER() OVER w FROM t GROUP BY () WINDOW w AS (ORDER BY MAX(1, 2))"),
            ("duckdb", "SELECT MIN(x, 2) FROM t GROUP BY ()"),
        ):
            with self.subTest(dialect=dialect, sql=sql):
                query = parse_one(sql, read=dialect)
                self.assertEqual(query.sql("sqlite", unsupported_level=ErrorLevel.IGNORE), sql)
                with self.assertRaisesRegex(UnsupportedError, r"GROUP BY \(\)"):
                    query.sql("sqlite", unsupported_level=ErrorLevel.RAISE)

    def test_group_concat_null_separator(self):
        for separator, duckdb_separator in (
            ("NULL", "NULL"),
            (":separator", "$separator"),
            ("CAST(NULL AS TEXT)", "CAST(NULL AS TEXT)"),
        ):
            sql = f"SELECT GROUP_CONCAT(x, {separator}) FROM t"
            self.validate_all(
                sql,
                write={
                    "sqlite": sql,
                    "duckdb": f"SELECT LISTAGG(x, COALESCE({duckdb_separator}, '')) FROM t",
                },
            )

        self.validate_all(
            "SELECT GROUP_CONCAT(x, NULL ORDER BY y) FILTER(WHERE z > 0) FROM t",
            write={
                "sqlite": "SELECT GROUP_CONCAT(x, NULL ORDER BY y) FILTER(WHERE z > 0) FROM t",
                "duckdb": "SELECT LISTAGG(x, COALESCE(NULL, '') ORDER BY y NULLS FIRST) FILTER(WHERE z > 0) FROM t",
            },
        )
        self.validate_all(
            "SELECT STRING_AGG(x, NULL) FROM t",
            write={
                "sqlite": "SELECT GROUP_CONCAT(x, NULL) FROM t",
                "duckdb": "SELECT LISTAGG(x, COALESCE(NULL, '')) FROM t",
            },
        )
        for sql, expected in (
            ("GROUP_CONCAT(x)", "LISTAGG(x, ',')"),
            ("GROUP_CONCAT(x, '')", "LISTAGG(x, '')"),
            ("GROUP_CONCAT(x, ',')", "LISTAGG(x, ',')"),
            ("GROUP_CONCAT(x, 'NULL')", "LISTAGG(x, 'NULL')"),
        ):
            self.validate_all(
                sql,
                write={"duckdb": expected},
            )

    def test_group_concat_distinct_separator(self):
        self.validate_all(
            "SELECT GROUP_CONCAT(DISTINCT x) FROM t",
            read={
                "postgres": "SELECT STRING_AGG(DISTINCT x, ',') FROM t",
                "duckdb": "SELECT STRING_AGG(DISTINCT x, ',') FROM t",
                "mysql": "SELECT GROUP_CONCAT(DISTINCT x SEPARATOR ',') FROM t",
            },
        )
        self.validate_all(
            "SELECT GROUP_CONCAT(DISTINCT x ORDER BY x NULLS LAST) FROM t",
            read={"postgres": "SELECT STRING_AGG(DISTINCT x, ',' ORDER BY x) FROM t"},
        )
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                "CREATE TABLE t(x TEXT); INSERT INTO t VALUES ('a'), ('a'), ('b'), (NULL);"
            )
            for where in ("", " WHERE x IS NULL", " WHERE FALSE"):
                generated = parse_one(
                    f"SELECT STRING_AGG(DISTINCT x, ',') FROM t{where}", dialect="postgres"
                ).sql("sqlite")
                self.assertEqual(
                    connection.execute(generated).fetchall(),
                    connection.execute(f"SELECT GROUP_CONCAT(DISTINCT x) FROM t{where}").fetchall(),
                )
        finally:
            connection.close()

    def test_unary_plus(self):
        for sql in (
            "SELECT +x FROM t",
            "SELECT +t.x FROM t",
            "SELECT +(x + 1) FROM t",
            "SELECT ++x, +-x, -+x FROM t",
            "SELECT +x COLLATE NOCASE FROM t",
            "SELECT 5 IS +TRUE, 5 IS NOT +TRUE, 0 IS +FALSE",
        ):
            with self.subTest(sql=sql):
                self.validate_identity(sql)

    def test_unary_plus_preserves_affinity(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                "CREATE TABLE t (x TEXT); INSERT INTO t VALUES ('1'), ('2'), ('01'), (NULL);"
            )
            for sql in (
                "SELECT x FROM t WHERE +x = 1",
                "SELECT x FROM t WHERE +x IN (1, 2)",
                "SELECT x FROM t WHERE +x < 2 ORDER BY x",
                "SELECT x FROM t WHERE +x IS 1",
                "SELECT x FROM t WHERE +x = '1'",
                "SELECT 5 IS +TRUE, 5 IS TRUE, 0 IS +FALSE",
            ):
                with self.subTest(sql=sql):
                    generated = self.parse_one(sql).sql("sqlite")
                    self.assertEqual(
                        connection.execute(sql).fetchall(),
                        connection.execute(generated).fetchall(),
                    )
        finally:
            connection.close()

    def test_interval_arithmetic_execution(self):
        connection = sqlite3.connect(":memory:")
        try:
            for read, sql, expected in (
                ("postgres", "SELECT CAST('2024-01-15' AS DATE) + INTERVAL '1 day'", "2024-01-16"),
                ("mysql", "SELECT CAST('2024-01-15' AS DATE) + INTERVAL 1 DAY", "2024-01-16"),
                ("mysql", "SELECT CAST('2024-01-15' AS DATE) - INTERVAL 1 MONTH", "2023-12-15"),
                ("duckdb", "SELECT INTERVAL 1 DAY + CAST('2024-01-15' AS DATE)", "2024-01-16"),
                ("postgres", "SELECT TIME '14:30:00' - INTERVAL '1 hour'", "13:30:00"),
                (
                    "postgres",
                    "WITH t AS (SELECT TIMESTAMP '2024-01-15 14:30:00' AS ts) "
                    "SELECT t.ts + INTERVAL '1 day' FROM t",
                    "2024-01-16 14:30:00",
                ),
                (
                    "postgres",
                    "SELECT CAST('2024-01-15 10:00:00' AS TIMESTAMP) - INTERVAL '2 hours'",
                    "2024-01-15 08:00:00",
                ),
                (
                    "postgres",
                    "SELECT CAST('2024-01-15 10:00:00' AS TIMESTAMP) + INTERVAL '1 week'",
                    "2024-01-22 10:00:00",
                ),
            ):
                with self.subTest(sql=sql):
                    generated = annotate_types(parse_one(sql, read=read), dialect=read).sql(
                        f"sqlite, version={sqlite3.sqlite_version}"
                    )
                    self.assertEqual([(expected,)], connection.execute(generated).fetchall())
        finally:
            connection.close()

    @unittest.skipIf(sqlite3.sqlite_version_info < (3, 46), "Requires SQLite's floor modifier")
    def test_month_end_interval_arithmetic(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            # A DATE plus a day-or-larger interval stays a DATE in the fork, so it compares
            # correctly with date text, even where the source dialect returns a TIMESTAMP.
            for read, sql, expected in (
                (
                    "postgres",
                    "SELECT DATE '2024-01-31' + INTERVAL '1 month'",
                    "2024-02-29",
                ),
                (
                    "postgres",
                    "SELECT DATE '2024-02-29' + INTERVAL '1 year'",
                    "2025-02-28",
                ),
                (
                    "postgres",
                    "SELECT INTERVAL '2 months' + TIMESTAMP '2023-12-31 12:34:56'",
                    "2024-02-29 12:34:56",
                ),
                ("mysql", "SELECT DATE_ADD(DATE '2024-01-31', INTERVAL 1 QUARTER)", "2024-04-30"),
                (
                    "mysql",
                    "SELECT DATE_SUB(t.d, INTERVAL t.n QUARTER) "
                    "FROM (SELECT DATE '2024-05-31' AS d, 1 AS n) AS t",
                    "2024-02-29",
                ),
            ):
                with self.subTest(sql=sql):
                    generated = annotate_types(parse_one(sql, read=read), dialect=read).sql(
                        "sqlite"
                    )
                    self.assertEqual(connection.execute(generated).fetchone(), (expected,))

    def test_month_end_interval_before_sqlite_3_46(self):
        expression = parse_one("SELECT d - INTERVAL 1 MONTH FROM t", read="mysql")
        with self.assertRaises(UnsupportedError):
            expression.sql("sqlite, version=3.45", unsupported_level=ErrorLevel.RAISE)
        self.assertEqual(
            expression.sql("sqlite, version=3.45", unsupported_level=ErrorLevel.IGNORE),
            "SELECT DATE(d, '-1 MONTH') FROM t",
        )

    def test_month_offset_overflow_guard_before_sqlite_3_46(self):
        old = "sqlite, version=3.45"
        current = "sqlite, version=3.46"
        unversioned = "sqlite"

        def generate(sql, dialect, read="postgres", level=ErrorLevel.RAISE):
            return parse_one(sql, read=read).sql(dialect, unsupported_level=level)

        for sql, plain, floored in (
            (
                "SELECT DATE '1994-01-01' + INTERVAL '3 month'",
                "SELECT DATE(DATE('1994-01-01'), '3 MONTH')",
                "SELECT DATE(DATE('1994-01-01'), '3 MONTH', 'floor')",
            ),
            (
                "SELECT DATE '1994-01-28' - INTERVAL '1 year'",
                "SELECT DATE(DATE('1994-01-28'), '-1 YEAR')",
                "SELECT DATE(DATE('1994-01-28'), '-1 YEAR', 'floor')",
            ),
            (
                "SELECT CAST('1994-01-01' AS DATE) + INTERVAL '1 month'",
                "SELECT DATE(DATE('1994-01-01'), '1 MONTH')",
                "SELECT DATE(DATE('1994-01-01'), '1 MONTH', 'floor')",
            ),
            (
                "SELECT DATE '1994-01-15' + INTERVAL '1 year'",
                "SELECT DATE(DATE('1994-01-15'), '1 YEAR')",
                "SELECT DATE(DATE('1994-01-15'), '1 YEAR', 'floor')",
            ),
        ):
            with self.subTest(sql=sql):
                self.assertEqual(generate(sql, old), plain)
                self.assertEqual(generate(sql, current), floored)
                self.assertEqual(generate(sql, unversioned), floored)

        for read, sql, plain in (
            (
                "mysql",
                "SELECT DATE_ADD(DATE '1994-01-01', INTERVAL 1 QUARTER)",
                "SELECT DATE(DATE('1994-01-01'), ('1' * 3) || ' MONTH')",
            ),
            (
                "mysql",
                "SELECT DATE_SUB(DATE '1994-12-28', INTERVAL 1 YEAR)",
                "SELECT DATE(DATE('1994-12-28'), '-1 YEAR')",
            ),
        ):
            with self.subTest(sql=sql):
                self.assertEqual(generate(sql, old, read), plain)

        for read, sql in (
            ("postgres", "SELECT DATE '1994-01-31' + INTERVAL '1 month'"),
            ("postgres", "SELECT DATE '1994-01-29' + INTERVAL '1 year'"),
            ("postgres", "SELECT DATE '1996-02-29' + INTERVAL '1 year'"),
            ("postgres", "SELECT CAST('1994-01-31' AS DATE) + INTERVAL '1 month'"),
            ("mysql", "SELECT DATE_ADD(DATE '1994-01-31', INTERVAL 1 QUARTER)"),
            ("mysql", "SELECT d - INTERVAL 1 MONTH FROM t"),
            ("mysql", "SELECT CURRENT_DATE + INTERVAL 1 YEAR"),
            ("postgres", "SELECT CAST('not a date' AS DATE) + INTERVAL '1 month'"),
            ("postgres", "SELECT CAST('1994-02-30' AS DATE) + INTERVAL '1 month'"),
            ("postgres", "SELECT CAST(x AS DATE) + INTERVAL '1 month' FROM t"),
        ):
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(UnsupportedError, "requires SQLite 3.46"):
                    generate(sql, old, read)
                self.assertIn("'floor'", generate(sql, current, read, ErrorLevel.IGNORE))

        self.assertEqual(
            generate("SELECT DATE '1994-01-31' + INTERVAL '1 day'", old),
            "SELECT DATE(DATE('1994-01-31'), '1 DAY')",
        )

    def test_month_offset_of_a_safe_literal_executes_on_any_sqlite(self):
        dialect = "sqlite, version=3.45"
        with closing(sqlite3.connect(":memory:")) as connection:
            for sql, expected in (
                ("SELECT DATE '1994-01-01' + INTERVAL '3 month'", "1994-04-01"),
                ("SELECT DATE '1994-01-28' + INTERVAL '1 month'", "1994-02-28"),
                ("SELECT DATE '1996-02-28' + INTERVAL '1 year'", "1997-02-28"),
                ("SELECT DATE '1994-03-15' - INTERVAL '1 year'", "1993-03-15"),
            ):
                with self.subTest(sql=sql):
                    generated = parse_one(sql, read="postgres").sql(
                        dialect, unsupported_level=ErrorLevel.RAISE
                    )
                    self.assertEqual(connection.execute(generated).fetchone(), (expected,))

    def test_interval_arithmetic_unknown_type(self):
        for sql in (
            "SELECT ts + INTERVAL 1 DAY FROM t",
            "SELECT INTERVAL 1 WEEK + ts FROM t",
            "SELECT ts - INTERVAL 1 MONTH FROM t",
            "SELECT ts + INTERVAL 1 QUARTER FROM t",
            "SELECT DATE_ADD(ts, INTERVAL 1 YEAR) FROM t",
        ):
            with (
                self.subTest(sql=sql),
                self.assertRaisesRegex(UnsupportedError, "may discard the time"),
            ):
                parse_one(sql, read="duckdb").sql("sqlite", unsupported_level=ErrorLevel.RAISE)

        query = parse_one("SELECT t.ts + INTERVAL 1 DAY FROM t", read="mysql")
        annotate_types(query, dialect="mysql")
        with self.assertLogs(generator_logger, level="WARNING") as logs:
            self.assertEqual(query.sql("sqlite"), "SELECT DATE(t.ts, '1 DAY') FROM t")
        self.assertIn("may discard the time", logs.output[0])

        for dtype, func in (("DATE", "DATE"), ("TIMESTAMP", "DATETIME")):
            with self.subTest(dtype=dtype):
                annotate_types(query, schema={"t": {"ts": dtype}}, dialect="mysql")
                self.assertEqual(
                    query.sql("sqlite", unsupported_level=ErrorLevel.RAISE),
                    f"SELECT {func}(t.ts, '1 DAY') FROM t",
                )

    def test_interval_arithmetic_text_column(self):
        for sql in (
            "SELECT t.ts + INTERVAL 1 DAY FROM t",
            "SELECT DATE_ADD(t.ts, INTERVAL 1 DAY) FROM t",
        ):
            with self.subTest(sql=sql):
                query = annotate_types(
                    parse_one(sql, read="mysql"),
                    schema={"t": {"ts": "VARCHAR(32)"}},
                    dialect="mysql",
                )
                with self.assertRaisesRegex(UnsupportedError, "may discard the time"):
                    query.sql("sqlite", unsupported_level=ErrorLevel.RAISE)

    def test_date_plus_interval_stays_a_date(self):
        # A DATE plus a day-or-larger interval stays a DATE even where the source dialect types
        # the sum as a TIMESTAMP, so it compares correctly with date text (TPC-DS Q21 and Q40).
        self.validate_all(
            "SELECT DATE(DATE('2024-01-31'), '30 DAYS'), DATE(DATE('2024-01-15'), '1 MONTH', 'floor')",
            read={
                "postgres": "SELECT DATE '2024-01-31' + INTERVAL '30 days', "
                "DATE '2024-01-15' + INTERVAL '1 month'"
            },
        )
        for sql, expected in (
            (
                "SELECT DATE '2024-01-31' - INTERVAL '30 days'",
                "SELECT DATE(DATE('2024-01-31'), '-30 DAYS')",
            ),
            (
                "SELECT DATE '2024-01-31' + INTERVAL '1 month'",
                "SELECT DATE(DATE('2024-01-31'), '1 MONTH', 'floor')",
            ),
        ):
            with self.subTest(sql=sql):
                annotated = annotate_types(parse_one(sql, read="postgres"), dialect="postgres")
                self.assertEqual(annotated.sql("sqlite"), expected)
        for sql in (
            "SELECT CURRENT_DATE - INTERVAL 90 DAY",
            "SELECT DATE_SUB(CURRENT_DATE, INTERVAL 90 DAY)",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(
                    parse_one(sql, read="mysql").sql("sqlite", unsupported_level=ErrorLevel.RAISE),
                    "SELECT DATE(CURRENT_DATE, '-90 DAY')",
                )

    def test_date_plus_interval_stays_a_date_execution(self):
        cases = [
            ("SELECT DATE '2024-01-31' + INTERVAL '30 days'", "2024-03-01"),
            (
                "SELECT TIMESTAMP '2024-01-15 23:30:00' + INTERVAL '1 day'",
                "2024-01-16 23:30:00",
            ),
            (
                "SELECT d - INTERVAL '2 hours' FROM (SELECT '2024-01-15' AS d)",
                "2024-01-14 22:00:00",
            ),
        ]
        if sqlite3.sqlite_version_info >= (3, 46):
            cases += [
                ("SELECT DATE '2024-01-15' + INTERVAL '1 month'", "2024-02-15"),
                ("SELECT DATE '2024-02-28' + INTERVAL '1 year'", "2025-02-28"),
            ]
        with closing(sqlite3.connect(":memory:")) as connection:
            for sql, expected in cases:
                with self.subTest(sql=sql):
                    generated = parse_one(sql, read="postgres").sql(
                        "sqlite", unsupported_level=ErrorLevel.RAISE
                    )
                    self.assertEqual([(expected,)], connection.execute(generated).fetchall())

    def test_date_add_sub_day_units_retain_time(self):
        connection = sqlite3.connect(":memory:")
        try:
            for sql, expected in (
                (
                    "SELECT DATE_ADD('2024-01-01 10:00:00', INTERVAL 2 HOUR)",
                    "2024-01-01 12:00:00",
                ),
                (
                    "SELECT DATE_ADD('2024-01-01 10:00:00', INTERVAL 30 MINUTE)",
                    "2024-01-01 10:30:00",
                ),
                (
                    "SELECT DATE_ADD('2024-01-01 10:00:00', INTERVAL 45 SECOND)",
                    "2024-01-01 10:00:45",
                ),
                (
                    "SELECT DATE_SUB('2024-01-01 10:00:00', INTERVAL 2 HOUR)",
                    "2024-01-01 08:00:00",
                ),
            ):
                with self.subTest(sql=sql):
                    generated = parse_one(sql, read="mysql").sql("sqlite")
                    self.assertEqual([(expected,)], connection.execute(generated).fetchall())
        finally:
            connection.close()

    def test_pragma_quoted_names(self):
        for source, expected in (
            ('PRAGMA "a b"', 'PRAGMA "a b"'),
            ('PRAGMA "a.b"', 'PRAGMA "a.b"'),
            ('PRAGMA "select"', 'PRAGMA "select"'),
            ('PRAGMA "a""b"', 'PRAGMA "a""b"'),
            ("PRAGMA [a b]", 'PRAGMA "a b"'),
            ("PRAGMA `a b`", 'PRAGMA "a b"'),
            ("PRAGMA 'a b'", 'PRAGMA "a b"'),
            ('PRAGMA "x; SELECT 123"', 'PRAGMA "x; SELECT 123"'),
            ('PRAGMA "x--comment"', 'PRAGMA "x--comment"'),
            ('PRAGMA main."table_info"(items)', 'PRAGMA main."table_info" = items'),
        ):
            with self.subTest(source=source):
                self.validate_identity(source, expected)

    def test_pragma_quoted_schemas(self):
        for source, expected in (
            ('"aux db"', '"aux db"'),
            ('"aux.db"', '"aux.db"'),
            ('"aux""db"', '"aux""db"'),
            ("[aux db]", '"aux db"'),
            ("`aux db`", '"aux db"'),
            ("'aux db'", '"aux db"'),
        ):
            with self.subTest(schema=source):
                self.validate_identity(
                    f"PRAGMA {source}.table_info(items)",
                    f"PRAGMA {expected}.table_info = items",
                )

        self.validate_identity("""PRAGMA "aux db".table_info = 'items'""")

    def test_safe_div(self):
        self.validate_all(
            "SELECT CAST(a AS INTEGER) / CAST(b AS INTEGER) FROM t",
            write={
                "duckdb": "SELECT CAST(a AS INT) // NULLIF(CAST(b AS INT), 0) FROM t",
                "snowflake": "SELECT CAST(TRUNC(CAST(a AS INT) / NULLIF(CAST(b AS INT), 0)) AS BIGINT) FROM t",
                "bigquery": "SELECT DIV(CAST(a AS INT64), NULLIF(CAST(b AS INT64), 0)) FROM t",
            },
        )
        for sql, expected in (
            (
                "SELECT 7 / 0",
                {
                    "duckdb": "SELECT 7 // NULLIF(0, 0)",
                    "snowflake": "SELECT CAST(TRUNC(7 / NULLIF(0, 0)) AS BIGINT)",
                    "bigquery": "SELECT DIV(7, NULLIF(0, 0))",
                },
            ),
            (
                "SELECT t.a / t.b FROM t",
                {
                    "duckdb": "SELECT CAST(TRUNC(t.a / NULLIF(t.b, 0)) AS BIGINT) FROM t",
                    "snowflake": "SELECT CAST(TRUNC(t.a / NULLIF(t.b, 0)) AS BIGINT) FROM t",
                    "bigquery": "SELECT CAST(TRUNC(t.a / NULLIF(t.b, 0)) AS INT64) FROM t",
                },
            ),
        ):
            expression = annotate_types(
                self.parse_one(sql), schema={"t": {"a": "INT", "b": "INT"}}, dialect="sqlite"
            )
            for dialect, output in expected.items():
                with self.subTest(sql=sql, dialect=dialect):
                    self.assertEqual(expression.sql(dialect), output)

    def test_parenthesized_set_operands(self):
        self.validate_all(
            "SELECT * FROM (SELECT DISTINCT a FROM t) EXCEPT SELECT * FROM (SELECT DISTINCT a FROM u)",
            read={"duckdb": "(SELECT DISTINCT a FROM t) EXCEPT (SELECT DISTINCT a FROM u)"},
        )
        self.validate_all(
            "SELECT 0 UNION ALL SELECT * FROM (SELECT a FROM t ORDER BY a DESC LIMIT 1)",
            read={"duckdb": "SELECT 0 UNION ALL (SELECT a FROM t ORDER BY a DESC LIMIT 1)"},
        )

    def test_subquery_modifiers(self):
        for source, expected in (
            (
                "SELECT 3 UNION ALL ((SELECT 2 INTERSECT SELECT 2) ORDER BY 1)",
                "SELECT 3 UNION ALL SELECT * FROM (SELECT 2 INTERSECT SELECT 2 ORDER BY 1 NULLS LAST)",
            ),
            (
                "((SELECT 2 INTERSECT SELECT 2) ORDER BY 1) UNION ALL SELECT 3",
                "SELECT * FROM (SELECT 2 INTERSECT SELECT 2 ORDER BY 1 NULLS LAST) UNION ALL SELECT 3",
            ),
            (
                "SELECT 9 UNION ALL ((SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3) ORDER BY 1 DESC LIMIT 1 OFFSET 1)",
                "SELECT 9 UNION ALL SELECT * FROM (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 ORDER BY 1 DESC NULLS FIRST LIMIT 1 OFFSET 1)",
            ),
            (
                "SELECT 9 UNION ALL (((SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3) ORDER BY 1 LIMIT 2) OFFSET 1)",
                "SELECT 9 UNION ALL SELECT * FROM (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 ORDER BY 1 NULLS LAST LIMIT 2 OFFSET 1)",
            ),
            (
                "SELECT * FROM ((SELECT 1 UNION ALL SELECT 2) ORDER BY 1 DESC LIMIT 1) AS t",
                "SELECT * FROM (SELECT 1 UNION ALL SELECT 2 ORDER BY 1 DESC NULLS FIRST LIMIT 1) AS t",
            ),
            (
                "SELECT ((SELECT 1 UNION ALL SELECT 2) ORDER BY 1 DESC LIMIT 1)",
                "SELECT (SELECT 1 UNION ALL SELECT 2 ORDER BY 1 DESC NULLS FIRST LIMIT 1)",
            ),
            (
                "WITH t AS (SELECT 1 AS x) (SELECT x FROM t UNION ALL SELECT 2) ORDER BY 1 DESC LIMIT 1",
                "WITH t AS (SELECT 1 AS x) SELECT x FROM t UNION ALL SELECT 2 ORDER BY 1 DESC NULLS FIRST LIMIT 1",
            ),
            (
                "(SELECT 1) LIMIT 1",
                "SELECT 1 LIMIT 1",
            ),
            (
                "(SELECT 1) OFFSET 1",
                "SELECT 1 LIMIT -1 OFFSET 1",
            ),
            (
                "(SELECT 1 UNION ALL SELECT 2) OFFSET 1",
                "SELECT 1 UNION ALL SELECT 2 LIMIT -1 OFFSET 1",
            ),
            (
                "SELECT 9 UNION ALL ((SELECT 1 UNION ALL SELECT 2) OFFSET 1)",
                "SELECT 9 UNION ALL SELECT * FROM (SELECT 1 UNION ALL SELECT 2 LIMIT -1 OFFSET 1)",
            ),
            (
                "SELECT 1 UNION ALL SELECT 2 LIMIT ALL OFFSET 1",
                "SELECT 1 UNION ALL SELECT 2 LIMIT -1 OFFSET 1",
            ),
            (
                "SELECT 1 LIMIT ALL OFFSET 1",
                "SELECT 1 LIMIT -1 OFFSET 1",
            ),
        ):
            with self.subTest(source=source):
                self.validate_all(expected, read={"postgres": source})

    def test_subquery_modifier_conflicts(self):
        for source, expected in (
            (
                "(SELECT 1 AS x UNION ALL SELECT 2 UNION ALL SELECT 3 ORDER BY 1 LIMIT 2) ORDER BY 1 DESC LIMIT 1",
                "SELECT * FROM (SELECT 1 AS x UNION ALL SELECT 2 UNION ALL SELECT 3 ORDER BY 1 LIMIT 2) ORDER BY 1 DESC LIMIT 1",
            ),
            (
                "(SELECT 1 AS x UNION ALL SELECT 2 UNION ALL SELECT 3 ORDER BY 1 LIMIT 2) ORDER BY 1 DESC",
                "SELECT * FROM (SELECT 1 AS x UNION ALL SELECT 2 UNION ALL SELECT 3 ORDER BY 1 LIMIT 2) ORDER BY 1 DESC",
            ),
            (
                "(SELECT 1 AS x UNION ALL SELECT 2 LIMIT 1) LIMIT 2",
                "SELECT * FROM (SELECT 1 AS x UNION ALL SELECT 2 LIMIT 1) LIMIT 2",
            ),
            (
                "(SELECT 1 AS x UNION ALL SELECT 2 UNION ALL SELECT 3 ORDER BY 1 LIMIT 2 OFFSET 1) LIMIT 1 OFFSET 1",
                "SELECT * FROM (SELECT 1 AS x UNION ALL SELECT 2 UNION ALL SELECT 3 ORDER BY 1 LIMIT 2 OFFSET 1) LIMIT 1 OFFSET 1",
            ),
            (
                "WITH a AS (SELECT 1 AS x) (WITH b AS (SELECT 2 AS x) SELECT * FROM b) LIMIT 1",
                "WITH a AS (SELECT 1 AS x) SELECT * FROM (WITH b AS (SELECT 2 AS x) SELECT * FROM b) LIMIT 1",
            ),
            (
                "WITH a AS (SELECT 1 AS x) (WITH a AS (SELECT 2 AS x) SELECT * FROM a) LIMIT 1",
                "WITH a AS (SELECT 1 AS x) SELECT * FROM (WITH a AS (SELECT 2 AS x) SELECT * FROM a) LIMIT 1",
            ),
        ):
            with self.subTest(source=source):
                self.validate_all(expected, read={"mysql": source})

    def test_negative_limit(self):
        # A negative LIMIT means "no limit" in SQLite
        self.validate_all(
            "SELECT x FROM t LIMIT -1 OFFSET 5",
            write={
                "sqlite": "SELECT x FROM t LIMIT -1 OFFSET 5",
                "duckdb": "SELECT x FROM t LIMIT ALL OFFSET 5",
                "postgres": "SELECT x FROM t LIMIT ALL OFFSET 5",
                "tsql": "SELECT x FROM t ORDER BY (SELECT NULL) OFFSET 5 ROWS",
            },
        )
        self.validate_all(
            "SELECT x FROM t LIMIT 5, -2",
            write={
                "sqlite": "SELECT x FROM t LIMIT -1 OFFSET 5",
                "duckdb": "SELECT x FROM t LIMIT ALL OFFSET 5",
            },
        )
        self.validate_all(
            "SELECT x FROM t LIMIT -1",
            write={
                "sqlite": "SELECT x FROM t",
                "duckdb": "SELECT x FROM t LIMIT ALL",
            },
        )

    def test_cast(self):
        for dtype, func in (
            ("DATETIME", "DATETIME"),
            ("TIMESTAMP", "DATETIME"),
            ("TIMESTAMPNTZ", "DATETIME"),
            ("TIME", "TIME"),
        ):
            for value in ("'2014-09-01 12:34:56'", "x", "NULL"):
                with self.subTest(dtype=dtype, value=value):
                    self.validate_all(
                        f"SELECT {func}({value})",
                        read={"": f"SELECT CAST({value} AS {dtype})"},
                    )

    def test_json_cast(self):
        for dtype, dialects in (
            ("JSON", ("", "postgres", "duckdb")),
            ("JSONB", ("", "postgres")),
        ):
            for value in ("'{\"a\": 1}'", "x", "NULL"):
                with self.subTest(dtype=dtype, value=value):
                    from_ = " FROM (SELECT '{\"a\": 1}' AS x) AS t" if value == "x" else ""
                    self.validate_all(
                        f"SELECT JSON({value}){from_}",
                        read={
                            dialect: f"SELECT CAST({value} AS {dtype}){from_}"
                            for dialect in dialects
                        },
                    )

            self.validate_all(
                "SELECT JSON('{\"a\": 1}') ->> '$.a'",
                read={"postgres": f"SELECT '{{\"a\": 1}}'::{dtype} ->> 'a'"},
            )

    def test_is_precedence(self):
        self.validate_identity("SELECT 'x' LIKE 'x' NOT LIKE '1'")
        for suffix in ("IN (0)", "BETWEEN 0 AND 0", "LIKE '0'", "GLOB '0'"):
            for predicate, klass in (
                ("NULL IS NULL", exp.Is),
                ("NULL IS NOT DISTINCT FROM NULL", exp.NullSafeEQ),
            ):
                lhs = "(NULL IS NULL)" if suffix.startswith("BETWEEN") else "NULL IS NULL"
                expression = self.validate_identity(
                    f"SELECT {predicate} {suffix}", f"SELECT {lhs} {suffix}"
                )
                self.assertIsInstance(expression.expressions[0].this.unnest(), klass)

        self.validate_identity(
            "SELECT NULL IS NULL < FALSE IN (0)", "SELECT NULL IS (NULL < FALSE) IN (0)"
        )
        for predicate in ("NOT NULL", "NOTNULL", "IS NOT NULL"):
            for suffix in ("IN (0, 1)", "BETWEEN 0 AND 1", "NOT IN (0, 1)"):
                negated = suffix.startswith("NOT ")
                self.validate_identity(
                    f"SELECT NULL {predicate} {suffix}",
                    f"SELECT {'NOT ' if negated else ''}(NOT NULL IS NULL) {suffix.removeprefix('NOT ')}",
                )
        self.validate_all(
            "SELECT FALSE IS DISTINCT FROM TRUE IS DISTINCT FROM FALSE",
            write={
                "sqlite": "SELECT (FALSE IS NOT TRUE) IS NOT FALSE",
                "postgres": "SELECT (FALSE IS DISTINCT FROM TRUE) IS DISTINCT FROM FALSE",
            },
        )
        self.validate_identity("SELECT NULL IS NULL < FALSE", "SELECT NULL IS (NULL < FALSE)")
        self.validate_all(
            "SELECT FALSE IS DISTINCT FROM NULL = NULL",
            write={
                "sqlite": "SELECT (FALSE IS NOT NULL) = NULL",
                "postgres": "SELECT (FALSE IS DISTINCT FROM NULL) = NULL",
                "duckdb": "SELECT (FALSE IS DISTINCT FROM NULL) = NULL",
            },
        )

    def test_date_extraction(self):
        for part, fmt in (("YEAR", "%Y"), ("MONTH", "%m"), ("DAY", "%d")):
            self.validate_all(
                f"SELECT CAST(STRFTIME('{fmt}', DATE('2020-06-01')) AS INTEGER)",
                read={
                    dialect: f"SELECT EXTRACT({part} FROM DATE '2020-06-01')"
                    for dialect in ("postgres", "duckdb")
                },
            )
        self.validate_all(
            "SELECT CAST(CAST(STRFTIME('%Y', DATE('2020-06-01')) AS INTEGER) AS REAL) / 3 > 673",
            read={
                dialect: "SELECT EXTRACT(YEAR FROM DATE '2020-06-01') / 3 > 673"
                for dialect in ("postgres", "duckdb")
            },
        )
        for source in (
            "EXTRACT(YEAR FROM TIMESTAMP '2020-06-01 12:00:00')",
            "EXTRACT(HOUR FROM DATE '2020-06-01')",
            "EXTRACT(YEAR FROM INTERVAL '1' YEAR)",
            "EXTRACT(EPOCH FROM d)",
            "DATE_PART('year', DATE '2020-06-01')",
        ):
            with self.subTest(source=source), self.assertRaises(UnsupportedError):
                parse_one(source, read="postgres").sql("sqlite", unsupported_level=ErrorLevel.RAISE)

    def test_date_extraction_execution(self):
        connection = sqlite3.connect(":memory:")
        try:
            for dialect in ("postgres", "duckdb"):
                for value in ("2000-02-29", "1900-03-01", "2024-12-31", None):
                    source_value = "NULL" if value is None else f"'{value}'"
                    fields = tuple(map(int, value.split("-"))) if value else (None, None, None)
                    query = "SELECT " + ", ".join(
                        f"EXTRACT({part} FROM CAST({source_value} AS DATE))"
                        for part in ("YEAR", "MONTH", "DAY")
                    )
                    sql = parse_one(query, read=dialect).sql("sqlite")
                    self.assertEqual(connection.execute(sql).fetchone(), fields)

                for query, expected in (
                    ("SELECT 2020 / 3 > 673", (0 if dialect == "postgres" else 1,)),
                    (
                        "SELECT CAST(2020 AS INTEGER) / 3 > 673",
                        (0 if dialect == "postgres" else 1,),
                    ),
                    ("SELECT -EXTRACT(YEAR FROM DATE '2020-06-01') / 3 < -673", (1,)),
                    ("SELECT EXTRACT(YEAR FROM CAST(NULL AS DATE)) / 3 IS NULL", (1,)),
                    (
                        "WITH dates(d) AS (VALUES ('2020-06-01')) "
                        "SELECT EXTRACT(YEAR FROM CAST(d AS DATE)) / 3 > 673 FROM dates",
                        (1,),
                    ),
                    (
                        "WITH t AS (SELECT EXTRACT(YEAR FROM DATE '2020-06-01') AS y) "
                        "SELECT CAST(y AS TEXT), y / 3 > 673, COALESCE(y, 0) / 3 > 673 FROM t",
                        ("2020", 1, 1),
                    ),
                    (
                        "SELECT EXTRACT(YEAR FROM COALESCE(CAST(NULL AS DATE), DATE '2020-06-01'))",
                        (2020,),
                    ),
                    (
                        "SELECT CAST(EXTRACT(YEAR FROM DATE '2020-06-01') AS INTEGER) / 3 > 673",
                        (0 if dialect == "postgres" else 1,),
                    ),
                    (
                        "SELECT (EXTRACT(YEAR FROM DATE '2020-06-01') / 3) / 2 > 336.5",
                        (1,),
                    ),
                ):
                    with self.subTest(dialect=dialect, query=query):
                        sql = parse_one(query, read=dialect).sql("sqlite")
                        self.assertEqual(connection.execute(sql).fetchone(), expected)
        finally:
            connection.close()

    def test_integer_division_dividend_is_real(self):
        # SQLite divides two INTEGER values with truncation. A source `/` that may truncate
        # (typed: PostgreSQL, Netezza, MySQL...) is written as an exact quotient unless an
        # operand is already REAL or both operands are integer by syntax (integer literals,
        # integer CASTs), which keep the source's truncating division.
        for source in ("netezza", "postgres", "mysql", "duckdb"):
            with self.subTest(source=source):
                self.validate_all(
                    "SELECT CAST(a AS REAL) / b, CAST(SUM(a) AS REAL) / SUM(b), "
                    "CAST((a + 1) AS REAL) / 2, CAST(COUNT(*) AS REAL) / COUNT(b)",
                    read={
                        source: "SELECT a / b, SUM(a) / SUM(b), (a + 1) / 2, COUNT(*) / COUNT(b)"
                    },
                )
        for source in ("netezza", "postgres"):
            with self.subTest(source=source):
                self.validate_all(
                    "SELECT 2020 / 3, (1000 - 64) / 10, 7 / 2 / 2",
                    read={source: "SELECT 2020 / 3, (1000 - 64) / 10, 7 / 2 / 2"},
                )
        self.validate_all(
            "SELECT CAST(2020 AS REAL) / 3, CAST((1000 - 64) AS REAL) / 10",
            read={"duckdb": "SELECT 2020 / 3, (1000 - 64) / 10"},
        )
        for source in ("netezza", "postgres"):
            with self.subTest(source=source):
                self.validate_all(
                    "SELECT a / 2.0, 100.00 * a / b, (CAST(a AS REAL) / b) / c, "
                    "CAST(a AS REAL) / b / c, a / CAST(b AS REAL), AVG(a) / b, "
                    "CAST(a AS INTEGER) / 3, CAST(a AS INTEGER) / CAST(b AS INTEGER), "
                    "(CAST(a AS INTEGER) + 1) / 2",
                    read={
                        source: "SELECT a / 2.0, 100.00 * a / b, (a / b) / c, a / b / c, "
                        "a / CAST(b AS DOUBLE PRECISION), AVG(a) / b, CAST(a AS INT) / 3, "
                        "CAST(a AS INT) / CAST(b AS INT), (CAST(a AS INT) + 1) / 2"
                    },
                )

    @requires_engine("sqlite")
    @requires_engine("duckdb")
    def test_integer_division_ratio_execution(self):
        # TPC-DS Q21 shape: a ratio of two integer SUMs inside BETWEEN 2.0/3.0 AND 3.0/2.0.
        # DuckDB divides exactly and is the oracle for the rows SQLite must return.
        setup = (
            "CREATE TABLE inv (w VARCHAR, item VARCHAR, d VARCHAR, q INTEGER)",
            "INSERT INTO inv VALUES "
            "('W1', 'a', '2000-01-01', 3), ('W1', 'a', '2000-02-01', 4), "
            "('W1', 'b', '2000-01-01', 3), ('W1', 'b', '2000-02-01', 2), "
            "('W1', 'c', '2000-01-01', 2), ('W1', 'c', '2000-02-01', 2), "
            "('W1', 'd', '2000-01-01', 1), ('W1', 'd', '2000-02-01', 2), "
            "('W1', 'e', '2000-01-01', 0), ('W1', 'e', '2000-02-01', 5)",
        )
        ratio = (
            "SELECT * FROM (SELECT w, item, "
            "SUM(CASE WHEN d < '2000-01-15' THEN q ELSE 0 END) AS inv_before, "
            "SUM(CASE WHEN d >= '2000-01-15' THEN q ELSE 0 END) AS inv_after "
            "FROM inv GROUP BY w, item) AS x "
            "WHERE (CASE WHEN inv_before > 0 THEN inv_after / inv_before ELSE NULL END) "
            "BETWEEN 2.0 / 3.0 AND 3.0 / 2.0 ORDER BY w, item"
        )
        quotients = (
            "SELECT item, SUM(q) / COUNT(*), CAST(SUM(q) AS INTEGER) / 2, SUM(q) / 2.0, "
            "(1000 - 64) / 10, 7 / 2 FROM inv GROUP BY item ORDER BY item"
        )
        for source in ("netezza", "postgres", "duckdb"):
            for query in (ratio, quotients):
                with self.subTest(source=source, query=query[:40]):
                    results = {}
                    for engine, target in (("sqlite", "sqlite"), ("duckdb", "duckdb")):
                        with engines.connect(engine) as connection:
                            for statement in setup:
                                connection.execute(statement)
                            translated = parse_one(query, read=source).sql(target)
                            results[engine] = connection.execute(translated)
                    if query is ratio:
                        self.assertEqual(
                            results["duckdb"],
                            [("W1", "a", 3, 4), ("W1", "b", 3, 2), ("W1", "c", 2, 2)],
                        )
                        self.assertEqual(results["sqlite"], results["duckdb"])
                    else:
                        # Every quotient but the explicit CAST(...) one is exact on both engines.
                        for sqlite_row, duckdb_row in zip(results["sqlite"], results["duckdb"]):
                            self.assertEqual(sqlite_row[0], duckdb_row[0])
                            self.assertAlmostEqual(sqlite_row[1], duckdb_row[1])
                            self.assertAlmostEqual(sqlite_row[3], duckdb_row[3])
                            literals = (93.6, 3.5) if source == "duckdb" else (93, 3)
                            self.assertEqual(sqlite_row[4:6], literals)

    @requires_engine("sqlite")
    @unittest.skipIf(sqlite3.sqlite_version_info < (3, 46), "Requires SQLite's floor modifier")
    def test_date_interval_month_year_execution(self):
        # Month and year offsets clamp at month end with SQLite's 'floor' modifier, so
        # they agree with calendar arithmetic; SQLite before 3.46 returns NULL for it.
        cases = (
            ("DATE '1994-01-01' + INTERVAL '1' YEAR", "1995-01-01"),
            ("DATE '1994-12-01' + INTERVAL '3' MONTH", "1995-03-01"),
            ("DATE '1996-02-28' + INTERVAL '1' YEAR", "1997-02-28"),
            ("DATE '2024-01-28' + INTERVAL '1' MONTH", "2024-02-28"),
            ("DATE '1994-01-01' - INTERVAL '1' MONTH", "1993-12-01"),
            ("DATE '1994-01-15' - INTERVAL '2' MONTH", "1993-11-15"),
            ("DATE '1994-03-10' - INTERVAL '-2' MONTH", "1994-05-10"),
            ("DATE '2024-03-01' - INTERVAL '1' YEAR", "2023-03-01"),
            ("CAST('1994-01-01' AS DATE) + INTERVAL '1' YEAR", "1995-01-01"),
            ("CAST(NULL AS DATE) + INTERVAL '1' MONTH", None),
        )
        with engines.connect("sqlite") as connection:
            for dialect in ("postgres", "duckdb"):
                for source, expected in cases:
                    with self.subTest(dialect=dialect, source=source):
                        sql = parse_one(f"SELECT {source}", read=dialect).sql(
                            "sqlite", unsupported_level=ErrorLevel.RAISE
                        )
                        rows = connection.execute(sql)
                        self.assertEqual(rows, [(expected,)])
                        self.assertIs(type(rows[0][0]), type(expected))

    @requires_engine("sqlite")
    @unittest.skipIf(sqlite3.sqlite_version_info < (3, 46), "Requires SQLite's floor modifier")
    def test_date_interval_text_comparison_execution(self):
        with engines.connect("sqlite") as connection:
            connection.execute("CREATE TABLE items (ship_date TEXT)")
            for value in ("1993-12-31", "1994-01-01", "1994-06-15", "1994-12-31", "1995-01-01"):
                connection.execute(f"INSERT INTO items VALUES ('{value}')")
            sql = parse_one(
                "SELECT ship_date FROM items WHERE ship_date >= DATE '1994-01-01' "
                "AND ship_date < DATE '1994-01-01' + INTERVAL '1' YEAR ORDER BY 1",
                read="postgres",
            ).sql("sqlite", unsupported_level=ErrorLevel.RAISE)
            self.assertEqual(
                connection.execute(sql), [("1994-01-01",), ("1994-06-15",), ("1994-12-31",)]
            )

    def test_extract_time_parts_and_unknown_input(self):
        for part, fmt in (
            ("YEAR", "%Y"),
            ("MONTH", "%m"),
            ("DAY", "%d"),
            ("HOUR", "%H"),
            ("MINUTE", "%M"),
            ("SECOND", "%S"),
        ):
            self.validate_all(
                f"SELECT CAST(STRFTIME('{fmt}', d) AS INTEGER) FROM t",
                read={
                    "postgres": f"SELECT EXTRACT({part} FROM d) FROM t",
                    "duckdb": f"SELECT EXTRACT({part} FROM d) FROM t",
                    "clickhouse": f"SELECT EXTRACT({part} FROM d) FROM t",
                },
            )
        self.validate_all(
            "SELECT CAST(CAST(STRFTIME('%Y', d) AS INTEGER) AS REAL) / 3 FROM t",
            read={"postgres": "SELECT EXTRACT(YEAR FROM d) / 3 FROM t"},
        )
        self.validate_all(
            "SELECT CAST(STRFTIME('%H', event_time) AS INTEGER), "
            "CAST(STRFTIME('%M', event_time) AS INTEGER), "
            "STRFTIME('%Y-%m-%d %H:%M:00', event_time) FROM events",
            read={
                "clickhouse": "SELECT EXTRACT(HOUR FROM event_time), "
                "EXTRACT(MINUTE FROM event_time), DATE_TRUNC('MINUTE', event_time) FROM events"
            },
        )
        for source in (
            "EXTRACT(YEAR FROM CAST(d AS TIMESTAMP))",
            "EXTRACT(SECOND FROM TIMESTAMP '2020-06-01 12:30:45.5')",
            "EXTRACT(MINUTE FROM DATE '2020-06-01')",
            "EXTRACT(WEEK FROM d)",
            "DATE_PART('hour', d)",
        ):
            with self.subTest(source=source), self.assertRaises(UnsupportedError):
                parse_one(source, read="postgres").sql("sqlite", unsupported_level=ErrorLevel.RAISE)

    @requires_engine("sqlite")
    def test_extract_time_parts_and_unknown_input_execution(self):
        with engines.connect("sqlite") as connection:
            connection.execute("CREATE TABLE t (d TEXT, ts TEXT)")
            connection.execute(
                "INSERT INTO t VALUES ('2024-02-29', '2024-02-29 13:45:07'), "
                "('1999-12-31', '1999-12-31 23:59:59.5'), (NULL, NULL)"
            )
            cases = (
                (
                    "SELECT EXTRACT(YEAR FROM d), EXTRACT(MONTH FROM d), EXTRACT(DAY FROM d) FROM t",
                    [(2024, 2, 29), (1999, 12, 31), (None, None, None)],
                ),
                (
                    "SELECT EXTRACT(HOUR FROM ts), EXTRACT(MINUTE FROM ts), EXTRACT(SECOND FROM ts) FROM t",
                    [(13, 45, 7), (23, 59, 59), (None, None, None)],
                ),
                ("SELECT EXTRACT(YEAR FROM d) / 3 FROM t", [(2024 / 3,), (1999 / 3,), (None,)]),
            )
            for dialect in ("postgres", "duckdb"):
                for source, expected in cases:
                    with self.subTest(dialect=dialect, source=source):
                        sql = parse_one(source, read=dialect).sql(
                            "sqlite", unsupported_level=ErrorLevel.RAISE
                        )
                        rows = connection.execute(sql)
                        self.assertEqual(rows, expected)
                        self.assertEqual(
                            [tuple(map(type, row)) for row in rows],
                            [tuple(map(type, row)) for row in expected],
                        )

    def test_date_trunc(self):
        for unit, fmt in (
            ("YEAR", "%Y-01-01 00:00:00"),
            ("MONTH", "%Y-%m-01 00:00:00"),
            ("DAY", "%Y-%m-%d 00:00:00"),
            ("HOUR", "%Y-%m-%d %H:00:00"),
            ("MINUTE", "%Y-%m-%d %H:%M:00"),
            ("SECOND", "%Y-%m-%d %H:%M:%S"),
        ):
            self.validate_all(
                f"SELECT STRFTIME('{fmt}', ts) FROM t",
                read={
                    dialect: f"SELECT DATE_TRUNC('{unit.lower()}', ts) FROM t"
                    for dialect in ("postgres", "duckdb", "clickhouse")
                },
            )
        self.validate_all(
            "SELECT STRFTIME('%Y-%m-%d 00:00:00', DATE('2020-03-17'))",
            read={"postgres": "SELECT DATE_TRUNC('day', DATE '2020-03-17')"},
        )
        for source in (
            "DATE_TRUNC('week', ts)",
            "DATE_TRUNC('quarter', ts)",
            "DATE_TRUNC('year', ts, 'UTC')",
            "DATE_TRUNC('day', CAST(ts AS TIMESTAMP))",
        ):
            with self.subTest(source=source), self.assertRaises(UnsupportedError):
                parse_one(source, read="postgres").sql("sqlite", unsupported_level=ErrorLevel.RAISE)

    @requires_engine("sqlite")
    def test_date_trunc_execution(self):
        with engines.connect("sqlite") as connection:
            connection.execute("CREATE TABLE t (ts TEXT)")
            connection.execute(
                "INSERT INTO t VALUES ('2024-02-29 13:45:07'), ('1999-12-31 23:59:59'), (NULL)"
            )
            expected = {
                "year": ("2024-01-01 00:00:00", "1999-01-01 00:00:00", None),
                "month": ("2024-02-01 00:00:00", "1999-12-01 00:00:00", None),
                "day": ("2024-02-29 00:00:00", "1999-12-31 00:00:00", None),
                "hour": ("2024-02-29 13:00:00", "1999-12-31 23:00:00", None),
                "minute": ("2024-02-29 13:45:00", "1999-12-31 23:59:00", None),
                "second": ("2024-02-29 13:45:07", "1999-12-31 23:59:59", None),
            }
            for dialect in ("postgres", "duckdb", "clickhouse"):
                for unit, values in expected.items():
                    source = f"SELECT DATE_TRUNC('{unit}', ts) FROM t"
                    with self.subTest(dialect=dialect, source=source):
                        sql = parse_one(source, read=dialect).sql(
                            "sqlite", unsupported_level=ErrorLevel.RAISE
                        )
                        rows = connection.execute(sql)
                        self.assertEqual(rows, [(value,) for value in values])
                        self.assertEqual(
                            [type(row[0]) for row in rows], [type(value) for value in values]
                        )

    def test_sqlite(self):
        # Range operators (IN/LIKE) are left-associative and can be chained.
        self.validate_identity("SELECT 1 IN (0) IN (1)")
        self.validate_identity("SELECT 1 LIKE 2 LIKE 3")
        # A leading NOT binds to the first term only and is parenthesized so the
        # chain does not re-associate (exp.In has no inline NOT to render).
        self.validate_identity("SELECT 1 NOT IN (0) IN (0, 1)", "SELECT (NOT 1 IN (0)) IN (0, 1)")
        self.validate_identity("SELECT x NOT IN (1) IS TRUE", "SELECT (NOT x IN (1)) IS TRUE")
        self.validate_identity("SELECT 0 NOT IN (1) NOT IN (2)", "SELECT NOT (NOT 0 IN (1)) IN (2)")
        self.validate_identity("WITH xyz(x) AS (SELECT 1) SELECT x FROM xyz")
        self.validate_all(
            "SELECT CHAR(72, 105, x)",
            write={
                "sqlite": "SELECT CHAR(72, 105, x)",
                "mysql": "SELECT CHAR(72, 105, x)",
                "clickhouse": "SELECT CHAR(72, 105, x)",
                "duckdb": "SELECT CHR(72) || CHR(105) || CHR(x)",
                "postgres": "SELECT CHR(72) || CHR(105) || CHR(x)",
                "snowflake": "SELECT CHR(72) || CHR(105) || CHR(x)",
                "bigquery": "SELECT CHR(72) || CHR(105) || CHR(x)",
                "oracle": "SELECT CHR(72) || CHR(105) || CHR(x)",
                "spark": "SELECT CHR(72) || CHR(105) || CHR(x)",
                "trino": "SELECT CHR(72) || CHR(105) || CHR(x)",
                "tsql": "SELECT CHAR(72) + CHAR(105) + CHAR(x)",
            },
        )
        self.validate_identity("SELECT * FROM t AS t INDEXED BY s.i")
        self.validate_identity("SELECT * FROM t INDEXED BY s.i")
        self.validate_identity("SELECT * FROM t INDEXED BY i")
        self.validate_identity("SELECT * FROM t NOT INDEXED")
        self.validate_identity("SELECT match FROM t")
        self.validate_identity("SELECT rowid FROM t1 WHERE t1 MATCH 'lorem'")
        self.validate_identity("SELECT * FROM t WHERE a REGEXP 'x'")
        self.validate_identity("SELECT GROUP_CONCAT(x ORDER BY y)")
        self.validate_identity("SELECT GROUP_CONCAT(x, ',' ORDER BY y)")
        self.validate_identity("SELECT GROUP_CONCAT(DISTINCT x ORDER BY y DESC)")
        self.validate_identity("SELECT GROUP_CONCAT(x, ',') OVER (PARTITION BY z ORDER BY y)")
        self.validate_all(
            "SELECT GROUP_CONCAT(x, ',' ORDER BY y)",
            write={
                "mysql": "SELECT GROUP_CONCAT(x ORDER BY y SEPARATOR ',')",
                "tsql": "SELECT STRING_AGG(x, ',') WITHIN GROUP (ORDER BY y)",
            },
        )
        self.validate_all(
            "SELECT GROUP_CONCAT(x, ',' ORDER BY y) OVER (PARTITION BY z)",
            write={"sqlite": UnsupportedError},
        )
        self.validate_all(
            "SELECT GROUP_CONCAT(x, ',') FILTER(WHERE y > 0) OVER (PARTITION BY z)",
            read={
                "duckdb": "SELECT STRING_AGG(x, ',' ORDER BY y) FILTER (WHERE y > 0) OVER (PARTITION BY z)",
            },
        )
        self.validate_identity(
            "SELECT SUM(z) OVER (PARTITION BY GROUP_CONCAT(x ORDER BY y)) FROM t GROUP BY z"
        )
        self.validate_identity("SELECT RANK() OVER (RANGE CURRENT ROW) FROM tbl")
        self.validate_identity(
            "SELECT RANK() OVER (RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) FROM tbl"
        )
        self.validate_identity(
            "SELECT RANK() OVER (RANGE BETWEEN CURRENT ROW AND CURRENT ROW) FROM tbl"
        )
        self.validate_identity(
            "SELECT RANK() OVER (ORDER BY x RANGE BETWEEN CURRENT ROW AND 1 + 1 FOLLOWING) FROM tbl"
        )
        self.validate_identity("UNHEX(a, b)")
        self.validate_identity("SELECT DATE()")
        self.validate_identity("SELECT DATE('now', 'start of month', '+1 month', '-1 day')")
        self.validate_all(
            "SELECT DATE('2020-01-31', '+1 month')",
            read={"sqlite": "SELECT DATE('2020-01-31', '+1 month')"},
            write={
                "sqlite": "SELECT DATE('2020-01-31', '+1 month')",
                "duckdb": "SELECT DATE('2020-01-31', '+1 month')",
            },
        )
        self.validate_all(
            "SELECT DATE('2020-03-15', 'start of month', '+1 day')",
            read={"sqlite": "SELECT DATE('2020-03-15', 'start of month', '+1 day')"},
            write={
                "sqlite": "SELECT DATE('2020-03-15', 'start of month', '+1 day')",
                "duckdb": "SELECT DATE('2020-03-15', 'start of month', '+1 day')",
            },
        )
        self.validate_all(
            "SELECT DATE(d, '1 DAY') FROM t",
            read={
                "duckdb": "SELECT DATE_ADD(d, INTERVAL 1 DAY) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE_ADD(d, '1'') ; SELECT 1; --')",
            write={"sqlite": "SELECT DATE(d, '1'') ; SELECT 1; --')"},
        )
        self.validate_all(
            "SELECT DATE(d, '-1 DAY') FROM t",
            read={
                "duckdb": "SELECT DATE_ADD(d, INTERVAL (-1) DAY) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, (n) || ' DAY') FROM t",
            read={
                "duckdb": "SELECT DATE_ADD(d, INTERVAL (n) DAY) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, (n + 1) || ' DAY') FROM t",
            read={
                "duckdb": "SELECT DATE_ADD(d, INTERVAL (n + 1) DAY) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, (-n) || ' MONTH', 'floor') FROM t",
            read={
                "duckdb": "SELECT DATE_ADD(d, INTERVAL (-n) MONTH) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, (NULL) || ' DAY') FROM t",
            read={
                "duckdb": "SELECT DATE_ADD(d, INTERVAL (NULL) DAY) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, '-1 MONTH', 'floor') FROM t",
            read={
                "bigquery": "SELECT DATE_SUB(d, INTERVAL 1 MONTH) FROM t",
                "mysql": "SELECT DATE_SUB(d, INTERVAL 1 MONTH) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, '2 DAY') FROM t",
            read={
                "mysql": "SELECT DATE_SUB(d, INTERVAL -2 DAY) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, '-2 DAY') FROM t",
            read={"mysql": "SELECT DATE_SUB(d, INTERVAL '+2' DAY) FROM t"},
        )
        self.validate_all(
            "SELECT DATE(d, (-n) || ' DAY') FROM t",
            read={
                "mysql": "SELECT DATE_SUB(d, INTERVAL n DAY) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, (-(n + 1)) || ' DAY') FROM t",
            read={
                "mysql": "SELECT DATE_SUB(d, INTERVAL (n + 1) DAY) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, (n) || ' DAY') FROM t",
            read={
                "mysql": "SELECT DATE_SUB(d, INTERVAL -n DAY) FROM t",
            },
        )
        # DATE drops the time component, so sub-day units use DATETIME instead
        self.validate_all(
            "SELECT DATETIME(d, '2 HOUR') FROM t",
            read={
                "mysql": "SELECT DATE_ADD(d, INTERVAL 2 HOUR) FROM t",
                "duckdb": "SELECT DATE_ADD(d, INTERVAL 2 HOUR) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATETIME(d, '30 MINUTE') FROM t",
            read={
                "mysql": "SELECT DATE_ADD(d, INTERVAL 30 MINUTE) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATETIME(d, '45 SECOND') FROM t",
            read={
                "mysql": "SELECT DATE_ADD(d, INTERVAL 45 SECOND) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATETIME(d, '-2 HOUR') FROM t",
            read={
                "mysql": "SELECT DATE_SUB(d, INTERVAL 2 HOUR) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATETIME(d, (n) || ' HOUR') FROM t",
            read={
                "duckdb": "SELECT DATE_ADD(d, INTERVAL (n) HOUR) FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, (-(SELECT 1)) || ' DAY') FROM t",
            read={"mysql": "SELECT DATE_SUB(d, INTERVAL (SELECT 1) DAY) FROM t"},
        )
        # SQLite has no INTERVAL, so date arithmetic operators become date modifiers
        self.validate_all(
            "SELECT DATE(d, '1 DAY') FROM t",
            read={
                "postgres": "SELECT d + INTERVAL '1 day' FROM t",
                "mysql": "SELECT d + INTERVAL 1 DAY FROM t",
                "duckdb": "SELECT INTERVAL 1 DAY + d FROM t",
            },
        )
        self.validate_all(
            "SELECT DATE(d, '-1 MONTH', 'floor') FROM t",
            read={"mysql": "SELECT d - INTERVAL 1 MONTH FROM t"},
        )
        self.validate_all(
            "SELECT DATETIME(d, '-2 HOURS') FROM t",
            read={"postgres": "SELECT d - INTERVAL '2 hours' FROM t"},
        )
        self.validate_all(
            "SELECT DATETIME(DATETIME(d), '1 DAY') FROM t",
            read={"postgres": "SELECT CAST(d AS TIMESTAMP) + INTERVAL '1 day' FROM t"},
        )
        self.validate_all(
            "SELECT DATETIME(DATETIME('2024-01-15 23:30:00'), '1 DAY')",
            read={
                "mysql": "SELECT TIMESTAMP '2024-01-15 23:30:00' + INTERVAL 1 DAY",
                "spark": "SELECT CAST('2024-01-15 23:30:00' AS TIMESTAMP_LTZ) + INTERVAL 1 DAY",
            },
        )
        self.validate_all(
            "SELECT DATETIME(CURRENT_TIMESTAMP, '-7 DAYS')",
            read={"postgres": "SELECT CURRENT_TIMESTAMP - INTERVAL '7 days'"},
        )
        self.validate_all(
            "SELECT DATE_SUB(d, '1'') ; SELECT 1; --')",
            write={"sqlite": "SELECT DATE(d, '-1'') ; SELECT 1; --')"},
        )
        self.validate_identity("SELECT DATETIME(1092941466, 'unixepoch')")
        self.validate_identity("SELECT DATETIME(1092941466, 'auto')")
        self.validate_identity("SELECT DATETIME(1092941466, 'unixepoch', 'localtime')")
        self.validate_identity("SELECT UNIXEPOCH()")
        self.validate_identity("SELECT JULIANDAY('now') - JULIANDAY('1776-07-04')")
        self.validate_identity("SELECT UNIXEPOCH() - UNIXEPOCH('2004-01-01 02:34:56')")
        self.validate_identity("SELECT DATE('now', 'start of year', '+9 months', 'weekday 2')")
        self.validate_identity("SELECT (JULIANDAY('now') - 2440587.5) * 86400.0")
        self.validate_identity("SELECT UNIXEPOCH('now', 'subsec')")
        self.validate_identity("SELECT TIMEDIFF('now', '1809-02-12')")
        self.validate_identity("SELECT * FROM GENERATE_SERIES(1, 5)")
        self.validate_all(
            "SELECT * FROM GENERATE_SERIES(1, 5 - 1)",
            read={"duckdb": "SELECT * FROM RANGE(1, 5)"},
        )
        self.validate_all(
            "SELECT * FROM GENERATE_SERIES(5, 1 + 1, -1)",
            read={"duckdb": "SELECT * FROM RANGE(5, 1, -1)"},
        )
        self.validate_identity("SELECT INSTR(haystack, needle)")
        self.validate_identity(
            "SELECT a, SUM(b) OVER (ORDER BY a ROWS BETWEEN -1 PRECEDING AND 1 FOLLOWING) FROM t1 ORDER BY 1"
        )
        self.validate_all(
            "SELECT JSON_EXTRACT('[10, 20, [30, 40]]', '$[2]', '$[0]', '$[1]')",
            write={
                "sqlite": "SELECT JSON_EXTRACT('[10, 20, [30, 40]]', '$[2]', '$[0]', '$[1]')",
                "duckdb": "SELECT TO_JSON(JSON_EXTRACT('[10, 20, [30, 40]]', ['$[2]', '$[0]', '$[1]']))",
            },
        )
        # Single-path json_extract() returns an SQL value like ->>, except that
        # object/array results keep the JSON subtype (json_subtype variant);
        # each of the three spellings round-trips to itself
        self.validate_identity("SELECT JSON_EXTRACT(a, '$.k') FROM t")
        self.validate_identity("SELECT a -> '$.k' FROM t")
        self.validate_identity("SELECT a ->> '$.k' FROM t")
        with self.assertLogs(helper_logger, level="WARNING") as logs:
            self.validate_identity("SELECT a -> 'it''s' FROM t")
            self.validate_identity("SELECT a ->> 'it''s' FROM t")
            self.assertEqual(len(logs.output), 2)
            self.assertTrue(all("Invalid JSON path syntax" in message for message in logs.output))
        self.validate_identity("SELECT JSON_SET('{}', '$.x', JSON_EXTRACT(a, '$.k'))")
        self.validate_identity("SELECT JSON_EXTRACT(a, '$') FROM t")
        self.validate_identity("SELECT JSON_EXTRACT(a, b) FROM t")
        fn = (
            self.parse_one("SELECT JSON_EXTRACT(a, '$.k') FROM t")
            .selects[0]
            .assert_is(exp.JSONExtractScalar)
        )
        op = self.parse_one("SELECT a ->> '$.k' FROM t").selects[0].assert_is(exp.JSONExtractScalar)
        self.assertTrue(fn.args.get("json_subtype"))
        self.assertNotEqual(fn, op)
        self.validate_all(
            "SELECT JSON_EXTRACT(a, '$.k') FROM t",
            write={
                "postgres": "SELECT JSON_EXTRACT_PATH_TEXT(a, 'k') FROM t",
                "mysql": "SELECT a ->> '$.k' FROM t",
            },
        )
        self.validate_all(
            "SELECT JSON_EXTRACT('[10, 20, [30, 40]]', '$[1]')",
            write={
                "sqlite": "SELECT JSON_EXTRACT('[10, 20, [30, 40]]', '$[1]')",
                "mysql": "SELECT CAST('[10, 20, [30, 40]]' AS JSON) ->> '$[1]'",
                "duckdb": "SELECT '[10, 20, [30, 40]]' ->> '$[1]'",
            },
        )
        self.validate_identity(
            """SELECT item AS "item", some AS "some" FROM data WHERE (item = 'value_1' COLLATE NOCASE) AND (some = 't' COLLATE NOCASE) ORDER BY item ASC LIMIT 1 OFFSET 0"""
        )
        self.validate_identity(
            "SELECT a FROM t1 WHERE a NOT NULL AND a NOT NULL ORDER BY a",
            "SELECT a FROM t1 WHERE NOT a IS NULL AND NOT a IS NULL ORDER BY a",
        )
        self.validate_identity(
            "SELECT a, b FROM t1 WHERE b + a NOT NULL ORDER BY 1",
            "SELECT a, b FROM t1 WHERE NOT b + a IS NULL ORDER BY 1",
        )
        self.validate_identity(
            "SELECT * FROM t1, t2",
            "SELECT * FROM t1 CROSS JOIN t2",
        )
        self.validate_identity(
            "ALTER TABLE t RENAME a TO b",
            "ALTER TABLE t RENAME COLUMN a TO b",
        )
        self.validate_identity("ALTER TABLE t1 RENAME TO t2")

        self.validate_all("SELECT LIKE(y, x)", write={"sqlite": "SELECT x LIKE y"})
        self.validate_all("SELECT GLOB('*y*', 'xyz')", write={"sqlite": "SELECT 'xyz' GLOB '*y*'"})
        self.validate_all(
            "SELECT LIKE('%y%', 'xyz', '')", write={"sqlite": "SELECT 'xyz' LIKE '%y%' ESCAPE ''"}
        )
        self.validate_all(
            "SELECT MIN(a, b) FROM t",
            read={
                "sqlite": "SELECT MIN(a, b) FROM t",
            },
            write={
                "sqlite": "SELECT MIN(a, b) FROM t",
                "duckdb": "SELECT CASE WHEN a IS NULL OR b IS NULL THEN NULL ELSE LEAST(a, b) END FROM t",
                "trino": "SELECT LEAST(a, b) FROM t",
                "oracle": "SELECT LEAST(a, b) FROM t",
            },
        )
        self.validate_all(
            "SELECT MAX(a, b) FROM t",
            read={
                "sqlite": "SELECT MAX(a, b) FROM t",
            },
            write={
                "sqlite": "SELECT MAX(a, b) FROM t",
                "duckdb": "SELECT CASE WHEN a IS NULL OR b IS NULL THEN NULL ELSE GREATEST(a, b) END FROM t",
                "trino": "SELECT GREATEST(a, b) FROM t",
                "oracle": "SELECT GREATEST(a, b) FROM t",
            },
        )
        # GREATEST/LEAST ignore NULL args in Postgres/DuckDB, but SQLite's
        # multi-arg MAX/MIN return NULL if any argument is NULL, so the args
        # are rewrapped as a rotation of COALESCEs to keep that behavior.
        self.validate_all(
            "SELECT MAX(COALESCE(a, b), COALESCE(b, a)) FROM t",
            read={"postgres": "SELECT GREATEST(a, b) FROM t"},
        )
        self.validate_all(
            "SELECT MIN(COALESCE(a, b), COALESCE(b, a)) FROM t",
            read={"postgres": "SELECT LEAST(a, b) FROM t"},
        )
        self.validate_all(
            "SELECT MAX(COALESCE(a, b, c), COALESCE(b, c, a), COALESCE(c, a, b)) FROM t",
            read={"duckdb": "SELECT GREATEST(a, b, c) FROM t"},
        )
        self.validate_all(
            "SELECT MIN(COALESCE(a, b, c), COALESCE(b, c, a), COALESCE(c, a, b)) FROM t",
            read={"postgres": "SELECT LEAST(a, b, c) FROM t"},
        )
        # Literal NULLs exercise the ignore-NULLs semantics: a NULL anywhere in
        # the arguments must be skipped and only an all-NULL input returns NULL.
        self.validate_all(
            "SELECT MAX(COALESCE(NULL, 1), COALESCE(1, NULL))",
            read={"postgres": "SELECT GREATEST(NULL, 1)"},
        )
        self.validate_all(
            "SELECT MIN(COALESCE(3, NULL, 1), COALESCE(NULL, 1, 3), COALESCE(1, 3, NULL))",
            read={"postgres": "SELECT LEAST(3, NULL, 1)"},
        )
        # MySQL GREATEST/LEAST propagate NULLs (same as SQLite), so no COALESCE
        # rotation is needed -- just rename to MAX/MIN.
        self.validate_all(
            "SELECT MAX(a, b) FROM t",
            read={"mysql": "SELECT GREATEST(a, b) FROM t"},
        )
        self.validate_all(
            "SELECT MIN(a, b) FROM t",
            read={"mysql": "SELECT LEAST(a, b) FROM t"},
        )
        # CONCAT and CONCAT_WS skip NULL args in SQLite, like in these dialects
        self.validate_all(
            "SELECT CONCAT(a, b) FROM t",
            read={
                "duckdb": "SELECT CONCAT(a, b) FROM t",
                "postgres": "SELECT CONCAT(a, b) FROM t",
                "tsql": "SELECT CONCAT(a, b) FROM t",
            },
            write={
                "duckdb": "SELECT CONCAT(a, b) FROM t",
                "mysql": "SELECT CONCAT(COALESCE(a, ''), COALESCE(b, '')) FROM t",
                "postgres": "SELECT CONCAT(a, b) FROM t",
                "snowflake": "SELECT CONCAT(COALESCE(a, ''), COALESCE(b, '')) FROM t",
                "sqlite": "SELECT CONCAT(a, b) FROM t",
            },
        )
        self.validate_all(
            "SELECT CONCAT_WS(',', a, b) FROM t",
            read={
                "duckdb": "SELECT CONCAT_WS(',', a, b) FROM t",
                "mysql": "SELECT CONCAT_WS(',', a, b) FROM t",
                "postgres": "SELECT CONCAT_WS(',', a, b) FROM t",
            },
            write={
                "duckdb": "SELECT CONCAT_WS(',', a, b) FROM t",
                "mysql": "SELECT CONCAT_WS(',', a, b) FROM t",
                "postgres": "SELECT CONCAT_WS(',', a, b) FROM t",
                "snowflake": "SELECT CONCAT_WS(',', COALESCE(a, ''), COALESCE(b, '')) FROM t",
                "sqlite": "SELECT CONCAT_WS(',', a, b) FROM t",
            },
        )
        # CONCAT propagates NULL in these dialects, so || already matches
        self.validate_all(
            "SELECT a || b FROM t",
            read={
                "mysql": "SELECT CONCAT(a, b) FROM t",
                "snowflake": "SELECT CONCAT(a, b) FROM t",
            },
        )
        self.validate_all(
            "SELECT CASE WHEN ',' IS NULL OR a IS NULL OR b IS NULL THEN NULL ELSE CONCAT_WS(',', a, b) END FROM t",
            read={"snowflake": "SELECT CONCAT_WS(',', a, b) FROM t"},
        )
        self.validate_all(
            "SELECT JSON_GROUP_ARRAY(name) FROM t",
            read={
                "postgres": "SELECT JSON_AGG(name) FROM t",
                "sqlite": "SELECT JSON_GROUP_ARRAY(name) FROM t",
            },
            write={
                "postgres": "SELECT JSON_AGG(name) FROM t",
            },
        )
        self.validate_all(
            "SELECT JSON_GROUP_OBJECT(name, value) FROM t",
            read={
                "postgres": "SELECT JSON_OBJECT_AGG(name, value) FROM t",
                "sqlite": "SELECT JSON_GROUP_OBJECT(name, value) FROM t",
            },
            write={
                "postgres": "SELECT JSON_OBJECT_AGG(name, value) FROM t",
            },
        )
        self.validate_all(
            "INSERT OR IGNORE INTO foo (x, y) VALUES (1, 2)",
            read={
                "mysql": "INSERT IGNORE INTO foo (x, y) VALUES (1, 2)",
                "sqlite": "INSERT OR IGNORE INTO foo (x, y) VALUES (1, 2)",
            },
        )
        self.validate_all(
            "CURRENT_DATE",
            read={
                "": "CURRENT_DATE",
                "snowflake": "CURRENT_DATE()",
            },
        )
        self.validate_all(
            "CURRENT_TIME",
            read={
                "": "CURRENT_TIME",
            },
        )
        self.validate_all(
            "CURRENT_TIMESTAMP",
            read={
                "": "CURRENT_TIMESTAMP",
                "snowflake": "CURRENT_TIMESTAMP()",
            },
        )
        self.validate_all(
            "SELECT DATE('2020-01-01 16:03:05')",
            read={
                "snowflake": "SELECT CAST('2020-01-01 16:03:05' AS DATE)",
            },
        )
        self.validate_all(
            "SELECT CAST([a].[b] AS SMALLINT) FROM foo",
            write={
                "sqlite": 'SELECT CAST("a"."b" AS INTEGER) FROM foo',
                "spark": "SELECT CAST(`a`.`b` AS SMALLINT) FROM foo",
            },
        )
        self.validate_all(
            "EDITDIST3(col1, col2)",
            read={
                "sqlite": "EDITDIST3(col1, col2)",
                "spark": "LEVENSHTEIN(col1, col2)",
            },
            write={
                "sqlite": "EDITDIST3(col1, col2)",
                "spark": "LEVENSHTEIN(col1, col2)",
            },
        )
        self.validate_all(
            "SELECT fname, lname, age FROM person ORDER BY age DESC NULLS FIRST, fname ASC NULLS LAST, lname",
            write={
                "spark": "SELECT fname, lname, age FROM person ORDER BY age DESC NULLS FIRST, fname ASC NULLS LAST, lname",
                "sqlite": "SELECT fname, lname, age FROM person ORDER BY age DESC NULLS FIRST, fname ASC NULLS LAST, lname",
            },
        )
        self.validate_all("x", read={"snowflake": "LEAST(x)"})
        self.validate_all("x", read={"postgres": "GREATEST(x)"})
        self.validate_all("MIN(x)", read={"snowflake": "MIN(x)"}, write={"snowflake": "MIN(x)"})
        self.validate_all(
            "MIN(x, y, z)",
            read={"snowflake": "LEAST(x, y, z)"},
            write={"snowflake": "LEAST(x, y, z)"},
        )
        self.validate_all(
            "UNICODE(x)",
            write={
                "": "UNICODE(x)",
                "duckdb": "NULLIF(UNICODE(x), -1)",
                "mysql": "ORD(CONVERT(x USING utf32))",
                "oracle": "ASCII(UNISTR(x))",
                "postgres": "ASCII(x)",
                "redshift": "ASCII(x)",
                "spark": "ASCII(x)",
            },
        )
        self.validate_identity("SELECT * FROM station WHERE city IS NOT ''")
        self.validate_identity("SELECT JSON_OBJECT('col1', 1, 'col2', '1')")
        self.validate_identity(
            'CREATE TABLE "foo t" ("foo t id" TEXT NOT NULL, PRIMARY KEY ("foo t id"))',
            'CREATE TABLE "foo t" ("foo t id" TEXT NOT NULL PRIMARY KEY)',
        )
        self.validate_identity("REPLACE INTO foo (x, y) VALUES (1, 2)", check_command_warning=True)
        self.validate_identity(
            "ATTACH DATABASE 'foo' AS schema_name", "ATTACH 'foo' AS schema_name"
        )
        self.validate_identity(
            "ATTACH DATABASE NOT EXISTS(SELECT 1) AS schema_name",
            "ATTACH NOT EXISTS(SELECT 1) AS schema_name",
        )
        self.validate_identity(
            "ATTACH DATABASE IIF(NOT EXISTS(SELECT 1), 'foo1', 'foo2') AS schema_name",
            "ATTACH IIF(NOT EXISTS(SELECT 1), 'foo1', 'foo2') AS schema_name",
        )
        self.validate_identity(
            "ATTACH DATABASE 'foo' || '.foo2' AS schema_name",
            "ATTACH 'foo' || '.foo2' AS schema_name",
        )
        self.validate_identity("DETACH DATABASE schema_name", "DETACH schema_name")
        self.validate_identity("SELECT * FROM t WHERE NULL IS y")
        self.validate_identity("SELECT * FROM t WHERE NULL IS NOT y")
        self.validate_identity("SELECT SQLITE_VERSION()")

    def test_unicode(self):
        for arg in ("''", "NULL", "'A'", "'Ω'", "'😀'", "LOWER('')"):
            with self.subTest(arg=arg):
                sql = f"SELECT UNICODE({arg})"
                self.validate_all(
                    sql,
                    write={
                        "sqlite": sql,
                        "duckdb": f"SELECT NULLIF(UNICODE({arg}), -1)",
                    },
                )

    def test_json_arrow_precedence(self):
        # ||, -> and ->> form a single left-associative precedence tier that binds
        # tighter than multiplication: https://sqlite.org/lang_expr.html
        expr = self.parse_one(
            "SELECT a.data ->> b.key ->> a.final_key FROM source AS a JOIN keys AS b ON TRUE"
        ).selects[0]
        expr.assert_is(exp.JSONExtractScalar).this.assert_is(exp.JSONExtractScalar)

        self.validate_identity("SELECT a.data ->> b.key ->> a.final_key FROM t")
        self.validate_identity("SELECT a -> b ->> c -> d FROM t")
        self.validate_identity("SELECT a || b ->> c", "SELECT (a || b) ->> c")
        self.validate_identity("SELECT a ->> b || c FROM t")
        self.validate_all(
            "SELECT a ->> ('$.' || f)",
            read={"spark": "SELECT GET_JSON_OBJECT(a, CONCAT('$.', f))"},
        )
        self.validate_all(
            "SELECT a -> ('$.' || f)",
            read={"mysql": "SELECT JSON_EXTRACT(a, CONCAT('$.', f))"},
        )
        self.validate_identity("SELECT 2 * 3 || 4", "SELECT 2 * (3 || 4)")
        self.validate_identity("SELECT 1 + 2 || 3", "SELECT 1 + (2 || 3)")
        self.validate_identity("SELECT a || b * c FROM t", "SELECT (a || b) * c FROM t")
        self.validate_identity("SELECT a - b ->> c FROM t", "SELECT a - (b ->> c) FROM t")
        self.validate_identity("SELECT a ->> b + c FROM t", "SELECT (a ->> b) + c FROM t")
        self.validate_identity("SELECT -a || b FROM t")
        self.validate_identity("SELECT a || b COLLATE NOCASE FROM t ORDER BY 1")
        self.validate_identity(
            "SELECT a COLLATE NOCASE || b -> c FROM t",
            "SELECT (a COLLATE NOCASE || b) -> c FROM t",
        )

        # COLLATE binds tighter than the ||, ->, ->> tier
        expr = self.parse_one("SELECT a || b COLLATE NOCASE FROM t").selects[0]
        expr.assert_is(exp.DPipe).expression.assert_is(exp.Collate)

        self.validate_identity(
            "SELECT a COLLATE NOCASE || b * c FROM t",
            "SELECT (a COLLATE NOCASE || b) * c FROM t",
        )
        self.validate_identity(
            "SELECT a COLLATE NOCASE -> b + c FROM t",
            "SELECT (a COLLATE NOCASE -> b) + c FROM t",
        )

    def test_now(self):
        self.validate_all(
            "SELECT DATE('nOw'), STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW')",
            write={
                "sqlite": "SELECT DATE('now'), STRFTIME('%Y-%m-%d %H:%M:%f', 'now')",
                "duckdb": "SELECT CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE), "
                "STRFTIME(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M:%S.%g')",
            },
        )
        self.validate_all(
            "SELECT STRFTIME('%Y-%m-%d %H:%M:%f', 'now')",
            read={"duckdb": "SELECT CURRENT_TIMESTAMP AT TIME ZONE 'UTC'"},
        )
        self.validate_identity("SELECT DATE(now), STRFTIME('%f', now) FROM t")

    def test_strftime(self):
        self.validate_identity("SELECT STRFTIME('%Y-%m-%d', '2016-10-16', 'start of month')")
        self.validate_identity(
            "SELECT STRFTIME('%s')",
            "SELECT STRFTIME('%s', 'now')",
        )

        self.validate_all(
            "SELECT STRFTIME('%Y-%m-%d', '2020-01-01 12:05:03')",
            write={
                "duckdb": "SELECT STRFTIME(CAST('2020-01-01 12:05:03' AS TIMESTAMP), '%Y-%m-%d')",
                "sqlite": "SELECT STRFTIME('%Y-%m-%d', '2020-01-01 12:05:03')",
            },
        )
        self.validate_all(
            "SELECT STRFTIME('%Y-%m-%d', CURRENT_TIMESTAMP)",
            write={
                "duckdb": "SELECT STRFTIME(CAST(CURRENT_TIMESTAMP AS TIMESTAMP), '%Y-%m-%d')",
                "sqlite": "SELECT STRFTIME('%Y-%m-%d', CURRENT_TIMESTAMP)",
            },
        )

    def test_strftime_fractional_seconds(self):
        self.validate_all(
            "SELECT STRFTIME('%Y-%m-%d %H:%M:%f', '2024-02-29 12:34:56.123')",
            write={
                "duckdb": "SELECT STRFTIME(CAST('2024-02-29 12:34:56.123' AS TIMESTAMP), '%Y-%m-%d %H:%M:%S.%g')",
                "sqlite": "SELECT STRFTIME('%Y-%m-%d %H:%M:%f', '2024-02-29 12:34:56.123')",
            },
        )
        for value in ("'2024-02-29 12:34:00.007'", "'2024-02-29 12:34:56'", "NULL"):
            self.validate_all(
                f"SELECT STRFTIME('%f', {value})",
                write={
                    "duckdb": f"SELECT STRFTIME(CAST({value} AS TIMESTAMP), '%S.%g')",
                    "sqlite": f"SELECT STRFTIME('%f', {value})",
                },
            )

        self.validate_all(
            "SELECT STRFTIME('%f', ts) FROM (SELECT '2024-02-29 12:34:56.123' AS ts) AS t",
            write={
                "duckdb": "SELECT STRFTIME(CAST(ts AS TIMESTAMP), '%S.%g') FROM (SELECT '2024-02-29 12:34:56.123' AS ts) AS t",
            },
        )

        for source, target in (
            ("%%f", "%%f"),
            ("%%%f", "%%%S.%g"),
            ("%%%%f", "%%%%f"),
            ("%S.%f", "%S.%S.%g"),
            ("%f|%%f|%f", "%S.%g|%%f|%S.%g"),
        ):
            self.validate_all(
                f"SELECT STRFTIME('{source}', '2024-02-29 12:34:56.123')",
                write={
                    "duckdb": f"SELECT STRFTIME(CAST('2024-02-29 12:34:56.123' AS TIMESTAMP), '{target}')",
                    "sqlite": f"SELECT STRFTIME('{source}', '2024-02-29 12:34:56.123')",
                },
            )

        self.validate_all(
            "SELECT STRFTIME('%f')",
            write={
                "sqlite": "SELECT STRFTIME('%f', 'now')",
                "duckdb": "SELECT STRFTIME(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', '%S.%g')",
            },
        )
        self.validate_identity("SELECT STRFTIME('%f', CURRENT_TIMESTAMP)")
        self.validate_identity("SELECT STRFTIME('%f', '2024-02-29 12:34:56.123', '+1 second')")
        self.validate_identity(
            "SELECT STRFTIME(fmt, '2024-02-29 12:34:56.123') FROM (SELECT '%f' AS fmt) AS t"
        )

    def test_datediff(self):
        self.assertEqual(
            parse_one("DATEDIFF(a, b, 'day')").sql("sqlite", unsupported_level=ErrorLevel.RAISE),
            "CAST((JULIANDAY(a) - JULIANDAY(b)) AS INTEGER)",
        )
        self.validate_all(
            "DATEDIFF(a, b, 'day')",
            write={"sqlite": "CAST((JULIANDAY(a) - JULIANDAY(b)) AS INTEGER)"},
        )
        self.validate_all(
            "CAST((JULIANDAY(a, 'start of day') - JULIANDAY(b, 'start of day')) AS INTEGER)",
            read={
                "duckdb": "DATE_DIFF('day', b, a)",
                "mysql": "DATEDIFF(a, b)",
                "snowflake": "DATEDIFF(DAY, b, a)",
            },
        )
        self.validate_all(
            "DATEDIFF(a, b, 'hour')",
            write={"sqlite": "CAST((JULIANDAY(a) - JULIANDAY(b)) * 24.0 AS INTEGER)"},
        )
        self.validate_all(
            "DATEDIFF(a, b, 'year')",
            write={"sqlite": "CAST((JULIANDAY(a) - JULIANDAY(b)) / 365.0 AS INTEGER)"},
        )
        self.validate_all(
            "DATEDIFF(a, b, 'nanosecond')",
            write={"sqlite": "CAST((JULIANDAY(a) - JULIANDAY(b)) * 86400000000000.0 AS INTEGER)"},
        )
        self.validate_all(
            "CAST((JULIANDAY(a) - JULIANDAY(b)) * 86400000000000.0 AS INTEGER)",
            read={
                "snowflake": "DATEDIFF(NANOSECOND, b, a)",
                "tsql": "DATEDIFF_BIG(NANOSECOND, b, a)",
            },
        )

    def test_datediff_day_boundary(self):
        sql = parse_one(
            "SELECT DATE_DIFF('day', a, b), DATE_DIFF('day', b, a) "
            "FROM (SELECT TIMESTAMP '2024-03-01 23:59:59' AS a, "
            "TIMESTAMP '2024-03-02 00:00:00' AS b) AS t",
            dialect="duckdb",
        ).sql("sqlite")
        with closing(sqlite3.connect(":memory:")) as conn:
            self.assertEqual(conn.execute(sql).fetchone(), (1, -1))

    def test_hexadecimal_literal(self):
        for sql, expected in (
            ("0XCC", "204"),
            ("0xcc", "204"),
            ("0x0", "0"),
            ("0x1F + 1", "31 + 1"),
            ("-0x10", "-16"),
            ("0x7FFFFFFFFFFFFFFF", "9223372036854775807"),
            ("0x8000000000000000", "-9223372036854775808"),
            ("0xFFFFFFFFFFFFFFFF", "-1"),
            ("-0xFFFFFFFFFFFFFFFF", "- -1"),
        ):
            with self.subTest(sql=sql):
                self.validate_all(
                    f"SELECT {sql}",
                    write={
                        "sqlite": f"SELECT {expected}",
                        "duckdb": f"SELECT {expected}",
                        "mysql": f"SELECT {expected}",
                    },
                )

        self.validate_all(
            "SELECT x'CC'",
            write={
                "sqlite": "SELECT x'CC'",
                "duckdb": "SELECT UNHEX('CC')",
                "mysql": "SELECT x'CC'",
            },
        )
        self.validate_identity("SELECT X'0a', 0x0a", "SELECT x'0a', 10")

    def test_digit_separators(self):
        self.validate_identity("SELECT 1_000", "SELECT 1000")
        self.validate_identity("SELECT 1_000_000 AS x", "SELECT 1000000 AS x")
        self.validate_identity("SELECT 1_0.2_5", "SELECT 10.25")
        self.validate_identity("SELECT .5_5", "SELECT 0.55")

    def test_window_null_treatment(self):
        self.validate_all(
            "SELECT FIRST_VALUE(Name) OVER (PARTITION BY AlbumId ORDER BY Bytes DESC) AS LargestTrack FROM tracks",
            write={
                "sqlite": "SELECT FIRST_VALUE(Name) OVER (PARTITION BY AlbumId ORDER BY Bytes DESC) AS LargestTrack FROM tracks"
            },
        )

    def test_floating_point_types(self):
        # REAL, FLOAT and DOUBLE are all 8-byte floats in SQLite, so they transpile to
        # DOUBLE rather than to the 4-byte REAL/FLOAT of other dialects
        for dtype in ("REAL", "FLOAT", "DOUBLE"):
            self.validate_all(
                f"SELECT CAST(x AS {dtype}) FROM t",
                write={
                    "sqlite": "SELECT CAST(x AS REAL) FROM t",
                    "duckdb": "SELECT CAST(x AS DOUBLE) FROM t",
                    "postgres": "SELECT CAST(x AS DOUBLE PRECISION) FROM t",
                },
            )

        self.validate_all(
            "CREATE TABLE t (x REAL, y FLOAT)",
            write={
                "sqlite": "CREATE TABLE t (x REAL, y REAL)",
                "duckdb": "CREATE TABLE t (x DOUBLE, y DOUBLE)",
                "postgres": "CREATE TABLE t (x DOUBLE PRECISION, y DOUBLE PRECISION)",
            },
        )

    def test_longvarchar_dtype(self):
        self.validate_all(
            "CREATE TABLE foo (bar LONGVARCHAR)",
            write={"sqlite": "CREATE TABLE foo (bar TEXT)"},
        )

    def test_warnings(self):
        with self.assertLogs(helper_logger) as cm:
            self.validate_identity(
                "SELECT * FROM t AS t(c1, c2)",
                "SELECT * FROM t AS t",
            )

            self.assertIn("Named columns are not supported in table alias.", cm.output[0])

    def test_trunc(self):
        # SQLite TRUNC only accepts one argument
        self.validate_identity("TRUNC(3.14)").assert_is(exp.Trunc)

        # Decimals arg is dropped with warning (best-effort transpilation)
        with self.assertLogs(helper_logger) as cm:
            self.validate_identity("TRUNC(3.14, 2)", "TRUNC(3.14)").assert_is(exp.Trunc)
            self.assertIn("'decimals' is not supported", cm.output[0])

    def test_generated_columns(self):
        # SQLite uses STORED/VIRTUAL; PERSISTED is T-SQL and is rejected by SQLite.
        # https://www.sqlite.org/gencol.html
        self.validate_identity("CREATE TABLE t (a INTEGER, b INTEGER AS (a * 2) STORED)")
        self.validate_identity("CREATE TABLE t (a INTEGER, b INTEGER AS (a * 2))")
        # Typeless computed columns must keep STORED/VIRTUAL (not parse them as types).
        self.validate_identity("CREATE TABLE t (a INTEGER, b AS (a * 2) STORED)")
        self.validate_identity("CREATE TABLE t (a INTEGER, b AS (a * 2))")
        self.validate_identity(
            "CREATE TABLE t (a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2) STORED)",
            "CREATE TABLE t (a INTEGER, b INTEGER AS (a * 2) STORED)",
        )
        self.validate_identity(
            "CREATE TABLE t (a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2) VIRTUAL)",
            "CREATE TABLE t (a INTEGER, b INTEGER AS (a * 2))",
        )
        self.validate_identity(
            "CREATE TABLE t (a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2))",
            "CREATE TABLE t (a INTEGER, b INTEGER AS (a * 2))",
        )
        # True identity maps to AUTOINCREMENT; SQLite requires PRIMARY KEY.
        self.validate_identity(
            "CREATE TABLE t (a INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY)",
            "CREATE TABLE t (a INTEGER PRIMARY KEY AUTOINCREMENT)",
        )
        self.validate_all(
            "CREATE TABLE t (a INTEGER, b AS (a * 2) STORED NOT NULL)",
            read={"tsql": "CREATE TABLE t (a INT, b AS (a * 2) PERSISTED NOT NULL)"},
        )

    def test_identity_columns(self):
        for generated in ("ALWAYS", "BY DEFAULT", "BY DEFAULT ON NULL"):
            for options in ("", " (START WITH 10 INCREMENT BY 2)"):
                identity = f"GENERATED {generated} AS IDENTITY{options}"
                for schema, expected in (
                    (f"a INT {identity}", "a INTEGER"),
                    (
                        f"a INT {identity} PRIMARY KEY",
                        "a INTEGER PRIMARY KEY AUTOINCREMENT",
                    ),
                    (
                        f"a INT {identity}, PRIMARY KEY (a)",
                        "a INTEGER PRIMARY KEY AUTOINCREMENT",
                    ),
                    (
                        f"a INT {identity}, b INT PRIMARY KEY",
                        "a INTEGER, b INTEGER PRIMARY KEY",
                    ),
                    (
                        f"a INT {identity}, b INT, PRIMARY KEY (a, b)",
                        "a INTEGER, b INTEGER, PRIMARY KEY (a, b)",
                    ),
                ):
                    with self.subTest(schema):
                        self.validate_all(
                            f"CREATE TABLE t ({expected})",
                            read={"": f"CREATE TABLE t ({schema})"},
                        )

    def test_unsupported_auto_increment(self):
        for dialect, constraint, expected in (
            ("postgres", "GENERATED BY DEFAULT AS IDENTITY", "a INTEGER"),
            ("mysql", "UNIQUE AUTO_INCREMENT", "a INTEGER UNIQUE"),
            ("snowflake", "AUTOINCREMENT", "a INTEGER"),
        ):
            with self.subTest(dialect):
                expression = parse_one(f"CREATE TABLE t (a INT {constraint})", read=dialect)
                message = "SQLite AUTOINCREMENT requires an INTEGER PRIMARY KEY"

                for level in (ErrorLevel.RAISE, ErrorLevel.IMMEDIATE):
                    with self.assertRaisesRegex(UnsupportedError, message):
                        expression.sql("sqlite", unsupported_level=level)

                with self.assertLogs(generator_logger) as cm:
                    self.assertEqual(expression.sql("sqlite"), f"CREATE TABLE t ({expected})")
                self.assertIn(message, cm.output[0])

                self.validate_all(
                    f"CREATE TABLE t ({expected})",
                    read={dialect: f"CREATE TABLE t (a INT {constraint})"},
                )

    def test_update_or(self):
        for action in ("ABORT", "FAIL", "IGNORE", "REPLACE", "ROLLBACK"):
            with self.subTest(action=action):
                update = self.validate_identity(f"UPDATE OR {action} t SET b = 'x' WHERE id = 1")
                self.assertIsInstance(update, exp.Update)
                self.assertEqual(update.args["alternative"], action)

        sql = "UPDATE OR IGNORE t SET b = 'x'"
        self.validate_all(
            sql,
            read={"": sql, "sqlite": "update or ignore t set b = 'x'"},
            write={"": sql, "postgres": UnsupportedError, "teradata": UnsupportedError},
        )
        with self.assertRaises(ParseError):
            self.parse_one("UPDATE OR t SET b = 'x'")

    def test_ddl(self):
        for conflict_action in ("ABORT", "FAIL", "IGNORE", "REPLACE", "ROLLBACK"):
            with self.subTest(f"ON CONFLICT {conflict_action}"):
                self.validate_identity(
                    f"CREATE TABLE a (b, c, UNIQUE (b, c) ON CONFLICT {conflict_action})"
                )

        for constraint in (
            "PRIMARY KEY ON CONFLICT REPLACE",
            "NOT NULL ON CONFLICT IGNORE",
            "NULL ON CONFLICT IGNORE",
            "UNIQUE ON CONFLICT FAIL",
            "CONSTRAINT pk PRIMARY KEY ASC ON CONFLICT REPLACE AUTOINCREMENT",
            "NOT NULL ON CONFLICT ABORT UNIQUE ON CONFLICT ROLLBACK",
        ):
            with self.subTest(constraint=constraint):
                self.validate_identity(f"CREATE TABLE a (x INTEGER {constraint})")

        self.validate_identity("CREATE TABLE a (x INTEGER NULL)")
        for columns in (
            "x INTEGER NOT NULL",
            "x INTEGER NULL",
            "x INTEGER PRIMARY KEY",
            "x INTEGER UNIQUE",
            "x INTEGER, UNIQUE (x)",
        ):
            with self.subTest(columns=columns), self.assertRaises(ParseError):
                self.parse_one(f"CREATE TABLE a ({columns} ON CONFLICT DO NOTHING)")

        self.validate_identity("CREATE TABLE over (x, y)")
        self.validate_identity("INSERT OR ABORT INTO foo (x, y) VALUES (1, 2)")
        self.validate_identity("INSERT OR FAIL INTO foo (x, y) VALUES (1, 2)")
        self.validate_identity("INSERT OR IGNORE INTO foo (x, y) VALUES (1, 2)")
        self.validate_identity("INSERT OR REPLACE INTO foo (x, y) VALUES (1, 2)")
        self.validate_identity("INSERT OR ROLLBACK INTO foo (x, y) VALUES (1, 2)")
        self.validate_identity("INSERT INTO foo AS f (x, y) VALUES (1, 2)")
        self.validate_identity(
            "INSERT INTO foo AS f (x, y) VALUES (1, 2) ON CONFLICT(x) DO UPDATE SET y = f.y + 1"
        )
        self.validate_identity(
            "INSERT INTO tbl (x, y) SELECT 1, 'a' WHERE TRUE ON CONFLICT(x, LOWER(y)) DO UPDATE SET y = excluded.y"
        )
        self.validate_identity(
            "INSERT INTO tbl (x, y) VALUES (1, 'a') ON CONFLICT(x, LOWER(y) COLLATE NOCASE ASC, y DESC) DO NOTHING"
        )
        self.validate_identity(
            "INSERT INTO tbl (x, y) VALUES (1, 'a') ON CONFLICT(CASE WHEN x > 0 THEN x ELSE -x END) DO NOTHING"
        )
        self.validate_identity(
            "INSERT INTO tbl (x, y) VALUES (1, 'a') ON CONFLICT(x) WHERE x > 0 DO UPDATE SET y = excluded.y"
        )
        self.validate_identity("CREATE TABLE foo (id INTEGER PRIMARY KEY ASC)")
        self.validate_identity("CREATE TEMPORARY TABLE foo (id INTEGER)")
        self.validate_identity("CREATE VIRTUAL TABLE docs USING fts5(title, content)")
        self.validate_identity("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(title, content)")
        self.validate_identity("CREATE VIRTUAL TABLE main.docs USING fts5(title, content)")
        self.validate_identity(
            "CREATE VIRTUAL TABLE demo_index USING rtree(id, minX, maxX, minY, maxY)"
        )
        self.validate_identity("CREATE VIRTUAL TABLE t USING module_name")
        self.validate_identity("PRAGMA table_info")
        self.validate_identity("PRAGMA schema")
        self.validate_identity("PRAGMA full_column_names = on")
        self.validate_identity("PRAGMA full_column_names = off")
        self.validate_identity("PRAGMA cache_size = 2000")
        self.validate_identity("PRAGMA foo = -2000")
        self.validate_identity("PRAGMA foo(-2000)", "PRAGMA foo = -2000")
        self.validate_identity("PRAGMA encoding = 'UTF-16'")
        self.validate_identity("PRAGMA main.cache_size")
        self.validate_identity("PRAGMA main.cache_size = 2000")
        self.validate_identity("PRAGMA cache_size(2000)", "PRAGMA cache_size = 2000")
        self.validate_identity("PRAGMA main.cache_size(2000)", "PRAGMA main.cache_size = 2000")

        self.validate_all(
            """
            CREATE TABLE "Track"
            (
                CONSTRAINT "PK_Track" FOREIGN KEY ("TrackId"),
                FOREIGN KEY ("AlbumId") REFERENCES "Album" (
                    "AlbumId"
                ) ON DELETE NO ACTION ON UPDATE NO ACTION,
                FOREIGN KEY ("AlbumId") ON DELETE CASCADE ON UPDATE RESTRICT,
                FOREIGN KEY ("AlbumId") ON DELETE SET NULL ON UPDATE SET DEFAULT
            )
            """,
            write={
                "sqlite": """CREATE TABLE "Track" (
  CONSTRAINT "PK_Track" FOREIGN KEY ("TrackId"),
  FOREIGN KEY ("AlbumId") REFERENCES "Album" (
    "AlbumId"
  ) ON DELETE NO ACTION ON UPDATE NO ACTION,
  FOREIGN KEY ("AlbumId") ON DELETE CASCADE ON UPDATE RESTRICT,
  FOREIGN KEY ("AlbumId") ON DELETE SET NULL ON UPDATE SET DEFAULT
)""",
            },
            pretty=True,
        )
        self.validate_all(
            "CREATE TABLE z (a INTEGER UNIQUE PRIMARY KEY AUTOINCREMENT)",
            read={
                "mysql": "CREATE TABLE z (a INT UNIQUE PRIMARY KEY AUTO_INCREMENT)",
                "postgres": "CREATE TABLE z (a INT GENERATED BY DEFAULT AS IDENTITY NOT NULL UNIQUE PRIMARY KEY)",
            },
            write={
                "sqlite": "CREATE TABLE z (a INTEGER UNIQUE PRIMARY KEY AUTOINCREMENT)",
                "mysql": "CREATE TABLE z (a INT UNIQUE PRIMARY KEY AUTO_INCREMENT)",
                "postgres": "CREATE TABLE z (a INT GENERATED BY DEFAULT AS IDENTITY NOT NULL UNIQUE PRIMARY KEY)",
            },
        )
        for constraint in ("PRIMARY KEY AUTO_INCREMENT", "AUTO_INCREMENT PRIMARY KEY"):
            with self.subTest(constraint):
                self.validate_all(
                    "CREATE TABLE z (a INTEGER PRIMARY KEY AUTOINCREMENT)",
                    read={
                        "mysql": f"CREATE TABLE z (a INT {constraint})",
                    },
                    write={
                        "sqlite": "CREATE TABLE z (a INTEGER PRIMARY KEY AUTOINCREMENT)",
                        "mysql": "CREATE TABLE z (a INT PRIMARY KEY AUTO_INCREMENT)",
                    },
                )
        self.validate_all(
            """CREATE TABLE "x" ("Name" NVARCHAR(200) NOT NULL)""",
            write={
                "sqlite": """CREATE TABLE "x" ("Name" TEXT(200) NOT NULL)""",
                "mysql": "CREATE TABLE `x` (`Name` VARCHAR(200) NOT NULL)",
            },
        )

        self.validate_identity(
            "CREATE TABLE store (store_id INTEGER PRIMARY KEY AUTOINCREMENT, mgr_id INTEGER NOT NULL UNIQUE REFERENCES staff ON UPDATE CASCADE)"
        )

    def test_table_options(self):
        self.validate_identity("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT) STRICT")
        self.validate_identity("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT) WITHOUT ROWID")
        self.validate_identity(
            "CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT) WITHOUT ROWID, STRICT"
        )
        self.validate_identity(
            "CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT) STRICT, WITHOUT ROWID"
        )
        self.validate_all(
            "CREATE TABLE t (a INTEGER PRIMARY KEY) WITHOUT ROWID",
            write={
                "sqlite": "CREATE TABLE t (a INTEGER PRIMARY KEY) WITHOUT ROWID",
                "duckdb": "CREATE TABLE t (a INT PRIMARY KEY)",
            },
        )

    def test_analyze(self):
        self.validate_identity("ANALYZE tbl")
        self.validate_identity("ANALYZE schma.tbl")

    def test_reindex(self):
        self.validate_identity("REINDEX tbl", check_command_warning=True)
        self.validate_identity("REINDEX schma.idx", check_command_warning=True)
        self.validate_identity("SELECT reindex FROM t")

    def test_end_transaction(self):
        for sql in ("END", "END TRANSACTION"):
            with self.subTest(sql=sql):
                self.assertIsInstance(self.validate_identity(sql, "COMMIT"), exp.Commit)
                block = self.validate_identity(f"BEGIN; {sql}", "BEGIN TRANSACTION; COMMIT")
                self.assertIsInstance(block.expressions[-1], exp.Commit)

        self.validate_identity("end + 1")

    def test_create_trigger(self):
        """Test that SQLite CREATE TRIGGER statements fall back to Command parsing."""
        self.validate_identity(
            "CREATE TRIGGER log_insert AFTER INSERT ON users BEGIN INSERT INTO audit_log (user_id, action, created_at) VALUES (NEW.id, 'INSERT', datetime('now')) END",
            check_command_warning=True,
        )

        self.validate_identity(
            "CREATE TRIGGER check_balance BEFORE UPDATE OF balance ON accounts WHEN NEW.balance < 0 BEGIN UPDATE accounts SET balance = 0 WHERE id = NEW.id END",
            check_command_warning=True,
        )

        self.validate_identity(
            "CREATE TRIGGER view_insert INSTEAD OF INSERT ON employee_view BEGIN INSERT INTO employees (id, name, department) VALUES (NEW.id, NEW.name, NEW.department) END",
            check_command_warning=True,
        )

        for prefix in ("", "EXPLAIN QUERY PLAN "):
            trigger = (
                f"{prefix}CREATE TEMP TRIGGER tr AFTER INSERT ON t BEGIN "
                "SELECT CASE WHEN NEW.x THEN 1 END; SELECT 2; END"
            )
            with self.subTest(prefix=prefix), self.assertLogs(parser_logger):
                self.validate_identity(
                    f"BEGIN; {trigger}; END", f"BEGIN TRANSACTION; {trigger}; COMMIT"
                )

    def test_is_operator_null_safe_comparison(self):
        # issue #8855: SQLite's IS / IS NOT is a null-safe equality against any
        # expression; DuckDB and Postgres only accept NULL/TRUE/FALSE/UNKNOWN
        # on the right side. Non-trivial right sides parse as IS
        # [NOT] DISTINCT FROM so each transpiler emits the syntax it supports,
        # while the SQLite round trip keeps IS / IS NOT.
        self.validate_transpile(
            "SELECT a IS b, a IS NOT b FROM t",
            "SELECT a IS NOT DISTINCT FROM b, a IS DISTINCT FROM b FROM t",
            write_dialect="duckdb",
        )
        self.validate_transpile(
            "SELECT a IS b FROM t",
            "SELECT a IS NOT DISTINCT FROM b FROM t",
            write_dialect="postgres",
        )
        self.validate_identity("SELECT a IS b, a IS NOT b FROM t")
        self.validate_identity(
            "SELECT a IS NULL, a IS NOT NULL FROM t",
            "SELECT a IS NULL, NOT a IS NULL FROM t",
        )

        for rhs in ("TRUE", "FALSE", "NULL"):
            for wrapped in (f"({rhs})", f"(({rhs}))"):
                for negate in (False, True):
                    sql = f"SELECT 2 IS {'NOT ' if negate else ''}{wrapped}"
                    expected = f"SELECT {'NOT ' if negate else ''}2 IS {rhs}"
                    self.validate_all(sql, write={"sqlite": expected, "duckdb": expected})
                    expression = self.parse_one(sql).expressions[0]
                    self.assertIsInstance(expression.this if negate else expression, exp.Is)

    def test_date_integer_arithmetic_counts_days(self):
        for source, expected in (
            ("CAST(d AS DATE) + 5", "DATE(DATE(d), '5 DAY')"),
            ("5 + CAST(d AS DATE)", "DATE(DATE(d), '5 DAY')"),
            ("CAST(d AS DATE) - 5", "DATE(DATE(d), '-5 DAY')"),
            (
                "CAST(d AS DATE) + (CAST(n AS INT) + 1)",
                "DATE(DATE(d), (CAST(n AS INTEGER) + 1) || ' DAY')",
            ),
            ("CAST(d AS DATE) - CAST(n AS INT)", "DATE(DATE(d), (-CAST(n AS INTEGER)) || ' DAY')"),
            (
                "CAST(d AS DATE) - CAST(e AS DATE)",
                "CAST((JULIANDAY(DATE(d)) - JULIANDAY(DATE(e))) AS INTEGER)",
            ),
        ):
            for dialect in ("postgres", "duckdb", "netezza"):
                with self.subTest(dialect=dialect, source=source):
                    sql = parse_one(f"SELECT {source} FROM t", read=dialect).sql(
                        "sqlite", unsupported_level=ErrorLevel.RAISE
                    )
                    self.assertEqual(sql, f"SELECT {expected} FROM t")

        for source in (
            "d + 5",
            "n + 5",
            "CAST(d AS DATE) + 1.5",
            "CAST(d AS DATE) + n",
            "5 - CAST(d AS DATE)",
        ):
            with self.subTest(source=source):
                sql = parse_one(f"SELECT {source} FROM t", read="postgres").sql("sqlite")
                self.assertNotIn("DAY", sql)
                self.assertNotIn("JULIANDAY", sql)

    def test_date_truncation_compares_with_a_date(self):
        for source, expected in (
            (
                "DATE_TRUNC('month', d) = CAST(e AS DATE)",
                "STRFTIME('%Y-%m-01 00:00:00', d) = DATETIME(DATE(e))",
            ),
            (
                "CAST(e AS DATE) <> DATE_TRUNC('month', d)",
                "DATETIME(DATE(e)) <> STRFTIME('%Y-%m-01 00:00:00', d)",
            ),
            (
                "DATE_TRUNC('year', d) BETWEEN CAST(e AS DATE) AND CAST(f AS DATE)",
                "STRFTIME('%Y-01-01 00:00:00', d) BETWEEN DATETIME(DATE(e)) AND DATETIME(DATE(f))",
            ),
            (
                "CAST(e AS DATE) BETWEEN DATE_TRUNC('year', d) AND DATE_TRUNC('year', f)",
                "DATETIME(DATE(e)) BETWEEN STRFTIME('%Y-01-01 00:00:00', d) "
                "AND STRFTIME('%Y-01-01 00:00:00', f)",
            ),
            (
                "DATE_TRUNC('day', d) >= CAST(e AS DATE) AND DATE_TRUNC('day', d) < CAST(f AS DATE)",
                "STRFTIME('%Y-%m-%d 00:00:00', d) >= DATETIME(DATE(e)) "
                "AND STRFTIME('%Y-%m-%d 00:00:00', d) < DATETIME(DATE(f))",
            ),
            (
                "DATE_TRUNC('day', d) <= CAST(e AS DATE) OR DATE_TRUNC('day', d) > CAST(f AS DATE)",
                "STRFTIME('%Y-%m-%d 00:00:00', d) <= DATETIME(DATE(e)) "
                "OR STRFTIME('%Y-%m-%d 00:00:00', d) > DATETIME(DATE(f))",
            ),
            ("DATE_TRUNC('month', d) = e", "STRFTIME('%Y-%m-01 00:00:00', d) = e"),
            ("CAST(e AS DATE) = f", "DATE(e) = f"),
        ):
            for dialect in ("postgres", "duckdb"):
                with self.subTest(dialect=dialect, source=source):
                    sql = parse_one(f"SELECT 1 FROM t WHERE {source}", read=dialect).sql(
                        "sqlite", unsupported_level=ErrorLevel.RAISE
                    )
                    self.assertEqual(sql, f"SELECT 1 FROM t WHERE {expected}")

    def test_marked_date_columns_are_dates(self):
        from sqlglot.benchbox.rewrites import mark_date_columns

        for source, expected in (
            ("d_date + 5", "DATE(d_date, '5 DAY')"),
            ("d1.d_date - 30", "DATE(d1.d_date, '-30 DAY')"),
            (
                "d1.d_date - d2.d_date",
                "CAST((JULIANDAY(d1.d_date) - JULIANDAY(d2.d_date)) AS INTEGER)",
            ),
            ("d_date_sk + 5", "d_date_sk + 5"),
        ):
            for dialect in ("netezza", "postgres", "duckdb"):
                with self.subTest(dialect=dialect, source=source):
                    tree = mark_date_columns(
                        parse_one(f"SELECT {source} FROM t", read=dialect), ["d_date"]
                    )
                    self.assertEqual(
                        tree.sql("sqlite", unsupported_level=ErrorLevel.RAISE),
                        f"SELECT {expected} FROM t",
                    )

    @requires_engine("sqlite")
    @requires_engine("duckdb")
    def test_date_semantics_match_duckdb_execution(self):
        from sqlglot.benchbox.rewrites import mark_date_columns

        rows = (
            ("2000-01-01", "2000-01-06", "2000-01-01"),
            ("2000-01-10", "2000-01-10", "2000-03-01"),
            ("2000-02-29", "2000-03-05", "2000-02-01"),
            ("1999-12-31", "2000-01-01", "2000-01-01"),
            (None, "2000-01-01", None),
        )
        queries = (
            "SELECT d1 + 5, d1 - 5, d2 - d1 FROM t ORDER BY d1, d2",
            "SELECT d1 FROM t WHERE d2 > d1 + 5 ORDER BY d1",
            "SELECT d1 FROM t WHERE d2 BETWEEN d1 - 1 AND d1 + 5 ORDER BY d1",
            "SELECT d1 FROM t WHERE DATE_TRUNC('month', d1) = d3 ORDER BY d1",
            "SELECT d1 FROM t WHERE DATE_TRUNC('month', d1) <> d3 ORDER BY d1",
            "SELECT d1 FROM t WHERE DATE_TRUNC('month', d1) >= d3 ORDER BY d1",
            "SELECT d1 FROM t WHERE DATE_TRUNC('month', d1) BETWEEN d3 AND d2 ORDER BY d1",
            "SELECT d1 FROM t WHERE d3 < DATE_TRUNC('year', d1) ORDER BY d1",
        )
        for dialect in ("postgres", "duckdb"):
            for query in queries:
                outputs = {}
                for engine in ("sqlite", "duckdb"):
                    with engines.connect(engine) as connection:
                        column = "TEXT" if engine == "sqlite" else "DATE"
                        connection.execute(
                            f"CREATE TABLE t (d1 {column}, d2 {column}, d3 {column})"
                        )
                        for row in rows:
                            values = ", ".join("NULL" if v is None else f"'{v}'" for v in row)
                            connection.execute(f"INSERT INTO t VALUES ({values})")
                        tree = mark_date_columns(parse_one(query, read=dialect), ["d1", "d2", "d3"])
                        sql = tree.sql(engine, unsupported_level=ErrorLevel.RAISE)
                        outputs[engine] = [
                            tuple(None if v is None else str(v) for v in row)
                            for row in connection.execute(sql)
                        ]
                with self.subTest(dialect=dialect, query=query):
                    self.assertEqual(outputs["sqlite"], outputs["duckdb"])

    def test_unpadded_date_literals_are_written_zero_padded(self):
        for source, expected in (
            ("CAST('1999-5-01' AS DATE)", "DATE('1999-05-01')"),
            ("DATE '1999-5-1'", "DATE('1999-05-01')"),
            ("CAST('99-12-3' AS DATE)", "DATE('0099-12-03')"),
            ("CAST('1999-05-01' AS DATE)", "DATE('1999-05-01')"),
            ("CAST(' 1999-5-1 ' AS DATE)", "DATE('1999-05-01')"),
            ("CAST('1999-5-01' AS DATE) + INTERVAL '60' DAY", "DATE(DATE('1999-05-01'), '60 DAY')"),
            (
                "CAST('1999-5-01' AS DATE) - INTERVAL '2' MONTH",
                "DATE(DATE('1999-05-01'), '-2 MONTH', 'floor')",
            ),
            ("CAST('1999-5-1 3:04' AS TIMESTAMP)", "DATETIME('1999-05-01 03:04:00')"),
            (
                "CAST('1999-5-1 3:04:05.25' AS TIMESTAMP)",
                "DATETIME('1999-05-01 03:04:05.25')",
            ),
            ("CAST('1999-5-1' AS TIMESTAMP)", "DATETIME('1999-05-01')"),
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    parse_one(f"SELECT {source}", read="duckdb").sql("sqlite"),
                    f"SELECT {expected}",
                )

    def test_invalid_date_literals_are_left_unchanged(self):
        for literal in (
            "1999-13-01",
            "1999-2-30",
            "0-1-1",
            "1999-5",
            "1999/5/1",
            "May 1",
            "1999-5-1x",
        ):
            with self.subTest(literal=literal):
                self.assertEqual(
                    parse_one(f"SELECT CAST('{literal}' AS DATE)", read="duckdb").sql("sqlite"),
                    f"SELECT DATE('{literal}')",
                )

    def test_unpadded_literal_compared_with_a_date_column(self):
        from sqlglot.benchbox.rewrites import mark_date_columns

        marked = mark_date_columns(
            parse_one(
                "SELECT 1 FROM t WHERE d = '1999-2-1' AND d IN ('1999-2-1', 'x', '2000-1-1') "
                "AND d BETWEEN '1999-2-1' AND '1999-12-3' AND '1999-2-1' < d AND d <> '1999-2-1' "
                "AND other = '1999-2-1'",
                read="duckdb",
            ),
            ["d"],
        )
        self.assertEqual(
            marked.sql("sqlite"),
            "SELECT 1 FROM t WHERE d = '1999-02-01' AND d IN ('1999-02-01', 'x', '2000-01-01') "
            "AND d BETWEEN '1999-02-01' AND '1999-12-03' AND '1999-02-01' < d AND d <> '1999-02-01' "
            "AND other = '1999-2-1'",
        )
        self.assertEqual(
            parse_one("SELECT 1 FROM t WHERE d = '1999-2-1'", read="duckdb").sql("sqlite"),
            "SELECT 1 FROM t WHERE d = '1999-2-1'",
        )

    def test_unpadded_date_literals_match_duckdb_execution(self):
        from sqlglot.benchbox.rewrites import mark_date_columns

        rows = ("1999-01-31", "1999-02-01", "1999-02-15", "1999-05-01", "1999-12-01", "2000-02-01")
        queries = [
            "SELECT COUNT(*) FROM t WHERE d BETWEEN CAST('1999-2-01' AS DATE) "
            "AND CAST('1999-2-01' AS DATE) + INTERVAL '60' DAY",
            "SELECT COUNT(*) FROM t WHERE d >= DATE '1999-2-1' AND d < DATE '1999-5-1'",
            "SELECT COUNT(*) FROM t WHERE d = '1999-2-1'",
            "SELECT COUNT(*) FROM t WHERE d IN ('1999-2-1', '1999-5-1', '2000-2-1')",
            "SELECT COUNT(*) FROM t WHERE d BETWEEN '1999-2-1' AND '1999-5-1'",
            "SELECT COUNT(*) FROM t WHERE d > '1999-2-1' AND d < '1999-12-1'",
        ]
        # A month offset clamps with SQLite's 'floor' modifier, which needs SQLite 3.46
        if sqlite3.sqlite_version_info >= (3, 46):
            queries.append(
                "SELECT COUNT(*) FROM t WHERE d <= CAST('1999-12-1' AS DATE) - INTERVAL '1' MONTH"
            )
        for query in queries:
            outputs = {}
            for engine in ("sqlite", "duckdb"):
                with engines.connect(engine) as connection:
                    column = "TEXT" if engine == "sqlite" else "DATE"
                    connection.execute(f"CREATE TABLE t (d {column})")
                    for row in rows:
                        connection.execute(f"INSERT INTO t VALUES ('{row}')")
                    tree = mark_date_columns(parse_one(query, read="duckdb"), ["d"])
                    outputs[engine] = [
                        tuple(row)
                        for row in connection.execute(
                            tree.sql(engine, unsupported_level=ErrorLevel.RAISE)
                        )
                    ]
            with self.subTest(query=query):
                self.assertEqual(outputs["sqlite"], outputs["duckdb"])
                self.assertGreater(outputs["duckdb"][0][0], 0)

    def test_create_index_drops_null_ordering(self):
        for source in ("duckdb", "netezza", "postgres"):
            with self.subTest(source=source):
                self.assertEqual(
                    parse_one("CREATE INDEX idx_a ON tbl (c1, c2 DESC)", read=source).sql("sqlite"),
                    "CREATE INDEX idx_a ON tbl(c1, c2 DESC)",
                )
        self.assertEqual(
            parse_one("SELECT a FROM t ORDER BY a", read="duckdb").sql("sqlite"),
            "SELECT a FROM t ORDER BY a NULLS LAST",
        )

    @requires_engine("sqlite")
    def test_create_index_without_null_ordering_executes(self):
        ddl = parse_one("CREATE INDEX idx_a ON t (k, v DESC)", read="duckdb").sql("sqlite")
        self.assertNotIn("NULLS", ddl)
        with engines.connect("sqlite") as connection:
            connection.execute("CREATE TABLE t (k INTEGER, v TEXT)")
            connection.execute("INSERT INTO t VALUES (1, 'a'), (NULL, 'b'), (2, 'c')")
            connection.execute(ddl)
            (count,) = connection.execute("SELECT COUNT(*) FROM t")[0]
        self.assertEqual(count, 3)
