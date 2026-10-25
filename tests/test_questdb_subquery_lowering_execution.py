import unittest

import sqlglot
from tests import engines
from tests.engines import requires_engine

PREFIX = "qd_"

TABLES = {
    "catalog_sales": "(cs_order_number INT, cs_warehouse_sk INT, cs_ship_date_sk INT, "
    "cs_ship_addr_sk INT, cs_call_center_sk INT, cs_ext_ship_cost DOUBLE, cs_net_profit DOUBLE)",
    "catalog_returns": "(cr_order_number INT)",
    "web_sales": "(ws_order_number INT, ws_warehouse_sk INT, ws_ship_date_sk INT, "
    "ws_ship_addr_sk INT, ws_web_site_sk INT, ws_ext_ship_cost DOUBLE, ws_net_profit DOUBLE)",
    "web_returns": "(wr_order_number INT)",
    "date_dim": "(d_date_sk INT, d_date DATE)",
    "customer_address": "(ca_address_sk INT, ca_state VARCHAR)",
    "call_center": "(cc_call_center_sk INT, cc_county VARCHAR)",
    "web_site": "(web_site_sk INT, web_company_name VARCHAR)",
}

SALES = [
    (1, 1, 1, 1, 1, 10.0, 1.0),
    (1, 2, 1, 1, 1, 20.0, 2.0),
    (2, 1, 1, 1, 1, 30.0, 3.0),
    (2, 2, 1, 1, 1, 40.0, 4.0),
    (3, 1, 1, 1, 1, 50.0, 5.0),
    (3, 1, 1, 1, 1, 60.0, 6.0),
    (4, 1, 2, 2, 2, 70.0, 7.0),
    (4, 3, 2, 2, 2, 80.0, 8.0),
    (4, None, 2, 2, 2, 90.0, 9.0),
    (5, None, 1, 1, 1, 11.0, 1.0),
    (5, None, 1, 1, 1, 12.0, 1.0),
    (6, 1, 3, 1, 1, 13.0, 1.0),
    (6, 2, 3, 1, 1, 14.0, 1.0),
    (7, 1, 1, 5, 1, 15.0, 1.0),
    (7, 2, 1, 5, 1, 16.0, 1.0),
    (8, 1, 1, 1, 3, 17.0, 1.0),
    (8, 2, 1, 1, 3, 18.0, 1.0),
    (9, 1, 5, 1, 1, 19.0, 1.0),
    (9, 2, 4, 1, 1, 21.0, 1.0),
    (None, 1, 1, 1, 1, 22.0, 1.0),
    (None, 2, 1, 1, 1, 23.0, 1.0),
    (10, 5, 1, 3, 1, 24.0, 1.0),
    (10, 6, 1, 3, 1, 25.0, 1.0),
]
WEB_ADDRESS = {1: 3, 2: 4, 3: 4, 5: 5}

ROWS = {
    "catalog_sales": SALES,
    "web_sales": [(o, w, d, WEB_ADDRESS[a], s, c, p) for o, w, d, a, s, c, p in SALES],
    "catalog_returns": [(2,), (2,), (11,), (None,)],
    "web_returns": [(2,), (2,), (11,), (None,), (4,)],
    "date_dim": [
        (1, "2000-03-05"),
        (2, "2000-03-20"),
        (3, "2000-06-30"),
        (4, "2000-02-10"),
        (5, "2000-03-10"),
    ],
    "customer_address": [(1, "IA"), (2, "IA"), (3, "NC"), (4, "NC"), (5, "TX")],
    "call_center": [(1, "Williamson County"), (2, "Williamson County"), (3, "Other")],
    "web_site": [(1, "pri"), (2, "pri"), (3, "xyz")],
}

SELECT_TOTALS = (
    'SELECT COUNT(DISTINCT {order}) AS "order count", SUM({cost}) AS "total shipping cost", '
    'SUM({profit}) AS "total net profit" '
)

Q16 = (
    SELECT_TOTALS.format(order="cs_order_number", cost="cs_ext_ship_cost", profit="cs_net_profit")
    + "FROM catalog_sales AS cs1, date_dim, customer_address, call_center "
    "WHERE d_date BETWEEN '2000-3-01' AND (CAST('2000-3-01' AS DATE) + INTERVAL '60 DAY') "
    "AND cs1.cs_ship_date_sk = d_date_sk AND cs1.cs_ship_addr_sk = ca_address_sk "
    "AND ca_state = 'IA' AND cs1.cs_call_center_sk = cc_call_center_sk "
    "AND cc_county IN ('Williamson County', 'Williamson County', 'Williamson County', "
    "'Williamson County', 'Williamson County') "
    "AND EXISTS(SELECT * FROM catalog_sales AS cs2 WHERE cs1.cs_order_number = cs2.cs_order_number "
    "AND cs1.cs_warehouse_sk <> cs2.cs_warehouse_sk) "
    "AND NOT EXISTS(SELECT * FROM catalog_returns AS cr1 "
    "WHERE cs1.cs_order_number = cr1.cr_order_number) "
    "ORDER BY COUNT(DISTINCT cs_order_number) LIMIT 100"
)

