"""Execute ``reduce_list_aggregates`` (TPC-Havoc ``1_v7`` and bare list aggregates) against DuckDB.

DuckDB runs the original query; the Spark/Databricks form runs on PySpark (``in-process`` job) and
the Presto/Trino/Athena form on Trino (``trino`` job). BigQuery has no engine in CI, so its UNNEST
form is text-checked in ``tests/test_benchbox_list_aggregates.py`` (``downstream/ENGINE-GAPS.md``).
"""

import unittest
from decimal import Decimal

import sqlglot
from sqlglot import exp
from sqlglot.benchbox.compat import order_list_aggregates, reduce_list_aggregates
from tests import engines
from tests.engines import requires_engine

LINEITEM = """
SELECT * FROM (VALUES
  ('A', 'F', 1, 1, CAST(17.00 AS DECIMAL(15,2)), CAST(21168.23 AS DECIMAL(15,2)), CAST(0.04 AS DECIMAL(15,2)), CAST(0.02 AS DECIMAL(15,2)), CAST('1996-03-13' AS DATE)),
  ('A', 'F', 1, 2, CAST(36.00 AS DECIMAL(15,2)), CAST(45983.16 AS DECIMAL(15,2)), CAST(0.09 AS DECIMAL(15,2)), CAST(0.06 AS DECIMAL(15,2)), CAST('1996-04-12' AS DATE)),
  ('A', 'F', 2, 1, CAST(8.00 AS DECIMAL(15,2)), CAST(13309.60 AS DECIMAL(15,2)), CAST(0.10 AS DECIMAL(15,2)), CAST(0.02 AS DECIMAL(15,2)), CAST('1997-01-28' AS DATE)),
  ('N', 'O', 3, 1, CAST(45.00 AS DECIMAL(15,2)), CAST(54058.05 AS DECIMAL(15,2)), CAST(0.06 AS DECIMAL(15,2)), CAST(0.00 AS DECIMAL(15,2)), CAST('1997-11-30' AS DATE)),
  ('N', 'O', 3, 2, CAST(49.00 AS DECIMAL(15,2)), CAST(46796.47 AS DECIMAL(15,2)), CAST(0.10 AS DECIMAL(15,2)), CAST(0.00 AS DECIMAL(15,2)), CAST('1998-01-02' AS DATE)),
  ('N', 'O', 4, 1, CAST(30.00 AS DECIMAL(15,2)), CAST(41544.10 AS DECIMAL(15,2)), CAST(0.03 AS DECIMAL(15,2)), CAST(0.08 AS DECIMAL(15,2)), CAST('1998-08-19' AS DATE)),
  ('R', 'F', 5, 1, CAST(3.00 AS DECIMAL(15,2)), CAST(4400.00 AS DECIMAL(15,2)), CAST(0.05 AS DECIMAL(15,2)), CAST(0.04 AS DECIMAL(15,2)), CAST('1995-06-01' AS DATE)),
  ('R', 'F', 6, 1, CAST(9.00 AS DECIMAL(15,2)), CAST(9000.00 AS DECIMAL(15,2)), CAST(0.07 AS DECIMAL(15,2)), CAST(0.01 AS DECIMAL(15,2)), CAST('1998-12-01' AS DATE))
) AS t(l_returnflag, l_linestatus, l_orderkey, l_linenumber, l_quantity, l_extendedprice, l_discount, l_tax, l_shipdate)
"""

