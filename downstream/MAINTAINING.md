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

## Verified wheel delivery

`.github/workflows/benchbox-delivery.yml` (`downstream/delivery.py`) is the only
producer of a verified delivery. Jobs have only `contents: read`, `actions: read`
and `checks: read` permissions; Actions' artifact service uploads verified bundles.
It has no sleeping step, publishes no release, tag or package, and uses no state
branch, registry or database. It runs when any of the
three required push/`main` workflows (`BenchBox distribution`, `BenchBox real engines`,
`Run tests and linter checks`) completes, and on manual dispatch with `source_sha`.
Pending checks end the run quietly without a delivery; the last workflow to finish
delivers. A dispatch treats pending, tooling-only or missing as failure.

**Source.** The delivery source is the first-parent `main` commit that introduced the
manifest version, not a later tooling-only commit: the wheel provenance embeds the
source SHA, so rebuilding a tooling commit would give the same version a different
wheel. A trigger for a later same-version commit delivers nothing. The trusted
publisher commit (`delivery.commit`, the `main` tip running the workflow) is separate
from the source commit (`forkSha`) and is never the source of wheel bytes. v30.29's
release source is `6fd92da9333eb5c9920306237d8c4e4893012c60` even though `main` has since
moved on with the same version.

**Gate.** The producer reads the required status checks that apply to `main`, from
both rulesets and enforced classic branch protection. Every required context and
publisher constraint must be covered by successful jobs from the canonical workflows.
A new uncovered context, a different publisher, or unreadable protection refuses
delivery. At the source SHA each workflow must have a completed successful `push`
run on `main` of this repository (no pull request or synthetic merge), with a GitHub
Actions check suite at that SHA. Its attempt's jobs must exactly equal the jobs and
matrix its workflow file defines at that SHA, all `success`; skipped, missing, extra
or failed jobs refuse. A `workflow_run` trigger must match the discovered run id and
attempt. Every required run is read again just before delivery; a rerun, a new
attempt or a changed conclusion since the gate refuses delivery.
The unexpired canonical `benchbox-sqlglot` artifact is reused without rebuilding.
Its creation time must fall within the successful build upload step of that exact
attempt, not a previous attempt. It must belong to the run, SHA and `main`, match
its recorded zip digest, and contain exactly three flat regular files within size
bounds: wheel, sdist and `SHA256SUMS`. Checksums must agree, and wheel provenance must
match the source manifest, fork SHA, upstream SHA and clean state. Deeper wheel
verification stays in `downstream.verify`.

**One version, one wheel.** Before delivering, the producer lists every run of the
publisher workflow, whatever its current status or commit (main branch, publisher
path, this repository, trigger `workflow_run` or `workflow_dispatch`), and reads each
attempt's jobs, so neither a rerun nor an older publisher can miss a receipt. A successful
job named exactly
`delivered benchbox-sqlglot <version> sha256:<wheel sha256>` is the durable receipt,
which survives artifact and log expiry because job metadata is retained. A different
hash for a delivered version, or a delivered hash under another version, is refused;
a changed product needs a new fork version. A truncated, short-counted or unreadable
history is refused rather than assumed empty. All publisher runs share one
concurrency group that queues rather than replaces pending runs, so conflicting
deliveries cannot race and no completed release trigger is dropped. The `deliver`
job succeeds only after this passes; candidate code does not set its receipt name.

**Delivery.** Artifact `benchbox-sqlglot-delivery-a<run attempt>` of the publisher run
(retained 90 days) holds `delivery.json`, the wheel, sdist and `SHA256SUMS`.
`delivery.json` (`schema` 1, `kind` `benchbox-sqlglot-verified-delivery`) carries the
version, `forkSha`, `upstreamSha`, wheel filename/sha256/size, sdist, the source run id,
attempt and artifact id/name/digest, every required check run with its jobs, and the
delivery run id, attempt, trusted commit, receipt `jobName` and the
`api.github.com` run and jobs URLs. A consumer fixes the trusted repository, workflow
path, `main` ref and event itself and confirms the record by reading that run
(`path`, `head_branch`, `conclusion`) and the receipt job from the GitHub API; the
record cannot change its own trust root. Because the record carries the wheel bytes,
it stays verifiable after the original canonical artifact expires.

