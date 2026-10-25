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

Every service container and `docker run` image is pinned by digest as well as tag
(`name:tag@sha256:...`), so a moved tag cannot change what the jobs test; a tag bump updates
its digest in the same change. The QuestDB image's default `admin` / `quest` credentials in
`tests/engines.py` belong to an ephemeral container bound to the runner's loopback port; the
workflow holds no secret and no credential reaches an external service.

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
`tests/test_benchbox_list_aggregates_engines.py` runs the TPC-Havoc `1_v7` query and a NULL/uneven-zip query: DuckDB gives the
expected rows, the SQLite form executes on `sqlite3` (JSON arrays), the Spark and Databricks forms execute on PySpark (`in-process` job) and the Presto, Trino and Athena forms on Trino
(`trino` job; Presto and Athena are not engines of their own there). BigQuery's `UNNEST` form is a gap: it executes as its text
transpiled to DuckDB with `SAFE_OFFSET(i)` read as `SAFE_ORDINAL(i + 1)` (the DuckDB generator drops the base shift for a column
offset), and `tests/test_benchbox_list_aggregates.py` pins the BigQuery text.
`tests/test_benchbox_runner_overrides_engines.py` runs TPC-DS Q36, Q70, Q86 and Q90 on PostgreSQL (`postgres` job), Q47, Q57, Q66 and Q90 on
chDB (`in-process` and `postgres` jobs) and the guarded Q90 on DuckDB, which also runs each query unrewritten as the reference. Each test
asserts that the engine rejects the unrewritten text (`lochierarchy` unknown on PostgreSQL, `d_year` unknown on ClickHouse, an aggregate inside an
aggregate on ClickHouse, division by zero on PostgreSQL and ClickHouse; DuckDB returns `inf`), so the rewrites cannot become no-ops unnoticed. The
data is a few inline rows per table. The queries also ran at scale factor 0.01 on dsdgen data (PostgreSQL 16.2, ClickHouse 26.1.9.5, the version
of chDB's core, and DuckDB 1.5) when the rewrites were written. ClickHouse's Q36 and Q66 quotients differed from DuckDB's in the second decimal
there, because ClickHouse keeps the dividend's scale when it divides decimals; `decimal_division_to_double` fixes that, and its tests
(`TestDecimalDivisionOnClickHouse` and Q66) use quotients that do not fit two decimals and assert the unrewritten rows differ from DuckDB's.
`TestRollupNullsOnClickHouse` runs a Q5-shaped rollup over literal channel keys and `TestDiscountBoundsOnClickHouse` TPC-H Q6 with rows on the
discount bounds; each asserts the unrewritten ClickHouse rows differ from DuckDB's.
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

## Unpadded date literals

dsqgen writes `[YEAR]-[MONTH]-01` with an unpadded month (`1999-5-01`), and TPC-DS Q16, Q94 and Q95 bound a date range by
`CAST('1999-5-01' AS DATE)` and a bare `'1999-5-01'`. `tests/test_unpadded_date_literals_engines.py` runs in every engine
job and executes, per engine, `SELECT CAST('1999-5-01' AS DATE)`, `SELECT CAST('2000-3-01' AS DATE) + INTERVAL '60' DAY`
(the engine's own spelling of the interval), `DATE '1999-5-1'`, a `TIMESTAMP` cast, and BETWEEN, comparison and `IN` predicates on a
table of rows that sit exactly on the bounds (the rows must come back, not just a non-error). Each case prints a
`DATE-LITERAL-MATRIX` line with the engine version and the value, so the job log is the record. Values below are from CI
(the container images in the engine table above) and the local QuestDB, all with the fork generator at the time the fix was written:

| Engine (version executed) | `CAST('1999-5-01' AS DATE)` | `CAST('2000-3-01' AS DATE) + 60 days` | Bound rows `[2, 3]` | Result |
|---|---|---|---|---|
| QuestDB (`questdb/questdb:10.0.1`; `version()` answers `PostgreSQL 12.3 ... QuestDB`) | `NULL` before the fix, `1999-05-01` after | `NULL` before, `2000-04-30` after | `[]` before, `[2, 3]` after; a bare `'1999-2-01'` beside a date was `Invalid date` | failed; the generator pads the literal |
| SQLite 3.45.1 (CI), 3.53.1 (local) | `1999-05-01` | `2000-04-30` | `[2, 3]` | fixed earlier by `sqlite-date-literals` |
| DuckDB v1.5.6 | `1999-05-01` | `2000-04-30` | `[2, 3]` | reads unpadded text |
| DataFusion 54.1.0 | `1999-05-01` | `2000-04-30` | `[2, 3]` | reads unpadded dates; `CAST('1999-5-1 3:04:05' AS TIMESTAMP)` is an error, so the generator pads timestamp casts only |
| chDB 26.9.2.1 (ClickHouse core) | `1999-05-01` | `2000-04-30` | `[2, 3]` | reads unpadded text, timestamps included |
| PySpark 4.2.0 (Spark and Databricks dialects) | `1999-05-01` | `2000-04-30` | `[2, 3]` | reads unpadded text |
| PostgreSQL 16.15 | `1999-05-01` | `2000-04-30` | `[2, 3]` | reads unpadded text |
| Trino 484 (Athena and Presto forms) | `1999-05-01` | `2000-04-30` | casts `[2, 3]` | reads unpadded casts; a bare string against a `DATE` is `TYPE_MISMATCH` padded or not (below) |
| StarRocks 4.0.16 (`version()` answers `8.0.33`) | `1999-05-01` | `2000-04-30` | `[2, 3]` | reads unpadded text |
| Doris 4.1.3 (`version()` answers `5.7.99`) | `1999-05-01` | `2000-04-30` | `[2, 3]` | reads unpadded text |

Trino does not coerce a string to a `DATE` in a comparison, `BETWEEN` or `IN` (`Cannot check if date is BETWEEN varchar(9) and date`),
whether the literal is padded or not, so that failure is not about padding and the padding generator does not change it; the test
asserts the rejection for both spellings. A Trino run of a query that compares a `DATE` column with a bare string literal (TPC-DS Q16, Q94
and Q95 write `d_date BETWEEN '2000-3-01' AND ...`) needs a cast that this fix does not add.

Engines with no engine in CI are gaps. Vendor documentation is the only evidence, and it is weaker than execution; none of these rows is
confirmed:

| Platform | Documentation | Status |
|---|---|---|
| BigQuery | [Date literals](https://cloud.google.com/bigquery/docs/reference/standard-sql/lexical#date_literals) and [`CAST AS DATE`](https://cloud.google.com/bigquery/docs/reference/standard-sql/conversion_functions#cast_as_date) | documented gap; both name a canonical date format and a string beside a `DATE` coerces to it, and neither says whether a one-digit month or day is accepted |
| Snowflake | [Date and time input formats](https://docs.snowflake.com/en/sql-reference/date-time-input-output) | documented gap; the page lists `YYYY-MM-DD` and does not say whether one-digit fields are accepted |
| Amazon Redshift | [Date, time and timestamp literals](https://docs.aws.amazon.com/redshift/latest/dg/r_Date_and_time_literals.html) | documented gap; no unpadded `YYYY-M-DD` example |
| Azure Synapse, SQL Server, Fabric | [date (Transact-SQL)](https://learn.microsoft.com/en-us/sql/t-sql/data-types/date-transact-sql) | documented gap; the page says the month and day of the six- and eight-digit forms are always two digits and does not cover `YYYY-M-DD` |
| SingleStore | [Time and Date](https://docs.singlestore.com/cloud/reference/sql-reference/data-types/time-and-date) | documented gap; it follows MySQL, whose [date literal rules](https://dev.mysql.com/doc/refman/8.4/en/date-and-time-literals.html) say two digits are unnecessary for a month or day below 10 |
| Databricks (managed) | open-source Spark in the `in-process` job reads the literal, Databricks is Spark based | documented gap, same substitute as the Spark row |
| Amazon Athena | Trino 484 in the `trino` job | documented gap, same substitute as the Trino row |

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
| BigQuery | Managed service, no official local engine; community emulators do not implement GoogleSQL faithfully, and credentials are secrets CI must not hold | AST and golden-output tests; parse the output back with the `bigquery` dialect; reviewed against the GoogleSQL reference. `TableLayout` `PARTITION BY` / `CLUSTER BY` / `OPTIONS` (`TestBigQueryLayout`) is asserted as text and parsed back; DuckDB cannot run BigQuery DDL, so partition forms, the four-column cluster limit and the option names come from the `CREATE TABLE` reference, not from a run |
| Snowflake | Managed service, no local engine, credentials needed | AST and golden-output tests (including the `CLUSTER BY` layout, `TestSnowflakeLayout`); parse the output back with the `snowflake` dialect; DuckDB execution only for the ANSI-portable part |
| Databricks (managed features: Unity Catalog, Delta table properties, liquid clustering, Photon, SQL warehouses) | Managed service; open-source Spark in the `pyspark` job has no Delta or Unity Catalog | Spark-SQL-compatible output is executed on PySpark; managed-only DDL and properties get golden-output tests |
| Amazon Athena | Managed service; its Trino engine is not runnable locally with Hive/Iceberg table formats | Query output executed on the `trino` job (Athena v3 is Trino based): BenchBox generates Athena workload queries in the `athena` dialect, and `tests/test_benchbox_native_dialects.py` pins that text equal to the `trino` dialect's and runs it on Trino; Athena-only DDL (external tables, `TBLPROPERTIES`, Hive backtick quoting) gets golden-output tests |
| Amazon Redshift | Managed service, no container | Postgres execution for the shared subset (Redshift derives from PostgreSQL 8); Redshift-only features (`DISTKEY`, `SORTKEY`, `ENCODE`) get golden-output tests |
| Azure Synapse | Managed service (the SQL Server container is not Synapse T-SQL and has licensing terms) | Golden-output tests with the `tsql` dialect (including `TestSynapseLayout`: `DISTRIBUTION`, index and `PARTITION` table options); no execution substitute |
| Microsoft Fabric (warehouse, lakehouse, Spark) | Managed service | Golden-output tests with the `fabric` dialect; Fabric Spark output executed on PySpark where it is plain Spark SQL |
| pg_mooncake | Postgres extension that the `postgres:16` service image does not ship; its access method needs the extension's own image | Golden-output tests of `USING mooncake` and the rename and `mooncake.create_table` statements; plain `CREATE TABLE` output executes on the `postgres` job (`tests/test_benchbox_ddl.py`) |
| ClickHouse Cloud, MotherDuck, Firebolt, Starburst, OneHouse, Snowpark Connect | Managed services needing credentials | Same substitutes as the open engine they share a dialect with: chdb, DuckDB, Postgres/Trino; otherwise golden-output tests |

### Obtainable engine, impractical on hosted runners

| Engine | Why | Substitute evidence |
|---|---|---|
| SingleStore | The only dev container (`singlestoredb-dev`) requires a license key, which is a secret; the workflow forbids secrets | Golden-output tests with the `singlestore` dialect, including the query forms BenchBox generates from it (`:>` casts and `NULLS FIRST`/`NULLS LAST` instead of the `CASE WHEN ... IS NULL` emulation the `mysql` dialect writes; `tests/test_benchbox_native_dialects.py`). Those forms are text only: no run checks `:>` precedence against `+ INTERVAL`, or `NULLS FIRST`/`LAST`, on a SingleStore server. The MySQL-protocol client path is covered by the StarRocks and Doris jobs |

### Obtainable engine, not yet added to CI

These can be added by following the pattern in the workflow when a moved piece needs
them: ClickHouse server (chdb covers the engine, not server-only DDL), Databend,
Citus, TimescaleDB, ParadeDB, CedarDB, Presto, DuckLake, Lakesail, Velox and InfluxDB.
Until added, treat them as gaps with the substitute of their closest executed engine.

### Platform dialects without their own engine

The `netezza`, `greenplum` and `vertica` dialects are `postgres` subclasses. `greenplum` and `vertica`
add almost no behavior of their own yet (Greenplum's exact-division cast type, Vertica declining that cast), so their identity and
transpilation tests (`tests/dialects/test_<dialect>.py`) are the evidence for what they add, and the
`postgres` job executes the shared output they inherit. `netezza` adds parser behavior, described in its row.
No engine for them is in CI:

| Platform | Why no engine | Substitute evidence |
|---|---|---|
| Netezza | Proprietary appliance; IBM distributes no local engine. The dialect is a `postgres` subclass whose parser reads `DATE('now' ...)`, `JULIANDAY` and `+ N days` (the SQLite idioms the benchmark queries carry) into portable nodes and marks `/` for exact division | The nodes are executed on the engines that run the target SQL: `tests/test_sqlite_idioms_execution.py` and `tests/test_netezza_exact_division_engines.py` (see above and the next section); identity and transpile tests in `tests/dialects/test_netezza.py`. Netezza's own SQL is never executed |
| Greenplum | No official single-container image; a cluster is too heavy for a hosted job | Identity and transpile tests; shared syntax executes on the `postgres` job |
| Vertica | Not added to CI in this slice | Identity and transpile tests; shared syntax executes on the `postgres` job |

A dialect that later overrides generator or parser behavior must add an engine or
record why one cannot be obtained.

### `ROUND` of doubles from a DuckDB source (`postgres-round-inputs`)

`tests/test_round_inputs_engines.py` runs the FlightData `ROUND` shapes (`ROUND(AVG(d), 2)`, one-argument `ROUND`
over exact binary ties such as `0.5`, `2.5`, `-2.5`, `0.125`) on DuckDB (the reference) and PostgreSQL (`in-process`
and `postgres` jobs). It asserts that the rewritten PostgreSQL rows equal DuckDB's, that unrewritten PostgreSQL rounds
one-argument ties half to even and rejects `ROUND(double precision, integer)` on an untyped column, and that
the generator's own cast already covers `ROUND(AVG(d), 2)`. Documented gap: a double just below a decimal tie
(`0.285`) rounds to `0.28` on DuckDB and `0.29` on PostgreSQL after the cast; PostgreSQL has no exact
double-to-`numeric` conversion, so no rewrite closes it. Other engines (Trino, ClickHouse, MySQL family) round
doubles by their own rules; none is covered, and no BenchBox query set names them for `ROUND` casting.

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

### Accepted limitations

- **BigQuery aggregate windows and `NULLS FIRST`/`LAST`.** The BigQuery generator writes a non-default
  `NULLS` clause as a `key IS NULL` sort key only for `FIRST_VALUE`, `LAST_VALUE` and `NTH_VALUE` without a
  `ROWS` frame (`bigquery-window-nulls-ordering`). For an aggregate window (`SUM(x) OVER (ORDER BY d)`) upstream
  removes the clause and reports an unsupported warning; `test_null_ordering` in `tests/dialects/test_bigquery.py`
  pins that. Rendering the key for aggregate windows too was tried and fails those upstream tests, and
  BigQuery's window grammar lists no `NULLS` modifier at all, with no engine to settle which forms it rejects,
  so the fork keeps upstream behavior there. A query that sorts a nullable key in an aggregate window can differ from
  its source in where the NULLs fall.
- **QuestDB comma joins with unqualified columns.** `FROM a, b, c WHERE x = y` cannot say which table owns an
  unqualified column, so the first equality that can be a join condition becomes the `ON` of the first comma
  join it fits. The rows are the same (an inner join's `ON` and `WHERE` are interchangeable; `TestCommaJoins.
  test_columns_without_a_qualifier` executes it on QuestDB), but the join order QuestDB plans, and its timing,
  can differ from a query written with qualified columns, as TPC-H Q5's unqualified columns do.
- **`JULIANDAY` read from a Netezza source.** `JULIANDAY(a) - JULIANDAY(b)` is a whole-day difference on every
  target. A lone `JULIANDAY(x)` is the integer day number `DATEDIFF(DAY, 1970-01-01, x) + 2440588`, which is
  0.5 above SQLite's own midnight value, and a timestamp input keeps no time of day. The benchmark queries
  (TPC-DI AQ7, AQ8, AQ10) use `JULIANDAY` only in differences of `DATE` columns, where the result is the same
  integer as SQLite's.
- **SQLite exact division is generator behavior.** `sqlite-exact-integer-division` changes every SQLite output,
  including SQLite to SQLite, so `a / b` over integers is a real quotient, not SQLite's truncation. The owner
  accepted this for benchmark correctness over an opt-in rewrite like `safe_division`; integer-literal-only
  quotients keep their truncating meaning.
