"""DDL parity with the engines' own grammars: QuestDB layouts, BigQuery constraints, array types.

The QuestDB class runs in the ``questdb`` job (and locally with ``BENCHBOX_ENGINE_QUESTDB``):
it creates the tables on a real QuestDB and reads the column types, designated timestamp and
partitioning back. BigQuery has no engine in CI (``downstream/ENGINE-GAPS.md``), so its
constraint and naming output is asserted as text against the grammar in the BigQuery
``CREATE TABLE`` reference.
"""

import os
import tempfile
import unittest
import uuid

import sqlglot
from sqlglot import exp
from sqlglot.benchbox.ddl import DdlOptions, transpile_ddl
from sqlglot.benchbox.ddl_engines import SINGLESTORE_TYPE_MAP, STARROCKS_TYPE_MAP
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
                    default_layout=TableLayout(
                        designated_timestamp="l_shipdate", time_partition=unit
                    ),
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
                self.assertEqual(
                    ddl(self.SQL, write),
                    [
                        f"CREATE TABLE v (id {'INT64' if write == 'bigquery' else 'BIGINT'}, e {expected})"
                    ],
                )

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
            self.assertEqual(
                [str(getattr(o, "name", o)) for o in key.args["options"]], ["NOT ENFORCED"]
            )

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
            f'SELECT "column", type, "designated" FROM table_columns(\'{self.TABLE}\')'
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


class TestRedshiftSortKey(unittest.TestCase):
    LAYOUT = TableLayout(distribution="auto", sort_auto=True)

    def test_the_statements_own_sortkey_is_the_only_one(self):
        for layout in (self.LAYOUT, TableLayout(distribution="auto", sort_by=["b"])):
            with self.subTest(layout=layout):
                self.assertEqual(
                    ddl(
                        "CREATE TABLE t (a INT, b INT) SORTKEY (a)",
                        "redshift",
                        default_layout=layout,
                    ),
                    ["CREATE TABLE t (a INTEGER, b INTEGER) SORTKEY(a) DISTSTYLE AUTO"],
                )

    def test_sort_auto_is_written_without_a_sortkey(self):
        self.assertEqual(
            ddl("CREATE TABLE t (a INT) DISTSTYLE EVEN", "redshift", default_layout=self.LAYOUT),
            ["CREATE TABLE t (a INTEGER) DISTSTYLE EVEN SORTKEY AUTO"],
        )


class TestSparkFileFormat(unittest.TestCase):
    def test_the_layouts_format_replaces_the_statements_using(self):
        for fmt in ("delta", "orc"):
            with self.subTest(fmt=fmt):
                self.assertEqual(
                    ddl(
                        "CREATE TABLE t (a INT) USING PARQUET",
                        "spark",
                        read="spark",
                        default_layout=TableLayout(format=fmt),
                    ),
                    [f"CREATE TABLE t (a INT) USING {fmt.upper()}"],
                )

    def test_the_layouts_format_replaces_the_statements_stored_as(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE t (a INT) STORED AS ORC",
                "hive",
                read="hive",
                default_layout=TableLayout(stored_as="parquet", properties={"k": "v"}),
            ),
            ["CREATE TABLE t (a INT) STORED AS PARQUET TBLPROPERTIES ('k'='v')"],
        )

    def test_the_statements_format_stays_without_a_layout_format(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE t (a INT) USING PARQUET",
                "spark",
                read="spark",
                default_layout=TableLayout(properties={"k": "v"}),
            ),
            ["CREATE TABLE t (a INT) USING PARQUET TBLPROPERTIES ('k'='v')"],
        )

    def test_bucketed_tables_keep_their_sort_columns(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE t (a INT, b INT)",
                "hive",
                default_layout=TableLayout(
                    stored_as="parquet", distribute_by=["a"], sort_by=["b"], buckets=4
                ),
            ),
            [
                "CREATE TABLE t (a INT, b INT) CLUSTERED BY (a) SORTED BY (b) INTO 4 BUCKETS "
                "STORED AS PARQUET"
            ],
        )