# TPC-Havoc 1_v7, as shipped.
Q1_V7 = """
with aggregated as (
    select l_returnflag, l_linestatus,
        list(l_quantity) as quantities, list(l_extendedprice) as prices,
        list(l_discount) as discounts, list(l_tax) as taxes
    from lineitem
    where l_shipdate <= date '1998-12-01' - interval '90' day
    group by l_returnflag, l_linestatus
)
select l_returnflag, l_linestatus,
    list_sum(quantities) as sum_qty,
    list_sum(prices) as sum_base_price,
    list_sum(list_transform(list_zip(prices, discounts), x -> x[1] * (1 - x[2]))) as sum_disc_price,
    list_sum(list_transform(list_zip(prices, discounts, taxes), x -> x[1] * (1 - x[2]) * (1 + x[3]))) as sum_charge,
    list_avg(quantities) as avg_qty,
    list_avg(prices) as avg_price,
    list_avg(discounts) as avg_disc,
    len(quantities) as count_order
from aggregated
order by l_returnflag, l_linestatus
"""

# NULL elements, an all-NULL list, a one-element list and an uneven zip.
BARE = """
with src as (
  select * from (values
    ('a', 1, 10.0, 1), ('a', 2, 20.0, 2), ('a', 3, NULL, 3),
    ('b', 1, NULL, 5), ('b', 2, NULL, 5),
    ('c', 1, 4.0, 7),
    ('d', 1, 2.0, 1), ('d', 2, 3.0, NULL)
  ) as t(k, o, x, y)
),
agg as (
  select k, list(x) as xs, list(y) as ys from src group by k
)
select k,
  list_sum(xs) as s,
  list_avg(xs) as a,
  len(xs) as n,
  list_sum(list_transform(xs, v -> v * 2)) as doubled,
  list_sum(list_transform(list_zip(xs, ys), z -> z[1] * z[2])) as dot
from agg
order by k
"""


def normalize(rows):
    return [
        tuple(
            float(v) if isinstance(v, (Decimal, float)) and not isinstance(v, bool) else v
            for v in row
        )
        for row in rows
    ]


def with_lineitem(tree: exp.Expr, dialect: str) -> exp.Expr:
    """Prefix a ``lineitem`` CTE so the query needs no table on the engine."""
    cte = sqlglot.parse_one(f"WITH lineitem AS ({LINEITEM}) SELECT 1", read="duckdb").find(exp.CTE)
    with_ = tree.find(exp.With)
    with_.set("expressions", [cte, *with_.expressions])
    return tree


def name_values_columns(tree: exp.Expr) -> exp.Expr:
    """Name each aliased ``VALUES`` column in a select list, for engines whose table alias takes no columns."""
    tree = tree.copy()
    for values in list(tree.find_all(exp.Values)):
        alias = values.args.get("alias")
        if alias is None or not alias.columns:
            continue
        bare = values.copy()
        bare.set("alias", None)
        columns = [
            exp.alias_(exp.column(f"column{i}"), column.name)
            for i, column in enumerate(alias.columns, 1)
        ]
        values.replace(
            exp.Subquery(
                this=exp.select(*columns).from_(bare),
                alias=exp.TableAlias(this=alias.this.copy()),
            )
        )
    return tree


