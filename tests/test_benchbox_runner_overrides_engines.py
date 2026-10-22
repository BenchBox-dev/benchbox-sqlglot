"""Execute the rewrites that replace the TPC-DS runner overrides on real engines.

The queries are TPC-DS 36, 47, 57, 66, 70, 86 and 90 as BenchBox generates them
(``tests/tpcds_queries.py``); the data is a few inline rows per table, so every statement runs on
every engine and the chDB connection (one statement per call) needs no tables. DuckDB runs the
query as written and is the reference: PostgreSQL (``postgres`` job) and ClickHouse (chDB,
``in-process`` job) run the rewritten text and must return the same rows. Each test also runs the
text without the rewrite and asserts that the engine rejects it, which is why the rewrite exists.
The same queries ran at scale factor 0.01 against the generated TPC-DS data (``downstream/ENGINE-GAPS.md``).
"""

import unittest
from decimal import Decimal

import sqlglot
from sqlglot.benchbox.compat import inline_order_by_aliases
from sqlglot.benchbox.engine_rewrites import (
    alias_qualified_projections,
    qualify_alias_shadowed_columns,
)
from sqlglot.benchbox.rewrites import nullif_zero_divisors
from tests import engines
from tests.engines import requires_engine
from tests.tpcds_queries import Q36, Q47, Q57, Q66, Q70, Q86, Q90


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, Decimal):
        return f"CAST({value} AS DECIMAL(9, 2))"
    return repr(value)


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


def data(**tables):
    return "WITH " + ", ".join(f"{name} AS ({sql})" for name, sql in tables.items())


def with_data(prefix, sql):
    """Put the data tables in front of the query's own ``WITH`` entries, if it has any."""
    sql = sql.rstrip().rstrip(";")
    return f"{prefix}, {sql[5:]}" if sql.upper().startswith("WITH ") else f"{prefix} {sql}"


def comparable(rows):
    """Rows as text-independent values: numbers rounded to six places, NULL spellings unified."""
    result = []
    for row in rows:
        cells = []
        for cell in row:
            if cell is None or cell in ("\\N", "NULL"):
                cells.append(None)
                continue
            try:
                cells.append(round(float(cell), 6))
            except (TypeError, ValueError):
                cells.append(str(cell))
        result.append(tuple(cells))
    return result


def d(value):
    return Decimal(value)


def write(tree, dialect):
    """The text BenchBox sends: identifiers quoted for every engine except ClickHouse."""
    return tree.sql(dialect, identify=dialect != "clickhouse")


def as_written(query, dialect):
    return write(sqlglot.parse_one(query, read="netezza"), dialect)


def run_reference(prefix, query):
    return comparable(engines.execute("duckdb", with_data(prefix, as_written(query, "duckdb"))))


