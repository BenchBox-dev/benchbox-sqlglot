# BenchBox SQLGlot

[`BenchBox-dev/benchbox-sqlglot`](https://github.com/BenchBox-dev/benchbox-sqlglot)
is the maintained source fork of [SQLGlot](https://github.com/tobymao/sqlglot),
licensed under the original MIT license. The fork targets SQL used
by BenchBox. The base is upstream 30.35.0 plus the entries in
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

## Engine versions in CI

The `BenchBox real engines` workflow executes translated SQL on the engines BenchBox supports, so
`downstream/engines-requirements.txt` holds each in-process engine to the release series BenchBox
locks and supports (`uv.lock` and `pyproject.toml` in BenchBox): DuckDB 1.5 (below 2), DataFusion 54,
chDB 4.1 and PySpark 4.2. Service engines are pinned by image tag in the workflow. A newer major is
not tested until BenchBox supports it; raise a pin in the same change that raises BenchBox's lock.
Every job prints the installed package versions (`python -m tests.engines versions`), and the job log
is the version record for the evidence of a change.

## Dialects

The fork provides every upstream dialect plus `netezza`, `greenplum`, `vertica`,
`datafusion` and `questdb` (each derived from `postgres`). It does not provide
`ansi` or `standard`: callers resolve those aliases themselves (BenchBox maps them
in its platform registry) and pass a dialect that exists. An unknown name raises
instead of falling back.

`sqlglot.benchbox.rewrites` holds expression rewrites whose gates are arguments, for
example `date_integer_arithmetic_to_interval(expression, date_columns)`, which turns
`d_date + 5` into a day interval for the columns the caller says hold dates.
`mark_date_columns(expression, date_columns)` types those columns as `DATE` on the tree, so the SQLite
generator writes `d_date + 5` as `DATE(d_date, '5 DAY')`, `d2 - d1` as a day count and a `DATE_TRUNC`
compared with a date as timestamp text; pass the marked expression, not its SQL text, to `.sql('sqlite')`.
The SQLite generator also writes a string literal that is cast to `DATE` or `TIMESTAMP` (`CAST('1999-5-01' AS DATE)`,
`DATE '1999-5-1'`), or compared with, `IN`-listed against or `BETWEEN`-bounded by a known date (a marked column or a
cast), in zero-padded `YYYY-MM-DD[ HH:MM:SS]` form, because SQLite reads only padded date text (`DATE('1999-5-01')`
is `NULL` and `'1999-5-1'` sorts after `'1999-12-01'`). A string that is not a valid date is left unchanged.
The QuestDB generator does the same through `DateLiteralPadding` (`sqlglot/benchbox/date_literals.py`), because QuestDB reads
`CAST('1999-5-01' AS DATE)` as `NULL` and rejects `d = '1999-2-1'` as an invalid date; the DataFusion generator uses it for `TIMESTAMP`
casts only, where an unpadded string is an error. Every other engine in CI reads unpadded text (`downstream/ENGINE-GAPS.md`).

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
- `nullif_zero_divisors(expression, columns)` turns `a / CAST(pmc AS DECIMAL(15, 4))` into
  `a / NULLIF(CAST(pmc AS DECIMAL(15, 4)), 0)` for every division whose divisor is, once parentheses and casts are
  removed, a column named in `columns` (TPC-DS Q90 passes `["pmc"]`). PostgreSQL, ClickHouse and SQL Server fail on a
  zero divisor and DuckDB returns `inf`, so the guard gives every engine NULL. It changes results (an error or `inf`
  becomes NULL), is opt-in, and unlike `safe_division` works for every dialect.
- `round_inputs_to_decimal(expression)` casts the input of every `ROUND` to an unconstrained `DECIMAL`. DuckDB rounds a
  double half away from zero (`ROUND(2.5)` is 3); PostgreSQL rounds `double precision` half to even (2) and has no
  `ROUND(double precision, integer)`. The PostgreSQL generator already casts a two-argument `ROUND` whose input the type
  annotator knows is a double, so this rewrite adds the one-argument case and columns of unknown type. Use it for engines
  whose bare `DECIMAL` has no default scale (the PostgreSQL family); on DuckDB, whose bare `DECIMAL` is `DECIMAL(18,3)`,
  it would truncate. A value whose binary double sits just below a decimal tie (`0.285`) rounds to `0.29` on PostgreSQL
  and `0.28` on DuckDB, because PostgreSQL converts a double to `numeric` through its shortest decimal form. It changes
  every FlightData query with a `ROUND` (FlightData is written in DuckDB SQL, the reference).

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
- `alias_qualified_projections(expression, top_level=False)` writes `v1.d_year AS d_year` for every qualified column
  projected by a `WITH` entry or subquery (a repeated name, `*` and the outermost query are left alone).
  ClickHouse names the output of `SELECT v1.d_year FROM v1, v1 AS v1_lag, v1 AS v1_lead` `v1.d_year`, so the outer
  `WHERE d_year = 2001` of TPC-DS Q47 and Q57 fails with an unknown identifier.
- `qualify_alias_shadowed_columns(expression)` qualifies a column with its source when a projection alias of the same
  name hides it, outside `ORDER BY`, for a `SELECT` whose every `FROM`/`JOIN` source is a derived table or `WITH`
  entry with a readable output list and exactly one source outputs the name: `SUM(x.jan_sales / sq_ft) ... FROM (...)
  AS x`. ClickHouse reads the unqualified `jan_sales` as the alias `SUM(jan_sales) AS jan_sales` and fails with
  `ILLEGAL_AGGREGATION` (TPC-DS Q66). `ORDER BY` keeps the alias, as SQL does, and the query-wide setting
  `prefer_column_name_to_alias` is not used because it also moves `ORDER BY total_sales` of Q33, Q56, Q60 and Q71
  to the column and breaks them.
- `decimal_division_to_double(expression)` casts the dividend of every `/` to `DOUBLE`
  (`CAST(x AS Nullable(Float64))` on ClickHouse). ClickHouse gives `Decimal / Decimal` and `Decimal / Int` the dividend's
  scale and truncates (`CAST(75 AS Decimal(9, 2)) / 7` is `10.71`), where DuckDB divides in doubles. Integer quotients are
  already `Float64` there; float dividends, float literals and chained divisions are left alone. A zero divisor then gives
  `inf` as in DuckDB; pair it with `safe_division` or `nullif_zero_divisors` for NULL. It changes TPC-DS Q2, Q12, Q20,
  Q36, Q59, Q66, Q98 and TPC-H Q8 at scale factor 0.01 and no query for the worse.
  A `CASE`/`IF` that returns such a quotient beside a `CAST(0 AS DECIMAL(p, s))` zero (written by `decimal_zero_literals`)
  gets `CAST(0 AS DOUBLE)` for the zero, because ClickHouse has no common type for `Float64` and `Decimal` without the
  Variant type (`use_variant_as_common_type = 1`); `decimal_zero_literals` leaves such a zero as `0.0`. TPC-DS Q11.
- `use_nulls_for_rollups(expression)` adds `SETTINGS group_by_use_nulls = 1` to a ClickHouse query with `ROLLUP`, `CUBE` or
  `GROUPING SETS`. Without it ClickHouse writes `''` or `0` in a rolled-up key whose column is not `Nullable` (a literal from a
  `UNION ALL` branch, TPC-DS Q5, Q14, Q77 and Q80), where SQL has NULL. Other queries are unchanged.
- `fold_between_literal_arithmetic(expression)` folds literal-only sums and differences in `BETWEEN` bounds and in the operands of
  `=`, `<>`, `<`, `<=`, `>`, `>=` to exact decimals, for every table and column. SQLite (`REAL`) and ClickHouse (`Float64`)
  read `0.07 - 0.01` as a double, so TPC-H Q6 (`l_discount BETWEEN 0.07 - 0.01 AND 0.07 + 0.01`) and its Data Vault form
  (`sat_lineitem`) drop the rows at the bounds without it. An expression whose double result already equals the exact value is
  left alone.

StarRocks and Doris need no generator change: both already quote the reserved words that fail as aliases
(checked unquoted on StarRocks 4.0.16 and Doris 4.1.3), write `SUBSTRING(x FROM a FOR b)` as
`SUBSTRING(x, a, b)` and quote identifiers with backticks. The Netezza parser reads `<expr> + N days` and
`<expr> - N days` (also `DAY`) as day intervals.

`sqlglot.benchbox.compat` holds the engine-compatibility rewrites. Each takes an expression, leaves
every gate (which columns, which alias, which keys) to the caller and returns a copy by default:

- `inline_select_aliases(expression, clauses=("having", "where"))` replaces a reference to a
  projection alias in `HAVING` or `WHERE` with the aliased expression, for engines that resolve
  aliases only in `ORDER BY`.
- `inline_order_by_aliases(expression, names=None)` replaces a projection alias used inside an `ORDER BY` expression
  (`CASE WHEN lochierarchy = 0 THEN i_category END`) with the aliased expression, in parentheses where an operator
  needs them; a sort key that is only the alias, qualified columns, other scopes, `GROUP BY` and `HAVING` stay.
  PostgreSQL (and the engines built on it) resolve an output alias in `ORDER BY` only as the whole key (TPC-DS Q36,
  Q70 and Q86). `names` limits the aliases that are inlined.
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
  `SELECT` that never reads it, leaving a `FROM`-less `SELECT 0 AS r WHERE FALSE`. The inner `SELECT`
  must be provably one row (a lone numeric literal with no `FROM`, `WHERE`, `LIMIT`, `GROUP BY`,
  `HAVING`, `DISTINCT` or other clause), so `(SELECT 1 FROM t)` or `(SELECT 1 WHERE FALSE)` keep their
  row counts. No rewrite writes
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
  into `ARRAY_AGG(x ORDER BY keys)`, on Spark, Spark 2 and Databricks into
  `TRANSFORM(SORT_ARRAY(COLLECT_LIST(STRUCT(keys..., x AS _v))), r -> r._v)`, or on SQLite into the JSON array
  `JSON_GROUP_ARRAY(x)` (SQLite steps every aggregate of a `GROUP BY` query over the same rows in the same order, so
  the lists align; an ordered aggregate needs SQLite 3.44, so `order_by` is not applied); `dialect` picks the NULL
  ordering that needs no clause.
- `reduce_list_aggregates(expression, dialect)` rewrites DuckDB's list functions for `presto`, `trino`, `athena`,
  `bigquery`, `spark`, `databricks`, `sqlite`, `postgres` and `datafusion` (other dialects, including the PostgreSQL derivatives, are returned unchanged). `LIST_SUM(list)` and `LIST_AVG(list)`,
  alone or over `LIST_TRANSFORM(LIST_ZIP(a, b, ...), x -> f(x[1], x[2], ...))` (TPC-Havoc `1_v7`), become `REDUCE`
  over `ZIP_WITH` (two arrays) or `TRANSFORM(ZIP(...))` (three to five) on Presto, Trino and Athena; `AGGREGATE`
  over `ZIP_WITH` (arrays beyond the second are zipped into structs first) on Spark and Databricks; and a scalar
  `(SELECT SUM(...) FROM UNNEST(...))` that reads the zipped arrays by `SAFE_OFFSET` over
  `GENERATE_ARRAY(0, GREATEST(ARRAY_LENGTH(...)) - 1)` on BigQuery, and a scalar
  `(SELECT SUM(...) FROM JSON_EACH(...))` that aligns the zipped JSON arrays by their `key` position in one grouped pass on SQLite, a scalar
  `(SELECT SUM(...) FROM UNNEST(a, b, ...) AS t(v0, v1, ...))` on PostgreSQL (its multi-argument `UNNEST` zips and pads with `NULL`, and a transform outside an
  aggregate becomes `ARRAY(SELECT ... WITH ORDINALITY ... ORDER BY ordinality)`), and, on DataFusion (which has neither array folds nor a correlated `UNNEST`),
  a `SUM`, `AVG(CAST(... AS DOUBLE))` or `SUM(f(...))` column added to the `WITH` or derived table that builds the lists, read in place of the fold; lists with different
  `FILTER`s, a lambda that reads another column, and lists that no `GROUP BY` select builds are left unchanged on DataFusion. The `REDUCE`/`AGGREGATE` forms compute in `DOUBLE`
  (BigQuery keeps the column type); `NULL` elements are skipped and an empty or all-`NULL` list gives `NULL`, as in
  DuckDB, and a shorter zipped array reads `NULL` past its end, as `LIST_ZIP` pads. A `LIST_TRANSFORM` over
  `LIST_ZIP` outside an aggregate becomes the same `ZIP_WITH` forms (an ordered `ARRAY(SELECT ...)` on BigQuery),
  a bare `LIST_ZIP` becomes `ZIP` or `ARRAYS_ZIP`, and `LEN`/`LENGTH` of a column that a `LIST` or `ARRAY_AGG` defines
  in a `WITH` or derived table becomes `CARDINALITY`, `SIZE`, `ARRAY_LENGTH` or `JSON_ARRAY_LENGTH`. A lambda that uses its parameter other
  than as `x[<literal>]` over a zip is left unchanged. It is a rewrite, not a generator change, because the
  array-typed `LEN` and the correlated `UNNEST` need the surrounding query. Call `order_list_aggregates` first so
  every zipped list is built in the same order; BenchBox calls
  `apply_rewrite("order_list_aggregates", tree, dialect, order_by=["l_orderkey", "l_linenumber"])` and then
  `apply_rewrite("reduce_list_aggregates", tree, dialect)` on the tree parsed from the DuckDB query, and writes the
  tree for the target (`translate_sql_query` has no `rewrites=` argument; `apply_rewrite` takes the string or the tree).

`sqlglot.benchbox.shapes` and `sqlglot.benchbox.dialect_rewrites` hold the query-shape rewrites BenchBox ran locally
before they moved here. Each takes an expression, returns a copy (`copy=False` mutates), and every gate is an
argument; the text each writes is the text BenchBox wrote. Names are the ones BenchBox's `transformer_id` strings use.

- `shapes`: `drop_empty_group_by`, `unwrap_single_grouping_set`, `add_dual_source_to_filtered_selects` (the bare
  keyword `FROM DUAL`), `inline_sourceless_exists`, `exists_to_derived_join`,
  `drop_correlated_scalar_group_by(expression, columns)` (run it before `fold_scalar_having_into_case`, which needs the
  `GROUP BY` gone), `fold_scalar_having_into_case`, `qualify_aggregated_columns(expression, tables)` (aggregate
  arguments only), `wrap_order_by_subquery_aliases`, `alias_order_by_projections`,
  `date_column_integer_arithmetic(expression, suffix)` (a column whose name ends with `suffix` plus or minus an
  integer becomes `DATE_ADD`/`DATE_SUB` in days; the caller names the convention), `rollup_empty_input_row` and
  `hoist_aggregate_subqueries`.
- `dialect_rewrites`: `alias_outer_table_references(expression, table, alias)`, `alias_derived_tables(expression,
  prefix="_dt")` (unquoted `_dt1`, `_dt2`, ... in breadth-first order, which `compat`'s and `engine_rewrites`'s
  derived-table aliases do not write), `coalesce_count_subqueries`, `trim_char_case_comparisons`,
  `in_subquery_or_to_left_join`, `factor_common_conjuncts`, `positive_count_subquery_to_exists` and
  `hoist_qualify_outer_clauses`, which runs `transforms.eliminate_qualify` and keeps `ORDER BY`, `LIMIT`, `OFFSET` and
  `DISTINCT` on the outermost select (it logs through the `sqlglot` logger when a sort key is not projected).

Order matters in two chains BenchBox runs: `drop_correlated_scalar_group_by` then `fold_scalar_having_into_case`, and
`positive_count_subquery_to_exists`, `factor_common_conjuncts`, then `correlated_exists_to_in`. The Doris and StarRocks
generators inline named windows (`transforms.eliminate_window_clause`, patch `doris-starrocks-named-windows`) and drop
`GROUP BY ()` of a global aggregate (`SUPPORTS_EMPTY_GROUP_BY = False`), so BenchBox needs no rewrite for TPC-Havoc 1_v8,
6_v2 or 14_v2 on them. `tests/test_benchbox_shapes_engines.py` and `tests/test_benchbox_doris_starrocks_generators.py`
execute them (ENGINE-GAPS.md lists SingleStore, which has no engine in CI).

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

`sqlglot.benchbox.series.lower_series_selects(expression, dialect, copy=True)` writes
`SELECT unnest(generate_series(lo, hi)) AS n` (DuckDB and PostgreSQL write it as is) in the form the target runs,
returning the same rows. SQLite, Doris, StarRocks and SingleStore have no `GENERATE_SERIES` or `UNNEST` in a select
list, so without it they get `EXPLODE(GENERATE_SERIES(...))`. The select is replaced wherever it sits (a derived table,
a CTE, a set operation) and has to be exactly that shape: one aliased projection, no `FROM` or other clause, no step and
no exclusive end; anything else is left alone. `dialect` is the dialect the tree will be written in:

| Dialect | Form |
|---|---|
| `sqlite` | `WITH RECURSIVE _series(n) AS (SELECT lo UNION ALL SELECT n + 1 FROM _series WHERE n < hi) SELECT n FROM _series` |
| `doris` | `SELECT number + lo AS n FROM NUMBERS('number' = hi - lo + 1)` (integer literal bounds, else `ValueError`) |
| `singlestore` | the digits 0 to 9 cross joined four times, filtered with `n BETWEEN lo AND hi` (at most 9999) |
| `snowflake` | `FLATTEN(ARRAY_GENERATE_RANGE(lo, hi + 1))` |
| `bigquery` | `UNNEST(GENERATE_ARRAY(lo, hi)) AS n` |
| `questdb`, `starrocks` | `generate_series(lo, hi)` as a table |
| `postgres` | `SELECT generate_series(lo, hi) AS n`: every `UNNEST(GENERATE_SERIES(...))` in a select list loses its `UNNEST` |

Any other dialect raises `ValueError`. The rest of a transactional script needs no rewrite: `BEGIN TRANSACTION` is
written `BEGIN` for Doris, StarRocks, SingleStore and QuestDB, `CREATE TEMP TABLE` is `CREATE TEMPORARY TABLE`,
`DATE '1998-01-01'` is `'1998-01-01' :> DATE` on SingleStore and `DATE('1998-01-01')` on SQLite. The QuestDB
generator writes `CURRENT_TIMESTAMP` (without a precision) as `now()`.

The generators write two `EXTRACT` shapes that DuckDB, the source of NYC Taxi, reads natively. `EXTRACT(DOW FROM x)`
(Sunday is 0) is `(EXTRACT(DAYOFWEEK FROM x) - 1)` on BigQuery, `(DAYOFWEEK(x) - 1)` on Spark, Databricks and
SingleStore, and `CAST(STRFTIME('%w', x) AS INTEGER)` on SQLite. `EXTRACT(EPOCH FROM (a - b))` is
`(UNIX_SECONDS(TIMESTAMP(a)) - UNIX_SECONDS(TIMESTAMP(b)))` on BigQuery, `(UNIX_TIMESTAMP(a) - UNIX_TIMESTAMP(b))` on
Spark and Databricks, `(CAST(STRFTIME('%s', a) AS INTEGER) - CAST(STRFTIME('%s', b) AS INTEGER))` on SQLite (an operand
of a known non-text type is unsupported, as for the other SQLite extractions), `(DATEDIFF(second, b, a))` on Snowflake
and `TIMESTAMPDIFF(SECOND, b, a)` on SingleStore (MySQL's `UNIX_TIMESTAMP((a - b))` subtracts two datetimes as numbers,
which is not a number of seconds). `EXTRACT(EPOCH FROM x)` of a single value is unchanged. The shapes are found by
`sqlglot.benchbox.extract_parts`.

DuckDB's `ARRAY_DISTANCE` and `ARRAY_COSINE_SIMILARITY` are read as `EuclideanDistance` and the new
`CosineSimilarity` (both carry `array=True`, so DuckDB writes `ARRAY_DISTANCE` and `ARRAY_COSINE_SIMILARITY` back,
while `LIST_DISTANCE` stays `LIST_DISTANCE`). SingleStore writes `EUCLIDEAN_DISTANCE(a, b)` and
`DOT_PRODUCT(a, b) / NULLIF(SQRT(DOT_PRODUCT(a, a)) * SQRT(DOT_PRODUCT(b, b)), 0)` (parenthesized inside a larger
expression; a zero vector gives NULL). Other targets write `COSINE_SIMILARITY(a, b)`, which none of them has, as they did
`ARRAY_COSINE_SIMILARITY(a, b)` before.

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
own, except the file format of Spark, Databricks, Hive and Athena: a layout's `format` or `stored_as` replaces the
statement's own `USING` or `STORED AS` (`TestSparkFamily`), and a layout with neither keeps it. A layout is
applied to permanent `CREATE TABLE` statements only, after the other options, and reads the
statement's own primary key before anything is stripped.

| Dialect | Clauses written |
|---|---|
| ClickHouse | `ENGINE = <engine>`, `PARTITION BY (..)`, `ORDER BY (..)` from `sort_by`, else the statement's primary key, else `tuple()`, then `PRIMARY KEY (..)` after a given `sort_by` (the statement's own `PRIMARY KEY` is removed then), `SETTINGS`. `nullable_columns` decides which columns are `Nullable` (a key column listed there is a `ValueError`); without it SQLGlot's `Nullable` wrapping stays except on `NOT NULL` columns, which get a plain type. Fixed-size arrays (`REAL[3]`) become plain arrays. |
| Doris, StarRocks | `DUPLICATE`, `UNIQUE`, `AGGREGATE` or `PRIMARY KEY (..)` from `key_model` and `key_columns`, `PARTITION BY`, `DISTRIBUTED BY HASH(..) BUCKETS n` or `RANDOM`, StarRocks `ORDER BY (..)`, `PROPERTIES (..)`. `DUPLICATE` only sorts rows, so its key is the leading run of defined, keyable `key_columns` (else the first keyable column). `UNIQUE`, `AGGREGATE` and `PRIMARY` take `key_columns`, else the statement's primary key, and raise `ValueError` unless every key column is defined, keyable and among the leading columns of the table: a narrower key would merge or replace rows, so the key is never narrowed and the model never downgraded to `DUPLICATE`. A composite key such as TPC-H `lineitem`'s `(l_orderkey, l_linenumber)` therefore needs its columns first in the statement. Primary and foreign keys are removed, fixed-size arrays lose their size. `DORIS_TYPE_MAP` and `STARROCKS_TYPE_MAP` are the column type maps to pass as `type_map`. |
| Redshift | `DISTSTYLE KEY DISTKEY(..)`, `EVEN`, `ALL` or `AUTO`; `SORTKEY(..)` or `SORTKEY AUTO`; with `external`, `CREATE EXTERNAL TABLE ... STORED AS .. LOCATION .. TABLE PROPERTIES (..)` |
| Spark, Databricks | `USING <format>` (Delta, Iceberg, Parquet, ORC, Hudi) or `STORED AS`; `PARTITIONED BY (..)` (Iceberg transforms such as `bucket(8, id)` are written as given); `CLUSTER BY (..)` or `CLUSTER BY AUTO`; `CLUSTERED BY (..) [SORTED BY (..)] INTO n BUCKETS`; `LOCATION`; `TBLPROPERTIES` |
| Hive, Athena | `PARTITIONED BY (name type)` with the partition columns moved out of the column list, bucketing, `ROW FORMAT DELIMITED FIELDS/LINES TERMINATED BY`, `STORED AS`, `LOCATION`, `TBLPROPERTIES`; `external` writes `CREATE EXTERNAL TABLE`. With `if_not_exists`, `table_suffix`, `strip_not_null` and a `type_map` this is Athena's external and staging table |
| Trino, Presto | one `WITH (..)` property each for `format`, `partitioned_by` (`partitioning` when `format="iceberg"`), `bucketed_by` and `bucket_count`, `sorted_by`, `location` (`external_location` with `external`) and `properties`. Leave the layout off for a connector that rejects properties (the memory catalog) |
| T-SQL (Synapse, Fabric) | `WITH (DISTRIBUTION = ROUND_ROBIN \| REPLICATE \| HASH(..))`; the `schema` option writes the schema prefix Fabric Warehouse needs |
| PostgreSQL | `USING <format>` access method (`mooncake`); `mooncake_mirror_statements(table, staging_table, schema)` writes the two statements that promote a loaded heap table to a `pg_mooncake` mirror |
