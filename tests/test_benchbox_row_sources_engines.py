"""Execute the single-row-source and nested-count rewrites on real engines.

Run by the ``in-process``, ``postgres``, ``starrocks`` and ``doris`` jobs of
``benchbox-engines.yml``; each class skips when its engine is not available.
"""

import unittest

import sqlglot
from sqlglot.benchbox.compat import (
    add_row_source_to_fromless_selects,
    drop_literal_row_source,
    flatten_count_of_count,
)
from tests import engines
from tests.engines import requires_engine

ROWS = (
    "(SELECT 1 AS id, 'a' AS k, 1 AS x, 1 AS y"
    " UNION ALL SELECT 2, 'a', 2, 5"
    " UNION ALL SELECT 3, 'a', 4, 5"
    " UNION ALL SELECT 4, 'b', 3, 5"
    " UNION ALL SELECT 5, 'b', 3, 0"
    " UNION ALL SELECT 6, 'c', 1, 0)"
)

# The TPC-Havoc Q17 variant 4 shape: a dead UNION ALL leg that borrows a one-row source.
DUAL_LEG = (
    f"SELECT SUM(r) AS s FROM (SELECT x AS r FROM {ROWS} AS t "
    "UNION ALL SELECT 0 AS r FROM (SELECT 1) AS dual WHERE FALSE) AS c"
)
# The TPC-DI EQ3 shape: a scalar subquery that counts the rows of an inner COUNT(*).
COUNT_OF_COUNT = (
    f"SELECT (SELECT COUNT(*) FROM (SELECT COUNT(*) AS source_count FROM {ROWS} AS t WHERE y > 0)) AS source_records"
)
SOURCE_ROWS = 4

# Each way to write a one-row source, as a complete query that returns the one row.
SPELLINGS = {
    "fromless": "SELECT 7 AS v",
    "fromless_where": "SELECT 7 AS v WHERE 1 = 1",
    "derived_table": "SELECT 7 AS v FROM (SELECT 1) AS d",
    "derived_table_named_column": "SELECT 7 AS v FROM (SELECT 1 AS c) AS d",
    "values_derived": "SELECT 7 AS v FROM (VALUES (1)) AS d(c)",
    "values_derived_unnamed": "SELECT 7 AS v FROM (VALUES (1)) AS d",
    "from_values": "SELECT 7 AS v FROM VALUES (1) AS d(c)",
    "system_one": "SELECT 7 AS v FROM system.one",
    "numbers_1": "SELECT 7 AS v FROM numbers(1)",
    "dual": "SELECT 7 AS v FROM dual",
}


def numbers(rows):
    return [tuple(int(float(cell)) for cell in row) for row in rows]


class RowSources:
    ENGINE = ""
    DIALECT = ""
    # Spellings from SPELLINGS this engine runs, as established by executing them.
    ACCEPTS: frozenset = frozenset()

    def rows_of(self, expression):
        return numbers(engines.execute(self.ENGINE, expression.sql(dialect=self.DIALECT)))

    def parse(self, sql):
        return sqlglot.parse_one(sql, read="postgres")

    def test_spellings(self):
        accepted = set()
        for name, sql in SPELLINGS.items():
            try:
                rows = numbers(engines.execute(self.ENGINE, sql))
            except Exception:
                continue
            self.assertEqual(rows, [(7,)], name)
            accepted.add(name)
        self.assertEqual(accepted, set(self.ACCEPTS))

    def test_dropping_the_literal_row_source_runs_the_dead_leg(self):
        rewritten = drop_literal_row_source(self.parse(DUAL_LEG), "dual")
        self.assertNotIn("dual", rewritten.sql(dialect=self.DIALECT))
        self.assertNotIn("VALUES", rewritten.sql(dialect=self.DIALECT).upper())
        self.assertEqual(self.rows_of(rewritten), [(1 + 2 + 4 + 3 + 3 + 1,)])

    def test_dropping_the_literal_row_source_keeps_a_live_leg(self):
        sql = f"SELECT SUM(r) FROM (SELECT x AS r FROM {ROWS} AS t UNION ALL SELECT 100 AS r FROM (SELECT 1) AS dual WHERE TRUE) AS c"
        rewritten = drop_literal_row_source(self.parse(sql), "dual")
        self.assertEqual(self.rows_of(rewritten), [(114,)])

    def test_row_source_added_back_for_engines_that_need_one(self):
        fromless = (
            f"SELECT id FROM {ROWS} AS t WHERE id IN (SELECT 1 WHERE 1 = 1 UNION ALL SELECT 3 WHERE 1 = 1) ORDER BY id"
        )
        rewritten = add_row_source_to_fromless_selects(
            self.parse(fromless), "(SELECT 1) AS one", self.DIALECT
        )
        self.assertEqual(self.rows_of(rewritten), [(1,), (3,)])

    def test_count_of_count_returns_the_source_row_count(self):
        rewritten = flatten_count_of_count(self.parse(COUNT_OF_COUNT))
        self.assertEqual(self.rows_of(rewritten), [(SOURCE_ROWS,)])

    def test_nested_count_returns_one_without_the_rewrite(self):
        if not self.ORIGINAL_RUNS:
            with self.assertRaises(Exception):
                self.rows_of(self.parse(COUNT_OF_COUNT))
            return
        self.assertEqual(self.rows_of(self.parse(COUNT_OF_COUNT)), [(1,)])

    ORIGINAL_RUNS = True


