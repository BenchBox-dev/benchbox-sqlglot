# Maintaining BenchBox SQLGlot

## Source and release boundaries

The canonical repository is `BenchBox-dev/benchbox-sqlglot`. BenchBox-dev owns
the fork; upstream remains `tobymao/sqlglot`. Keep local `origin` remotes pointed
at `https://github.com/BenchBox-dev/benchbox-sqlglot`.

`main` preserves upstream ancestry and the original `sqlglot/` source layout.
`downstream/patches.json` records the selected upstream tag and commit, every
prepared patch's disposition, and the normalized patches used for refreshes.
Normalized patches include the adaptations and regression tests present in the
fork. Their original source commits remain provenance, rather than being
replayed in place of those adaptations. The reconstruction test must reproduce
the current patch-owned source files exactly.

Use a linked worktree and a named branch. Inspect effective Git identity before
committing. Submit source and tooling changes through pull requests. Never
rewrite consumed `main` commits or immutable tags, including `benchbox-v30.23.0.1`.
The fork retains upstream's seven-Python source and compiled `make check` gates;
the private runtime wheel is pure Python and supports Python 3.11 through 3.14 in CI.

## Wheel-only shipping, builds and verification

BenchBox checks in and ships only the pinned, built pure-Python runtime wheel
inside its own published wheel and sdist. It must not check in or unpack the
fork's Python source tree. The private import namespace is
`benchbox._vendor.sqlglot`. There is no separately published `benchbox-sqlglot`
PyPI dependency, trusted publisher or production publication workflow.
Ordinary BenchBox installs and builds consume the packaged wheel bytes:
they never fetch this repository, a Git dependency or a custom index.

From a clean pinned fork checkout with the upstream tag available:

```sh
make benchbox-upstream-tag
uv run --no-project -- python -m downstream.build --out-dir /tmp/benchbox-sqlglot-dist
uv run --no-project -- python -m downstream.verify --dist-dir /tmp/benchbox-sqlglot-dist --full --sdist
```

The output directory must be empty. The builder rejects tracked and untracked
source changes, validates upstream ancestry, and writes one private wheel,
a verification-only sdist and `SHA256SUMS`. Only the wheel is copied into
BenchBox package data. No separate installation or PyPI publication is needed.
Its payload contains UTF-8 Python, static `_version.py`, `py.typed`, original
`LICENSE` and `benchbox_provenance.json`; no bytecode or compiled extensions.
Provenance records schema, distribution, version, upstream/fork commits, commit
timestamp, dirty status, complete patch manifest, canonical repository,
private namespace and SHA256 of every runtime file except provenance itself.

The BenchBox maintainer refresh operation invokes this builder in an explicitly
pinned clean checkout and copies the reviewed wheel, not its extracted files.
Record the exact wheel hash and fork commit with the consumer pin and notices.
Do not build the fork during a normal BenchBox package build.
`--allow-dirty` and verifier `--development` are explicit local diagnostics only;
never ship those artifacts.

BenchBox's real `benchbox._vendor` package extends only its own `__path__` with
the packaged wheel path plus `/benchbox/_vendor`. Standard `zipimport` loads
the private runtime directly from the nested wheel. Do not modify global
`sys.path`, extract source, run pip, install the wheel, or fetch dependencies at
runtime. Resources are read with `importlib.resources`, not filesystem paths.
The original fork sources remain solely in this repository.

Retain native source and compiled checks:

```sh
uv run --no-project --with-editable '.[dev]' -- make check NPROC=2
make benchbox-dist
make benchbox-check
```

`benchbox-check` verifies direct wheel zipimport, then runs the relocated
upstream suite from an installed private wheel outside the checkout and
rebuilds the unpacked verification sdist offline. The pinned setuptools and
wheel dependencies must first be cached; the direct build seeds them and CI
seeds them on its separate runner. Verification compares runtime payloads and
complete wheel bytes, and checks public SQLGlot coexistence, both import
orders, separate plugin groups, pickle/spawn, built-in dialects and both
uninstall directions. The candidate workflow retains artifacts for 30 days;
it cannot publish them. Source-only suite exclusions live in
`downstream/relocate.py`; SQL fixtures are unchanged. `SKIP_INTEGRATION=1`
matches upstream's `make unit` contract.

