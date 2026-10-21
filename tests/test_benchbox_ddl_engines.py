import time
import unittest
import uuid

from sqlglot import parse_one
from sqlglot.benchbox.ddl import DdlOptions, transpile_ddl
from sqlglot.benchbox.ddl_engines import DORIS_TYPE_MAP, STARROCKS_TYPE_MAP
from sqlglot.benchbox.layout import TableLayout
from tests import engines
from tests.engines import requires_engine

FACT = (
    "CREATE TABLE fact (\n"
    "  f_key BIGINT NOT NULL,\n"
    "  f_ref INTEGER NOT NULL,\n"
    "  f_name VARCHAR(25),\n"
    "  f_note TEXT,\n"
    "  f_day DATE,\n"
    "  f_amount DECIMAL(15,2),\n"
    "  f_small SMALLINT,\n"
    "  PRIMARY KEY (f_key),\n"
    "  FOREIGN KEY (f_ref) REFERENCES dim (d_key)\n"
    ")"
)
PLAIN = "CREATE TABLE plain (a INTEGER, b VARCHAR(10), t TIME, v REAL[3])"


def ddl(sql, write, read="postgres", **options):
    return transpile_ddl(sql, read=read, write=write, options=DdlOptions(**options))[0]


class TestTableLayout(unittest.TestCase):
    def test_layout_is_frozen_normalized_and_copied(self):
        source = {"a": "1"}
        layout = TableLayout(
            sort_by="k",
            distribution="HASH",
            key_model="Duplicate",
            properties=source,
            nullable_columns=["A", "b"],
        )
        source["z"] = "2"
        self.assertEqual(layout.sort_by, ("k",))
        self.assertEqual((layout.distribution, layout.key_model), ("hash", "duplicate"))
        self.assertEqual(dict(layout.properties), {"a": "1"})
        self.assertEqual(layout.nullable_columns, frozenset({"a", "b"}))
        with self.assertRaises(TypeError):
            layout.properties["x"] = "y"  # type: ignore[index]
        with self.assertRaises(Exception):
            layout.engine = "x"  # type: ignore[misc]

    def test_options_match_layouts_by_lowercase_table_name(self):
        options = DdlOptions(layouts={"Fact": TableLayout(engine="MergeTree()")})
        self.assertEqual(set(options.layouts), {"fact"})
        self.assertIsNone(options.default_layout)

    def test_layout_needs_a_supported_dialect(self):
        with self.assertRaises(ValueError):
            ddl(PLAIN, "sqlite", default_layout=TableLayout(engine="x"))

    def test_layout_is_applied_to_permanent_tables_only(self):
        sql = "CREATE TEMPORARY TABLE tmp (a INT)"
        self.assertEqual(
            ddl(sql, "clickhouse", default_layout=TableLayout(engine="MergeTree()")),
            ddl(sql, "clickhouse"),
        )

    def test_layouts_win_over_the_default(self):
        options = DdlOptions(
            layouts={"fact": TableLayout(engine="Log()", sort_by=["f_day"])},
            default_layout=TableLayout(engine="MergeTree()"),
        )
        self.assertIn(
            "ENGINE=Log()",
            transpile_ddl(FACT, read="postgres", write="clickhouse", options=options)[0],
        )
        self.assertIn(
            "ENGINE=MergeTree()",
            transpile_ddl(PLAIN, read="postgres", write="clickhouse", options=options)[0],
        )

    def test_general_options(self):
        sql = ddl(
            "CREATE TABLE fact (a INT NOT NULL)",
            "duckdb",
            strip_not_null=True,
            if_not_exists=True,
            schema="main",
            table_suffix="_staging",
        )
        self.assertEqual(sql, "CREATE TABLE IF NOT EXISTS main.fact_staging (a INT)")
        qualified = ddl("CREATE TABLE s.fact (a INT)", "duckdb", schema="main")
        self.assertEqual(qualified, "CREATE TABLE s.fact (a INT)")


