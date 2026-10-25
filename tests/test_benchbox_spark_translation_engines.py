"""Execute the ClickBench Q29 and Q43 shapes written for Databricks on PySpark.

Run by the ``in-process`` job of ``benchbox-engines.yml``. ClickBench is written in ClickHouse. Spark
and Databricks read a backslash group reference in a ``REGEXP_REPLACE`` replacement as the digit and
accept only year, quarter, month and week in ``TRUNC``, so the untranslated forms collapse every
referrer into one group (Q29) and every timestamp into one NULL group (Q43). Each case runs the
translated query on PySpark and compares the grouped key values with DuckDB and with a result
computed by hand with Python's ``re`` and ``datetime``; a row count alone would not tell the cases
apart at small scale. Databricks itself has no engine in CI (``downstream/ENGINE-GAPS.md``).
"""

import re
import unittest
from collections import Counter
from datetime import datetime

import sqlglot
from tests import engines
from tests.engines import requires_engine

Q29 = (
    "SELECT REGEXP_REPLACE(Referer, '^https?://(?:www\\.)?([^/]+)/.*$', '\\\\1') AS k, "
    "AVG(LENGTH(Referer)) AS l, COUNT(*) AS c, MIN(Referer) FROM hits WHERE Referer <> '' "
    "GROUP BY k HAVING COUNT(*) > 1 ORDER BY l DESC LIMIT 25;"
)
Q43 = (
    "SELECT DATE_TRUNC('minute', EventTime) AS M, COUNT(*) AS PageViews FROM hits WHERE CounterID = 62 "
    "AND EventDate >= '2013-07-14' AND EventDate <= '2013-07-15' AND IsRefresh = 0 AND DontCountHits = 0 "
    "GROUP BY DATE_TRUNC('minute', EventTime) ORDER BY DATE_TRUNC('minute', EventTime);"
)

DATABRICKS_Q29 = (
    "SELECT REGEXP_REPLACE(Referer, '^https?://(?:www\\\\.)?([^/]+)/.*$', '$1') AS k, "
    "AVG(LENGTH(Referer)) AS l, COUNT(*) AS c, MIN(Referer) FROM hits WHERE Referer <> '' "
    "GROUP BY k HAVING COUNT(*) > 1 ORDER BY l DESC LIMIT 25"
)
DATABRICKS_Q43 = (
    "SELECT DATE_TRUNC('MINUTE', EventTime) AS M, COUNT(*) AS PageViews FROM hits WHERE CounterID = 62 "
    "AND EventDate >= '2013-07-14' AND EventDate <= '2013-07-15' AND IsRefresh = 0 AND DontCountHits = 0 "
    "GROUP BY DATE_TRUNC('MINUTE', EventTime) ORDER BY DATE_TRUNC('MINUTE', EventTime) NULLS LAST"
)

REFERERS = (
    ["http://www.example.com/a/b"] * 3
    + ["https://www.example.com/c"] * 2
    + ["https://news.site.org/x"] * 4
    + ["http://shop.example.net/p?q=1"] * 2
    + ["http://one.test/"] * 1
    + ["ftp://nope"] * 2
    + [""] * 2
)
PATTERN = r"^https?://(?:www\.)?([^/]+)/.*$"

HITS = (
    (62, "2013-07-14 10:01:05", "2013-07-14", 0, 0),
    (62, "2013-07-14 10:01:40", "2013-07-14", 0, 0),
    (62, "2013-07-14 10:02:00", "2013-07-14", 0, 0),
    (62, "2013-07-14 23:59:59", "2013-07-14", 0, 0),
    (62, "2013-07-14 23:59:01", "2013-07-14", 0, 0),
    (62, "2013-07-15 00:00:09", "2013-07-15", 0, 0),
    (62, "2013-07-15 00:00:10", "2013-07-15", 0, 0),
    (62, "2013-07-15 00:00:11", "2013-07-15", 0, 0),
    (61, "2013-07-14 10:01:05", "2013-07-14", 0, 0),
    (62, "2013-07-16 10:01:05", "2013-07-16", 0, 0),
    (62, "2013-07-14 12:30:00", "2013-07-14", 1, 0),
    (62, "2013-07-14 12:31:00", "2013-07-14", 0, 1),
)


def hits_view(engine):
    if engine == "pyspark":
        rows = ", ".join(f"('{r}')" for r in REFERERS)
        return f"CREATE OR REPLACE TEMP VIEW hits AS SELECT * FROM VALUES {rows} AS t(Referer)"
    rows = ", ".join(f"('{r}')" for r in REFERERS)
    return f"CREATE OR REPLACE TEMP VIEW hits AS SELECT * FROM (VALUES {rows}) AS t(Referer)"


