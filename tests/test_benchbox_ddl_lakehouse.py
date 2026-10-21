import os
import tempfile
import unittest
import uuid

from sqlglot.benchbox.ddl import DdlOptions, transpile_ddl
from sqlglot.benchbox.ddl_engines import mooncake_mirror_statements
from sqlglot.benchbox.layout import TableLayout
from tests import engines
from tests.engines import requires_engine

ORDERS = (
    "CREATE TABLE orders (\n"
    "  o_key BIGINT NOT NULL,\n"
    "  o_cust INTEGER,\n"
    "  o_day DATE,\n"
    "  o_comment VARCHAR(20),\n"
    "  PRIMARY KEY (o_key)\n"
    ")"
)
STRIP = dict(strip_primary_keys=True)


def ddl(write, read="postgres", sql=ORDERS, **options):
    return transpile_ddl(sql, read=read, write=write, options=DdlOptions(**options))[0]


def tail(sql):
    return sql[sql.index(") ", sql.index("o_comment")) :]


class TestSparkFamily(unittest.TestCase):
    def test_delta_partitioned_and_clustered_with_properties(self):
        layout = TableLayout(
            format="delta",
            partition_by=["o_day"],
            cluster_by=["o_cust"],
            properties={
                "delta.autoOptimize.optimizeWrite": "true",
                "delta.autoOptimize.autoCompact": "true",
            },
        )
        self.assertEqual(
            tail(ddl("databricks", default_layout=layout, **STRIP)),
            ") USING DELTA PARTITIONED BY (o_day) CLUSTER BY (o_cust) TBLPROPERTIES "
            "('delta.autoOptimize.optimizeWrite'='true', 'delta.autoOptimize.autoCompact'='true')",
        )

    def test_automatic_liquid_clustering_and_catalog_managed_tables(self):
        layout = TableLayout(
            format="delta",
            cluster_by_auto=True,
            properties={"delta.feature.catalogManaged": "supported"},
        )
        self.assertEqual(
            tail(ddl("databricks", or_replace=True, default_layout=layout, **STRIP)),
            ") USING DELTA CLUSTER BY AUTO TBLPROPERTIES ('delta.feature.catalogManaged'='supported')",
        )
        self.assertTrue(
            ddl("databricks", or_replace=True, default_layout=layout).startswith(
                "CREATE OR REPLACE TABLE orders"
            )
        )

    def test_iceberg_transforms_and_sort_order(self):
        layout = TableLayout(
            format="iceberg",
            partition_by=["bucket(8, o_cust)", "days(o_day)"],
            properties={"write.sort-order": "o_key ASC"},
        )
        self.assertEqual(
            tail(ddl("spark", default_layout=layout, **STRIP)),
            ") USING ICEBERG PARTITIONED BY (BUCKET(8, o_cust), DAYS(o_day)) "
            "TBLPROPERTIES ('write.sort-order'='o_key ASC')",
        )

    def test_bucketed_parquet_with_location(self):
        layout = TableLayout(
            format="parquet",
            partition_by=["o_day"],
            distribute_by=["o_cust"],
            sort_by=["o_key"],
            buckets=8,
            location="s3://bucket/it's/",
        )
        self.assertEqual(
            tail(ddl("spark", default_layout=layout, **STRIP)),
            ") USING PARQUET PARTITIONED BY (o_day) CLUSTERED BY (o_cust) SORTED BY (o_key) "
            "INTO 8 BUCKETS LOCATION 's3://bucket/it\\'s/'",
        )
        with self.assertRaises(ValueError):
            ddl("spark", default_layout=TableLayout(distribute_by=["o_cust"]))

    def test_orc_and_hive_serde_formats(self):
        self.assertTrue(
            ddl("spark", default_layout=TableLayout(format="orc"), **STRIP).endswith(") USING ORC")
        )
        self.assertTrue(
            ddl("spark", default_layout=TableLayout(stored_as="orc"), **STRIP).endswith(
                ") STORED AS ORC"
            )
        )

    def test_hudi_properties(self):
        layout = TableLayout(
            format="hudi",
            partition_by=["o_day"],
            properties={"type": "cow", "primaryKey": "o_key", "preCombineField": "o_day"},
        )
        self.assertEqual(
            tail(ddl("spark", default_layout=layout, **STRIP)),
            ") USING HUDI PARTITIONED BY (o_day) "
            "TBLPROPERTIES ('type'='cow', 'primaryKey'='o_key', 'preCombineField'='o_day')",
        )

    def test_statement_clauses_are_kept(self):
        sql = ddl(
            "spark",
            read="spark",
            sql="CREATE TABLE t (a INT) USING CSV",
            default_layout=TableLayout(format="parquet", partition_by=["a"]),
        )
        self.assertEqual(sql, "CREATE TABLE t (a INT) USING CSV PARTITIONED BY (a)")