**Recovery.** An expired or missing canonical artifact, a missing run or an
unavailable history makes the publisher fail with an `::error::` naming the cause: a
fork-owned exception. Rerun the canonical distribution workflow at the same merged
`source` SHA, wait for the required checks to pass again, and the completion triggers
a new delivery attempt (or dispatch `source_sha`). The new record carries the same
version and hash with a new run attempt; an old receipt is never rebound. A different
hash is the conflict above.

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
permissions; only the upstream controller's `publish` job holds write capability,
through a GitHub App token, as `tests/test_downstream_contract.py` requires.

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

SQLite and DataFusion have no `PIVOT`, `UNPIVOT` or `ORDER BY ALL`, and the upstream generator wrote a query
with the pivot dropped (a `PIVOT` returned its unpivoted rows, an `UNPIVOT` returned the literal column names) and
`ORDER BY ALL DESC` read from a Netezza source as a sort on the constant `"ALL"`. `sqlglot.benchbox.pivot.lower_pivots`
now writes a `PIVOT` with one `SUM`, `MIN`, `MAX`, `AVG` or `COUNT` over literal `IN` values as conditional aggregation,
and an `UNPIVOT` of plain columns as a `UNION ALL` (`NULL` inputs excluded unless `INCLUDE NULLS`), both only over a
derived table whose select list has no `*` so the grouping columns are known; `expand_order_by_all` writes
`ORDER BY ALL` (the DuckDB keyword, or the unquoted column `ALL` that Netezza and PostgreSQL read) as one ordinal key
per selected column with the same direction and null placement. Any other pivot (a table or CTE source, several
aggregates, `GROUP BY`, `DISTINCT`, a subquery `IN` list, multi-column `UNPIVOT`) raises `UnsupportedError` from the
generator at every `unsupported_level`, so BenchBox records a failed query instead of a wrong result. The SQLite hook
is the carried patch `sqlite-pivot-lowering`; the module, the DataFusion generator hook and
`tests/test_benchbox_pivot.py`, which executes each shape on SQLite and DataFusion against DuckDB, are the `original`
entry `pivot-lowering`. PostgreSQL and the other generators that map `Pivot` to `no_pivot_sql` still drop it with a
warning.

`ansi` and `standard` are not dialects the fork provides, and the fork has no alias
dialects. The caller resolves them before calling SQLGlot: BenchBox's platform
registry maps `ansi` and `standard` (and every platform name) to a dialect that
exists. `Dialect.get_or_raise` raises `ValueError` for an unknown name, and the
fork never falls back to `postgres` silently.

## Upstream refresh

The maintainer reviews upstream promptly for correctness or security fixes.
`.github/workflows/benchbox-upstream.yml` (`downstream/controller.py`) owns the
whole upstream lifecycle in this repository: it runs daily and on
`workflow_dispatch`, discovers a newer final upstream tag, refreshes
deterministically, opens the fork pull request, merges it once the required checks
pass, and raises fork issues for failures. BenchBox core holds no fork credential
and no upstream logic. The controller never releases or submits upstream PRs;
wheel delivery stays with `benchbox-delivery.yml`, which runs after the merged
refresh's required workflows succeed.

