import sqlite3
from datetime import datetime, timedelta

from sqlglot import ErrorLevel, UnsupportedError, exp, parse_one
from tests import engines
from tests.engines import requires_engine
from sqlglot.generator import logger as generator_logger
from sqlglot.helper import logger as helper_logger
from tests.dialects.test_dialect import Validator


class TestSQLite(Validator):
    dialect = "sqlite"

    def test_is_precedence(self):
        self.validate_identity("SELECT 'x' LIKE 'x' NOT LIKE '1'")
        for suffix in ("IN (0)", "BETWEEN 0 AND 0", "LIKE '0'", "GLOB '0'"):
            for predicate, klass in (
                ("NULL IS NULL", exp.Is),
                ("NULL IS NOT DISTINCT FROM NULL", exp.NullSafeEQ),
            ):
                lhs = f"({predicate})" if suffix.startswith("BETWEEN") else predicate
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
                "sqlite": "SELECT (FALSE IS DISTINCT FROM TRUE) IS DISTINCT FROM FALSE",
                "postgres": "SELECT (FALSE IS DISTINCT FROM TRUE) IS DISTINCT FROM FALSE",
            },
        )
        self.validate_identity("SELECT NULL IS NULL < FALSE", "SELECT NULL IS (NULL < FALSE)")
        self.validate_all(
            "SELECT FALSE IS DISTINCT FROM NULL = NULL",
            write={
                "sqlite": "SELECT (FALSE IS DISTINCT FROM NULL) = NULL",
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

    def test_parenthesised_set_operation_operands(self):
        self.validate_all(
            "SELECT * FROM (SELECT * FROM (SELECT a FROM t) EXCEPT SELECT * FROM (SELECT a FROM u)) AS x",
            read={"duckdb": "SELECT * FROM ((SELECT a FROM t) EXCEPT (SELECT a FROM u)) AS x"},
        )
        self.validate_all(
            "SELECT * FROM (SELECT 1) UNION ALL SELECT * FROM (SELECT 2) ORDER BY 1 NULLS LAST LIMIT 3",
            read={"duckdb": "(SELECT 1) UNION ALL (SELECT 2) ORDER BY 1 LIMIT 3"},
        )
        self.validate_all(
            "SELECT * FROM (SELECT a FROM t ORDER BY a NULLS LAST LIMIT 2) INTERSECT SELECT a FROM u",
            read={"duckdb": "(SELECT a FROM t ORDER BY a LIMIT 2) INTERSECT SELECT a FROM u"},
        )
        self.validate_all(
            "SELECT a FROM t UNION SELECT a FROM u",
            read={"duckdb": "SELECT a FROM t UNION SELECT a FROM u"},
        )

    @requires_engine("sqlite")
    @requires_engine("duckdb")
    def test_parenthesised_set_operation_execution(self):
        # TPC-DS Q87 shape: a count over (SELECT DISTINCT ...) EXCEPT (SELECT DISTINCT ...) EXCEPT (...).
        setup = (
            "CREATE TABLE s (a INTEGER, b VARCHAR)",
            "CREATE TABLE c (a INTEGER, b VARCHAR)",
            "CREATE TABLE w (a INTEGER, b VARCHAR)",
            "INSERT INTO s VALUES (1, 'x'), (1, 'x'), (2, 'y'), (3, 'z'), (4, NULL), (5, 'q'), (6, 'r')",
            "INSERT INTO c VALUES (2, 'y'), (2, 'y'), (7, 'u'), (6, 'r')",
            "INSERT INTO w VALUES (3, 'z'), (8, 'v'), (4, NULL)",
        )
        queries = (
            "SELECT COUNT(*) FROM ((SELECT DISTINCT a, b FROM s) EXCEPT (SELECT DISTINCT a, b FROM c) "
            "EXCEPT (SELECT DISTINCT a, b FROM w)) AS cool_cust",
            "SELECT * FROM ((SELECT a, b FROM s) EXCEPT (SELECT a, b FROM c)) AS d ORDER BY a",
            "(SELECT a FROM s) UNION (SELECT a FROM c) UNION ALL (SELECT a FROM w) ORDER BY a",
            "((SELECT a FROM s) INTERSECT (SELECT a FROM c)) UNION ALL (SELECT a FROM w) ORDER BY a",
            "(SELECT a FROM s ORDER BY a DESC LIMIT 3) UNION ALL (SELECT a FROM c ORDER BY a LIMIT 2) ORDER BY a",
            "SELECT a, b FROM s EXCEPT (SELECT a, b FROM c) ORDER BY a",
        )
        for query in queries:
            with self.subTest(query=query):
                results = {}
                for engine in ("sqlite", "duckdb"):
                    with engines.connect(engine) as connection:
                        for statement in setup:
                            connection.execute(statement)
                        results[engine] = connection.execute(
                            parse_one(query, read="duckdb").sql(engine)
                        )
                self.assertTrue(results["duckdb"])
                self.assertEqual(results["sqlite"], results["duckdb"])

    def test_date_interval_arithmetic(self):
        for operator, amount in (("+", "5"), ("-", "-5")):
            for unit in ("DAY", "HOUR", "MINUTE", "SECOND"):
                function = "DATE" if unit == "DAY" else "DATETIME"
                self.validate_all(
                    f"SELECT {function}(DATE('2025-01-01'), '{amount} {unit}')",
                    read={
                        "duckdb": f"SELECT DATE '2025-01-01' {operator} INTERVAL '5' {unit}",
                        "postgres": f"SELECT DATE '2025-01-01' {operator} INTERVAL '5' {unit}",
                    },
                )
        self.validate_all(
            "SELECT DATE(DATE(d), '-1 DAY') FROM t",
            read={"postgres": "SELECT INTERVAL '-1' DAY + CAST(d AS DATE) FROM t"},
        )
        self.validate_all(
            "SELECT DATE(DATE(d), '1 DAY') FROM t",
            read={"postgres": "SELECT CAST(d AS DATE) - INTERVAL '-1' DAY FROM t"},
        )
        for source in (
            "DATE '2024-01-31' + INTERVAL '1' MONTH",
            "DATE '2024-02-29' + INTERVAL '1' YEAR",
            "DATE '2024-01-01' + INTERVAL '0.1' SECOND",
            "TIMESTAMP '2024-01-01 12:30:00.123456' + INTERVAL '1' DAY",
            "d + INTERVAL '1' DAY",
            "INTERVAL '1' DAY",
        ):
            with self.subTest(source=source), self.assertRaises(UnsupportedError):
                self.parse_one(source, dialect="postgres").sql(
                    "sqlite", unsupported_level=ErrorLevel.RAISE
                )
        self.validate_identity("SELECT a + 1, b - 2 FROM t")
        for source in (
            "CAST(d AS DATE) + INTERVAL '1' MONTH",
            "DATE '2023-01-29' + INTERVAL '1' MONTH",
            "DATE '2024-03-31' - INTERVAL '1' MONTH",
            "DATE '2024-01-01' + INTERVAL '1' QUARTER",
            "DATE '2024-01-01' + INTERVAL '1' WEEK",
        ):
            with self.subTest(source=source), self.assertRaises(UnsupportedError):
                self.parse_one(source, dialect="postgres").sql(
                    "sqlite", unsupported_level=ErrorLevel.RAISE
                )

    def test_date_interval_month_year(self):
        self.validate_all(
            "SELECT DATE('1994-01-01', '+1 year')",
            read={
                "postgres": "SELECT DATE '1994-01-01' + INTERVAL '1' YEAR",
                "duckdb": "SELECT DATE '1994-01-01' + INTERVAL '1' YEAR",
            },
        )
        self.validate_all(
            "SELECT DATE('1994-01-01', '-3 month')",
            read={"postgres": "SELECT DATE '1994-01-01' - INTERVAL '3' MONTH"},
        )
        self.validate_all(
            "SELECT DATE('1994-03-10', '+2 month')",
            read={"postgres": "SELECT DATE '1994-03-10' - INTERVAL '-2' MONTH"},
        )
        self.validate_all(
            "SELECT DATE('1994-01-01', '+2 month')",
            read={"postgres": "SELECT INTERVAL '2' MONTH + CAST('1994-01-01' AS DATE)"},
        )
        self.validate_all(
            "SELECT DATE('1998-09-01', '+10 month')",
            read={"duckdb": "SELECT DATE('1998-09-01') + INTERVAL '10' MONTH"},
        )
        self.validate_all(
            "SELECT DATE(NULL, '+1 month')",
            read={"postgres": "SELECT CAST(NULL AS DATE) + INTERVAL '1' MONTH"},
        )
        # The function form of a DATE is typed like a DATE literal.
        self.validate_all(
            "SELECT DATE(DATE('1998-09-01'), '10 DAY') FROM t",
            read={"duckdb": "SELECT DATE('1998-09-01') + INTERVAL '10' DAY FROM t"},
        )

    @requires_engine("sqlite")
    def test_date_interval_month_year_execution(self):
        # SQLite rolls an overflowing month end into the next month, so a month or
        # year offset is lowered only for a day of month that cannot overflow: those
        # inputs must agree with calendar arithmetic, the others are Unsupported.
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

    def test_date_interval_execution(self):
        with sqlite3.connect(":memory:") as connection:
            for dialect in ("postgres", "duckdb"):
                for unit, duration in (
                    ("DAY", timedelta(days=1)),
                    ("HOUR", timedelta(hours=1)),
                    ("MINUTE", timedelta(minutes=1)),
                    ("SECOND", timedelta(seconds=1)),
                ):
                    for amount in (-5, 0, 5):
                        query = f"SELECT DATE '2024-02-29' + INTERVAL '{amount}' {unit}"
                        moment = datetime(2024, 2, 29) + amount * duration
                        # A whole-day offset of a DATE is date text; a time unit adds a time.
                        expected = (
                            moment.date().isoformat() if unit == "DAY" else moment.isoformat(" ")
                        )
                        sql = parse_one(query, read=dialect).sql(
                            "sqlite", unsupported_level=ErrorLevel.RAISE
                        )
                        with self.subTest(dialect=dialect, query=query):
                            self.assertEqual(connection.execute(sql).fetchone(), (expected,))
                query = "SELECT CAST(NULL AS DATE) + INTERVAL '1' DAY"
                sql = parse_one(query, read=dialect).sql(
                    "sqlite", unsupported_level=ErrorLevel.RAISE
                )
                self.assertEqual(connection.execute(sql).fetchone(), (None,))

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
            "SELECT DATE(d, '1 DAY') FROM t",
            read={
                "duckdb": "SELECT DATE_ADD(d, INTERVAL 1 DAY) FROM t",
            },
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
            "SELECT DATE(d, (-n) || ' MONTH') FROM t",
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
        self.validate_identity("SELECT INSTR(haystack, needle)")
        self.validate_identity(
            "SELECT a, SUM(b) OVER (ORDER BY a ROWS BETWEEN -1 PRECEDING AND 1 FOLLOWING) FROM t1 ORDER BY 1"
        )
        self.validate_identity(
            "SELECT JSON_EXTRACT('[10, 20, [30, 40]]', '$[2]', '$[0]', '$[1]')",
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
        )
        self.validate_all(
            "SELECT MAX(a, b) FROM t",
            read={
                "sqlite": "SELECT MAX(a, b) FROM t",
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
                "mysql": "ORD(CONVERT(x USING utf32))",
                "oracle": "ASCII(UNISTR(x))",
                "postgres": "ASCII(x)",
                "redshift": "ASCII(x)",
                "spark": "ASCII(x)",
            },
        )
        self.validate_identity(
            "SELECT * FROM station WHERE city IS NOT ''",
            "SELECT * FROM station WHERE NOT city IS ''",
        )
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
        self.validate_identity(
            "SELECT * FROM t WHERE NULL IS NOT y", "SELECT * FROM t WHERE NOT NULL IS y"
        )
        self.validate_identity("SELECT SQLITE_VERSION()")

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

    def test_strftime(self):
        self.validate_identity("SELECT STRFTIME('%Y/%m/%d', 'now')")
        self.validate_identity("SELECT STRFTIME('%Y-%m-%d', '2016-10-16', 'start of month')")
        self.validate_identity(
            "SELECT STRFTIME('%s')",
            "SELECT STRFTIME('%s', CURRENT_TIMESTAMP)",
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

    def test_datediff(self):
        self.validate_all(
            "DATEDIFF(a, b, 'day')",
            write={"sqlite": "CAST((JULIANDAY(a) - JULIANDAY(b)) AS INTEGER)"},
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

    def test_hexadecimal_literal(self):
        self.validate_all(
            "SELECT 0XCC",
            write={
                "sqlite": "SELECT x'CC'",
                "mysql": "SELECT x'CC'",
            },
        )

    def test_window_null_treatment(self):
        self.validate_all(
            "SELECT FIRST_VALUE(Name) OVER (PARTITION BY AlbumId ORDER BY Bytes DESC) AS LargestTrack FROM tracks",
            write={
                "sqlite": "SELECT FIRST_VALUE(Name) OVER (PARTITION BY AlbumId ORDER BY Bytes DESC) AS LargestTrack FROM tracks"
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

    def test_ddl(self):
        for conflict_action in ("ABORT", "FAIL", "IGNORE", "REPLACE", "ROLLBACK"):
            with self.subTest(f"ON CONFLICT {conflict_action}"):
                self.validate_identity("CREATE TABLE a (b, c, UNIQUE (b, c) ON CONFLICT IGNORE)")

        self.validate_identity("CREATE TABLE over (x, y)")
        self.validate_identity("INSERT OR ABORT INTO foo (x, y) VALUES (1, 2)")
        self.validate_identity("INSERT OR FAIL INTO foo (x, y) VALUES (1, 2)")
        self.validate_identity("INSERT OR IGNORE INTO foo (x, y) VALUES (1, 2)")
        self.validate_identity("INSERT OR REPLACE INTO foo (x, y) VALUES (1, 2)")
        self.validate_identity("INSERT OR ROLLBACK INTO foo (x, y) VALUES (1, 2)")
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

    def test_analyze(self):
        self.validate_identity("ANALYZE tbl")
        self.validate_identity("ANALYZE schma.tbl")

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

    def test_date_day_difference_and_subtraction(self):
        for unit in ("DAY", "day"):
            with self.subTest(unit=unit):
                self.assertEqual(
                    exp.DateDiff(
                        this=exp.column("a"), expression=exp.column("b"), unit=exp.var(unit)
                    ).sql("sqlite", unsupported_level=ErrorLevel.RAISE),
                    "CAST((JULIANDAY(a) - JULIANDAY(b)) AS INTEGER)",
                )

        for amount, unit, expected in (
            (exp.Literal.number(30), "DAY", "DATE(CURRENT_DATE, '-30 DAY')"),
            (exp.Literal.number(2), "MONTH", "DATE(CURRENT_DATE, '-2 MONTH')"),
            (exp.Literal.number(1), "YEAR", "DATE(CURRENT_DATE, '-1 YEAR')"),
            (exp.Paren(this=exp.column("n")), "DAY", "DATE(CURRENT_DATE, (-(n)) || ' DAY')"),
        ):
            with self.subTest(amount=amount.sql(), unit=unit):
                node = exp.DateSub(this=exp.CurrentDate(), expression=amount, unit=exp.var(unit))
                self.assertEqual(node.sql("sqlite", unsupported_level=ErrorLevel.RAISE), expected)

    @requires_engine("sqlite")
    def test_date_subtraction_executes(self):
        node = exp.select(
            exp.DateSub(
                this=exp.CurrentDate(), expression=exp.Literal.number(30), unit=exp.var("DAY")
            ),
            exp.DateSub(
                this=exp.Literal.string("2024-03-15"),
                expression=exp.Literal.number(1),
                unit=exp.var("MONTH"),
            ),
        )
        with engines.connect("sqlite") as connection:
            (row,) = connection.execute(node.sql("sqlite"))
            (expected,) = connection.execute("SELECT DATE('now', '-30 days')")
        self.assertEqual(row, (expected[0], "2024-02-15"))

    def test_date_day_offset_is_a_date(self):
        # A whole-day offset of a DATE stays date text; only offsets with a time unit
        # carry a time of day.
        for source, expected in (
            ("DATE '1998-04-08' - INTERVAL '30' DAY", "DATE(DATE('1998-04-08'), '-30 DAY')"),
            ("DATE '1998-04-08' + INTERVAL '30' DAY", "DATE(DATE('1998-04-08'), '30 DAY')"),
            ("INTERVAL '30' DAY + DATE '1998-04-08'", "DATE(DATE('1998-04-08'), '30 DAY')"),
            ("DATE '1998-04-08' - INTERVAL '-30' DAY", "DATE(DATE('1998-04-08'), '30 DAY')"),
            ("CAST(d AS DATE) - INTERVAL '30' DAY", "DATE(DATE(d), '-30 DAY')"),
            (
                "CAST('1998-04-08' AS DATE) + INTERVAL '30' DAY",
                "DATE(DATE('1998-04-08'), '30 DAY')",
            ),
            ("DATE '1998-04-08' + INTERVAL '36' HOUR", "DATETIME(DATE('1998-04-08'), '36 HOUR')"),
            ("DATE '1998-04-08' - INTERVAL '1' YEAR", "DATE('1998-04-08', '-1 year')"),
            ("DATE '1998-04-08' + INTERVAL '2' MONTH", "DATE('1998-04-08', '+2 month')"),
        ):
            for dialect in ("postgres", "duckdb"):
                with self.subTest(dialect=dialect, source=source):
                    sql = parse_one(f"SELECT {source} FROM t", read=dialect).sql(
                        "sqlite", unsupported_level=ErrorLevel.RAISE
                    )
                    self.assertEqual(sql, f"SELECT {expected} FROM t")

        # A TIMESTAMP operand is not a DATE: its day offset stays Unsupported (DATETIME
        # would drop fractional seconds) and its truncation stays a STRFTIME.
        for source in (
            "CAST(ts AS TIMESTAMP) + INTERVAL '1' DAY",
            "CAST(ts AS TIMESTAMP) - INTERVAL '30' DAY",
            "TIMESTAMP '1998-04-08 10:00:00' - INTERVAL '30' DAY",
        ):
            with self.subTest(source=source), self.assertRaises(UnsupportedError):
                parse_one(f"SELECT {source}", read="postgres").sql(
                    "sqlite", unsupported_level=ErrorLevel.RAISE
                )
        self.assertEqual(
            parse_one("SELECT DATE_TRUNC('day', ts) FROM t", read="postgres").sql("sqlite"),
            "SELECT STRFTIME('%Y-%m-%d 00:00:00', ts) FROM t",
        )

    @requires_engine("sqlite")
    def test_date_day_offset_keeps_boundary_row_execution(self):
        # TPC-DS Q21/Q40 filter a DATE column BETWEEN date - 30 days AND date + 30 days.
        # Both bounds are date text, so rows on either boundary day are kept.
        days = ("1998-03-08", "1998-03-09", "1998-04-08", "1998-05-08", "1998-05-09")
        for dialect in ("postgres", "duckdb"):
            for column in ("TEXT", "DATE"):
                for lower, upper in (
                    (
                        "DATE '1998-04-08' - INTERVAL '30' DAY",
                        "DATE '1998-04-08' + INTERVAL '30' DAY",
                    ),
                    (
                        "CAST('1998-04-08' AS DATE) - INTERVAL '30' DAY",
                        "CAST('1998-04-08' AS DATE) + INTERVAL '30' DAY",
                    ),
                ):
                    with self.subTest(dialect=dialect, column=column, lower=lower):
                        sql = parse_one(
                            f"SELECT d FROM t WHERE d BETWEEN {lower} AND {upper} ORDER BY 1",
                            read=dialect,
                        ).sql("sqlite", unsupported_level=ErrorLevel.RAISE)
                        with engines.connect("sqlite") as connection:
                            connection.execute(f"CREATE TABLE t (d {column})")
                            for value in days:
                                connection.execute(f"INSERT INTO t VALUES ('{value}')")
                            rows = connection.execute(sql)
                        self.assertEqual(
                            rows, [("1998-03-09",), ("1998-04-08",), ("1998-05-08",)], sql
                        )

        with engines.connect("sqlite") as connection:
            for source, expected in (
                ("DATE '1998-04-08' - INTERVAL '30' DAY", "1998-03-09"),
                ("DATE '1998-04-08' + INTERVAL '1' MONTH", "1998-05-08"),
                ("DATE '1998-04-08' - INTERVAL '1' YEAR", "1997-04-08"),
                ("DATE '1998-04-08' + INTERVAL '36' HOUR", "1998-04-09 12:00:00"),
            ):
                sql = parse_one(f"SELECT {source}", read="postgres").sql(
                    "sqlite", unsupported_level=ErrorLevel.RAISE
                )
                self.assertEqual(connection.execute(sql), [(expected,)], source)

            # A TIMESTAMP column keeps its time through truncation.
            connection.execute("CREATE TABLE ev (ts TEXT)")
            connection.execute("INSERT INTO ev VALUES ('1998-04-08 13:45:10')")
            sql = parse_one("SELECT DATE_TRUNC('day', ts) FROM ev", read="postgres").sql("sqlite")
            self.assertEqual(connection.execute(sql), [("1998-04-08 00:00:00",)])

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
            ("CAST('1999-5-01' AS DATE) - INTERVAL '2' MONTH", "DATE('1999-05-01', '-2 month')"),
            ("CAST('1999-5-1 3:04' AS TIMESTAMP)", "CAST('1999-05-01 03:04:00' AS TIMESTAMPNTZ)"),
            (
                "CAST('1999-5-1 3:04:05.25' AS TIMESTAMP)",
                "CAST('1999-05-01 03:04:05.25' AS TIMESTAMPNTZ)",
            ),
            ("CAST('1999-5-1' AS TIMESTAMP)", "CAST('1999-05-01' AS TIMESTAMPNTZ)"),
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
        queries = (
            "SELECT COUNT(*) FROM t WHERE d BETWEEN CAST('1999-2-01' AS DATE) "
            "AND CAST('1999-2-01' AS DATE) + INTERVAL '60' DAY",
            "SELECT COUNT(*) FROM t WHERE d >= DATE '1999-2-1' AND d < DATE '1999-5-1'",
            "SELECT COUNT(*) FROM t WHERE d = '1999-2-1'",
            "SELECT COUNT(*) FROM t WHERE d IN ('1999-2-1', '1999-5-1', '2000-2-1')",
            "SELECT COUNT(*) FROM t WHERE d BETWEEN '1999-2-1' AND '1999-5-1'",
            "SELECT COUNT(*) FROM t WHERE d > '1999-2-1' AND d < '1999-12-1'",
            "SELECT COUNT(*) FROM t WHERE d <= CAST('1999-12-1' AS DATE) - INTERVAL '1' MONTH",
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
