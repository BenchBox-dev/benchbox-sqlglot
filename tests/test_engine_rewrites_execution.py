"""Execute the engine rewrites on real engines.

Run by the ``in-process``, ``starrocks`` and ``doris`` jobs of ``benchbox-engines.yml``;
locally each class skips when its engine is not available. Data is built inline with
``SELECT ... UNION ALL`` common table expressions, so the statements run on every engine
and the chDB connection (one statement per call) needs no tables.
"""

import unittest

import sqlglot
from sqlglot.benchbox.engine_rewrites import (
    add_derived_table_aliases,
    add_query_settings,
    correlated_exists_to_in,
    decimal_zero_literals,
    lowercase_identifiers,
    qualify_order_by_columns,
)
from sqlglot.benchbox.rewrites import safe_division
from tests import engines
from tests.engines import requires_engine

UPPERCASE_NAMES = (r"[A-Z][A-Z_]+_SK", r"[A-Z][A-Z_]+_AMT", "D_YEAR", "D_MOY")


def literal(value):
    return "NULL" if value is None else repr(value)


def union_all(columns, rows):
    selects = []
    for i, row in enumerate(rows):
        if i == 0:
            selects.append(
                "SELECT " + ", ".join(f"{literal(v)} AS {c}" for v, c in zip(row, columns))
            )
        else:
            selects.append("SELECT " + ", ".join(literal(v) for v in row))
    return " UNION ALL ".join(selects)


def with_data(**tables):
    return "WITH " + ", ".join(f"{name} AS ({sql})" for name, sql in tables.items()) + " "