The assembler rewrites imports, enumerated dynamic import paths and doctests.
It preserves local bindings, logger names, SQL text and `sqlglot.meta` /
`sqlglot.anonymous` markers. New module-looking runtime strings fail assembly
until reviewed. Keep allowlists narrow. Never alias `sys.modules['sqlglot']`
or add an upstream fallback. Plugins must explicitly opt into the
`benchbox._vendor.sqlglot.dialects` entry-point group; upstream plugins are not
silently reused.

Before consumer adoption, test BenchBox's wrapper, platform mixin, base
benchmark, SQLite exact arithmetic, Q6 bounds, DDL and TPC-Havoc variants against
the packaged private wheel and installed BenchBox wheel and sdist. The fork
checks do not replace actual consumer packaging checks. Preserve
upstream notices. New semantic changes require separately approved scope.

## Real-engine tests and gap list

Translated SQL is proven by executing it on a real engine wherever one can be
obtained. `.github/workflows/benchbox-engines.yml` runs the engine tests on
in-process engines and service containers, `tests/engines.py` is the shared
helper, and `downstream/engines-requirements.txt` declares the packages (a new file, so
`setup.py` stays untouched and refreshes need no carried patch). The engine files
are `original` manifest entries. A new moved piece adds
its own execution tests with `requires_engine`. Platforms with no engine in CI,
and the evidence that substitutes for them, are listed in
[ENGINE-GAPS.md](ENGINE-GAPS.md); a piece that targets one of them records the
gap in its tracker item. Keep every workflow free of publishing steps and write
permissions, as `tests/test_downstream_contract.py` requires.

## Manifest entries

Every downstream change is a `downstream/patches.json` entry:

- `carried`: an edit to an upstream-owned file, including registrations and
  generator changes that reference fork-added code. It has a normalized patch
  under `downstream/patches/`, is replayed on each refresh and is committed as
  `fix: retain <ID>`. Any edit to a file that exists upstream is a carried patch,
  however small.
- `original`: wholly new files that do not exist at the recorded upstream
  commit (for example a new dialect module and its tests). It lists `paths` and
  has no normalized patch; `source` may be a fork commit. Refresh copies the
  files from fork `main` into the candidate as a `feat: retain <ID>` commit
  before later entries are replayed, so carried patches may `depends` on it.
  `original` never covers a file that exists upstream. If an upstream tag adds
  one of an original entry's paths, `candidate` stops for manual reconciliation.
- `upstreamed` and `excluded` are dispositions only and own no paths.

A path belongs to exactly one entry. The contract test checks this, that
original paths are absent upstream, and that every file that differs from the
recorded upstream commit is owned by an entry, is a normalized patch under
`downstream/patches/`, or is listed in the top-level `infrastructure` array.

`infrastructure` entries (`id`, `paths`, optional `removed`, `reason`) cover fork
maintenance files and fork edits to upstream build and CI files that are not
replayed patches: they survive a refresh through the merge of upstream ancestry,
and a conflict in one stops the refresh for manual resolution. A new such file
or edit needs an entry in the same change.

Every `carried` entry records where it stands with upstream:
`upstream_state` is `not_offered` (no upstream PR yet), `offered` (an upstream
PR or issue exists; `upstream` lists its links and the reason says where it
stands) or `declined` (the fork keeps it by design and does not intend to
offer it, or upstream refused it; a refusal lists its issue or pull request in
`upstream`), with a
non-empty `upstream_reason`. Upstream links must be `tobymao/sqlglot` pull
request or issue URLs. A retired patch becomes `upstreamed` and loses these
fields; `upstreamed` and `excluded` entries keep their `upstream` links.

## The `sqlglot.benchbox` package