Refresh itself is `python -m downstream.refresh auto --tag TAG --work-dir DIR
--report FILE` in a clean checkout of `main`. `auto` fetches the tag, builds the
candidate and the integration branch exactly as the steps below do (with no
retirements) and follows the upstream base version in the fixtures and docs. It
exits 0 with `status: refreshed`, or exits 3 with `status: conflict` (a carried
patch does not apply, or changes nothing because upstream may already contain it;
the report names it, its files and git's error) or `status: failed`. `failed`
covers any other stopped step, and it includes a refresh that would change the
active workflows under `.github/workflows/`, which `auto` leaves for a human to
read before anything runs them. In both failing cases it removes the worktrees
and branches it created. It refuses to start, removing nothing, when either
`refresh/candidate-X`, `refresh/upstream-X` or its work directory already
exists, so run it in a clean clone or after deleting your manual branches.

**Controller jobs.** `prepare` runs with `contents`, `pull-requests`, `issues`,
`checks` and `statuses` read-only, no secrets, on trusted `main`: it reads GitHub,
runs discovery and `auto`, and uploads `state.json` (the plan) and
`refresh.bundle`. It never has the App credential, so nothing it executes on
upstream-influenced data can write. `publish` runs only when the kill switch is on,
the run is not a dry run and the plan has actions. It checks out trusted `main`,
downloads the artifact, fails closed with a clear error when the App secrets are
missing, mints a GitHub App token with `actions/create-github-app-token` (pinned
to a full commit SHA), and applies the plan. Every action re-reads live state first
and refuses on a mismatch: main moved, branch moved, marker changed, the bundle's
head or tree differs from the plan, or the bundle changes workflows. The workflow
runs in one serialized group (`cancel-in-progress: false`, `queue: max`). The
workflow `GITHUB_TOKEN` stays read-only; the App token is the only write
capability, as `tests/test_downstream_contract.py` requires.

**Kill switch and dry run.** Nothing is mutated unless the repository variable
`BENCHBOX_UPSTREAM_ENABLED` is exactly `true` (default off). A manual dispatch
defaults to `dry_run: true`; scheduled runs are never dry runs. Dry runs, and runs
with the variable unset, still compute and print the plan in the job summary and
perform zero hosted mutations (no push, pull request, merge, issue or label
change). `retry: true` re-attempts an input that already failed.

**Credentials.** The App is installed on this repository only, with
contents, pull-requests and issues write and checks, statuses and actions read.
It must not be a bypass actor of any ruleset or branch protection. Configure the
secrets `BENCHBOX_UPSTREAM_APP_ID` and `BENCHBOX_UPSTREAM_APP_PRIVATE_KEY` and the
variable `BENCHBOX_UPSTREAM_ENABLED` in this repository. The controller never
changes rulesets, branch protection or auto-merge settings.

**Ownership.** A branch name or author is never authority. A pull request is the
controller's only when all of these hold: the head is on this repository, the body
carries exactly one `<!-- benchbox-upstream {...} -->` marker recording (tag, base
`main` SHA, head SHA, tree), the head SHA equals the recorded one (or the recorded
pending update during a re-refresh), and recomputing the deterministic refresh for
that tag and base `main` SHA yields the same tree and the same two parents as the
head. A branch with no pull request (a push that was interrupted before the pull
request opened) is adopted by the same tree and parent proof. Anything else, such
as a human commit on top, a draft, a closed pull request, a `no-auto-merge` or
`hold` label, or a foreign branch, is left untouched and reported; all but draft
and held also raise an issue.

**Reconciliation.** Each pass is derived only from GitHub refs, pull requests and
issues; there is no state branch or database. With no branch, the refresh is
pushed and the pull request opened. If the recorded base is no longer `main`, the
refresh is recomputed, the branch is lease-pushed in place and the marker updated
(a pending marker is written first so an interrupted update recovers). The pull
request merges only when it is open, not draft, not held, based on the current
`main`, every required check on `main` (rulesets and enforced classic protection,
read as `downstream/delivery.py` reads them) succeeded on the exact head from the
required publisher, and the merge state is clean. The merge uses the `merge`
method (preserving upstream ancestry), bound to the exact head SHA, with no bypass.
Pending or not yet reported checks wait for the next pass.

**Issues.** A conflict, failure or refusal opens or updates one fork issue per
tag, keyed by (tag, base `main` SHA, outcome) in a marker and authored by the App.
An unchanged failed input is skipped on later runs until the tag or `main`
changes, `retry` is set, or the issue is closed. A healthy pull request, an
up-to-date `main` or a successful refresh closes it.

**Recovery.** To hold a refresh add the `no-auto-merge` label or mark the pull
request draft. To resolve a conflict, refresh by hand with the steps below on a
branch with a different name, merge it, and the controller sees `main` current. To
discard a stuck refresh, close its pull request and delete its branch, close the
issue, and dispatch with `dry_run: false`. To pause everything, set
`BENCHBOX_UPSTREAM_ENABLED` to anything but `true`.

1. Fetch the chosen upstream tag and record its commit. Review upstream changes
   and every carried, upstreamed and excluded disposition. New capabilities
   need separate approved scope. Identify patches that upstream now replaces
   and retain their equivalence tests.
2. Create a disposable rebased source candidate. For example:

   ```sh
   git fetch upstream tag v30.29.0
   uv run --no-project -- python -m downstream.refresh candidate \
     --tag v30.29.0 --path ../sqlglot.refresh-candidate \
     --branch refresh/candidate-30.29.0
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
     --tag v30.29.0 --candidate refresh/candidate-30.29.0 \
     --path ../sqlglot.refresh-integration --branch refresh/upstream-30.29.0
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
