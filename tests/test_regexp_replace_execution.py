"""Execute ``REGEXP_REPLACE`` with a non-capturing group written in ClickHouse on real engines.

Run by the ``in-process`` and ``postgres`` jobs of ``benchbox-engines.yml``. ClickHouse is the
reference: the same query written for every other dialect must return the same rows, and so must
the Snowflake form, whose pattern and group references the generator renumbers. Snowflake itself
has no engine in CI (``downstream/ENGINE-GAPS.md``), so that form is run on DuckDB's RE2 and on
Python's ``re``, which share the capturing-group semantics it relies on.
"""

import re
import unittest

import sqlglot
from sqlglot import exp, parse_one
from tests import engines
from tests.engines import requires_engine

REFERERS = (
    "http://www.example.com/a/b",
    "https://news.site.org/x",
    "ftp://nope",
    "http://example.com/",
)
SOURCE = r"""SELECT REGEXP_REPLACE(Referer, '^https?://(?:www\.)?([^/]+)/.*$', '\\1') AS k FROM (%s) AS hits ORDER BY Referer"""
EXPECTED = [("example.com",), ("example.com",), ("ftp://nope",), ("news.site.org",)]


def hits(quote):
    return " UNION ALL ".join(f"SELECT {quote(value)} AS Referer" for value in REFERERS)


def translate(dialect):
    return sqlglot.transpile(
        SOURCE % hits(lambda value: f"'{value}'"), read="clickhouse", write=dialect
    )[0]


def rows(engine, sql):
    with engines.connect(engine) as connection:
        return connection.execute(sql)


class TestRegexpReplaceAcrossEngines(unittest.TestCase):
    def assert_same_as_reference(self, engine, dialect):
        actual = [(str(r[0]),) for r in rows(engine, translate(dialect))]
        self.assertEqual(sorted(actual), sorted(EXPECTED))

    @requires_engine("chdb")
    def test_clickhouse_reference(self):
        self.assert_same_as_reference("chdb", "clickhouse")

    @requires_engine("duckdb")
    def test_duckdb(self):
        self.assert_same_as_reference("duckdb", "duckdb")

    @requires_engine("postgres")
    def test_postgres(self):
        self.assert_same_as_reference("postgres", "postgres")

    @requires_engine("datafusion")
    def test_datafusion(self):
        self.assert_same_as_reference("datafusion", "datafusion")


CASES = (
    (r"^https?://(?:www\\.)?([^/]+)/.*$", r"\\1", ("http://www.example.com/a", "http://a.org/b")),
    (r"(?:a)(b)(?:c)(d)", r"\\2\\1", ("abcd", "xabcdx", "abc")),
    (r"(x)(?:y)(z)[(?:]\\((?:q)", r"\\2-\\1", ("xyz(((q", "xyz:(q")),
)


def regexp_replace(tree):
    node = tree.find(exp.RegexpReplace)
    assert node is not None
    return node


def convert(pattern, replacement):
    clickhouse = parse_one(
        f"SELECT REGEXP_REPLACE(s, '{pattern}', '{replacement}')", read="clickhouse"
    )
    snowflake = parse_one(clickhouse.sql("snowflake"), read="snowflake")
    before, after = regexp_replace(clickhouse), regexp_replace(snowflake)
    return (
        before.expression.name,
        before.args["replacement"].name,
        after.expression.name,
        after.args["replacement"].name,
    )


def select(value, pattern, replacement, dialect):
    return exp.select(
        exp.RegexpReplace(
            this=exp.Literal.string(value),
            expression=exp.Literal.string(pattern),
            replacement=exp.Literal.string(replacement),
        )
    ).sql(dialect)


class TestSnowflakeGroupRenumbering(unittest.TestCase):
    def test_converted_pattern_has_only_capturing_groups(self):
        for pattern, replacement, _ in CASES:
            with self.subTest(pattern=pattern):
                new_pattern = convert(pattern, replacement)[2]
                self.assertNotIn("(?:", new_pattern.replace("[(?:]", ""))

    def test_python_re_gives_the_same_result(self):
        for pattern, replacement, values in CASES:
            old_pattern, old_replacement, new_pattern, new_replacement = convert(
                pattern, replacement
            )
            for value in values:
                with self.subTest(pattern=pattern, value=value):
                    self.assertEqual(
                        re.sub(old_pattern, old_replacement, value),
                        re.sub(new_pattern, new_replacement, value),
                    )

    @requires_engine("duckdb")
    def test_duckdb_re2_gives_the_same_result(self):
        for pattern, replacement, values in CASES:
            old_pattern, old_replacement, new_pattern, new_replacement = convert(
                pattern, replacement
            )
            for value in values:
                with self.subTest(pattern=pattern, value=value):
                    self.assertEqual(
                        rows("duckdb", select(value, old_pattern, old_replacement, "duckdb")),
                        rows("duckdb", select(value, new_pattern, new_replacement, "duckdb")),
                    )

    @requires_engine("chdb")
    def test_clickhouse_gives_the_same_result_for_the_converted_form(self):
        for pattern, replacement, values in CASES:
            old_pattern, old_replacement, new_pattern, new_replacement = convert(
                pattern, replacement
            )
            for value in values:
                with self.subTest(pattern=pattern, value=value):
                    self.assertEqual(
                        rows("chdb", select(value, old_pattern, old_replacement, "clickhouse")),
                        rows("chdb", select(value, new_pattern, new_replacement, "clickhouse")),
                    )