`sqlglot/benchbox/` holds engine-neutral expression rewrites that BenchBox calls
with its own gates (column names, table names) as arguments. The package and its
tests are `original` manifest entries because none of the files exist upstream.
A rewrite takes and returns an expression (`copy=True` by default), so the caller
chooses the read and write dialects, and it never names a benchmark, table or
column. Behavior that belongs to one dialect's output (for example the SQLite
date lowering) is a generator change and a carried patch instead.

Adapter rewrites in the same module follow the same rule: `safe_division` is opt-in and
never default, `deduplicate_output_aliases` and `flag_comparisons_to_boolean` take their
gates (the flag column names) as arguments, and `unqualified_tables` only finds the nodes;
the dataset or project that qualifies them stays with the caller. The Databricks generator
drops the ordering of `PRIMARY KEY` columns (Databricks accepts neither `ASC`/`DESC` nor
`NULLS FIRST`/`LAST` there); that edit to an upstream-owned generator is the carried patch
`databricks-primary-key-ordering`. `tests/test_benchbox_adapter_rewrites_engines.py` proves
each rewrite on the engines listed in ENGINE-GAPS.md.

`sqlglot/benchbox/ddl.py` follows the same rule for DDL: `DdlOptions` and `SingleStoreKeys` are frozen
dataclasses, table names, key columns and type names arrive as arguments, and no table of a benchmark
exists in the fork. The module and `tests/test_benchbox_ddl.py` are the `original` entry `benchbox-ddl`.
`sqlglot/benchbox/layout.py` (`TableLayout`) and `sqlglot/benchbox/ddl_engines.py` (the clause writers) hold
engine-specific DDL: ClickHouse, Doris, StarRocks, Redshift, Spark and Databricks, Hive and Athena, Trino and
Presto, Synapse and Fabric, and PostgreSQL access methods, with `tests/test_benchbox_ddl_engines.py` and
`tests/test_benchbox_ddl_lakehouse.py`; they are the `original` entries `benchbox-ddl-engines` and
`benchbox-ddl-lakehouse`. A dialect is added to `_family` with a builder that returns clause groups in the
target's syntax. BigQuery key enforcement and qualification are not part of it.

Engine rewrites (`sqlglot/benchbox/engine_rewrites.py`, an `original` file with its tests in the
`engine-rewrites` entry) follow the same rule: nothing in them names a benchmark, a query or a table.
The ClickHouse pipeline BenchBox runs after generation is `lowercase_identifiers`,
`decimal_zero_literals`, `safe_division(..., "clickhouse")` and `add_query_settings`; the TPC-DS
Q10/Q35 override is `correlated_exists_to_in` with `expected` set, and the Q58 ambiguity fix is
`qualify_order_by_columns` with the column and source passed in. Which query gets which rewrite, the
identifier patterns and the settings values are BenchBox data. `tests/test_engine_rewrites_execution.py`
runs them on chDB, DuckDB, StarRocks and Doris.

`sqlglot.benchbox.compat` follows the same rules for the engine-compatibility rewrites (FILTER removal,
alias inlining, column qualification, one-row sources for FROM-less selects, literal-subquery naming, grouped
scalar subqueries, ordered list aggregates). Behavior that belongs to one dialect's output is generator behavior and
a carried patch: `empty-group-by-generator` (a `SUPPORTS_EMPTY_GROUP_BY` flag in the base generator, off for
Snowflake and Spark; `sqlite-empty-group-by` turns it off for SQLite). The DataFusion generator is a new file, so it needs no patch. `tests/test_benchbox_compat_engines.py` proves each piece on the engines in ENGINE-GAPS.md.
BigQuery rejects a non-default `NULLS FIRST`/`LAST` in the window `ORDER BY` of `FIRST_VALUE`, `LAST_VALUE` and `NTH_VALUE`
unless the frame is an explicit `ROWS` frame; the BigQuery generator drops the clause when the key is also a `PARTITION BY`
key and otherwise sorts on `key IS NULL` first. That is the carried patch `bigquery-window-nulls-ordering`; a query's own
`ORDER BY` keeps its clause.