class TestClickHouseLayout(unittest.TestCase):
    STRIP = dict(strip_primary_keys=True, strip_foreign_keys=True)

    def test_engine_and_order_by_fall_back_to_the_primary_key(self):
        self.assertEqual(
            ddl(
                FACT,
                "clickhouse",
                strip_foreign_keys=True,
                default_layout=TableLayout(engine="MergeTree()"),
            ),
            "CREATE TABLE fact (f_key Int64 NOT NULL, f_ref Int32 NOT NULL, f_name Nullable(String), "
            "f_note Nullable(String), f_day Nullable(DATE), f_amount Nullable(Decimal(15, 2)), "
            "f_small Nullable(Int16), PRIMARY KEY (f_key)) ENGINE=MergeTree() ORDER BY (f_key)",
        )

    def test_order_by_tuple_without_a_primary_key(self):
        self.assertIn(
            ") ENGINE=MergeTree() ORDER BY tuple()",
            ddl(PLAIN, "clickhouse", default_layout=TableLayout(engine="MergeTree()")),
        )

    def test_tuned_keys_replace_the_primary_key(self):
        layout = TableLayout(
            engine="MergeTree()",
            partition_by=["toYYYYMM(f_day)"],
            sort_by=["f_ref", "f_day"],
            primary_key=["f_ref"],
            properties={"index_granularity": "4096", "storage_policy": "default"},
        )
        sql = ddl(FACT, "clickhouse", strip_foreign_keys=True, default_layout=layout)
        self.assertEqual(
            sql[sql.index(") ENGINE") :],
            ") ENGINE=MergeTree() PARTITION BY (toYYYYMM(f_day)) ORDER BY (f_ref, f_day) PRIMARY KEY (f_ref) "
            "SETTINGS index_granularity = 4096, storage_policy = 'default'",
        )
        self.assertNotIn("PRIMARY KEY (f_key)", sql)

    def test_statement_clauses_are_kept(self):
        sql = ddl(
            "CREATE TABLE t (a INT) ENGINE=Memory ORDER BY (a)",
            "clickhouse",
            read="clickhouse",
            default_layout=TableLayout(engine="MergeTree()", sort_by=["b"]),
        )
        self.assertEqual(sql, "CREATE TABLE t (a Int32) ENGINE=Memory ORDER BY (a)")

    def test_nullable_columns_decide_the_types(self):
        layout = TableLayout(engine="MergeTree()", nullable_columns=["f_name", "f_amount", "f_key"])
        sql = ddl(FACT, "clickhouse", default_layout=layout, **self.STRIP)
        self.assertIn("f_key Int64 NOT NULL", sql)
        self.assertIn("f_name Nullable(String)", sql)
        self.assertIn("f_amount Nullable(Decimal(15, 2))", sql)
        self.assertIn("f_note String", sql)
        self.assertIn("f_day DATE", sql)

    def test_nullable_key_columns_and_not_null_columns_are_errors(self):
        with self.assertRaises(ValueError):
            ddl(
                FACT,
                "clickhouse",
                default_layout=TableLayout(sort_by=["f_name"], nullable_columns=["f_name"]),
            )
        with self.assertRaises(ValueError):
            ddl(FACT, "clickhouse", default_layout=TableLayout(nullable_columns=["f_ref"]))

    def test_fixed_size_arrays_become_plain_arrays_and_mapped_types_follow(self):
        sql = ddl(PLAIN, "clickhouse", type_map={"TIME": "String"}, default_layout=TableLayout())
        self.assertIn("t Nullable(String)", sql)
        self.assertIn("v Array(Float32)", sql)
        self.assertEqual(parse_one(sql, read="clickhouse").sql("clickhouse"), sql)