SEMI_JOIN_DATA = with_data(
    customer=union_all(
        ("c_customer_sk", "c_name"),
        [(1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e"), (None, "f")],
    ),
    store_sales=union_all(
        ("ss_customer_sk", "ss_sold_date_sk"),
        [(1, 10), (1, 10), (2, 11), (4, 12), (None, 10), (4, 10)],
    ),
    web_sales=union_all(
        ("ws_bill_customer_sk", "ws_sold_date_sk"), [(1, 13), (2, 10), (3, 10), (3, 13)]
    ),
    catalog_sales=union_all(
        ("cs_ship_customer_sk", "cs_sold_date_sk"), [(4, 10), (5, 14), (None, 10)]
    ),
    date_dim=union_all(
        ("d_date_sk", "d_year", "d_moy", "d_qoy"),
        [(10, 2002, 2, 1), (11, 2002, 6, 2), (12, 2001, 2, 1), (13, 2002, 3, 1), (14, 2002, 1, 1)],
    ),
)
SEMI_JOIN_QUERY = (
    "SELECT c.c_name FROM customer AS c WHERE "
    "EXISTS (SELECT * FROM store_sales, date_dim WHERE c.c_customer_sk = ss_customer_sk "
    "AND ss_sold_date_sk = d_date_sk AND d_year = 2002 AND d_moy BETWEEN 1 AND 1 + 3) "
    "AND (EXISTS (SELECT * FROM web_sales, date_dim WHERE c.c_customer_sk = ws_bill_customer_sk "
    "AND ws_sold_date_sk = d_date_sk AND d_year = 2002 AND d_moy BETWEEN 1 AND 1 + 3) "
    "OR EXISTS (SELECT * FROM catalog_sales, date_dim WHERE c.c_customer_sk = cs_ship_customer_sk "
    "AND cs_sold_date_sk = d_date_sk AND d_year = 2002 AND d_moy BETWEEN 1 AND 1 + 3)) "
    "ORDER BY c.c_name"
)
SEMI_JOIN_EXPECTED = [("a",), ("d",)]


def names(rows):
    return [tuple(str(v) for v in row) for row in rows]


@requires_engine("duckdb")
class TestSemiJoinOnDuckDB(unittest.TestCase):
    def test_in_form_returns_the_rows_of_the_exists_form(self):
        sql = SEMI_JOIN_DATA + SEMI_JOIN_QUERY
        rewritten = correlated_exists_to_in(sqlglot.parse_one(sql, read="duckdb"), expected=3)
        self.assertNotIn("EXISTS", rewritten.sql("duckdb"))
        original = engines.execute("duckdb", sql)
        self.assertEqual(names(original), SEMI_JOIN_EXPECTED)
        self.assertEqual(engines.execute("duckdb", rewritten.sql("duckdb")), original)

    def test_null_keys_and_missing_matches_do_not_survive_either_form(self):
        sql = (
            SEMI_JOIN_DATA
            + "SELECT c.c_name FROM customer AS c WHERE EXISTS (SELECT 1 FROM store_sales "
            "WHERE ss_customer_sk = c.c_customer_sk) OR c.c_name = 'e' ORDER BY c.c_name"
        )
        rewritten = correlated_exists_to_in(sqlglot.parse_one(sql, read="duckdb"), expected=1)
        original = engines.execute("duckdb", sql)
        self.assertEqual(names(original), [("a",), ("b",), ("d",), ("e",)])
        self.assertEqual(engines.execute("duckdb", rewritten.sql("duckdb")), original)


@requires_engine("chdb")
class TestClickHouseRewrites(unittest.TestCase):
    def run_sql(self, tree):
        return engines.execute("chdb", tree.sql("clickhouse"))

    def test_semi_join_form_executes_and_matches_the_exists_result(self):
        tree = correlated_exists_to_in(
            sqlglot.parse_one(SEMI_JOIN_DATA + SEMI_JOIN_QUERY, read="clickhouse"), expected=3
        )
        self.assertEqual(self.run_sql(tree), SEMI_JOIN_EXPECTED)

    def test_uppercase_names_are_resolved_after_lowercasing(self):
        sql = (
            "WITH t AS (SELECT 2002 AS d_year, 5 AS ss_item_sk) "
            "SELECT SS_ITEM_SK FROM t WHERE D_YEAR = 2002"
        )
        with self.assertRaises(Exception):
            engines.execute("chdb", sql)
        tree = lowercase_identifiers(sqlglot.parse_one(sql, read="clickhouse"), UPPERCASE_NAMES)
        self.assertEqual(self.run_sql(tree), [("5",)])

    def test_zero_literals_division_and_settings_execute_together(self):
        sql = (
            "WITH t AS (SELECT CAST(10.50 AS Nullable(Decimal(15, 2))) AS net_amt, "
            "CAST(0 AS Nullable(Decimal(15, 2))) AS zero_amt, CAST(NULL AS Nullable(Decimal(15, 2))) AS null_amt) "
            "SELECT COALESCE(null_amt, 0.0) + net_amt AS total, net_amt / zero_amt AS ratio, "
            "CASE WHEN net_amt > 100 THEN net_amt ELSE 0.0 END AS capped FROM t"
        )
        tree = sqlglot.parse_one(sql, read="clickhouse")
        tree = decimal_zero_literals(tree)
        tree = safe_division(tree, "clickhouse")
        tree = add_query_settings(tree, {"joined_subquery_requires_alias": 0, "max_threads": 1})
        self.assertTrue(
            tree.sql("clickhouse").endswith(
                "SETTINGS joined_subquery_requires_alias = 0, max_threads = 1"
            )
        )
        (row,) = self.run_sql(tree)
        self.assertEqual((float(row[0]), row[1], float(row[2])), (10.5, "\\N", 0.0))

    def test_settings_on_a_union_apply_to_the_whole_query(self):
        tree = add_query_settings(
            sqlglot.parse_one("SELECT 1 AS a UNION ALL SELECT 2", read="clickhouse"),
            {"max_threads": 1},
        )
        self.assertEqual(sorted(self.run_sql(tree)), [("1",), ("2",)])

    def test_derived_table_aliases_execute(self):
        tree = add_derived_table_aliases(
            sqlglot.parse_one("SELECT * FROM (SELECT 1 AS x), (SELECT 2 AS y)", read="clickhouse")
        )
        self.assertEqual(self.run_sql(tree), [("1", "2")])


DAYS_QUERY = "SELECT d FROM ({dates}) AS dates WHERE d BETWEEN {low} AND {low} + 30 days ORDER BY d"


class NetezzaDayDurations:
    ENGINE = ""
    DIALECT = ""

    def date(self, text):
        return f"CAST('{text}' AS DATE)"

    def test_plus_and_minus_days_move_a_date(self):
        sql = (
            f"SELECT {self.date('2000-02-01')} + 30 days AS later, "
            f"{self.date('2000-02-01')} - 31 days AS earlier"
        )
        translated = sqlglot.transpile(sql, read="netezza", write=self.DIALECT)[0]
        (row,) = engines.execute(self.ENGINE, translated)
        self.assertEqual([str(v)[:10] for v in row], ["2000-03-02", "2000-01-01"])

    def test_days_in_a_range_predicate(self):
        dates = " UNION ALL ".join(
            f"SELECT {self.date(day)} AS d"
            for day in ("2000-01-31", "2000-02-01", "2000-03-02", "2000-03-03")
        )
        sql = DAYS_QUERY.format(dates=dates, low=self.date("2000-02-01"))
        translated = sqlglot.transpile(sql, read="netezza", write=self.DIALECT)[0]
        rows = engines.execute(self.ENGINE, translated)
        self.assertEqual([str(r[0])[:10] for r in rows], ["2000-02-01", "2000-03-02"])


@requires_engine("duckdb")
class TestDayDurationsOnDuckDB(NetezzaDayDurations, unittest.TestCase):
    ENGINE = "duckdb"
    DIALECT = "duckdb"


@requires_engine("chdb")
class TestDayDurationsOnClickHouse(NetezzaDayDurations, unittest.TestCase):
    ENGINE = "chdb"
    DIALECT = "clickhouse"


@requires_engine("starrocks")
class TestDayDurationsOnStarRocks(NetezzaDayDurations, unittest.TestCase):
    ENGINE = "starrocks"
    DIALECT = "starrocks"


@requires_engine("doris")
class TestDayDurationsOnDoris(NetezzaDayDurations, unittest.TestCase):
    ENGINE = "doris"
    DIALECT = "doris"


ORDER_BY_DATA = with_data(
    ss_items=union_all(("item_id", "ss_item_rev"), [("b", 2), ("a", 1)]),
    cs_items=union_all(("item_id", "cs_item_rev"), [("a", 10), ("b", 20)]),
    ws_items=union_all(("item_id", "ws_item_rev"), [("a", 100), ("b", 200)]),
)
ORDER_BY_QUERY = (
    "SELECT ss_items.item_id AS item_id, ss_item_rev, cs_item_rev, ws_item_rev "
    "FROM ss_items, cs_items, ws_items WHERE ss_items.item_id = cs_items.item_id "
    "AND ss_items.item_id = ws_items.item_id ORDER BY {order}, ss_item_rev"
)


@requires_engine("duckdb")
class TestQualifiedOrderByOnDuckDB(unittest.TestCase):
    def test_order_by_runs_in_every_spelling(self):
        for order in ("item_id", '"item_id"'):
            with self.subTest(order=order):
                sql = ORDER_BY_DATA + ORDER_BY_QUERY.format(order=order)
                tree = qualify_order_by_columns(
                    sqlglot.parse_one(sql, read="duckdb"), {"item_id": "ss_items"}
                )
                self.assertIn(
                    'ORDER BY "ss_items"."item_id"'
                    if '"' in order
                    else "ORDER BY ss_items.item_id",
                    tree.sql("duckdb"),
                )
                self.assertEqual(
                    [r[0] for r in engines.execute("duckdb", tree.sql("duckdb"))], ["a", "b"]
                )


class DerivedTableAliases:
    ENGINE = ""
    DIALECT = ""

    def run_aliased(self, sql):
        tree = add_derived_table_aliases(sqlglot.parse_one(sql, read=self.DIALECT))
        rows = engines.execute(self.ENGINE, tree.sql(self.DIALECT))
        return [tuple(int(v) for v in row) for row in rows]

    def test_from_subquery(self):
        self.assertEqual(self.run_aliased("SELECT * FROM (SELECT 1 AS x)"), [(1,)])

    def test_comma_joined_and_nested_subqueries(self):
        self.assertEqual(
            self.run_aliased("SELECT * FROM (SELECT 1 AS x), (SELECT * FROM (SELECT 2 AS y))"),
            [(1, 2)],
        )

    def test_subquery_in_a_union(self):
        self.assertEqual(
            self.run_aliased("SELECT x FROM (SELECT 1 AS x) UNION ALL SELECT 2 ORDER BY x"),
            [(1,), (2,)],
        )


RESERVED_ALIAS_WORDS = (
    "character rank order group key value values partition range rows table column columns "
    "index database schema status type default primary unique current_date current_time "
    "current_timestamp interval match natural dense_rank row_number percent_rank cume_dist ntile "
    "lead lag"
).split()


class MySqlFamilyGeneration:
    ENGINE = ""
    DIALECT = ""

    def translate_and_execute(self, sql, read="postgres"):
        translated = sqlglot.transpile(sql, read=read, write=self.DIALECT)[0]
        return engines.execute(self.ENGINE, translated)

    def test_reserved_words_work_as_aliases(self):
        for word in RESERVED_ALIAS_WORDS:
            with self.subTest(word=word):
                self.assertEqual(
                    [int(r[0]) for r in self.translate_and_execute(f"SELECT 1 AS {word}")], [1]
                )

    def test_ansi_substring_and_double_quoted_identifiers(self):
        rows = self.translate_and_execute(
            'SELECT SUBSTRING(s FROM 2 FOR 3) AS part, "n" FROM '
            "(SELECT 'abcdef' AS s, 7 AS \"n\") AS t"
        )
        self.assertEqual([(r[0], int(r[1])) for r in rows], [("bcd", 7)])

    def test_nested_ansi_substring(self):
        rows = self.translate_and_execute(
            "SELECT SUBSTRING(LOWER(s) FROM 2 FOR 2) FROM (SELECT 'ABCDEF' AS s) AS t"
        )
        self.assertEqual([r[0] for r in rows], ["bc"])


@requires_engine("starrocks")
class TestDerivedTableAliasesOnStarRocks(
    DerivedTableAliases, MySqlFamilyGeneration, unittest.TestCase
):
    ENGINE = "starrocks"
    DIALECT = "starrocks"


@requires_engine("doris")
class TestDerivedTableAliasesOnDoris(DerivedTableAliases, MySqlFamilyGeneration, unittest.TestCase):
    ENGINE = "doris"
    DIALECT = "doris"


@requires_engine("duckdb")
class TestDerivedTableAliasesOnDuckDB(DerivedTableAliases, unittest.TestCase):
    ENGINE = "duckdb"
    DIALECT = "duckdb"


if __name__ == "__main__":
    unittest.main()
