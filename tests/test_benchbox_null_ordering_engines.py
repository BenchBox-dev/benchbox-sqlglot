"""Execute the NULL placement a translation keeps on every engine in CI.

Run by every engine job of ``benchbox-engines.yml``. BenchBox benchmarks are written in one source
dialect and every engine must return the order that source defines: Netezza (TPC-H, TPC-DS, SSB and
the other catalog benchmarks) treats NULL as the largest value, so ASC puts NULLs last and DESC puts
them first; DuckDB (TSBS DevOps, Data Vault) and ClickHouse (ClickBench) put NULLs last in both
directions. Spark, Trino, PostgreSQL, DataFusion, chDB, SQLite and the MySQL-wire engines each default
differently, so the translated text has to carry the placement.

Each case runs the source query, transpiled to the engine, over the same six rows (two NULL sort keys)
with a LIMIT, and compares the rows with the placement the source dialect defines, written out by hand
below. Positional ORDER BY, window ORDER BY with ROWS and RANGE frames, GROUP BY keys and DESC are
covered. BigQuery, Snowflake, Databricks and T-SQL have no engine in CI: their text is asserted in
``tests/test_benchbox_null_ordering.py`` and the gap is listed in ``downstream/ENGINE-GAPS.md``.
"""

import unittest

import sqlglot
from tests import engines
from tests.engines import requires_engine

ROWS = (
    "SELECT 1 AS id, 'a' AS g, 3 AS k, 10 AS v "
    "UNION ALL SELECT 2, 'a', NULL, 20 "
    "UNION ALL SELECT 3, 'b', 1, 30 "
    "UNION ALL SELECT 4, 'b', NULL, 40 "
    "UNION ALL SELECT 5, 'a', 2, 50 "
    "UNION ALL SELECT 6, 'b', 3, 60"
)

QUERIES = {
    "asc_limit": f"SELECT id, k FROM ({ROWS}) AS t ORDER BY k, id LIMIT 4",
    "desc_limit": f"SELECT id, k FROM ({ROWS}) AS t ORDER BY k DESC, id LIMIT 4",
    "positional_asc": f"SELECT g, k, id FROM ({ROWS}) AS t ORDER BY 2, 3 LIMIT 3",
    "positional_desc": f"SELECT g, k, id FROM ({ROWS}) AS t ORDER BY 2 DESC, 3 LIMIT 3",
    "group_desc": f"SELECT k, COUNT(*) AS c FROM ({ROWS}) AS t GROUP BY k ORDER BY k DESC LIMIT 2",
    "window_rows_asc": (
        "SELECT id, SUM(v) OVER (ORDER BY k, id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s "
        f"FROM ({ROWS}) AS t ORDER BY id"
    ),
    "window_rows_desc": (
        "SELECT id, SUM(v) OVER (ORDER BY k DESC, id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s "
        f"FROM ({ROWS}) AS t ORDER BY id"
    ),
    "window_range_desc": (
        "SELECT id, SUM(v) OVER (ORDER BY k DESC RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s "
        f"FROM ({ROWS}) AS t ORDER BY id"
    ),
}

LARGE = {
    "asc_limit": [(3, 1), (5, 2), (1, 3), (6, 3)],
    "desc_limit": [(2, None), (4, None), (1, 3), (6, 3)],
    "positional_asc": [("b", 1, 3), ("a", 2, 5), ("a", 3, 1)],
    "positional_desc": [("a", None, 2), ("b", None, 4), ("a", 3, 1)],
    "group_desc": [(None, 2), (3, 2)],
    "window_rows_asc": [(1, 90), (2, 170), (3, 30), (4, 210), (5, 80), (6, 150)],
    "window_rows_desc": [(1, 70), (2, 20), (3, 210), (4, 60), (5, 180), (6, 130)],
    "window_range_desc": [(1, 130), (2, 60), (3, 210), (4, 60), (5, 180), (6, 130)],
}

LAST = {
    **LARGE,
    "desc_limit": [(1, 3), (6, 3), (5, 2), (3, 1)],
    "positional_desc": [("a", 3, 1), ("b", 3, 6), ("a", 2, 5)],
    "group_desc": [(3, 2), (2, 1)],
    "window_rows_desc": [(1, 10), (2, 170), (3, 150), (4, 210), (5, 120), (6, 70)],
    "window_range_desc": [(1, 70), (2, 210), (3, 150), (4, 210), (5, 120), (6, 70)],
}

SOURCES = {"netezza": LARGE, "duckdb": LAST, "clickhouse": LAST}


def number(value):
    if isinstance(value, str):
        if value in ("NULL", "\\N"):
            return None
        try:
            value = float(value)
        except ValueError:
            return value
    if value is None:
        return value
    as_float = float(value)
    return int(as_float) if as_float == int(as_float) else as_float


def normalise(rows):
    return [tuple(number(value) for value in row) for row in rows]


WINDOWS_WITHOUT_PLACEMENT = {
    "questdb": {"window_rows_asc", "window_rows_desc", "window_range_desc"},
}


def case(source, name):
    expected_rows = SOURCES[source]

    @requires_engine(name)
    class Case(unittest.TestCase):
        def run_query(self, key):
            sql = sqlglot.transpile(QUERIES[key], read=source, write=engines.ENGINES[name].dialect)[
                0
            ]
            with engines.connect(name) as connection:
                return normalise(connection.execute(sql))

    def make(key):
        def test(self):
            self.assertEqual(self.run_query(key), expected_rows[key])

        if key in WINDOWS_WITHOUT_PLACEMENT.get(name, ()):
            return unittest.skip(
                f"{name} rejects an expression in a window ORDER BY and has no NULLS clause"
            )(test)
        return test

    for key in QUERIES:
        setattr(Case, f"test_{key}", make(key))

    Case.__name__ = Case.__qualname__ = f"Test{source.title()}NullOrderingOn{name.title()}"
    return Case


ENGINE_NAMES = (
    "duckdb",
    "sqlite",
    "datafusion",
    "postgres",
    "trino",
    "pyspark",
    "chdb",
    "starrocks",
    "doris",
    "questdb",
)

for _source in SOURCES:
    for _name in ENGINE_NAMES:
        _case = case(_source, _name)
        globals()[_case.__name__] = _case

del _source, _name, _case


class TestSQLiteIndexNullOrdering(unittest.TestCase):
    @requires_engine("sqlite")
    def test_duckdb_create_index_executes_on_sqlite(self):
        ddl = sqlglot.transpile(
            "CREATE INDEX idx_k ON t (k, v DESC)", read="duckdb", write="sqlite"
        )[0]
        self.assertNotIn("NULLS", ddl)
        with engines.connect("sqlite") as connection:
            connection.execute("CREATE TABLE t (k INTEGER, v TEXT)")
            connection.execute("INSERT INTO t VALUES (2, 'b'), (NULL, 'a'), (1, 'c')")
            connection.execute(ddl)
            self.assertEqual(
                normalise(connection.execute("SELECT k FROM t ORDER BY k NULLS LAST")),
                [(1,), (2,), (None,)],
            )


if __name__ == "__main__":
    unittest.main()