# Q36 and Q86 category/class hierarchy, Q70 state/county hierarchy.
ITEMS = union_all(
    ("i_item_sk", "i_category", "i_class"),
    [
        (1, "Books", "fiction"),
        (2, "Books", "science"),
        (3, "Music", "rock"),
        (4, "Music", "jazz"),
        (5, "Toys", "games"),
    ],
)
Q36_DATA = data(
    item=ITEMS,
    date_dim=union_all(("d_date_sk", "d_year"), [(1, 2002)]),
    store=union_all(("s_store_sk", "s_state"), [(1, "TN")]),
    store_sales=union_all(
        ("ss_item_sk", "ss_sold_date_sk", "ss_store_sk", "ss_net_profit", "ss_ext_sales_price"),
        [
            (1, 1, 1, d("10.00"), d("100.00")),
            (1, 1, 1, d("5.00"), d("40.00")),
            (2, 1, 1, d("-3.00"), d("30.00")),
            (3, 1, 1, d("7.50"), d("50.00")),
            (4, 1, 1, d("2.00"), d("60.00")),
            (5, 1, 1, d("-8.00"), d("80.00")),
        ],
    ),
)
Q86_DATA = data(
    item=ITEMS,
    date_dim=union_all(("d_date_sk", "d_month_seq"), [(1, 1180), (2, 1300)]),
    web_sales=union_all(
        ("ws_item_sk", "ws_sold_date_sk", "ws_net_paid"),
        [
            (1, 1, d("10.00")),
            (1, 1, d("30.00")),
            (2, 1, d("25.00")),
            (3, 1, d("12.00")),
            (4, 1, d("40.00")),
            (5, 1, d("8.00")),
            (5, 2, d("99.00")),
        ],
    ),
)
Q70_DATA = data(
    date_dim=union_all(("d_date_sk", "d_month_seq"), [(1, 1180), (2, 1300)]),
    store=union_all(
        ("s_store_sk", "s_state", "s_county"),
        [(1, "TN", "Williamson"), (2, "TN", "Davidson"), (3, "GA", "Fulton"), (4, "OH", "Lorain")],
    ),
    store_sales=union_all(
        ("ss_store_sk", "ss_sold_date_sk", "ss_net_profit"),
        [
            (1, 1, d("10.00")),
            (1, 1, d("4.00")),
            (2, 1, d("21.00")),
            (3, 1, d("7.00")),
            (4, 1, d("3.00")),
            (4, 2, d("50.00")),
        ],
    ),
)


@requires_engine("duckdb")
@requires_engine("postgres")
class TestPostgresRollupOrdering(unittest.TestCase):
    """Q36, Q70 and Q86 sort on an expression of the ``lochierarchy`` alias, which PostgreSQL rejects."""

    def check(self, prefix, query):
        reference = run_reference(prefix, query)
        self.assertGreater(len(reference), 4)
        text = as_written(query, "postgres")
        with self.assertRaises(Exception) as caught:
            engines.execute("postgres", with_data(prefix, text))
        self.assertIn("lochierarchy", str(caught.exception))
        tree = inline_order_by_aliases(sqlglot.parse_one(query, read="netezza"))
        rewritten = comparable(
            engines.execute("postgres", with_data(prefix, write(tree, "postgres")))
        )
        self.assertEqual(rewritten, reference)
        # The rollup levels arrive grand total first, so the sort key really was evaluated.
        self.assertEqual(rewritten[0][3], 2.0)

    def test_q36(self):
        self.check(Q36_DATA, Q36)

    def test_q86(self):
        self.check(Q86_DATA, Q86)

    def test_q70(self):
        self.check(Q70_DATA, Q70)


Q90_DATA = data(
    household_demographics=union_all(("hd_demo_sk", "hd_dep_count"), [(1, 3), (2, 5)]),
    web_page=union_all(("wp_web_page_sk", "wp_char_count"), [(1, 5100), (2, 100)]),
    time_dim=union_all(("t_time_sk", "t_hour"), [(1, 6), (2, 7), (3, 17), (4, 18), (5, 12)]),
)


def q90_data(morning, evening):
    sales = [(1 if i % 2 == 0 else 2, 1, 1) for i in range(morning)]
    sales += [(3 if i % 2 == 0 else 4, 1, 1) for i in range(evening)]
    sales.append((5, 1, 1))
    sales.append((1, 2, 1))
    return (
        Q90_DATA
        + ", "
        + "web_sales AS ("
        + union_all(("ws_sold_time_sk", "ws_ship_hdemo_sk", "ws_web_page_sk"), sales)
        + ")"
    )


def q90_rewritten(dialect):
    return write(nullif_zero_divisors(sqlglot.parse_one(Q90, read="netezza"), ["pmc"]), dialect)