`sqlglot.benchbox.subqueries` is the same kind of module for subquery rewrites: nullability of columns and the schema
arrive as arguments, no table or query of a benchmark exists in the fork, and the module and its two test
files are the `original` entry `benchbox-subqueries`. `decorrelate_subqueries` reuses `decorrelate` and `unnest`
from `sqlglot.optimizer.unnest_subqueries` and replaces only that module's driver loop, so a refresh that changes
their signatures fails at import; keep the loop in step with `unnest_subqueries`.

## Platform dialects

The fork adds `netezza`, `greenplum`, `vertica`, `datafusion` and `questdb`, each a
subclass of `postgres` (upstream has no dialect with these names). A dialect starts
as a thin subclass and gains behavior only when it is proved against the engine.
Registration (the `Dialects` enum and `DIALECTS`) edits
upstream-owned files and is the carried patch `platform-dialects`; the dialect
modules and their tests are the `original` entry `platform-dialect-modules`.

The QuestDB generator (`sqlglot/generators/questdb.py`, an `original` file) writes the
constructs QuestDB rejects in a form it runs: comma joins become explicit joins,
`x +/- INTERVAL 'n' DAY|MONTH|YEAR` becomes `DATEADD('d'|'M'|'y', +/-n, x)`,
`SUBSTRING(x FROM a [FOR b])` becomes `SUBSTRING(x, a, b)` (the length of `x` when no
length is given) and a CTE column list moves into the query's aliases. Each is proved
by `tests/test_questdb_execution.py` on the `questdb` job's container. Correlated
`[NOT] EXISTS` and `IN (subquery)` conjuncts of a WHERE become joins to derived tables
(distinct keys, or per-key `MIN`/`MAX` for one `<>` correlation), proved against DuckDB
by `tests/test_questdb_subquery_lowering_execution.py`.

The Netezza parser (`sqlglot/parsers/netezza.py`, an `original` file) reads `DATE('now' ...)`
and `JULIANDAY` into portable date nodes, so no target generator needs to know the SQLite
spelling: `JULIANDAY(a) - JULIANDAY(b)` is recognized at the subtraction, and a lone
`JULIANDAY(x)` is `DATEDIFF(DAY, DATE '1970-01-01', x) + 2440588`. The SQLite generator needs two
fixes to write those nodes back (`DATEDIFF` in `DAY` units, and `DATE_SUB`), carried as
`sqlite-date-day-arithmetic`. The Snowflake `REGEXP_REPLACE` group renumbering is the carried patch
`snowflake-regexp-replace-groups`. `tests/test_sqlite_idioms_execution.py` and
`tests/test_regexp_replace_execution.py` execute them.

A whole-day offset of a DATE is written as `DATE(x, 'n DAY')`, not `DATETIME(...)`: SQLite stores
dates as text and `'1998-03-09 00:00:00'` sorts after `'1998-03-09'`, so a `DATETIME` bound dropped
the boundary day of `d BETWEEN <date> - INTERVAL '30' DAY AND ...` (TPC-DS Q21 and Q40). That is the
carried patch `sqlite-day-offset-date`; hour, minute and second offsets keep `DATETIME`.

SQLite divides two INTEGER values with truncation, and a column that looks decimal can hold INTEGER storage, so
no schema-free rule can prove a dividend is not an integer. A division the source marks as possibly truncating
(`typed`: PostgreSQL and Netezza) is therefore written with `CAST(dividend AS REAL)` unless an
operand is already REAL (a decimal literal, a CAST to a real type, `AVG`, arithmetic or a division involving
one) or both operands are integer by syntax (integer literals, CASTs to an integer type, arithmetic over
them), which keep the source's truncating division: `2020 / 3` from PostgreSQL stays 673. That is the
carried patch `sqlite-exact-integer-division` (TPC-DS Q21's `inv_after / inv_before`
is two integer SUMs); `tests/dialects/test_sqlite.py` executes the Q21 shape on SQLite and DuckDB.

