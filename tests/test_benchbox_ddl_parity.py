"""DDL parity with the engines' own grammars: QuestDB layouts, BigQuery constraints, array types.

The QuestDB class runs in the ``questdb`` job (and locally with ``BENCHBOX_ENGINE_QUESTDB``):
it creates the tables on a real QuestDB and reads the column types, designated timestamp and
partitioning back. BigQuery has no engine in CI (``downstream/ENGINE-GAPS.md``), so its
constraint and naming output is asserted as text against the grammar in the BigQuery
``CREATE TABLE`` reference.
"""

import unittest

import sqlglot
from sqlglot import exp
from sqlglot.benchbox.ddl import DdlOptions, transpile_ddl
from sqlglot.benchbox.layout import TableLayout
from tests import engines
from tests.engines import requires_engine

LINEITEM = (
    "CREATE TABLE lineitem (l_orderkey INTEGER NOT NULL, l_returnflag CHAR(1) NOT NULL, "
    "l_comment VARCHAR(44) NOT NULL, l_shipdate DATE NOT NULL, l_commitdate DATE NOT NULL, "
    "l_price DECIMAL(15,2), stamp TIMESTAMP(6), PRIMARY KEY (l_orderkey))"
)
QUESTDB_LAYOUT = TableLayout(
    symbol_columns=["l_returnflag", "not_a_column"],
    timestamp_columns=["l_commitdate"],
    designated_timestamp="l_shipdate",
    time_partition="month",
)


def ddl(sql, write, read="postgres", **options):
    return transpile_ddl(sql, read=read, write=write, options=DdlOptions(**options))


class TestQuestDBLayout(unittest.TestCase):
    def test_symbols_timestamps_designated_timestamp_and_partitioning(self):
        self.assertEqual(
            ddl(LINEITEM, "questdb", strip_primary_keys=True, default_layout=QUESTDB_LAYOUT),
            [
                "CREATE TABLE lineitem (l_orderkey INT NOT NULL, l_returnflag SYMBOL, "
                "l_comment VARCHAR NOT NULL, l_shipdate TIMESTAMP NOT NULL, "
                "l_commitdate TIMESTAMP NOT NULL, l_price DECIMAL(15, 2), stamp TIMESTAMP) "
                "timestamp(l_shipdate) PARTITION BY MONTH"
            ],
        )

    def test_no_partition_clause_for_none_and_the_default(self):
        for unit in (None, "none"):
            with self.subTest(unit=unit):
                [sql] = ddl(
                    LINEITEM,
                    "questdb",
                    strip_primary_keys=True,
                    default_layout=TableLayout(designated_timestamp="l_shipdate", time_partition=unit),
                )
                self.assertTrue(sql.endswith(") timestamp(l_shipdate)"), sql)

    def test_types_take_no_length_without_a_layout(self):
        [sql] = ddl(LINEITEM, "questdb", strip_primary_keys=True)
        self.assertEqual(
            sql,
            "CREATE TABLE lineitem (l_orderkey INT NOT NULL, l_returnflag VARCHAR NOT NULL, "
            "l_comment VARCHAR NOT NULL, l_shipdate DATE NOT NULL, l_commitdate DATE NOT NULL, "
            "l_price DECIMAL(15, 2), stamp TIMESTAMP)",
        )

    def test_invalid_layouts(self):
        for layout, message in (
            (TableLayout(designated_timestamp="missing"), "is not defined"),
            (TableLayout(designated_timestamp="l_comment"), "not a DATE or TIMESTAMP"),
            (TableLayout(time_partition="day"), "needs a designated_timestamp"),
        ):
            with self.subTest(layout=layout), self.assertRaisesRegex(ValueError, message):
                ddl(LINEITEM, "questdb", default_layout=layout)
        with self.assertRaisesRegex(ValueError, "time_partition must be one of"):
            TableLayout(time_partition="decade")


class TestArraySizes(unittest.TestCase):
    SQL = "CREATE TABLE v (id BIGINT, e DOUBLE[256])"

    def test_engines_without_fixed_size_arrays_get_a_plain_array(self):
        for write, expected in (
            ("spark", "ARRAY<DOUBLE>"),
            ("databricks", "ARRAY<DOUBLE>"),
            ("athena", "ARRAY<DOUBLE>"),
            ("trino", "ARRAY(DOUBLE)"),
            ("bigquery", "ARRAY<FLOAT64>"),
            ("questdb", "DOUBLE PRECISION[]"),
        ):
            with self.subTest(write=write):
                self.assertEqual(ddl(self.SQL, write), [f"CREATE TABLE v (id {'INT64' if write == 'bigquery' else 'BIGINT'}, e {expected})"])

    def test_duckdb_and_postgres_keep_the_size(self):
        self.assertEqual(ddl(self.SQL, "duckdb"), ["CREATE TABLE v (id BIGINT, e DOUBLE[256])"])
        self.assertEqual(
            ddl(self.SQL, "postgres"), ["CREATE TABLE v (id BIGINT, e DOUBLE PRECISION[256])"]
        )