class TestOlapLayouts(unittest.TestCase):
    STRIP = dict(strip_primary_keys=True, strip_foreign_keys=True)

    def test_doris_duplicate_key_prefix_distribution_and_properties(self):
        layout = TableLayout(
            key_model="duplicate",
            key_columns=["f_key", "f_ref", "f_day"],
            distribute_by=["f_key"],
            buckets=10,
            properties={"replication_num": "1", "storage_medium": "HDD"},
        )
        self.assertEqual(
            ddl(FACT, "doris", type_map=DORIS_TYPE_MAP, default_layout=layout),
            "CREATE TABLE fact (f_key BIGINT NOT NULL, f_ref INT NOT NULL, f_name VARCHAR(25), "
            "f_note VARCHAR(65533), f_day DATE, f_amount DECIMAL(15, 2), f_small INT) "
            "DUPLICATE KEY (f_key, f_ref) DISTRIBUTED BY HASH (f_key) BUCKETS 10 "
            "PROPERTIES ('replication_num'='1', 'storage_medium'='HDD')",
        )

    def test_doris_key_prefix_ends_at_the_first_unusable_column(self):
        sql = ddl(
            "CREATE TABLE t (a INT, j JSON, c INT)",
            "doris",
            default_layout=TableLayout(key_model="duplicate", key_columns=["a", "j", "c"]),
        )
        self.assertIn("DUPLICATE KEY (a)", sql)
        self.assertIn("DISTRIBUTED BY HASH (a)", sql)

    def test_doris_models_random_distribution_and_partition_clause(self):
        layout = TableLayout(
            key_model="unique",
            key_columns=["nothing_defined"],
            distribution="random",
            buckets=4,
            partition_clause='PARTITION BY RANGE(f_day) (PARTITION p1 VALUES LESS THAN ("1995-01-01"))',
        )
        sql = ddl(FACT, "doris", default_layout=layout)
        self.assertIn(
            "UNIQUE KEY (f_key) PARTITION BY RANGE (f_day) "
            "(PARTITION p1 VALUES LESS THAN ('1995-01-01')) DISTRIBUTED BY RANDOM BUCKETS 4",
            sql,
        )
        sql = ddl(FACT, "doris", default_layout=TableLayout(key_model="aggregate"))
        self.assertTrue(sql.endswith("AGGREGATE KEY(f_key) DISTRIBUTED BY HASH(f_key)"), sql)
        with self.assertRaises(ValueError):
            ddl(FACT, "doris", default_layout=TableLayout(key_model="bogus"))
        with self.assertRaises(ValueError):
            ddl(
                FACT,
                "doris",
                default_layout=TableLayout(key_model="duplicate", partition_by=["f_day"]),
            )

    def test_no_distribution_without_a_key_model_or_distribution(self):
        sql = ddl(
            FACT,
            "doris",
            default_layout=TableLayout(properties={"replication_num": "1"}),
            **self.STRIP,
        )
        self.assertEqual(sql[sql.index(") PROP") :], ") PROPERTIES ('replication_num'='1')")

    def test_starrocks_primary_model_from_the_statements_primary_key(self):
        layout = TableLayout(
            key_model="primary", buckets=8, partition_by=["f_day"], sort_by=["f_ref"]
        )
        sql = ddl(FACT, "starrocks", type_map=STARROCKS_TYPE_MAP, default_layout=layout)
        self.assertEqual(
            sql[sql.index(") PRIMARY") :],
            ") PRIMARY KEY (f_key) PARTITION BY (f_day) DISTRIBUTED BY HASH (f_key) BUCKETS 8 ORDER BY (f_ref)",
        )
        self.assertNotIn("FOREIGN", sql)

    def test_starrocks_primary_model_needs_the_key_to_lead_the_columns(self):
        sql = ddl(
            "CREATE TABLE t (a INT, b INT, PRIMARY KEY (b))",
            "starrocks",
            default_layout=TableLayout(key_model="primary"),
        )
        self.assertIn(") DUPLICATE KEY (a) DISTRIBUTED BY HASH (a)", sql)
        sql = ddl(
            "CREATE TABLE t (a INT, b INT, PRIMARY KEY (a, b))",
            "starrocks",
            default_layout=TableLayout(key_model="primary", distribute_by=["b"], buckets=3),
        )
        self.assertIn(") PRIMARY KEY (a, b) DISTRIBUTED BY HASH (b) BUCKETS 3", sql)

    def test_starrocks_default_is_a_duplicate_key_on_the_first_column(self):
        sql = ddl(
            PLAIN,
            "starrocks",
            type_map=STARROCKS_TYPE_MAP,
            default_layout=TableLayout(key_model="duplicate"),
        )
        self.assertIn(
            "t VARCHAR(10), v ARRAY<FLOAT>) DUPLICATE KEY (a) DISTRIBUTED BY HASH (a)", sql
        )

    def test_distribution_columns_the_table_does_not_define_fall_back_to_the_key(self):
        sql = ddl(
            PLAIN,
            "starrocks",
            default_layout=TableLayout(key_model="duplicate", distribute_by=["missing"], buckets=2),
        )
        self.assertIn("DUPLICATE KEY (a) DISTRIBUTED BY HASH (a) BUCKETS 2", sql)