Every other target whose `/` truncates integers (the dialects with `TYPED_DIVISION`: PostgreSQL and its family, DataFusion,
Redshift, T-SQL, Fabric, Presto, Trino, Athena, Teradata, Drill and Dremio) needs the same exact quotient for a
Netezza-sourced benchmark query. The Netezza parser marks every `/` it reads (`sqlglot/benchbox/division.py`, `original`
entry `netezza-exact-division`); the base generator's `div_sql` (carried patch `postgres-exact-division`, which also owns the
PostgreSQL and Redshift edits) writes the dividend of a marked division as `CAST(x AS <EXACT_DIVISION_TYPE>)` when the target
dialect has `TYPED_DIVISION`. `EXACT_DIVISION_TYPE` is `DOUBLE` (`FLOAT` on T-SQL), except `DECIMAL` on PostgreSQL,
Greenplum, Materialize and RisingWave, where it is an unconstrained `NUMERIC`; Redshift stays `DOUBLE` because its bare
`DECIMAL` is `DECIMAL(18,0)`. The SQLite rule decides which operands need no cast (decimal literals, REAL casts, `AVG`,
integer-literal-only quotients, which keep source semantics). Only Netezza sources are marked: SQL read as PostgreSQL,
DataFusion, T-SQL and so on is unchanged, and Netezza and Vertica output (`EXACT_MARKED_DIVISION = False`; Vertica's `/`
already returns a `NUMERIC`) keeps its own `/`. Targets whose `/` is already exact (DuckDB, Spark, Snowflake, ClickHouse,
BigQuery, MySQL family, Oracle) write the source's `/` unchanged. `tests/test_netezza_exact_division_engines.py` executes the
TPC-DS Q21, Q78 and Q83 shapes; `downstream/ENGINE-GAPS.md` lists which targets are executed and which are asserted as text.

SQLite rejects a parenthesised operand of `UNION`, `INTERSECT` and `EXCEPT`, so a generator with
`SET_OP_PARENTHESIZED_OPERANDS` off writes `(SELECT ...) EXCEPT (SELECT ...)` as
`SELECT * FROM (SELECT ...) EXCEPT SELECT * FROM (SELECT ...)` (TPC-DS Q87). That is the carried patch
`sqlite-set-operation-operands`; `tests/dialects/test_sqlite.py` executes the shape on SQLite and DuckDB. SQLite
also evaluates a compound select left to right with no operator precedence, and SQLGlot's parser reads an
unparenthesised `a UNION b INTERSECT c` left to right too (INTERSECT does not bind tighter, as in PostgreSQL and
DuckDB). The fork leaves that upstream parser behavior alone; no TPC-H or TPC-DS query mixes the operators
without parentheses.

SQLite has no `ROLLUP`, `CUBE`, `GROUPING SETS` or `GROUPING()`, and no `STDDEV_SAMP`. The SQLite generator
runs `sqlglot.benchbox.grouping_sets.expand_grouping_sets` before it writes SQL: each grouping set becomes one
`GROUP BY` select in a `UNION ALL`, the grouped expressions outside the set read `NULL`, `GROUPING(a, b)`
becomes its integer (the leftmost argument is the high bit) and the empty set drops its `GROUP BY` so it
returns the one grand-total row even on empty input. Window functions, `ORDER BY`, `LIMIT`, `OFFSET` and
`DISTINCT` run over the union in an outer select (TPC-DS Q36, Q70 and Q86 rank over `GROUPING()` and
aggregates; the outer select reads them as hidden columns). A shape the rewrite cannot prove equivalent keeps
its grouping sets and fails loudly on SQLite: a select with `QUALIFY` or named windows, a column that is neither
grouped nor aggregated, an empty set with no aggregate (it would return every row, not one), a window in
`ORDER BY`, and `GROUP BY ALL` or `WITH TOTALS`. `sqlglot.benchbox.moments.expand_moments` writes
`STDDEV`, `STDDEV_SAMP`, `STDDEV_POP`, `VARIANCE` and `VAR_POP` as
`(n * SUM(x*x) - SUM(x)^2) / (n * (n - 1))` (`n * n` for the population forms) over `CAST(x AS REAL)` with
`SQRT(MAX(0.0, ...))`; groups with fewer than two rows divide by zero, which SQLite answers with NULL, as
`STDDEV_SAMP` does. DISTINCT, FILTER and OVER forms warn as unsupported, and `SQRT` needs SQLite built with
`SQLITE_ENABLE_MATH_FUNCTIONS` (the shell enables it by default from 3.35; `SELECT SQRT(4)` tells a build). The generator hook is
the carried patch `sqlite-grouping-sets`; the modules and `tests/test_benchbox_grouping_sets.py`, which
executes the Q27, Q36 and Q67 shapes and the variances on SQLite against DuckDB, are the `original` entry
`sqlite-grouping-sets-modules`.

