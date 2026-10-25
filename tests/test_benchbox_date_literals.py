import unittest

import sqlglot
from sqlglot.benchbox.date_literals import canonical_date, canonical_literal, canonical_timestamp
from sqlglot.benchbox.rewrites import mark_date_columns


def questdb(sql, read="postgres", date_columns=()):
    tree = sqlglot.parse_one(sql, read=read)
    if date_columns:
        tree = mark_date_columns(tree, date_columns)
    return tree.sql("questdb")


class TestCanonicalText(unittest.TestCase):
    def test_dates(self):
        for text, expected in (
            ("1999-5-01", "1999-05-01"),
            ("1999-5-1", "1999-05-01"),
            (" 1999-5-1 ", "1999-05-01"),
            ("99-12-3", "0099-12-03"),
            ("1999-05-01", "1999-05-01"),
            ("1999-13-01", None),
            ("1999-2-30", None),
            ("0-1-1", None),
            ("1999-5", None),
            ("1999/5/1", None),
            ("1999-5-1x", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(canonical_date(text), expected)

    def test_timestamps(self):
        for text, expected in (
            ("1999-5-1 3:04", "1999-05-01 03:04:00"),
            ("1999-5-1T3:04:05", "1999-05-01 03:04:05"),
            ("1999-5-1 3:04:05.25", "1999-05-01 03:04:05.25"),
            ("1999-5-1", "1999-05-01"),
            ("1999-5-1 25:00", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(canonical_timestamp(text), expected)

    def test_literals_only(self):
        column = sqlglot.exp.column("d")
        self.assertIs(canonical_literal(column), column)
        number = sqlglot.exp.Literal.number(5)
        self.assertIs(canonical_literal(number), number)
        padded = sqlglot.exp.Literal.string("1999-05-01")
        self.assertIs(canonical_literal(padded), padded)


class TestQuestDBDateLiterals(unittest.TestCase):
    def test_cast_to_date_and_timestamp(self):
        for source, expected in (
            ("CAST('1999-5-01' AS DATE)", "CAST('1999-05-01' AS DATE)"),
            ("DATE '1999-5-1'", "CAST('1999-05-01' AS DATE)"),
            ("CAST('1999-5-1 3:04:05' AS TIMESTAMP)", "CAST('1999-05-01 03:04:05' AS TIMESTAMP)"),
            ("CAST('1999-13-01' AS DATE)", "CAST('1999-13-01' AS DATE)"),
            ("CAST('x' AS TEXT)", "CAST('x' AS TEXT)"),
        ):
            with self.subTest(source=source):
                self.assertEqual(questdb(f"SELECT {source}"), f"SELECT {expected}")

    def test_date_arithmetic_keeps_the_padded_base(self):
        self.assertEqual(
            questdb("SELECT CAST('2000-3-01' AS DATE) + INTERVAL '60' DAY"),
            "SELECT DATEADD('d', 60, CAST('2000-03-01' AS DATE))",
        )

    def test_literal_bound_beside_a_known_date(self):
        self.assertEqual(
            questdb(
                "SELECT 1 FROM t WHERE d_date BETWEEN '2000-3-01' AND "
                "(CAST('2000-3-01' AS DATE) + INTERVAL '60' DAY)"
            ),
            "SELECT 1 FROM t WHERE d_date BETWEEN '2000-03-01' AND "
            "(DATEADD('d', 60, CAST('2000-03-01' AS DATE)))",
        )

    def test_literal_compared_with_a_marked_column(self):
        self.assertEqual(
            questdb(
                "SELECT 1 FROM t WHERE d = '1999-2-1' AND d IN ('1999-2-1', 'x') "
                "AND d > '1999-2-1' AND other = '1999-2-1'",
                date_columns=["d"],
            ),
            "SELECT 1 FROM t WHERE d = '1999-02-01' AND d IN ('1999-02-01', 'x') "
            "AND d > '1999-02-01' AND other = '1999-2-1'",
        )

    def test_literal_without_a_known_date_is_unchanged(self):
        self.assertEqual(
            questdb("SELECT 1 FROM t WHERE d = '1999-2-1'"),
            "SELECT 1 FROM t WHERE d = '1999-2-1'",
        )

    def test_source_dialects(self):
        for read in ("duckdb", "netezza", "postgres", "snowflake"):
            with self.subTest(read=read):
                self.assertEqual(
                    questdb("SELECT CAST('1999-5-01' AS DATE)", read=read),
                    "SELECT CAST('1999-05-01' AS DATE)",
                )


class TestDataFusionTimestampLiterals(unittest.TestCase):
    def test_timestamp_cast_is_padded_and_date_is_untouched(self):
        tree = sqlglot.parse_one("SELECT CAST('1999-5-1 3:04:05' AS TIMESTAMP)", read="postgres")
        self.assertEqual(
            tree.sql("datafusion"), "SELECT CAST('1999-05-01 03:04:05' AS TIMESTAMP)"
        )
        tree = sqlglot.parse_one(
            "SELECT CAST('1999-5-01' AS DATE), 1 WHERE d = '1999-2-1'", read="postgres"
        )
        self.assertEqual(
            mark_date_columns(tree, ["d"]).sql("datafusion"),
            "SELECT CAST('1999-5-01' AS DATE), 1 WHERE d = '1999-2-1'",
        )


if __name__ == "__main__":
    unittest.main()
