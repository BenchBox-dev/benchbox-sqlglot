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
helper, and the `engines` extra declares the packages. A new moved piece adds
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

A path belongs to exactly one entry. The contract test checks this and that
original paths are absent upstream.

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
   git fetch upstream tag v30.24.0
   uv run --no-project -- python -m downstream.refresh candidate \
     --tag v30.24.0 --path ../sqlglot.refresh-candidate \
     --branch refresh/candidate-30.24.0
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
     --tag v30.24.0 --candidate refresh/candidate-30.24.0 \
     --path ../sqlglot.refresh-integration --branch refresh/upstream-30.24.0
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