`ansi` and `standard` are not dialects the fork provides, and the fork has no alias
dialects. The caller resolves them before calling SQLGlot: BenchBox's platform
registry maps `ansi` and `standard` (and every platform name) to a dialect that
exists. `Dialect.get_or_raise` raises `ValueError` for an unknown name, and the
fork never falls back to `postgres` silently.

## Upstream refresh

The maintainer reviews upstream monthly and promptly for correctness or
security fixes. The scheduled workflow reports newer tags with read-only
permissions. It never creates a release, changes refs or submits upstream PRs.

1. Fetch the chosen upstream tag and record its commit. Review upstream changes
   and every carried, upstreamed and excluded disposition. New capabilities
   need separate approved scope. Identify patches that upstream now replaces
   and retain their equivalence tests.
2. Create a disposable rebased source candidate. For example:

   ```sh
   git fetch upstream tag v30.25.0
   uv run --no-project -- python -m downstream.refresh candidate \
     --tag v30.25.0 --path ../sqlglot.refresh-candidate \
     --branch refresh/candidate-30.25.0
   ```

   Repeat `--retire PATCH_ID` for reviewed retirements (carried patches only).
   The helper materializes `original` files and applies the normalized carried
   patches in manifest order, one commit per entry, and stops on conflicts. Resolve
   conflicts explicitly, preserve one `fix: retain PATCH_ID` (carried) or
   `feat: retain ID` (original) commit per retained entry, apply the remaining patches in order, and review all adaptations.
   Never substitute an older original patch for the normalized patch. Run the
   source and compiled checks on this candidate.
3. Create a descendant integration branch from current fork `main`:

   ```sh
   uv run --no-project -- python -m downstream.refresh integrate \
     --tag v30.25.0 --candidate refresh/candidate-30.25.0 \
     --path ../sqlglot.refresh-integration --branch refresh/upstream-30.25.0
   ```

   For each retired patch, repeat `--retire 'PATCH_ID=reviewed upstream evidence
   and equivalence result'`. This merges upstream ancestry, then reconciles
   the union of previously carried and original source paths to the verified
   candidate.
   That union includes retired paths so old implementations cannot reappear.
   Unrelated conflicts stop for explicit resolution. The command updates the
   manifest and normalized patches and commits the integration result.
4. In the integration worktree, check the manifest, exact candidate-owned source
   tree, inherited workflow safety and excluded patch decisions. Run source,
   compiled, installed-artifact, reproducibility and prospective consumer
   checks. Record conflict count, test runtime and actual maintainer effort.
   Submit and merge the reviewed PR before preparing its release candidate.

The local Git fixture exercises two refresh cycles and an upstream replacement
that retires a carried patch. Final source must match the candidate on every
previously patch-owned path. Keep normal merge ancestry; merging a rebased
series alone onto an old fork history can resurrect a retired patch.

An empty carried patch set and passing upstream equivalence tests trigger an
owner decision about retiring the fork. Namespace migration is a separate
consumer change. Previously declined patches do not require new upstream PRs.

## Rollback

For initial adoption, revert the complete BenchBox adoption PR to restore its
public SQLGlot dependency, lockfile and imports; test that restoration in
isolation. Subsequently restore the previous tested wheel together
with its matching fork pin, wheel hash, provenance, notices and consumer lockfile. Rebuild
and exercise the BenchBox wheel and sdist before merging rollback. Never move
an immutable fork tag or replace runtime files without their provenance.
