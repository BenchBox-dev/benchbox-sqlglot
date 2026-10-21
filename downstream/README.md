# BenchBox SQLGlot

[`BenchBox-dev/benchbox-sqlglot`](https://github.com/BenchBox-dev/benchbox-sqlglot)
is the maintained source fork of [SQLGlot](https://github.com/tobymao/sqlglot),
licensed under the original MIT license. The fork targets SQL used
by BenchBox. The base is upstream 30.24.0 plus the entries in
`patches.json`: edits to upstream-owned files are carried patches (`fix: retain <ID>`
commits), and `original` entries are limited to wholly new files.

BenchBox checks in and ships **only the pinned built pure-Python runtime wheel**
as package data inside its own published wheel and sdist. The fork's unpacked
Python sources stay in this repository, never in the BenchBox checkout.
There is no separate PyPI publication, dependency or custom index. Ordinary
BenchBox installs and builds do not fetch Git or rebuild this fork.

BenchBox extends its real `benchbox._vendor` package's `__path__` with the
packaged wheel path plus `/benchbox/_vendor`. Standard `zipimport` loads the
runtime directly from the nested wheel without global `sys.path` changes,
pip installation, extraction or runtime network access:

```python
from benchbox._vendor import sqlglot

sqlglot.transpile("SELECT 1", read="postgres", write="sqlite")
```

Public SQLGlot can coexist as `sqlglot`; its expression and dialect classes are
distinct. Exchange SQL text rather than objects across namespaces. Plugins
must explicitly target `benchbox._vendor.sqlglot.dialects`; public SQLGlot's
plugin group remains separate. There is no `sys.modules` alias or fallback.

The private runtime wheel supports Python 3.11 and later and has no compiled
extension, optional dependency or console script. SQL comment markers such as
`sqlglot.meta` and `sqlglot.anonymous`, and upstream logger strings, retain their
spelling. Original upstream licensing and `py.typed` are embedded in the wheel.

From a clean, pinned source checkout, build into an empty output directory:

```sh
make benchbox-upstream-tag
uv run --no-project -- python -m downstream.build --out-dir /tmp/benchbox-sqlglot-dist
```

The builder writes the runtime wheel, verification-only sdist and `SHA256SUMS`.
Only the reviewed wheel is copied to BenchBox. Versions use `X.Y.Z.N`: the first
three components identify the upstream tag; the positive fourth component
increments for downstream revisions and resets to one on a new base. Existing
tags, including `benchbox-v30.23.0.1`, are immutable.

Read `benchbox_provenance.json` with
`importlib.resources.files("benchbox._vendor.sqlglot")` for exact upstream/fork
commits, commit timestamp, repository, namespace, complete patch manifest and
per-file SHA256 hashes. This works directly from the zipped wheel. See the
[maintenance guide](MAINTAINING.md) for refresh, verification and rollback.

## Dialects

The fork provides every upstream dialect plus `netezza`, `greenplum`, `vertica`,
`datafusion` and `questdb` (each derived from `postgres`). It does not provide
`ansi` or `standard`: callers resolve those aliases themselves (BenchBox maps them
in its platform registry) and pass a dialect that exists. An unknown name raises
instead of falling back.

`sqlglot.benchbox.rewrites` holds expression rewrites whose gates are arguments, for
example `date_integer_arithmetic_to_interval(expression, date_columns)`, which turns
`d_date + 5` into a day interval for the columns the caller says hold dates.

The adapter rewrites in the same module are:

- `safe_division(expression, dialect)` turns every `/` into `SAFE_DIVIDE(a, b)` (`bigquery`),
  `TRY_DIVIDE(a, b)` (`databricks`) or `a / NULLIF(b, 0)` (`snowflake`, `clickhouse`). It is
  opt-in and never applied by `transpile`: it changes results, because a zero divisor then
  yields NULL instead of an error or infinity. Any other dialect raises `ValueError`.
- `deduplicate_output_aliases(expression, nested=True)` suffixes repeated output names
  (`x`, `x_2`, ...) in every `SELECT`, except where `*` or an `ORDER BY`/`GROUP BY` reference
  to the repeated name makes renaming unsafe.
- `unqualified_tables(expression)` returns the `Table` nodes with no schema or catalog that are
  not CTE references, so a caller holding a dataset can set `db` on them.
- `flag_comparisons_to_boolean(expression, columns, use_is=False)` rewrites `flag = 1` and
  `flag = 0` for the named columns into `= TRUE`/`= FALSE` (or `IS TRUE`/`IS FALSE`).

`sqlglot.benchbox.engine_rewrites` holds the rewrites for engines that reject SQL their peers accept. Each
takes an expression and options (every name or number it matches is an argument) and returns a copy:

- `lowercase_identifiers(expression, patterns)` lowercases identifiers whose whole name matches a pattern
  (ClickHouse compares identifier case-sensitively); string literals are never touched.
- `decimal_zero_literals(expression, precision=15, scale=2)` replaces `0.0` with `CAST(0 AS DECIMAL(p, s))`
  inside `COALESCE`, arithmetic with a column and `THEN`/`ELSE` results, so ClickHouse does not mix `Float64`
  with decimals. ClickHouse division by zero reuses `safe_division(expression, "clickhouse")`.
- `add_query_settings(expression, settings)` appends `SETTINGS name = value, ...` to a top-level ClickHouse
  query and replaces a setting the query already carries.
- `correlated_exists_to_in(expression, expected=None)` turns `EXISTS (SELECT ... WHERE outer.k = inner.k ...)`
  that sits under `AND`/`OR` only into `outer.k IN (SELECT inner.k ...)`; `expected` raises `ValueError` when
  the number of rewrites differs, so a query of an unexpected shape fails closed.
- `add_derived_table_aliases(expression, prefix="_sq", quoted=True)` aliases unaliased derived tables, which
  StarRocks and Doris require.
- `qualify_order_by_columns(expression, qualifiers)` qualifies an unqualified `ORDER BY` column with the source
  named for it, for queries whose joined sources expose the same column.

StarRocks and Doris need no generator change: both already quote the reserved words that fail as aliases
(checked unquoted on StarRocks 4.0.16 and Doris 4.1.3), write `SUBSTRING(x FROM a FOR b)` as
`SUBSTRING(x, a, b)` and quote identifiers with backticks. The Netezza parser reads `<expr> + N days` and
`<expr> - N days` (also `DAY`) as day intervals.

`sqlglot.benchbox.compat` holds the engine-compatibility rewrites. Each takes an expression, leaves
every gate (which columns, which alias, which keys) to the caller and returns a copy by default:

- `inline_select_aliases(expression, clauses=("having", "where"))` replaces a reference to a
  projection alias in `HAVING` or `WHERE` with the aliased expression, for engines that resolve
  aliases only in `ORDER BY`.
- `qualify_columns_by_name(expression, tables, clauses=None, quoted=False)` qualifies unqualified columns by
  a column-to-table map the caller passes, optionally only inside the named clauses (`("having",)`).
- `add_row_source_to_fromless_selects(expression, source, dialect)` adds a one-row `FROM` source the caller
  passes (BigQuery's `UNNEST([1])`) to every `SELECT ... WHERE` that has no `FROM`.
- `filtered_aggregates_to_conditional(expression, dialect, windows_only=False)` removes `FILTER
  (WHERE ...)`: `COUNTIF` and `agg(IF(cond, x, NULL))` for `bigquery`, `countIf`, `uniqExactIf` and
  the `...OrNullIf` combinators for `clickhouse` (other aggregates raise `NotImplementedError`),
  `agg(IF(cond, x, NULL))` for everything else; `windows_only=True` leaves ordinary aggregates alone. The
  BigQuery generator already writes this form without a rewrite.
- `drop_literal_row_source(expression, alias)` removes the one-row `(SELECT 1) AS alias` source from a
  `SELECT` that never reads it, leaving a `FROM`-less `SELECT 0 AS r WHERE FALSE`. No rewrite writes
  `FROM VALUES (1)`, `(VALUES (1)) AS d(c)`, `system.one` or `dual`: the `FROM`-less form runs on
  PostgreSQL, DuckDB, DataFusion, Spark, ClickHouse, SQLite, StarRocks and Doris (BigQuery needs
  `add_row_source_to_fromless_selects` afterwards). `name_literal_subquery_columns(expression, column)`
  instead aliases the literal of an unnamed one-column derived table, for engines that need a named column.
- `flatten_count_of_count(expression)` turns `SELECT COUNT(*) FROM (SELECT COUNT(*) AS c FROM t WHERE p)`,
  which always returns 1, into the inner `SELECT COUNT(*) FROM t WHERE p`. It changes the returned value (1
  becomes the source row count), so a caller applies it only where the row count is the intended answer.
- `wrap_grouped_scalar_subqueries(expression, aggregate=exp.First)` wraps a scalar subquery projected
  by a `GROUP BY` select in an aggregate.
- `order_list_aggregates(expression, order_by, dialect)` turns `LIST(x)` and unordered `ARRAY_AGG(x)`
  into `ARRAY_AGG(x ORDER BY keys)`, or on Spark, Spark 2 and Databricks into
  `TRANSFORM(SORT_ARRAY(COLLECT_LIST(STRUCT(keys..., x AS _v))), r -> r._v)`; `dialect` picks the NULL ordering
  that needs no clause.

`sqlglot.benchbox.subqueries` holds the subquery rewrites for engines that plan a subquery badly or wrongly:

- `not_in_subquery_to_not_exists(expression, non_null_columns)` turns `x NOT IN (SELECT y FROM ... WHERE p)`
  into `NOT EXISTS (SELECT 1 FROM ... WHERE p AND y = x)` only when both columns are named in
  `non_null_columns` (`NOT IN` is three-valued, `NOT EXISTS` is not); any other `NOT IN` is left alone.
- `in_aggregate_subquery_to_exists(expression, alias="_in_subquery")` turns `x IN (SELECT k ... GROUP BY ...
  HAVING ...)` into `EXISTS (SELECT 1 FROM (SELECT k ...) AS alias WHERE alias.k = x)` where an `IN` can be
  replaced without changing the filtered rows (reached through `AND`, `OR` and parentheses from `WHERE`,
  `HAVING` or `ON`).
- `decorrelate_subqueries(expression, schema, dialect=None)` is SQLGlot's own `unnest_subqueries` made safe for
  such engines: columns are qualified from the `schema` the caller passes, comma joins next to a subquery
  become `CROSS JOIN` (a left join added after `FROM a, b` binds to `b` only), and a correlated subquery that
  correlates through anything but equality is left in place instead of becoming array functions. Scalar,
  correlated and `IN` subqueries become cross or left joins against derived tables.

Generator behavior needs no rewrite call: `GROUP BY ()` is dropped from a global aggregate (one with an
aggregate or `HAVING`) for `snowflake`, `spark`, `databricks` and `datafusion`, which reject it; any other
`GROUP BY ()` for those dialects warns as unsupported.

The Databricks generator also drops ordering (`ASC`, `DESC`, `NULLS FIRST`, `NULLS LAST`) from
`PRIMARY KEY` columns, which Databricks rejects, and its parser accepts that ordering.

The `netezza` dialect reads the SQLite date idioms that Netezza-sourced benchmark queries carry:
`DATE('now')` is `CURRENT_DATE`, `DATE('now', '-N days')` (also `+`, `month` and `year`) is a date
add or subtract, `JULIANDAY(a) - JULIANDAY(b)` is a day `DATE_DIFF` and a lone `JULIANDAY(x)` is the
day number `DATE_DIFF('DAY', DATE '1970-01-01', x) + 2440588`. Every target writes its own form, and
the SQLite generator writes `DATE(...)` and `JULIANDAY` back. Other dialects read these calls as
plain functions, as before. The Snowflake generator rewrites a `REGEXP_REPLACE` pattern that has
non-capturing groups `(?:...)` into capturing groups and renumbers the `\N` references in the
replacement, because Snowflake's regular expression syntax lacks non-capturing groups.

## DDL

`sqlglot.benchbox.ddl.transpile_ddl(sql, read, write, options=None, **generator_options)` translates
DDL statements and returns one string per statement. What it changes besides the dialect spelling is
set by `DdlOptions`, a frozen dataclass whose fields all default to off:

| Field | Type | Effect |
|---|---|---|
| `strip_primary_keys` | `bool` | removes inline and table-level `PRIMARY KEY`, named or not |
| `strip_foreign_keys` | `bool` | removes `FOREIGN KEY` and inline `REFERENCES`, with their `ON DELETE`/`ON UPDATE` actions |
| `strip_unique` | `bool` | removes inline and table-level `UNIQUE` |
| `or_replace` | `bool` | `CREATE TABLE` becomes `CREATE OR REPLACE TABLE` (not temporary tables, not `IF NOT EXISTS`) |
| `replace_if_not_exists` | `bool` | with `or_replace`, also replaces `CREATE TABLE IF NOT EXISTS` |
| `type_map` | `Mapping[str, str]` | column type name to the type written instead (`{"DECIMAL": "NUMERIC"}`), parameters kept |
| `drop_type_parameters` | `Collection[str]` | replacement types written without parameters (`["TEXT"]`) |
| `singlestore` | `SingleStoreKeys \| None` | SingleStore key clauses, below |
| `strip_not_null` | `bool` | removes `NOT NULL` from columns |
| `if_not_exists` | `bool` | `CREATE TABLE` becomes `CREATE TABLE IF NOT EXISTS` (permanent tables, not `OR REPLACE`) |
| `schema` | `str \| None` | qualifies table names that have no schema |
| `table_suffix` | `str` | appended to every created table name |
| `layouts` | `Mapping[str, TableLayout]` | engine-specific clauses per table name, below |
| `default_layout` | `TableLayout \| None` | the layout of permanent tables with no entry in `layouts` |

`SingleStoreKeys` (also frozen) carries the benchmark data the fork must not know: `shard_keys` and
`sort_keys` (table name to column or columns), `reference_tables` (written as `CREATE REFERENCE TABLE`
without keys) and `random_shard_key` (write `SHARD KEY ()` for a table with neither a mapped shard key
nor a primary key). Table names match case-insensitively and without a schema qualifier, and
`shard_key_clause` and `sort_key_clause` write a single clause. Stripping happens before the other
options, so a primary key removed by `strip_primary_keys` does not stop `SHARD KEY ()`.

Constraint stripping, replacement and type renaming act on the parsed statement, so `CHECK`
constraints, defaults and everything else are kept. Statements that are not `CREATE TABLE` are only
translated.

### Engine-specific table layouts

`TableLayout` (frozen, in `sqlglot.benchbox.layout`) carries one table's physical layout as data: `engine`,
`format`, `stored_as`, `partition_by` or `partition_clause`, `sort_by`, `primary_key`, `cluster_by`,
`distribution`, `distribute_by`, `buckets`, `key_model`, `key_columns`, `location`, `external`,
`properties` and the other fields its docstring lists with the dialects that read each one. A field a
dialect has no use for is ignored; a dialect with no layout support raises `ValueError`. Column names are
written the way the target quotes them, and anything that is not a plain identifier (`tuple()`,
`toYYYYMM(day)`) is written as given. Each clause group is written in the target's syntax and parsed back
with the target's parser, so SQLGlot's own generator spells and places it; a group the parser cannot read
(Redshift `SORTKEY AUTO`, Doris `AGGREGATE KEY`, Redshift Spectrum clauses) is appended to the statement
text. A statement that already has a clause of the same kind (`ENGINE`, `ORDER BY`, `DISTSTYLE`...) keeps its
own. A layout is applied to permanent `CREATE TABLE` statements only, after the other options, and reads the
statement's own primary key before anything is stripped.

| Dialect | Clauses written |
|---|---|
| ClickHouse | `ENGINE = <engine>`, `PARTITION BY (..)`, `ORDER BY (..)` from `sort_by`, else the statement's primary key, else `tuple()`, then `PRIMARY KEY (..)` after a given `sort_by` (the statement's own `PRIMARY KEY` is removed then), `SETTINGS`. `nullable_columns` decides which columns are `Nullable` (a key column listed there is a `ValueError`); without it SQLGlot's `Nullable` wrapping stays except on `NOT NULL` columns, which get a plain type. Fixed-size arrays (`REAL[3]`) become plain arrays. |
| Doris, StarRocks | `DUPLICATE`, `UNIQUE`, `AGGREGATE` or `PRIMARY KEY (..)` from `key_model` and `key_columns` (a leading run of defined, keyable columns; else the first keyable column), `PARTITION BY`, `DISTRIBUTED BY HASH(..) BUCKETS n` or `RANDOM`, StarRocks `ORDER BY (..)`, `PROPERTIES (..)`. Primary and foreign keys are removed, fixed-size arrays lose their size. `DORIS_TYPE_MAP` and `STARROCKS_TYPE_MAP` are the column type maps to pass as `type_map`. |
| Redshift | `DISTSTYLE KEY DISTKEY(..)`, `EVEN`, `ALL` or `AUTO`; `SORTKEY(..)` or `SORTKEY AUTO`; with `external`, `CREATE EXTERNAL TABLE ... STORED AS .. LOCATION .. TABLE PROPERTIES (..)` |
| Spark, Databricks | `USING <format>` (Delta, Iceberg, Parquet, ORC, Hudi) or `STORED AS`; `PARTITIONED BY (..)` (Iceberg transforms such as `bucket(8, id)` are written as given); `CLUSTER BY (..)` or `CLUSTER BY AUTO`; `CLUSTERED BY (..) [SORTED BY (..)] INTO n BUCKETS`; `LOCATION`; `TBLPROPERTIES` |
| Hive, Athena | `PARTITIONED BY (name type)` with the partition columns moved out of the column list, bucketing, `ROW FORMAT DELIMITED FIELDS/LINES TERMINATED BY`, `STORED AS`, `LOCATION`, `TBLPROPERTIES`; `external` writes `CREATE EXTERNAL TABLE`. With `if_not_exists`, `table_suffix`, `strip_not_null` and a `type_map` this is Athena's external and staging table |
| Trino, Presto | one `WITH (..)` property each for `format`, `partitioned_by` (`partitioning` when `format="iceberg"`), `bucketed_by` and `bucket_count`, `sorted_by`, `location` (`external_location` with `external`) and `properties`. Leave the layout off for a connector that rejects properties (the memory catalog) |
| T-SQL (Synapse, Fabric) | `WITH (DISTRIBUTION = ROUND_ROBIN \| REPLICATE \| HASH(..))`; the `schema` option writes the schema prefix Fabric Warehouse needs |
| PostgreSQL | `USING <format>` access method (`mooncake`); `mooncake_mirror_statements(table, staging_table, schema)` writes the two statements that promote a loaded heap table to a `pg_mooncake` mirror |