Q94 = (
    SELECT_TOTALS.format(order="ws_order_number", cost="ws_ext_ship_cost", profit="ws_net_profit")
    + "FROM web_sales AS ws1, date_dim, customer_address, web_site "
    "WHERE d_date BETWEEN '2000-2-01' AND (CAST('2000-2-01' AS DATE) + INTERVAL '60 DAY') "
    "AND ws1.ws_ship_date_sk = d_date_sk AND ws1.ws_ship_addr_sk = ca_address_sk "
    "AND ca_state = 'NC' AND ws1.ws_web_site_sk = web_site_sk AND web_company_name = 'pri' "
    "AND EXISTS(SELECT * FROM web_sales AS ws2 WHERE ws1.ws_order_number = ws2.ws_order_number "
    "AND ws1.ws_warehouse_sk <> ws2.ws_warehouse_sk) "
    "AND NOT EXISTS(SELECT * FROM web_returns AS wr1 "
    "WHERE ws1.ws_order_number = wr1.wr_order_number) "
    "ORDER BY COUNT(DISTINCT ws_order_number) LIMIT 100"
)

Q95 = (
    "WITH ws_wh AS (SELECT ws1.ws_order_number, ws1.ws_warehouse_sk AS wh1, "
    "ws2.ws_warehouse_sk AS wh2 FROM web_sales AS ws1, web_sales AS ws2 "
    "WHERE ws1.ws_order_number = ws2.ws_order_number "
    "AND ws1.ws_warehouse_sk <> ws2.ws_warehouse_sk) "
    + SELECT_TOTALS.format(order="ws_order_number", cost="ws_ext_ship_cost", profit="ws_net_profit")
    + "FROM web_sales AS ws1, date_dim, customer_address, web_site "
    "WHERE d_date BETWEEN '2000-2-01' AND (CAST('2000-2-01' AS DATE) + INTERVAL '60 DAY') "
    "AND ws1.ws_ship_date_sk = d_date_sk AND ws1.ws_ship_addr_sk = ca_address_sk "
    "AND ca_state = 'NC' AND ws1.ws_web_site_sk = web_site_sk AND web_company_name = 'pri' "
    "AND ws1.ws_order_number IN (SELECT ws_order_number FROM ws_wh) "
    "AND ws1.ws_order_number IN (SELECT wr_order_number FROM web_returns, ws_wh "
    "WHERE wr_order_number = ws_wh.ws_order_number) "
    "ORDER BY COUNT(DISTINCT ws_order_number) LIMIT 100"
)


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return f"'{value}'"
    return str(value)


def prefixed(sql):
    tree = sqlglot.parse_one(sql, read="postgres")
    for table in tree.find_all(sqlglot.exp.Table):
        if table.name in TABLES:
            table.set("this", sqlglot.exp.to_identifier(PREFIX + table.name))
    return tree


@requires_engine("questdb")
@requires_engine("duckdb")
class QuestDBSubqueryLoweringCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.questdb = engines.connect("questdb")
        self.addCleanup(self.questdb.close)
        self.duckdb = engines.connect("duckdb")
        self.addCleanup(self.duckdb.close)
        for name, columns in TABLES.items():
            for connection in (self.questdb, self.duckdb):
                connection.execute(f"DROP TABLE IF EXISTS {PREFIX}{name}")
                connection.execute(f"CREATE TABLE {PREFIX}{name} {columns}")
                for row in ROWS[name]:
                    values = ", ".join(
                        f"CAST('{v}' AS DATE)" if name == "date_dim" and i == 1 else literal(v)
                        for i, v in enumerate(row)
                    )
                    connection.execute(f"INSERT INTO {PREFIX}{name} VALUES ({values})")

    def both(self, sql):
        tree = prefixed(sql)
        expected = self.duckdb.execute(tree.sql(dialect="duckdb"))
        generated = tree.sql(dialect="questdb")
        return generated, self.questdb.execute(generated), expected