@requires_engine("postgres")
class TestRowSourcesOnPostgres(RowSources, unittest.TestCase):
    ENGINE = "postgres"
    DIALECT = "postgres"
    ACCEPTS = frozenset(
        {
            "fromless",
            "fromless_where",
            "derived_table",
            "derived_table_named_column",
            "values_derived",
            "values_derived_unnamed",
        }
    )


@requires_engine("duckdb")
class TestRowSourcesOnDuckDB(RowSources, unittest.TestCase):
    ENGINE = "duckdb"
    DIALECT = "duckdb"
    ACCEPTS = frozenset(
        {
            "fromless",
            "fromless_where",
            "derived_table",
            "derived_table_named_column",
            "values_derived",
            "values_derived_unnamed",
            "from_values",
        }
    )


@requires_engine("datafusion")
class TestRowSourcesOnDataFusion(RowSources, unittest.TestCase):
    ENGINE = "datafusion"
    DIALECT = "datafusion"
    ACCEPTS = frozenset(
        {
            "fromless",
            "fromless_where",
            "derived_table",
            "derived_table_named_column",
            "values_derived",
            "values_derived_unnamed",
            "from_values",
        }
    )


@requires_engine("pyspark")
class TestRowSourcesOnPySpark(RowSources, unittest.TestCase):
    ENGINE = "pyspark"
    DIALECT = "spark"
    ACCEPTS = frozenset(
        {
            "fromless",
            "fromless_where",
            "derived_table",
            "derived_table_named_column",
            "values_derived",
            "values_derived_unnamed",
            "from_values",
        }
    )


@requires_engine("chdb")
class TestRowSourcesOnClickHouse(RowSources, unittest.TestCase):
    ENGINE = "chdb"
    DIALECT = "clickhouse"
    ACCEPTS = frozenset(
        {
            "fromless",
            "fromless_where",
            "derived_table",
            "derived_table_named_column",
            "from_values",
            "system_one",
            "numbers_1",
        }
    )


@requires_engine("sqlite")
class TestRowSourcesOnSqlite(RowSources, unittest.TestCase):
    ENGINE = "sqlite"
    DIALECT = "sqlite"
    ACCEPTS = frozenset(
        {"fromless", "fromless_where", "derived_table", "derived_table_named_column", "values_derived_unnamed"}
    )


@requires_engine("starrocks")
class TestRowSourcesOnStarRocks(RowSources, unittest.TestCase):
    ENGINE = "starrocks"
    DIALECT = "starrocks"
    ACCEPTS = frozenset({"fromless", "fromless_where"})


@requires_engine("doris")
class TestRowSourcesOnDoris(RowSources, unittest.TestCase):
    ENGINE = "doris"
    DIALECT = "doris"
    ACCEPTS = frozenset({"fromless", "fromless_where"})


if __name__ == "__main__":
    unittest.main()