class Q90Cases:
    engine = ""
    dialect = ""

    def run_query(self, sql):
        return comparable(engines.execute(self.engine, sql))

    def test_ratio_is_the_same_with_and_without_the_guard(self):
        prefix = q90_data(morning=6, evening=4)
        expected = [(1.5,)]
        self.assertEqual(run_reference(prefix, Q90), expected)
        self.assertEqual(self.run_query(with_data(prefix, q90_rewritten(self.dialect))), expected)

    def test_zero_evening_count_gives_null_where_the_engine_would_fail(self):
        prefix = q90_data(morning=6, evening=0)
        # DuckDB divides by zero to infinity; the guard makes every engine answer NULL.
        self.assertEqual(run_reference(prefix, Q90), [(float("inf"),)])
        self.assertEqual(self.run_query(with_data(prefix, q90_rewritten(self.dialect))), [(None,)])
        with self.assertRaises(Exception) as caught:
            engines.execute(self.engine, with_data(prefix, as_written(Q90, self.dialect)))
        self.assertIn("zero", str(caught.exception).lower())


@requires_engine("duckdb")
@requires_engine("postgres")
class TestQ90OnPostgres(Q90Cases, unittest.TestCase):
    engine = "postgres"
    dialect = "postgres"


@requires_engine("duckdb")
@requires_engine("chdb")
class TestQ90OnClickHouse(Q90Cases, unittest.TestCase):
    engine = "chdb"
    dialect = "clickhouse"


@requires_engine("duckdb")
class TestQ90OnDuckDB(unittest.TestCase):
    def test_guarded_text_runs_and_matches(self):
        for morning, evening, expected in ((6, 4, [(1.5,)]), (6, 0, [(None,)])):
            prefix = q90_data(morning, evening)
            with self.subTest(evening=evening):
                rows = comparable(
                    engines.execute("duckdb", with_data(prefix, q90_rewritten("duckdb")))
                )
                self.assertEqual(rows, expected)


def months(first_sk, *year_month):
    return [(first_sk + i, year, month) for i, (year, month) in enumerate(year_month)]


MONTHS = months(1, (2000, 12), (2001, 1), (2001, 2), (2001, 3), (2001, 4), (2002, 1))
DATE_DIM = union_all(("d_date_sk", "d_year", "d_moy"), MONTHS)


def q47_data():
    # Brand series: December 2000, four months of 2001, January 2002. Every 2001 month of b1 deviates
    # from the 2001 average (142.5) by more than 10%, b2 never does, and no two deviations tie.
    series = {"b1": (100, 300, 120, 90, 60, 100), "b2": (50, 50, 50, 50, 50, 50)}
    sales = [
        (item, date_sk, 1, d(f"{price}.00"))
        for item, prices in ((1, series["b1"]), (2, series["b2"]))
        for date_sk, price in enumerate(prices, start=1)
    ]
    return data(
        item=union_all(
            ("i_item_sk", "i_category", "i_brand"), [(1, "Men", "b1"), (2, "Men", "b2")]
        ),
        date_dim=DATE_DIM,
        store=union_all(("s_store_sk", "s_store_name", "s_company_name"), [(1, "ese", "Unknown")]),
        store_sales=union_all(
            ("ss_item_sk", "ss_sold_date_sk", "ss_store_sk", "ss_sales_price"), sales
        ),
    )


def q57_data():
    series = {"b1": (100, 300, 120, 90, 60, 100), "b2": (50, 50, 50, 50, 50, 50)}
    sales = [
        (item, date_sk, 1, d(f"{price}.00"))
        for item, prices in ((1, series["b1"]), (2, series["b2"]))
        for date_sk, price in enumerate(prices, start=1)
    ]
    return data(
        item=union_all(
            ("i_item_sk", "i_category", "i_brand"), [(1, "Men", "b1"), (2, "Men", "b2")]
        ),
        date_dim=DATE_DIM,
        call_center=union_all(("cc_call_center_sk", "cc_name"), [(1, "NY Metro")]),
        catalog_sales=union_all(
            ("cs_item_sk", "cs_sold_date_sk", "cs_call_center_sk", "cs_sales_price"), sales
        ),
    )


def clickhouse_form(query, *rewrites):
    tree = sqlglot.parse_one(query, read="netezza")
    for rewrite in rewrites:
        tree = rewrite(tree)
    return write(tree, "clickhouse")


