# BenchBox SQLGlot

`benchbox-sqlglot` is a BenchBox maintained distribution of
[SQLGlot](https://github.com/tobymao/sqlglot), licensed under the MIT license.
It provides a small, reviewed patch set for SQL used by BenchBox.

```python
import benchbox_sqlglot

benchbox_sqlglot.transpile("SELECT 1", read="postgres", write="sqlite")
```

The import namespace is `benchbox_sqlglot`. Public SQLGlot can coexist as
`sqlglot`; their expression and dialect classes are distinct. Exchange SQL text
between them, rather than passing expression objects across namespaces.
Third-party dialect plugins use the `benchbox_sqlglot.dialects` entry-point
group. Upstream plugins must explicitly support that group before use here.

This distribution supports Python 3.11 and later and initially ships pure
Python only. It has no `c` or `rs` extra or console script. Run the module with
`python -m benchbox_sqlglot`. SQL comment markers such as `sqlglot.meta` remain
SQLGlot syntax and keep their original spelling.

Versions use `X.Y.Z.N`: the first three components identify the upstream tag;
the positive fourth component increments for downstream releases and resets
to one when the upstream base changes. Published versions and tags are immutable.

Read `benchbox_sqlglot/benchbox_provenance.json` with
`importlib.resources.files("benchbox_sqlglot")` for the exact upstream and fork
commits, build timestamp and patch manifest. The source repository records the selected upstream base and patch decisions
in `downstream/patches.json`.
