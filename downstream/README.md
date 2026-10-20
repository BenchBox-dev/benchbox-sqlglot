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

`SingleStoreKeys` (also frozen) carries the benchmark data the fork must not know: `shard_keys` and
`sort_keys` (table name to column or columns), `reference_tables` (written as `CREATE REFERENCE TABLE`
without keys) and `random_shard_key` (write `SHARD KEY ()` for a table with neither a mapped shard key
nor a primary key). Table names match case-insensitively and without a schema qualifier, and
`shard_key_clause` and `sort_key_clause` write a single clause. Stripping happens before the other
options, so a primary key removed by `strip_primary_keys` does not stop `SHARD KEY ()`.

Constraint stripping, replacement and type renaming act on the parsed statement, so `CHECK`
constraints, defaults and everything else are kept. Statements that are not `CREATE TABLE` are only
translated.
