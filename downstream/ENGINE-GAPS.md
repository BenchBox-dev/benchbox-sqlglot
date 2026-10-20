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
`tests/test_questdb_execution.py` runs in the `questdb` job: it executes the QuestDB
generator's comma-join expansion, `DATEADD`, `SUBSTRING` and CTE column list output.

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
