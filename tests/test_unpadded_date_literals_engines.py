"""Execute unpadded date literals (dsqgen writes ``[YEAR]-[MONTH]-01``, so ``1999-5-01``) on every engine in CI.

TPC-DS Q16, Q94 and Q95 bound a date range by ``CAST('1999-5-01' AS DATE)`` and a bare ``'1999-5-01'``. Each
engine must read them as 1999-05-01, so a table whose rows sit exactly on the BETWEEN bounds returns those
rows. An engine that reads the literal as NULL filters on a NULL bound and returns nothing. Every case prints
a ``DATE-LITERAL-MATRIX`` line with the engine version and the value it returned, so the CI log is the
cross-engine record. Engines with no engine in CI are listed in ``downstream/ENGINE-GAPS.md``.
"""

import unittest

import sqlglot
from sqlglot.benchbox.rewrites import mark_date_columns
from tests import engines
from tests.engines import requires_engine

VERSION_SQL = {"sqlite": "SELECT sqlite_version()"}
DEFAULT_VERSION_SQL = "SELECT version()"

ROWS = (
    "SELECT 1 AS d_id, CAST('1999-01-31' AS DATE) AS d "
    "UNION ALL SELECT 2, CAST('1999-02-01' AS DATE) "
    "UNION ALL SELECT 3, CAST('1999-04-02' AS DATE) "
    "UNION ALL SELECT 4, CAST('1999-04-03' AS DATE)"
)
BOUNDED = (
    "SELECT d_id FROM ({rows}) AS t WHERE d BETWEEN {low} AND "
    "CAST('1999-2-01' AS DATE) + INTERVAL '60' DAY ORDER BY d_id"
)

CASES = {
    "cast_date": ("SELECT CAST('1999-5-01' AS DATE)", "1999-05-01"),
    "cast_date_plus_days": (
        "SELECT CAST('2000-3-01' AS DATE) + INTERVAL '60' DAY",
        "2000-04-30",
    ),
    "date_keyword": ("SELECT DATE '1999-5-1'", "1999-05-01"),
    "cast_timestamp": ("SELECT CAST('1999-5-1 3:04:05' AS TIMESTAMP)", "1999-05-01"),
    "cast_both_bounds": (BOUNDED.format(rows=ROWS, low="CAST('1999-2-01' AS DATE)"), [2, 3]),
    "string_low_bound": (BOUNDED.format(rows=ROWS, low="'1999-2-01'"), [2, 3]),
    "string_compared_with_date": (
        f"SELECT d_id FROM ({ROWS}) AS t WHERE d >= '1999-2-1' AND d < '1999-4-3' ORDER BY d_id",
        [2, 3],
    ),
    "string_in_list": (
        f"SELECT d_id FROM ({ROWS}) AS t WHERE d IN ('1999-2-1', '1999-4-2') ORDER BY d_id",
        [2, 3],
    ),
}


def translate(engine, sql):
    tree = mark_date_columns(sqlglot.parse_one(sql, read="postgres"), ["d"])
    return tree.sql(engines.ENGINES[engine].dialect)


def normalise(rows):
    if len(rows) == 1 and len(rows[0]) == 1:
        value = rows[0][0]
        return None if value is None else str(value)[:10]
    return [int(row[0]) for row in rows]


TIMESTAMP_AS_NUMBER = {"sqlite"}
STRING_COMPARISON_CASES = {"string_low_bound", "string_compared_with_date", "string_in_list"}
REJECTS_STRING_AGAINST_DATE = {"trino": "TYPE_MISMATCH"}
PADDED = (("'1999-2-01'", "'1999-02-01'"), ("'1999-2-1'", "'1999-02-01'"))


class UnpaddedDateLiteralsMixin:
    engine = ""

    def assert_rejected_padded_or_not(self, sql, rejection):
        padded = sql
        for unpadded, zero_padded in PADDED:
            padded = padded.replace(unpadded, zero_padded)
        with engines.connect(self.engine) as connection:
            for text in (sql, padded):
                translated = translate(self.engine, text)
                with self.assertRaises(Exception) as raised:
                    connection.execute(translated)
                print(
                    f"DATE-LITERAL-MATRIX engine={self.engine} case=string_against_date "
                    f"value=rejected({rejection}) sql={translated}"
                )
                self.assertIn(rejection, str(raised.exception), translated)

    def run_case(self, name):
        if name == "cast_timestamp" and self.engine in TIMESTAMP_AS_NUMBER:
            self.skipTest(f"{self.engine} has no timestamp type to read the literal as")
        sql, expected = CASES[name]
        translated = translate(self.engine, sql)
        rejection = REJECTS_STRING_AGAINST_DATE.get(self.engine)
        if rejection and name in STRING_COMPARISON_CASES:
            self.assert_rejected_padded_or_not(sql, rejection)
            return
        with engines.connect(self.engine) as connection:
            version = connection.execute(VERSION_SQL.get(self.engine, DEFAULT_VERSION_SQL))
            actual = normalise(connection.execute(translated))
        print(
            f"DATE-LITERAL-MATRIX engine={self.engine} version={version[0][0]!r} "
            f"case={name} value={actual!r} sql={translated}"
        )
        self.assertEqual(actual, expected, translated)


def _add_tests(cls):
    for name in CASES:
        setattr(cls, f"test_{name}", lambda self, name=name: self.run_case(name))
    return cls


def case(name):
    @requires_engine(name)
    @_add_tests
    class Case(UnpaddedDateLiteralsMixin, unittest.TestCase):
        engine = name

    Case.__name__ = Case.__qualname__ = f"TestUnpaddedDateLiteralsOn{name.title()}"
    return Case


TestUnpaddedDateLiteralsOnSqlite = case("sqlite")
TestUnpaddedDateLiteralsOnDuckdb = case("duckdb")
TestUnpaddedDateLiteralsOnDatafusion = case("datafusion")
TestUnpaddedDateLiteralsOnChdb = case("chdb")
TestUnpaddedDateLiteralsOnPyspark = case("pyspark")
TestUnpaddedDateLiteralsOnPostgres = case("postgres")
TestUnpaddedDateLiteralsOnTrino = case("trino")
TestUnpaddedDateLiteralsOnStarrocks = case("starrocks")
TestUnpaddedDateLiteralsOnDoris = case("doris")
TestUnpaddedDateLiteralsOnQuestdb = case("questdb")


if __name__ == "__main__":
    unittest.main()
