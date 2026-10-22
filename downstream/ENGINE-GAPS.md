# Real-engine CI and gap list

Every piece of translation behavior moved into this fork is proven by executing
the translated SQL on a real engine where one can be obtained. This page records
what CI runs, how tests opt in, and which BenchBox platforms have no engine in CI
and what evidence substitutes for them.

## Engines in CI

The workflow `.github/workflows/benchbox-engines.yml` runs `tests/test_engines.py`
(and any future test using `tests/engines.py`) with `contents: read` only and no
secrets. Each job lists the engines it must have in `BENCHBOX_ENGINES_REQUIRED`,
so a missing package or an unreachable container fails the job.

| Engine | Dialect used | How it runs | Job |
|---|---|---|---|
| SQLite | `sqlite` | stdlib `sqlite3` | `in-process` |
| DuckDB | `duckdb` | in-process (`duckdb`) | `in-process` |
| DataFusion | `datafusion` | in-process (`datafusion`) | `in-process` |
| chDB (ClickHouse) | `clickhouse` | in-process (`chdb`) | `in-process` |
| PySpark | `spark` | local mode, Java 17 from the runner image | `in-process` |
| PostgreSQL | `postgres` | service container `postgres:16` | `postgres` |
| Trino | `trino` | service container `trinodb/trino:484`, memory catalog | `trino` |
| QuestDB | `questdb` | service container `questdb/questdb:10.0.1`, PG wire port | `questdb` |
| StarRocks | `starrocks` | `starrocks/allin1-ubuntu:4.0.16` started with `docker run` | `starrocks` |
| Doris | `doris` | `apache/doris:all-in-one-4.1.3` started with `docker run` | `doris` |