class TestTpcdsQueries(QuestDBSubqueryLoweringCase):
    def check(self, sql, orders):
        generated, rows, expected = self.both(sql)
        self.assertNotIn("EXISTS", generated)
        self.assertNotIn(" IN (SELECT", generated)
        self.assertEqual(expected[0][0], orders)
        self.assertEqual(rows, expected)

    def test_q16(self):
        self.check(Q16, 3)

    def test_q94(self):
        self.check(Q94, 3)

    def test_q95(self):
        self.check(Q95, 2)


class TestLoweredShapes(QuestDBSubqueryLoweringCase):
    def test_not_exists_keeps_rows_with_a_null_key(self):
        sql = (
            "SELECT COUNT(*) FROM web_sales AS s WHERE NOT EXISTS("
            "SELECT 1 FROM web_returns AS r WHERE s.ws_order_number = r.wr_order_number)"
        )
        generated, rows, expected = self.both(sql)
        self.assertNotIn("EXISTS", generated)
        self.assertEqual(expected, [(18,)])
        self.assertEqual(rows, expected)

    def test_exists_with_a_filter_on_the_inner_table(self):
        sql = (
            "SELECT COUNT(*) FROM web_sales AS s WHERE EXISTS("
            "SELECT 1 FROM web_returns AS r WHERE s.ws_order_number = r.wr_order_number "
            "AND r.wr_order_number > 2)"
        )
        generated, rows, expected = self.both(sql)
        self.assertNotIn("EXISTS", generated)
        self.assertEqual(expected, [(3,)])
        self.assertEqual(rows, expected)

    def test_exists_with_two_equalities(self):
        sql = (
            "SELECT COUNT(*) FROM web_sales AS s WHERE EXISTS("
            "SELECT 1 FROM web_sales AS t WHERE s.ws_order_number = t.ws_order_number "
            "AND s.ws_warehouse_sk = t.ws_warehouse_sk AND t.ws_net_profit > 2)"
        )
        generated, rows, expected = self.both(sql)
        self.assertNotIn("EXISTS", generated)
        self.assertEqual(rows, expected)

    def test_in_subquery_with_null_values(self):
        sql = (
            "SELECT SUM(ws_net_profit) FROM web_sales WHERE ws_order_number IN "
            "(SELECT wr_order_number FROM web_returns)"
        )
        generated, rows, expected = self.both(sql)
        self.assertNotIn(" IN (SELECT", generated)
        self.assertEqual(expected, [(31.0,)])
        self.assertEqual(rows, expected)

    def test_in_subquery_with_a_filter_and_group_by(self):
        sql = (
            "SELECT COUNT(*) FROM web_sales WHERE ws_order_number IN "
            "(SELECT ws_order_number FROM web_sales WHERE ws_warehouse_sk > 1 "
            "GROUP BY ws_order_number)"
        )
        generated, rows, expected = self.both(sql)
        self.assertNotIn(" IN (SELECT", generated)
        self.assertEqual(rows, expected)

    def test_inequality_ignores_a_null_warehouse_in_the_group(self):
        sql = (
            "SELECT COUNT(*) FROM web_sales AS s WHERE EXISTS("
            "SELECT 1 FROM web_sales AS t WHERE s.ws_order_number = t.ws_order_number "
            "AND s.ws_warehouse_sk <> t.ws_warehouse_sk)"
        )
        generated, rows, expected = self.both(sql)
        self.assertNotIn("EXISTS", generated)
        self.assertEqual(rows, expected)


class TestUnloweredShapes(QuestDBSubqueryLoweringCase):
    def generated(self, sql):
        return prefixed(sql).sql(dialect="questdb")

    def test_not_exists_with_an_inequality_is_left_as_written(self):
        sql = (
            "SELECT COUNT(*) FROM web_sales AS s WHERE NOT EXISTS("
            "SELECT 1 FROM web_sales AS t WHERE s.ws_order_number = t.ws_order_number "
            "AND s.ws_warehouse_sk <> t.ws_warehouse_sk)"
        )
        self.assertIn("NOT EXISTS(", self.generated(sql))

    def test_not_in_is_left_as_written(self):
        sql = (
            "SELECT COUNT(*) FROM web_sales WHERE ws_order_number NOT IN "
            "(SELECT wr_order_number FROM web_returns)"
        )
        self.assertIn("NOT ws_order_number IN (SELECT", self.generated(sql))

    def test_exists_under_or_is_left_as_written(self):
        sql = (
            "SELECT COUNT(*) FROM web_sales AS s WHERE s.ws_net_profit > 100 OR EXISTS("
            "SELECT 1 FROM web_returns AS r WHERE s.ws_order_number = r.wr_order_number)"
        )
        self.assertIn("OR EXISTS(", self.generated(sql))


if __name__ == "__main__":
    unittest.main()