def clicks_view(engine):
    rows = ", ".join(
        f"({c}, CAST('{ts}' AS TIMESTAMP), CAST('{d}' AS DATE), {r}, {n})"
        for c, ts, d, r, n in HITS
    )
    columns = "t(CounterID, EventTime, EventDate, IsRefresh, DontCountHits)"
    if engine == "pyspark":
        return f"CREATE OR REPLACE TEMP VIEW hits AS SELECT * FROM VALUES {rows} AS {columns}"
    return f"CREATE OR REPLACE TEMP VIEW hits AS SELECT * FROM (VALUES {rows}) AS {columns}"


def translate(sql, dialect):
    return sqlglot.transpile(sql, read="clickhouse", write=dialect)[0]


def run(engine, dialect, view, sql):
    with engines.connect(engine) as connection:
        connection.execute(view)
        return connection.execute(translate(sql, dialect))


def expected_q29():
    groups = {}
    for referer in REFERERS:
        if referer == "":
            continue
        groups.setdefault(re.sub(PATTERN, r"\1", referer), []).append(referer)
    rows = [
        (key, sum(len(m) for m in members) / len(members), len(members), min(members))
        for key, members in groups.items()
        if len(members) > 1
    ]
    return sorted(rows)


def expected_q43():
    counts = Counter(
        datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(second=0)
        for c, ts, d, r, n in HITS
        if c == 62 and "2013-07-14" <= d <= "2013-07-15" and r == 0 and n == 0
    )
    return sorted(counts.items())


def normalized_q29(rows):
    return sorted((str(k), float(avg), int(c), str(m)) for k, avg, c, m in rows)


def normalized_q43(rows):
    return sorted((m.replace(tzinfo=None), int(c)) for m, c in rows)


class TestDatabricksText(unittest.TestCase):
    def test_q29_replacement_uses_a_dollar_reference(self):
        self.assertEqual(translate(Q29, "databricks"), DATABRICKS_Q29)
        self.assertEqual(translate(Q29, "spark"), DATABRICKS_Q29)

    def test_q43_truncates_with_date_trunc(self):
        self.assertEqual(translate(Q43, "databricks"), DATABRICKS_Q43)

    def test_other_targets_keep_their_replacement(self):
        expected = {
            "duckdb": r"'\1', 'g')",
            "postgres": r"'\1', 'g')",
            "bigquery": r"'\\1')",
            "clickhouse": r"'\\1')",
            "snowflake": r"'\\2')",
        }
        for dialect, tail in expected.items():
            with self.subTest(dialect=dialect):
                self.assertIn(tail + " AS k", translate(Q29, dialect))

    def test_spark_keeps_its_own_dollar_reference_and_escapes(self):
        for sql in (
            "SELECT REGEXP_REPLACE(a, 'x(y)', '$1')",
            "SELECT REGEXP_REPLACE(a, 'x', '\\\\\\\\')",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(sqlglot.transpile(sql, read="spark", write="spark")[0], sql)

    def test_date_and_week_truncation_keep_trunc(self):
        for unit in ("YEAR", "QUARTER", "MONTH", "WEEK"):
            with self.subTest(unit=unit):
                self.assertEqual(
                    sqlglot.transpile(
                        f"SELECT TRUNC(d, '{unit}')", read="spark", write="databricks"
                    )[0],
                    f"SELECT TRUNC(d, '{unit}')",
                )


@requires_engine("pyspark")
@requires_engine("duckdb")
class TestClickBenchOnSpark(unittest.TestCase):
    def test_q29_groups_referrers_by_host(self):
        spark = normalized_q29(run("pyspark", "databricks", hits_view("pyspark"), Q29))
        duck = normalized_q29(run("duckdb", "duckdb", hits_view("duckdb"), Q29))
        expected = expected_q29()
        self.assertGreaterEqual(len({row[0] for row in expected}), 3)
        self.assertEqual(spark, expected)
        self.assertEqual(duck, expected)

    def test_q43_groups_by_minute(self):
        spark = normalized_q43(run("pyspark", "databricks", clicks_view("pyspark"), Q43))
        duck = normalized_q43(run("duckdb", "duckdb", clicks_view("duckdb"), Q43))
        expected = expected_q43()
        self.assertGreaterEqual(len(expected), 3)
        self.assertEqual(spark, expected)
        self.assertEqual(duck, expected)


if __name__ == "__main__":
    unittest.main()