class TestRedshiftLayout(unittest.TestCase):
    def test_distkey_and_sortkey(self):
        layout = TableLayout(distribute_by=["f_key"], sort_by=["f_day", "f_key"])
        sql = ddl(
            FACT,
            "redshift",
            strip_primary_keys=True,
            strip_foreign_keys=True,
            default_layout=layout,
        )
        self.assertTrue(sql.endswith(") DISTSTYLE KEY DISTKEY(f_key) SORTKEY(f_day, f_key)"), sql)

    def test_auto_even_all(self):
        self.assertTrue(
            ddl(
                PLAIN, "redshift", default_layout=TableLayout(distribution="auto", sort_auto=True)
            ).endswith(") DISTSTYLE AUTO SORTKEY AUTO")
        )
        self.assertTrue(
            ddl(
                PLAIN, "redshift", default_layout=TableLayout(distribution="even", sort_by=["a"])
            ).endswith(") DISTSTYLE EVEN SORTKEY(a)")
        )
        self.assertTrue(
            ddl(PLAIN, "redshift", default_layout=TableLayout(distribution="all")).endswith(
                "DISTSTYLE ALL"
            )
        )

    def test_statement_clauses_are_kept_and_bad_styles_fail(self):
        sql = ddl(
            "CREATE TABLE t (a INT) DISTSTYLE ALL",
            "redshift",
            read="redshift",
            default_layout=TableLayout(distribution="auto", sort_auto=True),
        )
        self.assertEqual(sql, "CREATE TABLE t (a INTEGER) DISTSTYLE ALL SORTKEY AUTO")
        with self.assertRaises(ValueError):
            ddl(PLAIN, "redshift", default_layout=TableLayout(distribution="hash"))
        with self.assertRaises(ValueError):
            ddl(PLAIN, "redshift", default_layout=TableLayout(distribution="key"))

    def test_spectrum_external_table(self):
        layout = TableLayout(
            external=True,
            stored_as="parquet",
            location="s3://bucket/it's/",
            properties={"table_type": "DELTA"},
        )
        sql = ddl(PLAIN, "redshift", schema="spectrum", default_layout=layout)
        self.assertTrue(sql.startswith("CREATE EXTERNAL TABLE spectrum.plain ("), sql)
        self.assertTrue(
            sql.endswith(
                ") STORED AS PARQUET LOCATION 's3://bucket/it''s/' TABLE PROPERTIES ('table_type'='DELTA')"
            ),
            sql,
        )

    def test_pretty_puts_appended_clauses_on_their_own_line(self):
        sql = transpile_ddl(
            PLAIN,
            read="postgres",
            write="redshift",
            options=DdlOptions(default_layout=TableLayout(distribution="auto", sort_auto=True)),
            pretty=True,
        )[0]
        self.assertTrue(sql.rstrip().endswith("SORTKEY AUTO"), sql)


def _chdb_session():
    from chdb import session

    return session.Session()


def _unwrap(expression):
    """chDB 4 reports ``(a, b)`` where chDB 3 reports ``a, b``; drop one wrapping pair."""
    if expression.startswith("(") and expression.endswith(")"):
        depth = 0
        for position, char in enumerate(expression):
            depth += (char == "(") - (char == ")")
            if depth == 0:
                return expression[1:-1] if position == len(expression) - 1 else expression
    return expression