class TestHiveFamily(unittest.TestCase):
    def test_hive_partition_columns_move_out_of_the_column_list_with_their_types(self):
        layout = TableLayout(
            stored_as="parquet",
            partition_by=["o_day"],
            location="s3://bucket/orders/",
            properties={"k": "v"},
        )
        self.assertEqual(
            ddl("hive", default_layout=layout, **STRIP),
            "CREATE TABLE orders (o_key BIGINT NOT NULL, o_cust INT, o_comment VARCHAR(20)) "
            "PARTITIONED BY (o_day DATE) STORED AS PARQUET LOCATION 's3://bucket/orders/' "
            "TBLPROPERTIES ('k'='v')",
        )

    def test_hive_bucketing(self):
        layout = TableLayout(
            stored_as="orc", distribute_by=["o_cust"], sort_by=["o_key"], buckets=4
        )
        self.assertTrue(
            ddl("hive", default_layout=layout, **STRIP).endswith(
                ") CLUSTERED BY (o_cust) SORTED BY (o_key) INTO 4 BUCKETS STORED AS ORC"
            )
        )

    def test_athena_staging_table_over_delimited_text(self):
        layout = TableLayout(
            external=True,
            field_delimiter="|",
            line_delimiter="\n",
            stored_as="textfile",
            location="s3://bucket/prefix/orders_staging/",
            properties={"skip.header.line.count": "0"},
        )
        self.assertEqual(
            ddl(
                "athena",
                strip_primary_keys=True,
                strip_not_null=True,
                if_not_exists=True,
                table_suffix="_staging",
                type_map={"VARCHAR": "STRING"},
                drop_type_parameters=["STRING"],
                default_layout=layout,
            ),
            "CREATE EXTERNAL TABLE IF NOT EXISTS orders_staging (o_key BIGINT, o_cust INT, "
            "o_day DATE, o_comment STRING) ROW FORMAT DELIMITED FIELDS TERMINATED BY '|' "
            "LINES TERMINATED BY '\\n' STORED AS TEXTFILE "
            "LOCATION 's3://bucket/prefix/orders_staging/' "
            "TBLPROPERTIES ('skip.header.line.count'='0')",
        )

    def test_athena_parquet_external_table_with_partitions(self):
        layout = TableLayout(
            external=True, stored_as="parquet", partition_by=["o_day"], location="s3://bucket/o/"
        )
        self.assertEqual(
            ddl("athena", strip_primary_keys=True, strip_not_null=True, default_layout=layout),
            "CREATE EXTERNAL TABLE orders (o_key BIGINT, o_cust INT, o_comment VARCHAR(20)) "
            "PARTITIONED BY (o_day DATE) STORED AS PARQUET LOCATION 's3://bucket/o/'",
        )


class TestTrinoFamily(unittest.TestCase):
    def test_hive_connector_properties(self):
        layout = TableLayout(
            format="parquet",
            partition_by=["o_day"],
            distribute_by=["o_cust"],
            buckets=4,
            sort_by=["o_key"],
        )
        self.assertEqual(
            tail(ddl("trino", default_layout=layout, **STRIP)),
            ") WITH (format='PARQUET', PARTITIONED_BY=ARRAY['o_day'], bucketed_by=ARRAY['o_cust'], "
            "bucket_count=4, sorted_by=ARRAY['o_key'])",
        )

    def test_iceberg_connector_properties(self):
        layout = TableLayout(
            format="iceberg",
            partition_by=["o_day"],
            sort_by=["o_key"],
            location="s3://bucket/o/",
            properties={"format_version": "2", "extra": "x"},
        )
        self.assertEqual(
            tail(ddl("trino", default_layout=layout, **STRIP)),
            ") WITH (format='ICEBERG', partitioning=ARRAY['o_day'], sorted_by=ARRAY['o_key'], "
            "LOCATION='s3://bucket/o/', format_version=2, extra='x')",
        )

    def test_presto_and_no_layout_for_the_memory_catalog(self):
        self.assertTrue(
            ddl("presto", default_layout=TableLayout(format="parquet"), **STRIP).endswith(
                ") WITH (format='PARQUET')"
            )
        )
        self.assertTrue(
            ddl("trino", strip_not_null=True, **STRIP).endswith("o_comment VARCHAR(20))")
        )

    def test_statement_properties_are_kept(self):
        sql = ddl(
            "trino",
            read="trino",
            sql="CREATE TABLE t (a INT) WITH (format = 'ORC')",
            default_layout=TableLayout(format="parquet", partition_by=["a"]),
        )
        self.assertEqual(
            sql, "CREATE TABLE t (a INTEGER) WITH (format='ORC', PARTITIONED_BY=ARRAY['a'])"
        )


