"""Execute the query shapes of the TPC-DS one-big-table (OBT) queries Q2, Q36, Q70, Q78 and Q86 on PostgreSQL.

Run by the ``in-process`` and ``postgres`` jobs of ``benchbox-engines.yml``. The OBT queries are
written in DuckDB and every measure is a ``DOUBLE`` column. PostgreSQL resolves an output alias
only as a whole ``ORDER BY`` key, so ``ORDER BY CASE WHEN lochierarchy = 0 THEN ...`` fails there
(``inline_order_by_aliases``), and it has no ``ROUND(double precision, integer)``
(``round_inputs_to_decimal``). DuckDB runs the query as written and is the reference.
"""

import unittest
from decimal import Decimal

import sqlglot
from sqlglot.benchbox.compat import inline_order_by_aliases
from sqlglot.benchbox.rewrites import round_inputs_to_decimal
from tests import engines
from tests.engines import requires_engine

OBT = """
(SELECT * FROM (VALUES
  (1, 'Music', 'pop', 'store', CAST(10.5 AS DOUBLE), CAST(100.25 AS DOUBLE)),
  (2, 'Music', 'pop', 'store', CAST(4.0 AS DOUBLE), CAST(50.0 AS DOUBLE)),
  (3, 'Music', 'rock', 'store', CAST(-3.5 AS DOUBLE), CAST(80.0 AS DOUBLE)),
  (4, 'Books', 'fiction', 'store', CAST(7.0 AS DOUBLE), CAST(40.0 AS DOUBLE)),
  (5, 'Books', 'travel', 'store', CAST(1.25 AS DOUBLE), CAST(10.0 AS DOUBLE)),
  (6, 'Books', 'travel', 'web', CAST(9.0 AS DOUBLE), CAST(30.0 AS DOUBLE)),
  (7, 'Shoes', 'boot', 'store', CAST(2.0 AS DOUBLE), CAST(15.0 AS DOUBLE))
) AS v(sk, item_i_category, item_i_class, channel, net_profit, ext_sales_price))
"""

Q36 = f"""
SELECT
  SUM(obt.net_profit) / SUM(obt.ext_sales_price) AS gross_margin,
  obt.item_i_category,
  obt.item_i_class,
  GROUPING(obt.item_i_category) + GROUPING(obt.item_i_class) AS lochierarchy,
  RANK() OVER (
    PARTITION BY GROUPING(obt.item_i_category) + GROUPING(obt.item_i_class),
      CASE WHEN GROUPING(obt.item_i_class) = 0 THEN obt.item_i_category END
    ORDER BY SUM(obt.net_profit) / SUM(obt.ext_sales_price) ASC
  ) AS rank_within_parent
FROM {OBT} AS obt
WHERE channel = 'store'
GROUP BY ROLLUP (obt.item_i_category, obt.item_i_class)
ORDER BY
  lochierarchy DESC NULLS FIRST,
  CASE WHEN lochierarchy = 0 THEN obt.item_i_category END,
  rank_within_parent
"""

Q2 = f"""
WITH weekly AS (
  SELECT item_i_category AS cat,
         SUM(CASE WHEN channel = 'store' THEN ext_sales_price ELSE NULL END) AS store_sales,
         SUM(CASE WHEN channel = 'web' THEN ext_sales_price ELSE NULL END) AS web_sales
  FROM {OBT} AS obt
  GROUP BY item_i_category
)
SELECT a.cat,
       ROUND(a.store_sales / b.store_sales, 2) AS ratio,
       ROUND(AVG(a.store_sales) OVER (), 2) AS mean
FROM weekly AS a, weekly AS b
WHERE a.cat = b.cat
ORDER BY a.cat
"""


def reference(sql):
    with engines.connect("duckdb") as connection:
        return normalize(connection.execute(sql))


def postgres(sql, *rewrites):
    tree = sqlglot.parse_one(sql, read="duckdb")
    for rewrite in rewrites:
        tree = rewrite(tree)
    with engines.connect("postgres") as connection:
        return normalize(connection.execute(tree.sql("postgres")))


def normalize(rows):
    return [tuple(float(v) if isinstance(v, (Decimal, float)) else v for v in row) for row in rows]


@requires_engine("duckdb")
@requires_engine("postgres")
class TestObtOnPostgres(unittest.TestCase):
    def test_alias_inside_an_order_by_expression(self):
        expected = reference(Q36)
        self.assertEqual(expected[0][3], 2)
        with self.assertRaises(Exception) as caught:
            postgres(Q36)
        self.assertIn("lochierarchy", str(caught.exception))
        self.assertEqual(postgres(Q36, inline_order_by_aliases), expected)

    def test_round_of_a_double_with_a_scale(self):
        expected = reference(Q2)
        self.assertEqual([row[0] for row in expected], ["Books", "Music", "Shoes"])
        with self.assertRaises(Exception) as caught:
            postgres(Q2)
        self.assertIn("round", str(caught.exception).lower())
        self.assertEqual(postgres(Q2, round_inputs_to_decimal), expected)


if __name__ == "__main__":
    unittest.main()
