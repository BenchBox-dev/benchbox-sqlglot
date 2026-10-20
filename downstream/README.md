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