`tests/test_platform_dialects_engines.py` runs in the `in-process` and `postgres` jobs
next to the seed tests: it executes DataFusion dialect output and the date
arithmetic rewrite.
`tests/test_benchbox_adapter_rewrites_engines.py` runs in the same two jobs and in addition
uses `chdb` and `pyspark`: it executes the division rewrite's BigQuery, Databricks and Snowflake
shapes on Postgres and DuckDB, the Databricks `TRY_DIVIDE` form and ANSI division failure on
PySpark, the ClickHouse `NULLIF` form on chDB, alias deduplication on Postgres, DuckDB and
PySpark, table qualification on Postgres and DuckDB, and flag folding on Postgres and DuckDB.
BigQuery `SAFE_DIVIDE` and Snowflake's `NULLIF` form are not executed in their own dialect
(see the BigQuery and Snowflake rows above); Databricks `PRIMARY KEY` is checked only for
syntax on PySpark, which parses it and then rejects it as an unsupported constraint, so
Delta constraint enforcement stays a managed-service gap.
`tests/test_sqlite_idioms_execution.py` runs the Netezza-read SQLite date idioms on SQLite, DuckDB,
DataFusion, chDB, PySpark and Postgres in those two jobs, and on StarRocks and Doris in their jobs.
`tests/test_regexp_replace_execution.py` runs a ClickHouse `REGEXP_REPLACE` with a non-capturing group
on chDB, DuckDB, DataFusion and Postgres; the Snowflake form (capturing groups, renumbered references)
has no Snowflake engine, so it is run on DuckDB's RE2, chDB and Python's `re` instead, which agree with
the original, and its output text is asserted (see the Snowflake row above). Spark and Databricks read
`\1` in a replacement as the digit 1 (they expect `$1`); that is not changed by this slice.
`tests/test_benchbox_ddl.py` runs in the same two jobs: it creates the stripped and unstripped schema
from `transpile_ddl` on SQLite, DuckDB and Postgres and compares columns and constraints, replaces a
table on DuckDB, and checks the types a `type_map` produces on DuckDB and Postgres. SingleStore
`SHARD KEY`, `SORT KEY` and `REFERENCE TABLE` output is a golden-output test only (the SingleStore row
below); the key clauses are not part of any other engine's grammar, so they cannot run elsewhere.
`tests/test_benchbox_compat_engines.py` runs in the `in-process` and `postgres` jobs: alias inlining,
column qualification, `FILTER` removal (windowed and not), literal-subquery naming, grouped scalar
subqueries and ordered list aggregates execute on Postgres, DuckDB, DataFusion and PySpark; the `GROUP BY ()`
drop runs on all four (DataFusion and PySpark also assert the engine rejects the original); the ClickHouse `FILTER` combinators run on chDB. The BigQuery `COUNTIF`/`IF` shapes execute as
BigQuery text transpiled to each engine, because BigQuery itself is a gap above; Trino and Presto get
the same generic `IF` form executed on the four engines, not on Trino. The Snowflake `GROUP BY ()` drop and
the BigQuery `FIRST_VALUE`/`LAST_VALUE`/`NTH_VALUE` window NULLS handling (`bigquery-window-nulls-ordering`) are AST/golden tests because Snowflake and BigQuery are gaps; BigQuery rejects the clause itself, so the rule comes from BigQuery's grammar and a live BigQuery run of TPC-Havoc 3_v9. The `IS NULL` sort key and the dropped clause are also executed as text on DuckDB (`TestBigQueryValueWindowNullOrderingOnDuckDB`) to prove they order rows as the original clause does.
`tests/test_benchbox_subqueries_engines.py` runs in the same two jobs: it creates small tables on Postgres, DuckDB,
DataFusion and PySpark and compares the rows of the original query with the rows of its `decorrelate_subqueries`,
`not_in_subquery_to_not_exists` and `in_aggregate_subquery_to_exists` rewrites (scalar subquery in `HAVING`, `NOT IN`,
`IN` over a grouped subquery behind comma joins, nested correlated subqueries, a correlated scalar subquery, and an
inequality-correlated `EXISTS` that must stay in place). Whether a given engine version needs these rewrites is not
something this test can show: current Postgres, DuckDB, DataFusion and Spark answer the originals correctly too.
`tests/test_benchbox_ddl_engines.py` runs in the `in-process`, `starrocks` and `doris` jobs: it creates
ClickHouse tables on chDB and compares engine, sorting, partition and primary keys and column types with
`system.tables` and `system.columns`, and creates Doris and StarRocks tables on their containers and
checks `SHOW CREATE TABLE` for the key model, distribution and partition clauses. Redshift `DISTSTYLE`,
`DISTKEY`, `SORTKEY` and Spectrum `CREATE EXTERNAL TABLE` output is golden-output only (the Redshift row
below).
`tests/test_benchbox_ddl_lakehouse.py` runs in the `in-process` and `trino` jobs: on PySpark it creates
partitioned, bucketed Parquet tables with a location and properties and ORC tables and reads the provider,
buckets, sort columns, partitioning and properties back with `DESCRIBE TABLE EXTENDED`, and on Trino it
creates a constraint-free table in the memory catalog. Everything else in the file is golden output: Delta,
Iceberg and Hudi need table-format jars and catalogs that the local PySpark session does not have
(Databricks row below), `STORED AS` needs a Hive metastore (PySpark's in-memory catalog rejects it), the
memory catalog rejects every `WITH` property (the Trino hive and iceberg connectors are not in the
container), and Spark's `REPLACE TABLE` needs a v2 catalog. Athena external tables, Synapse and Fabric
distribution clauses, Redshift Spectrum tables and the `pg_mooncake` statements have no engine (the rows
below).
`tests/test_questdb_execution.py` runs in the `questdb` job: it executes the QuestDB
generator's comma-join expansion, `DATEADD`, `SUBSTRING` and CTE column list output.
`tests/test_benchbox_ddl_parity.py` runs in the `in-process`, `postgres` and `questdb` jobs. In the
`questdb` job it creates tables from `transpile_ddl` output on QuestDB and reads back the `SYMBOL` and
`TIMESTAMP` column types, the designated timestamp and the partitioning; QuestDB rejects `VARCHAR(n)`,
`CHAR(n)` and fixed-size arrays, so those are written without the length. The BigQuery
`PRIMARY KEY` / `FOREIGN KEY ... NOT ENFORCED` and table-name output is golden output (the BigQuery row
below). The same file executes the Spark forms on PySpark (a layout format replacing the statement's
`USING`, a fixed-size array written as `ARRAY<DOUBLE>`, `CLUSTERED BY ... SORTED BY` bucket sort
columns) and, in the `postgres` job, that PostgreSQL rejects `WITH (format = ...)` unless
`strip_with_properties` removes it. `tests/test_benchbox_ddl_engines.py` also creates a StarRocks table
from standard DDL in the `starrocks` job and reads back its `PRIMARY KEY` model and `varchar(44)` /
`char(10)` lengths. Doris and StarRocks bare `VARCHAR` length, `ARRAY<FLOAT>` vectors and `float` columns are read back there too (`float_as_real`). Redshift `SORTKEY`, BigQuery `BIGNUMERIC`, SingleStore `DATETIME` and `VECTOR(n, F32|F64)` columns, and
T-SQL `VECTOR(n)` (SQL Server 2025 and Azure SQL only, behind `vector_type`) stay golden output (the Redshift,
SingleStore and T-SQL rows below). Synapse and Fabric Warehouse have no vector type, so an array column on
T-SQL without `vector_type` is a `ValueError` rather than invalid DDL.

`tests/test_engine_rewrites_execution.py` runs in the `in-process`, `starrocks` and `doris` jobs. On chDB it
executes the ClickHouse rewrites (lowercased identifiers, decimal zero literals with the zero-divisor
`NULLIF`, `SETTINGS`, the `IN` form of a correlated `EXISTS`, derived table aliases); on DuckDB it compares
the `EXISTS` and `IN` forms row for row (including NULL keys and duplicates) and runs the qualified `ORDER BY`;
on StarRocks and Doris it executes derived table aliasing, every reserved alias word the BenchBox StarRocks
rewrite quoted, ANSI `SUBSTRING ... FROM ... FOR` and double-quoted identifiers as the dialect writes them. All
four engines and DuckDB also run the Netezza `+ N days` durations. ClickHouse server-only behavior is not
covered (see below); the chDB engine runs the same planner and analyzer.

## Writing an engine test

```python
import unittest
import sqlglot
from tests import engines
from tests.engines import requires_engine


@requires_engine("duckdb")
class TestSomething(unittest.TestCase):
    def test_runs(self):
        sql = sqlglot.transpile("SELECT 1::int", read="postgres", write="duckdb")[0]
        self.assertEqual(engines.execute("duckdb", sql), [(1,)])
```

- Engine packages are imported lazily inside `engines.connect`, so collection and
  `make check` work with none installed, on every supported Python (3.9 included).
- `requires_engine` skips with a named reason (for example
  `duckdb: Python package 'duckdb' is not installed (pip install duckdb)`).
- `BENCHBOX_ENGINES_REQUIRED=1` (or `all`, or a comma-separated engine list) turns
  a missing engine into a failure. CI sets it; never set it in default local runs.
- Install the packages with `pip install -r downstream/engines-requirements.txt`.
  The file is separate from `setup.py` (which is upstream-owned) and is not part of
  the default install or of `dev`. `datafusion`, `chdb` and `pyspark` are gated to
  Python 3.10+ by marker.
- Service engines need `BENCHBOX_ENGINE_<NAME>=host:port` (for example
  `BENCHBOX_ENGINE_POSTGRES=127.0.0.1:5432`). To run one locally, start the
  container the workflow uses and export the variable.
- `python -m tests.engines status` prints what is available; `python -m
  tests.engines wait ENGINE [SECONDS]` waits until a service executes `SELECT 1`.

## Gap list

Platforms below have no engine in CI. A piece of translation behavior that targets
one of them ships with AST and generated-SQL tests, and its tracker item records the
gap. "Substitute" is the strongest evidence available instead of execution; it is
weaker than execution and must be named as such in the pull request.

### No obtainable engine (managed or proprietary)

| Platform | Why no engine | Substitute evidence |
|---|---|---|
| BigQuery | Managed service, no official local engine; community emulators do not implement GoogleSQL faithfully, and credentials are secrets CI must not hold | AST and golden-output tests; parse the output back with the `bigquery` dialect; reviewed against the GoogleSQL reference |
| Snowflake | Managed service, no local engine, credentials needed | AST and golden-output tests; parse the output back with the `snowflake` dialect; DuckDB execution only for the ANSI-portable part |
| Databricks (managed features: Unity Catalog, Delta table properties, liquid clustering, Photon, SQL warehouses) | Managed service; open-source Spark in the `pyspark` job has no Delta or Unity Catalog | Spark-SQL-compatible output is executed on PySpark; managed-only DDL and properties get golden-output tests |
| Amazon Athena | Managed service; its Trino engine is not runnable locally with Hive/Iceberg table formats | Query output executed on the `trino` job (Athena v3 is Trino based); Athena-only DDL (external tables, `TBLPROPERTIES`) gets golden-output tests |
| Amazon Redshift | Managed service, no container | Postgres execution for the shared subset (Redshift derives from PostgreSQL 8); Redshift-only features (`DISTKEY`, `SORTKEY`, `ENCODE`) get golden-output tests |
| Azure Synapse | Managed service (the SQL Server container is not Synapse T-SQL and has licensing terms) | Golden-output tests with the `tsql` dialect; no execution substitute |
| Microsoft Fabric (warehouse, lakehouse, Spark) | Managed service | Golden-output tests with the `fabric` dialect; Fabric Spark output executed on PySpark where it is plain Spark SQL |
| pg_mooncake | Postgres extension that the `postgres:16` service image does not ship; its access method needs the extension's own image | Golden-output tests of `USING mooncake` and the rename and `mooncake.create_table` statements; plain `CREATE TABLE` output executes on the `postgres` job (`tests/test_benchbox_ddl.py`) |
| ClickHouse Cloud, MotherDuck, Firebolt, Starburst, OneHouse, Snowpark Connect | Managed services needing credentials | Same substitutes as the open engine they share a dialect with: chdb, DuckDB, Postgres/Trino; otherwise golden-output tests |

### Obtainable engine, impractical on hosted runners

| Engine | Why | Substitute evidence |
|---|---|---|
| SingleStore | The only dev container (`singlestoredb-dev`) requires a license key, which is a secret; the workflow forbids secrets | Golden-output tests with the `singlestore` dialect; the MySQL-protocol client path is covered by the StarRocks and Doris jobs |

### Obtainable engine, not yet added to CI

These can be added by following the pattern in the workflow when a moved piece needs
them: ClickHouse server (chdb covers the engine, not server-only DDL), Databend,
Citus, TimescaleDB, ParadeDB, CedarDB, Presto, DuckLake, Lakesail, Velox and InfluxDB.
Until added, treat them as gaps with the substitute of their closest executed engine.

### Platform dialects without their own engine

The `netezza`, `greenplum` and `vertica` dialects are thin subclasses of `postgres`
with no behavior of their own yet, so their identity and transpilation tests
(`tests/dialects/test_<dialect>.py`) are the evidence for what they add, and the
`postgres` job executes the shared output they inherit. No engine for them is in CI:

| Platform | Why no engine | Substitute evidence |
|---|---|---|
| Netezza | Proprietary appliance; IBM distributes no local engine | Identity and transpile tests; the date arithmetic rewrite executes on the `postgres` job |
| Greenplum | No official single-container image; a cluster is too heavy for a hosted job | Identity and transpile tests; shared syntax executes on the `postgres` job |
| Vertica | Not added to CI in this slice | Identity and transpile tests; shared syntax executes on the `postgres` job |

A dialect that later overrides generator or parser behavior must add an engine or
record why one cannot be obtained.

### Exact integer division from a Netezza source (`netezza-exact-division`, `postgres-exact-division`)

`tests/test_netezza_exact_division_engines.py` runs the TPC-DS Q21, Q78 and Q83 ratio shapes and
integer-literal-only quotients on every engine in CI. Executed where `/` truncates integers: SQLite,
DataFusion, PostgreSQL and Trino (the Athena, Presto, Starburst and Dune dialect family shares the Trino
generator). Executed where `/` is already exact, to prove no cast is wanted: DuckDB, PySpark (Spark,
Databricks, Hive), chDB (ClickHouse), StarRocks and Doris (the MySQL family). Dialects whose `/` truncates
but that have no engine in CI get the `DOUBLE`-family cast (`FLOAT` on T-SQL) asserted as text in
`tests/dialects/test_netezza.py`: T-SQL, Synapse and Fabric (SQL Server's container needs a license
acceptance and is not Synapse T-SQL), Teradata, Drill, Dremio, Redshift (managed; `DECIMAL` there means
`DECIMAL(18,0)`, hence `DOUBLE PRECISION`), Greenplum, Firebolt (maps to `postgres`), Materialize and
RisingWave. No BenchBox platform targets Materialize, RisingWave or Druid; Druid SQL truncates integer
division but its dialect does not declare `TYPED_DIVISION`, so it is not changed. Vertica divides integers
to a `NUMERIC` since version 6 and keeps its `/`, as do Oracle, Exasol, Snowflake (Databend), BigQuery and
ClickHouse.