@requires_engine("chdb")
class TestClickHouseOnChdb(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        # chDB allows one connection per process; later tests use chdb.query.
        session = cls.__dict__.get("session")
        if session is not None:
            session.cleanup()

    def setUp(self):
        cls = type(self)
        if "session" not in cls.__dict__:
            cls.session = _chdb_session()
            cls.session.query("CREATE DATABASE IF NOT EXISTS ddl_layout")

    def create(self, sql, **options):
        name = "t" + uuid.uuid4().hex[:8]
        statement = transpile_ddl(
            sql.replace("fact", name).replace("plain", name),
            read="postgres",
            write="clickhouse",
            options=DdlOptions(schema="ddl_layout", strip_foreign_keys=True, **options),
        )[0]
        self.session.query(statement)
        return name

    def table(self, name):
        text = str(
            self.session.query(
                "SELECT engine, sorting_key, partition_key, primary_key FROM system.tables "
                f"WHERE database = 'ddl_layout' AND name = '{name}'",
                "TabSeparated",
            )
        )
        return tuple(_unwrap(field) for field in text.rstrip("\n").split("\t"))

    def columns(self, name):
        text = str(
            self.session.query(
                "SELECT name, type FROM system.columns "
                f"WHERE database = 'ddl_layout' AND table = '{name}' ORDER BY position",
                "TabSeparated",
            )
        )
        return [tuple(line.split("\t")) for line in text.rstrip("\n").split("\n")]

    def test_default_layout_orders_by_the_primary_key(self):
        name = self.create(FACT, default_layout=TableLayout(engine="MergeTree()"))
        self.assertEqual(self.table(name), ("MergeTree", "f_key", "", "f_key"))
        self.assertEqual(
            [dict(self.columns(name))[c] for c in ("f_key", "f_ref", "f_name", "f_amount")],
            ["Int64", "Int32", "Nullable(String)", "Nullable(Decimal(15, 2))"],
        )

    def test_tuned_layout_and_nullable_columns(self):
        layout = TableLayout(
            engine="MergeTree()",
            partition_by=["toYYYYMM(f_day)"],
            sort_by=["f_ref", "f_day"],
            primary_key=["f_ref"],
            properties={"index_granularity": "4096"},
            nullable_columns=["f_name", "f_note", "f_amount"],
        )
        name = self.create(FACT, default_layout=layout)
        self.assertEqual(
            self.table(name), ("MergeTree", "f_ref, f_day", "toYYYYMM(f_day)", "f_ref")
        )
        types = dict(self.columns(name))
        self.assertEqual(
            [types[c] for c in ("f_key", "f_name", "f_day", "f_small")],
            ["Int64", "Nullable(String)", "Date", "Int16"],
        )

    def test_no_primary_key_orders_by_tuple_and_arrays_and_types_work(self):
        name = self.create(
            PLAIN, type_map={"TIME": "String"}, default_layout=TableLayout(engine="MergeTree()")
        )
        self.assertEqual(self.table(name)[:2], ("MergeTree", ""))
        self.assertEqual(dict(self.columns(name))["v"], "Array(Float32)")
        self.assertEqual(dict(self.columns(name))["t"], "Nullable(String)")


class OlapOnService:
    ENGINE = ""
    DIALECT = ""
    TYPES: dict = {}
    DATABASE = "ddl_layout"

    def setUp(self):
        self.conn = engines.connect(self.ENGINE)
        self.addCleanup(self.conn.close)
        self.conn.execute(f"CREATE DATABASE IF NOT EXISTS {self.DATABASE}")
        self.wait_for_backend()

    def wait_for_backend(self, timeout=300.0):
        """The frontend answers before its backend has reported a disk; tables need one."""
        if self.ENGINE in OlapOnService.ready:
            return
        deadline = time.monotonic() + timeout
        probe = f"{self.DATABASE}.backend_probe"
        while True:
            try:
                self.conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {probe} (a INT) DISTRIBUTED BY HASH(a) BUCKETS 1 "
                    "PROPERTIES ('replication_num' = '1')"
                )
                self.conn.execute(f"DROP TABLE {probe}")
                OlapOnService.ready.add(self.ENGINE)
                return
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(5)

    ready: set = set()

    def create(self, sql, **options):
        name = "t" + uuid.uuid4().hex[:8]
        statement = transpile_ddl(
            sql.replace("fact", name).replace("plain", name),
            read="postgres",
            write=self.DIALECT,
            options=DdlOptions(schema=self.DATABASE, type_map=self.TYPES, **options),
        )[0]
        self.conn.execute(statement)
        return name

    def show_create(self, name):
        (row,) = self.conn.execute(f"SHOW CREATE TABLE {self.DATABASE}.{name}")
        return "".join(row[1].split()).replace("`", "").lower()

    def column_types(self, name):
        return {
            row[0].lower(): row[1].lower()
            for row in self.conn.execute(f"DESC {self.DATABASE}.{name}")
        }

    PROPS = {"replication_num": "1"}

    def test_duplicate_key_prefix_and_hash_distribution(self):
        layout = TableLayout(
            key_model="duplicate",
            key_columns=["f_key", "f_ref", "f_day"],
            distribute_by=["f_key"],
            buckets=4,
            properties=self.PROPS,
        )
        name = self.create(FACT, default_layout=layout)
        text = self.show_create(name)
        self.assertIn("duplicatekey(f_key,f_ref)", text)
        self.assertIn("distributedbyhash(f_key)buckets4", text)
        types = self.column_types(name)
        self.assertTrue(types["f_small"].startswith("int"), types)
        self.assertTrue(types["f_note"].startswith("varchar"), types)

    def test_random_distribution_and_first_column_key(self):
        name = self.create(
            PLAIN,
            default_layout=TableLayout(
                key_model="duplicate", distribution="random", buckets=2, properties=self.PROPS
            ),
        )
        text = self.show_create(name)
        self.assertIn("duplicatekey(a)", text)
        self.assertIn("distributedbyrandom", text)
        self.assertTrue(self.column_types(name)["t"].startswith("varchar"))