@requires_engine("duckdb")
@requires_engine("chdb")
class TestMonthlyAverageQueriesOnClickHouse(unittest.TestCase):
    """Q47 and Q57 project ``v1.d_year`` from a self join, which ClickHouse names ``v1.d_year``."""

    def check(self, query, prefix):
        reference = run_reference(prefix, query)
        self.assertGreaterEqual(len(reference), 3)
        with self.assertRaises(Exception) as caught:
            engines.execute("chdb", with_data(prefix, clickhouse_form(query)))
        self.assertIn("d_year", str(caught.exception))
        rewritten = clickhouse_form(query, alias_qualified_projections)
        self.assertEqual(
            comparable(engines.execute("chdb", with_data(prefix, rewritten))), reference
        )

    def test_q47(self):
        self.check(Q47, q47_data())

    def test_q57(self):
        self.check(Q57, q57_data())


def q66_data():
    # (date, warehouse, time, ship mode, price, quantity, net paid): January and February 2001 sales
    # of two warehouses; the last three web rows fall outside the year, the time window and the carriers.
    rows = {
        "ws": [
            (1, 1, 1, 1, d("25.00"), 2, d("30.00")),
            (2, 1, 1, 1, d("12.50"), 4, d("20.00")),
            (1, 2, 1, 1, d("10.00"), 3, d("11.00")),
            (3, 1, 1, 1, d("99.00"), 1, d("99.00")),
            (1, 1, 2, 1, d("77.00"), 1, d("77.00")),
            (1, 1, 1, 2, d("66.00"), 1, d("66.00")),
        ],
        "cs": [
            (1, 1, 1, 1, d("37.50"), 2, d("40.00")),
            (2, 2, 1, 1, d("5.00"), 2, d("6.00")),
            (1, 2, 1, 1, d("5.00"), 4, d("6.00")),
        ],
    }

    def sales(prefix):
        columns = (
            "sold_date_sk",
            "warehouse_sk",
            "sold_time_sk",
            "ship_mode_sk",
            "ext_sales_price",
            "quantity",
        )
        return union_all(
            tuple(f"{prefix}_{name}" for name in (*columns, "net_paid_inc_ship_tax")), rows[prefix]
        )

    return data(
        warehouse=union_all(
            (
                "w_warehouse_sk",
                "w_warehouse_name",
                "w_warehouse_sq_ft",
                "w_city",
                "w_county",
                "w_state",
                "w_country",
            ),
            [
                (
                    1,
                    "Conventional childr",
                    50,
                    "Midway",
                    "Williamson County",
                    "TN",
                    "United States",
                ),
                (2, "Doors canno", 20, "Fairview", "Williamson County", "TN", "United States"),
            ],
        ),
        date_dim=union_all(
            ("d_date_sk", "d_year", "d_moy"), [(1, 2001, 1), (2, 2001, 2), (3, 2000, 12)]
        ),
        time_dim=union_all(("t_time_sk", "t_time"), [(1, 2000), (2, 90000)]),
        ship_mode=union_all(("sm_ship_mode_sk", "sm_carrier"), [(1, "BARIAN"), (2, "OTHER")]),
        web_sales=sales("ws"),
        catalog_sales=sales("cs"),
    )


@requires_engine("duckdb")
@requires_engine("chdb")
class TestWarehouseMonthlyQueryOnClickHouse(unittest.TestCase):
    """Q66 sums ``jan_sales`` under the alias ``jan_sales``, which ClickHouse reads as the alias."""

    def test_q66(self):
        prefix = q66_data()
        reference = run_reference(prefix, Q66)
        self.assertEqual(len(reference), 2)
        with self.assertRaises(Exception) as caught:
            engines.execute("chdb", with_data(prefix, clickhouse_form(Q66)))
        self.assertIn("inside another aggregate", str(caught.exception))
        rewritten = clickhouse_form(Q66, qualify_alias_shadowed_columns)
        self.assertEqual(
            comparable(engines.execute("chdb", with_data(prefix, rewritten))), reference
        )


if __name__ == "__main__":
    unittest.main()