class TestBigQueryConstraints(unittest.TestCase):
    OPTIONS = {"or_replace": True, "schema": "proj.ds", "table_case": "upper"}

    def test_primary_key_is_table_level_and_not_enforced(self):
        for source in (
            "CREATE TABLE t (a INT PRIMARY KEY, b TEXT)",
            "CREATE TABLE t (a INT, b TEXT, PRIMARY KEY (a))",
            "CREATE TABLE t (a INT, b TEXT, CONSTRAINT pk PRIMARY KEY (a))",
        ):
            with self.subTest(source=source):
                [sql] = ddl(source, "bigquery", **self.OPTIONS)
                name = "CONSTRAINT pk " if "CONSTRAINT" in source else ""
                self.assertEqual(
                    sql,
                    "CREATE OR REPLACE TABLE proj.ds.T (a INT64, b STRING, "
                    f"{name}PRIMARY KEY (a) NOT ENFORCED)",
                )

    def test_foreign_keys_are_not_enforced_and_qualified_like_the_table(self):
        [sql] = ddl(
            "CREATE TABLE child (id INT, pid INT REFERENCES parent (id), oid INT, "
            "FOREIGN KEY (oid) REFERENCES other (id))",
            "bigquery",
            **self.OPTIONS,
        )
        self.assertEqual(
            sql,
            "CREATE OR REPLACE TABLE proj.ds.CHILD (id INT64, pid INT64, oid INT64, "
            "FOREIGN KEY (oid) REFERENCES proj.ds.OTHER (id) NOT ENFORCED, "
            "FOREIGN KEY (pid) REFERENCES proj.ds.PARENT (id) NOT ENFORCED)",
        )

    def test_parsed_back_the_constraints_are_not_enforced(self):
        [sql] = ddl(
            "CREATE TABLE t (a INT PRIMARY KEY, b INT REFERENCES r (x))", "bigquery", **self.OPTIONS
        )
        tree = sqlglot.parse_one(sql, read="bigquery")
        keys = [*tree.find_all(exp.PrimaryKey), *tree.find_all(exp.Reference)]
        self.assertEqual(len(keys), 2)
        for key in keys:
            self.assertEqual([str(getattr(o, "name", o)) for o in key.args["options"]], ["NOT ENFORCED"])

    def test_stripped_constraints_stay_stripped(self):
        [sql] = ddl(
            "CREATE TABLE t (a INT PRIMARY KEY, b INT REFERENCES r (x))",
            "bigquery",
            strip_primary_keys=True,
            strip_foreign_keys=True,
        )
        self.assertEqual(sql, "CREATE TABLE t (a INT64, b INT64)")

    def test_table_case_and_dotted_schema(self):
        self.assertEqual(
            ddl("CREATE TABLE LineItem (a INT)", "bigquery", schema="proj.ds"),
            ["CREATE TABLE proj.ds.LineItem (a INT64)"],
        )
        self.assertEqual(
            ddl("CREATE TABLE LineItem (a INT)", "postgres", table_case="lower", table_suffix="_S"),
            ["CREATE TABLE lineitem_s (a INT)"],
        )
        with self.assertRaisesRegex(ValueError, "table_case"):
            DdlOptions(table_case="title")


@requires_engine("questdb")
class TestQuestDBOnQuestDB(unittest.TestCase):
    TABLE = "qdp_lineitem"

    def setUp(self):
        self.connection = engines.connect("questdb")
        self.addCleanup(self.connection.close)
        self.connection.execute(f"DROP TABLE IF EXISTS {self.TABLE}")
        self.addCleanup(self.connection.execute, f"DROP TABLE IF EXISTS {self.TABLE}")

    def create(self, **options):
        [sql] = transpile_ddl(
            LINEITEM.replace("lineitem", self.TABLE),
            read="postgres",
            write="questdb",
            options=DdlOptions(strip_primary_keys=True, **options),
        )
        self.connection.execute(sql)

    def columns(self):
        rows = self.connection.execute(
            f"SELECT \"column\", type, \"designated\" FROM table_columns('{self.TABLE}')"
        )
        return {name: (kind, designated) for name, kind, designated in rows}

    def test_layout_creates_symbols_timestamps_and_a_partitioned_table(self):
        self.create(default_layout=QUESTDB_LAYOUT)
        self.assertEqual(
            self.columns(),
            {
                "l_orderkey": ("INT", False),
                "l_returnflag": ("SYMBOL", False),
                "l_comment": ("VARCHAR", False),
                "l_shipdate": ("TIMESTAMP", True),
                "l_commitdate": ("TIMESTAMP", False),
                "l_price": ("DECIMAL(15,2)", False),
                "stamp": ("TIMESTAMP", False),
            },
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT designatedTimestamp, partitionBy FROM tables() "
                f"WHERE table_name = '{self.TABLE}'"
            ),
            [("l_shipdate", "MONTH")],
        )

    def test_unsized_types_create_without_a_layout(self):
        self.create()
        self.assertEqual(self.columns()["l_returnflag"], ("VARCHAR", False))
        self.assertEqual(self.columns()["l_shipdate"], ("DATE", False))

    def test_fixed_size_array_is_a_plain_array(self):
        self.connection.execute(f"DROP TABLE IF EXISTS {self.TABLE}")
        [sql] = transpile_ddl(
            f"CREATE TABLE {self.TABLE} (id INT, v DOUBLE[128])", read="postgres", write="questdb"
        )
        self.connection.execute(sql)
        self.assertEqual(self.columns()["v"], ("DOUBLE[]", False))


if __name__ == "__main__":
    unittest.main()