@requires_engine("starrocks")
class TestStarRocksLayoutOnStarRocks(OlapOnService, unittest.TestCase):
    ENGINE = "starrocks"
    DIALECT = "starrocks"
    TYPES = STARROCKS_TYPE_MAP

    def test_primary_key_model_with_order_by(self):
        layout = TableLayout(
            key_model="primary", buckets=4, sort_by=["f_ref"], properties=self.PROPS
        )
        name = self.create(FACT, strip_foreign_keys=True, default_layout=layout)
        text = self.show_create(name)
        self.assertIn("primarykey(f_key)", text)
        self.assertIn("distributedbyhash(f_key)buckets4", text)
        self.assertIn("orderby(f_ref)", text)

    def test_partition_by_columns(self):
        layout = TableLayout(
            key_model="duplicate", partition_by=["f_day"], buckets=2, properties=self.PROPS
        )
        name = self.create(FACT, default_layout=layout)
        self.assertIn("partitionby", self.show_create(name))

    def test_standard_ddl_keeps_lengths_and_the_primary_key_model(self):
        layout = TableLayout(
            key_model="primary", distribute_by=["f_key"], buckets=4, properties=self.PROPS
        )
        name = self.create(
            "CREATE TABLE fact (f_key INTEGER NOT NULL, f_code CHAR(10) NOT NULL, "
            "f_note VARCHAR(44), PRIMARY KEY (f_key))",
            default_layout=layout,
        )
        self.assertIn("primarykey(f_key)", self.show_create(name))
        types = self.column_types(name)
        self.assertEqual(types["f_note"], "varchar(44)")
        self.assertEqual(types["f_code"], "char(10)")


@requires_engine("doris")
class TestDorisLayoutOnDoris(OlapOnService, unittest.TestCase):
    ENGINE = "doris"
    DIALECT = "doris"
    TYPES = DORIS_TYPE_MAP

    def test_unique_key_and_range_partitions(self):
        layout = TableLayout(
            key_model="unique",
            key_columns=["f_key", "f_day"],
            distribute_by=["f_key"],
            buckets=2,
            partition_clause=(
                'PARTITION BY RANGE(f_day) (PARTITION p1 VALUES LESS THAN ("1995-01-01"), '
                'PARTITION p2 VALUES LESS THAN ("1996-01-01"))'
            ),
            properties={**self.PROPS, "enable_unique_key_merge_on_write": "true"},
        )
        name = self.create(
            "CREATE TABLE fact (f_key BIGINT NOT NULL, f_day DATE NOT NULL, f_val INTEGER)",
            default_layout=layout,
        )
        text = self.show_create(name)
        self.assertIn("uniquekey(f_key,f_day)", text)
        self.assertIn("partitionbyrange(f_day)", text)
        self.assertIn("distributedbyhash(f_key)buckets2", text)


@requires_engine("duckdb")
class TestGeneralOptionsOnDuckDB(unittest.TestCase):
    def test_if_not_exists_schema_suffix_and_not_null_run(self):
        options = DdlOptions(
            strip_not_null=True, if_not_exists=True, schema="s", table_suffix="_stg"
        )
        with engines.connect("duckdb") as conn:
            conn.execute("CREATE SCHEMA s")
            for _ in range(2):
                (sql,) = transpile_ddl(
                    "CREATE TABLE fact (a INT NOT NULL, b VARCHAR(5))",
                    read="postgres",
                    write="duckdb",
                    options=options,
                )
                conn.execute(sql)
            conn.execute("INSERT INTO s.fact_stg (a) VALUES (NULL)")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM s.fact_stg"), [(1,)])