@requires_engine("pyspark")
class TestSparkOnPySpark(unittest.TestCase):
    def setUp(self):
        self.conn = engines.connect("pyspark")
        self.directory = tempfile.mkdtemp()
        self.name = "t" + uuid.uuid4().hex[:8]
        self.addCleanup(self.conn.execute, f"DROP TABLE IF EXISTS {self.name}")

    def create(self, sql, **options):
        [statement] = transpile_ddl(
            sql.replace("fact", self.name),
            read="postgres",
            write="spark",
            options=DdlOptions(**options),
        )
        self.conn.execute(statement)
        return {row[0]: row[1] for row in self.conn.execute(f"DESCRIBE TABLE EXTENDED {self.name}")}

    def test_layout_format_wins_over_using_and_arrays_are_plain(self):
        info = self.create(
            "CREATE TABLE fact (a INT, v DOUBLE[256]) USING PARQUET",
            default_layout=TableLayout(format="orc", location=os.path.join(self.directory, "orc")),
        )
        self.assertEqual(info["Provider"], "ORC")
        self.assertEqual(info["v"], "array<double>")

    def test_bucketed_table_keeps_its_sort_columns(self):
        info = self.create(
            "CREATE TABLE fact (a INT, b INT)",
            default_layout=TableLayout(
                format="parquet",
                distribute_by=["a"],
                sort_by=["b"],
                buckets=4,
                location=os.path.join(self.directory, "bucketed"),
            ),
        )
        self.assertEqual(info["Sort Columns"], "[`b`]")
        self.assertEqual(info["Num Buckets"], "4")

    def test_sorted_by_without_clustered_by_is_not_spark_sql(self):
        with self.assertRaises(Exception):
            self.conn.execute(
                f"CREATE TABLE {self.name} (k BIGINT, d DATE) USING PARQUET "
                "PARTITIONED BY (d) SORTED BY (k)"
            )


class TestStripWithProperties(unittest.TestCase):
    PARQUET = "CREATE TABLE t (a INT) WITH (format = 'PARQUET')"

    def test_postgres_keeps_the_with_clause_unless_asked(self):
        self.assertEqual(
            ddl(self.PARQUET, "postgres"), ["CREATE TABLE t (a INT) WITH (FORMAT='PARQUET')"]
        )
        self.assertEqual(
            ddl(self.PARQUET, "postgres", strip_with_properties=True), ["CREATE TABLE t (a INT)"]
        )

    def test_temporary_marker_and_layout_properties_stay(self):
        self.assertEqual(
            ddl(
                "CREATE TEMPORARY TABLE t (a INT) WITH (format = 'PARQUET')",
                "postgres",
                strip_with_properties=True,
            ),
            ["CREATE TEMPORARY TABLE t (a INT)"],
        )
        self.assertEqual(
            ddl(
                self.PARQUET,
                "trino",
                strip_with_properties=True,
                default_layout=TableLayout(format="orc"),
            ),
            ["CREATE TABLE t (a INTEGER) WITH (format='ORC')"],
        )


@requires_engine("postgres")
class TestStripWithPropertiesOnPostgres(unittest.TestCase):
    def test_postgres_rejects_the_format_parameter_the_option_removes(self):
        name = "swp_" + uuid.uuid4().hex[:8]
        sql = f"CREATE TABLE {name} (a INT) WITH (format = 'PARQUET')"
        with engines.connect("postgres") as conn:
            [kept] = transpile_ddl(sql, read="postgres", write="postgres")
            with self.assertRaises(Exception) as raised:
                conn.execute(kept)
            self.assertIn("format", str(raised.exception))
            [stripped] = transpile_ddl(
                sql,
                read="postgres",
                write="postgres",
                options=DdlOptions(strip_with_properties=True),
            )
            conn.execute(stripped)
            self.addCleanup(engines.execute, "postgres", f"DROP TABLE IF EXISTS {name}")


class TestSingleStoreTypes(unittest.TestCase):
    SQL = "CREATE TABLE t (a TIMESTAMP, b TIMESTAMP(6), c DATETIME)"

    def test_timestamps_follow_the_documented_recommendation(self):
        self.assertEqual(
            ddl(self.SQL, "singlestore"),
            ["CREATE TABLE t (a TIMESTAMP, b TIMESTAMP(6), c DATETIME)"],
        )
        self.assertEqual(
            ddl(self.SQL, "singlestore", type_map=SINGLESTORE_TYPE_MAP),
            ["CREATE TABLE t (a DATETIME, b DATETIME(6), c DATETIME)"],
        )


class TestOlapReadDialect(unittest.TestCase):
    SQL = "CREATE TABLE t (a INT, c CHAR(10), v VARCHAR(44), PRIMARY KEY (a))"
    LAYOUT = TableLayout(key_model="primary", distribution="hash", distribute_by=["a"], buckets=8)

    def starrocks(self, read):
        return ddl(
            self.SQL,
            "starrocks",
            read=read,
            type_map=STARROCKS_TYPE_MAP,
            default_layout=self.LAYOUT,
        )

    def test_standard_ddl_keeps_lengths_and_the_primary_key_model(self):
        self.assertEqual(
            self.starrocks("postgres"),
            [
                "CREATE TABLE t (a INT, c CHAR(10), v VARCHAR(44)) PRIMARY KEY (a) "
                "DISTRIBUTED BY HASH (a) BUCKETS 8"
            ],
        )

    def test_reading_duckdb_text_drops_lengths_as_duckdb_does(self):
        self.assertEqual(
            self.starrocks("duckdb"),
            [
                "CREATE TABLE t (a INT, c VARCHAR(65533), v VARCHAR(65533)) PRIMARY KEY (a) "
                "DISTRIBUTED BY HASH (a) BUCKETS 8"
            ],
        )


if __name__ == "__main__":
    unittest.main()