class ListAggregatesOn:
    ENGINE = ""
    DIALECT = ""

    def prepare(self, tree):
        return tree

    def execute(self, engine, sql):
        return normalize(engines.execute(engine, sql))

    def assert_rows_equal(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for got, want in zip(actual, expected):
            self.assertEqual(len(got), len(want))
            for g, w in zip(got, want):
                if isinstance(w, float) and g is not None:
                    self.assertAlmostEqual(g, w, delta=1e-9 * max(1.0, abs(w)))
                else:
                    self.assertEqual(g, w)

    def compare(self, sql, order_by, with_data=False):
        original = sqlglot.parse_one(sql, read="duckdb")
        if with_data:
            original = with_lineitem(original, "duckdb")
        expected = self.execute(
            "duckdb", order_list_aggregates(original, order_by, "duckdb").sql("duckdb")
        )
        ordered = order_list_aggregates(self.prepare(original), order_by, self.DIALECT)
        rewritten = reduce_list_aggregates(ordered, self.DIALECT).sql(self.DIALECT)
        self.assert_rows_equal(self.execute(self.ENGINE, rewritten), expected)
        return expected

    def test_havoc_1_v7(self):
        rows = self.compare(Q1_V7, ["l_orderkey", "l_linenumber"], with_data=True)
        # Each group sums its own rows; the 1998-12-01 row is past the 90-day cutoff.
        self.assertEqual([row[:2] for row in rows], [("A", "F"), ("N", "O"), ("R", "F")])
        self.assertAlmostEqual(rows[0][2], 61.0)
        self.assertAlmostEqual(rows[0][6], 61.0 / 3)
        self.assertEqual(rows[2][-1], 1)

    def test_bare_list_aggregates(self):
        rows = self.compare(BARE, ["o"])
        self.assertEqual(rows[0][:4], ("a", 30.0, 15.0, 3))
        self.assertEqual(rows[1][1:3], (None, None))
        self.assertEqual(rows[2][5], 28.0)
        # list_zip pads the shorter side with NULL, so the NULL product is skipped.
        self.assertEqual(rows[3][5], 2.0)


@requires_engine("duckdb")
class TestDuckDBListAggregates(ListAggregatesOn, unittest.TestCase):
    """DuckDB is the baseline: the rewrite must leave its own dialect's result alone."""

    ENGINE = "duckdb"
    DIALECT = "duckdb"


@requires_engine("duckdb")
class TestBigQueryListAggregatesOnDuckDB(ListAggregatesOn, unittest.TestCase):
    """BigQuery has no engine in CI: run its UNNEST form as the same text transpiled to DuckDB.

    The DuckDB generator writes ``a[SAFE_OFFSET(i)]`` as ``a[i]`` for a column ``i``, dropping the shift from
    BigQuery's 0-based offset to DuckDB's 1-based index, so the test reads ``SAFE_OFFSET(i)`` as the
    equivalent ``SAFE_ORDINAL(i + 1)`` before transpiling.
    """

    ENGINE = "duckdb"
    DIALECT = "bigquery"

    def execute(self, engine, sql):
        if sql.lstrip().upper().startswith(("WITH", "SELECT")) and "UNNEST" in sql.upper():
            sql = sql.replace("SAFE_OFFSET(_pos)", "SAFE_ORDINAL(_pos + 1)")
            sql = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
        return super().execute(engine, sql)


@requires_engine("duckdb")
@requires_engine("sqlite")
class TestSQLiteListAggregates(ListAggregatesOn, unittest.TestCase):
    ENGINE = "sqlite"
    DIALECT = "sqlite"

    def prepare(self, tree):
        return name_values_columns(tree)


@requires_engine("duckdb")
@requires_engine("postgres")
class TestPostgresListAggregates(ListAggregatesOn, unittest.TestCase):
    ENGINE = "postgres"
    DIALECT = "postgres"


@requires_engine("duckdb")
@requires_engine("datafusion")
class TestDataFusionListAggregates(ListAggregatesOn, unittest.TestCase):
    ENGINE = "datafusion"
    DIALECT = "datafusion"


@requires_engine("pyspark")
class TestSparkListAggregates(ListAggregatesOn, unittest.TestCase):
    ENGINE = "pyspark"
    DIALECT = "spark"


@requires_engine("pyspark")
class TestDatabricksListAggregates(ListAggregatesOn, unittest.TestCase):
    ENGINE = "pyspark"
    DIALECT = "databricks"


@requires_engine("trino")
class TestTrinoListAggregates(ListAggregatesOn, unittest.TestCase):
    ENGINE = "trino"
    DIALECT = "trino"


@requires_engine("trino")
class TestPrestoListAggregates(ListAggregatesOn, unittest.TestCase):
    """Presto and Athena share the Trino form; Trino is the engine that can run it."""

    ENGINE = "trino"
    DIALECT = "presto"


@requires_engine("trino")
class TestAthenaListAggregates(ListAggregatesOn, unittest.TestCase):
    ENGINE = "trino"
    DIALECT = "athena"


if __name__ == "__main__":
    unittest.main()