class TestWarehouses(unittest.TestCase):
    def test_synapse_distribution(self):
        self.assertEqual(
            ddl(
                "tsql",
                schema="dbo",
                default_layout=TableLayout(distribution="round_robin"),
                **STRIP,
            ),
            "CREATE TABLE dbo.orders (o_key BIGINT NOT NULL, o_cust INTEGER, o_day DATE, "
            "o_comment VARCHAR(20)) WITH (DISTRIBUTION=ROUND_ROBIN)",
        )
        self.assertTrue(
            ddl(
                "tsql", default_layout=TableLayout(distribute_by=["o_cust", "o_day"]), **STRIP
            ).endswith(") WITH (DISTRIBUTION=HASH(o_cust, o_day))")
        )
        self.assertTrue(
            ddl("tsql", default_layout=TableLayout(distribution="replicate"), **STRIP).endswith(
                ") WITH (DISTRIBUTION=REPLICATE)"
            )
        )
        with self.assertRaises(ValueError):
            ddl("tsql", default_layout=TableLayout(distribution="hash"))
        with self.assertRaises(ValueError):
            ddl("tsql", default_layout=TableLayout(distribution="even"))

    def test_fabric_warehouse_schema_prefix_only(self):
        self.assertEqual(
            ddl("fabric", schema="dbo", **STRIP),
            "CREATE TABLE dbo.orders (o_key BIGINT NOT NULL, o_cust INT, o_day DATE, "
            "o_comment VARCHAR(20))",
        )

    def test_postgres_access_method_and_the_mooncake_mirror(self):
        self.assertTrue(
            ddl("postgres", default_layout=TableLayout(format="mooncake")).endswith(
                ") USING mooncake"
            )
        )
        self.assertEqual(
            mooncake_mirror_statements("orders", "orders_heap", "tpc"),
            [
                'ALTER TABLE "tpc"."orders" RENAME TO "orders_heap"',
                "CALL mooncake.create_table('tpc.orders', 'tpc.orders_heap')",
            ],
        )
        self.assertEqual(
            mooncake_mirror_statements("orders", "orders_heap"),
            [
                'ALTER TABLE "orders" RENAME TO "orders_heap"',
                "CALL mooncake.create_table('orders', 'orders_heap')",
            ],
        )


@requires_engine("pyspark")
class TestSparkLayoutOnPySpark(unittest.TestCase):
    def setUp(self):
        self.conn = engines.connect("pyspark")
        self.directory = tempfile.mkdtemp()
        self.names = []

    def tearDown(self):
        for name in self.names:
            self.conn.execute(f"DROP TABLE IF EXISTS {name}")

    def create(self, layout, **options):
        name = "t" + uuid.uuid4().hex[:8]
        layout = layout_with_location(layout, os.path.join(self.directory, name))
        (sql,) = transpile_ddl(
            ORDERS.replace("orders", name),
            read="postgres",
            write="spark",
            options=DdlOptions(strip_primary_keys=True, default_layout=layout, **options),
        )
        self.names.append(name)
        self.conn.execute(sql)
        return {row[0]: row[1] for row in self.conn.execute(f"DESCRIBE TABLE EXTENDED {name}")}

    def test_partitioned_bucketed_parquet_table_with_properties(self):
        layout = TableLayout(
            format="parquet",
            partition_by=["o_day"],
            distribute_by=["o_cust"],
            sort_by=["o_key"],
            buckets=8,
            properties={"k": "v"},
        )
        info = self.create(layout)
        self.assertEqual(info["Provider"], "PARQUET")
        self.assertEqual(info["Num Buckets"], "8")
        self.assertEqual(info["Bucket Columns"], "[`o_cust`]")
        self.assertEqual(info["Sort Columns"], "[`o_key`]")
        self.assertEqual(info["Table Properties"], "[k=v]")
        self.assertIn("# Partition Information", info)
        self.assertEqual(info["o_day"], "date")

    def test_orc_table(self):
        info = self.create(TableLayout(format="orc"))
        self.assertEqual(info["Provider"], "ORC")
        self.assertEqual(info["o_key"], "bigint")


def layout_with_location(layout, location):
    from dataclasses import replace

    return replace(layout, location=location)


@requires_engine("trino")
class TestTrinoMemoryCatalog(unittest.TestCase):
    def test_table_without_constraints_or_properties_is_created(self):
        name = "t" + uuid.uuid4().hex[:8]
        with engines.connect("trino") as conn:
            (sql,) = transpile_ddl(
                ORDERS.replace("orders", name),
                read="postgres",
                write="trino",
                options=DdlOptions(strip_primary_keys=True, strip_not_null=True),
            )
            conn.execute(sql)
            conn.execute(f"INSERT INTO {name} (o_key) VALUES (1)")
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {name}"), [(1,)])
            conn.execute(f"DROP TABLE {name}")
