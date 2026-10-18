# Maintaining BenchBox SQLGlot

## Source and release boundaries

`main` preserves upstream ancestry and the original `sqlglot/` source layout.
`downstream/patches.json` records the selected upstream tag and commit, every
prepared patch's disposition, and the normalized patches used for refreshes.
Normalized patches include the adaptations and regression tests present in the
fork. Their original source commits remain provenance, rather than being
replayed in place of those adaptations. The reconstruction test must reproduce
the current patch-owned source files exactly.

Use a linked worktree and a named branch. Inspect effective Git identity before
committing. Submit source and tooling changes through pull requests. Never
rewrite consumed `main` commits or published tags. The fork retains upstream's
source and compiled `make check` gates; the downstream distribution is pure
Python and supports Python 3.11 through 3.14 in CI.

## Build and verification

From a clean checkout with the full upstream tags available:

```sh
uv run --no-project --with-editable '.[dev]' -- make check NPROC=2
make benchbox-upstream-tag
make benchbox-dist
make benchbox-check
```

`benchbox-dist` produces a wheel, a self-contained sdist and `SHA256SUMS` under
`dist/benchbox`. It requires a clean tree, validates upstream ancestry and
generates static metadata. `--allow-dirty` is for local development only;
artifact verification and publication reject those artifacts by default.

`benchbox-check` runs the full relocated upstream unit suite from a fresh
installed wheel outside the checkout, then rebuilds the unpacked sdist offline.
The pinned setuptools and wheel dependencies must first be cached; the direct
build seeds them. CI seeds them explicitly on its separate verification runner.
It compares runtime payloads and complete wheel bytes, checks transitive
upstream installation, both import orders, plugin groups, pickle/spawn, all
built-in dialects and both uninstall directions. `downstream/relocate.py` lists
the source-only suite exclusions and their reasons. SQL fixtures are copied
unchanged. The standard unit mode skips the upstream executor integration
cases through `SKIP_INTEGRATION=1`, matching upstream's `make unit` contract.

The assembler rewrites Python imports, the enumerated dynamic import paths and
doctest imports. It preserves local bindings, logger names, SQL text and
`sqlglot.meta` / `sqlglot.anonymous` SQL markers. New module-looking runtime
strings fail assembly until reviewed. Keep retained-string and dynamic-path
allowlists narrow. Do not alias `sys.modules['sqlglot']` or add an upstream
fallback. The generated distribution has no optional dependencies or console
entry points and includes `py.typed`, original licensing and provenance.

Before a release, test BenchBox's wrapper, platform mixin, base benchmark,
SQLite exact arithmetic and TPC-Havoc BigQuery behavior against the built wheel
in a disposable copy of merged BenchBox. Apply the planned import migration in
that copy. A new semantic regression requires an explicit scope decision;
do not silently add capabilities or change the reviewed upstream base.

## Publication

Only the explicit `Publish BenchBox SQLGlot` workflow can publish. An ordinary
upstream `v*` tag cannot publish. Inherited mutating workflows remain preserved
under `.github/disabled-workflows` for provenance and stay inactive.

The owner must first approve the exact version, source commit and both artifact
SHA256 hashes. Configure PyPI trusted publishing for `benchbox-sqlglot`, this
repository's `benchbox-release.yml` workflow and the
`benchbox-sqlglot-pypi` environment. That environment must have required human
reviewers. Name ownership, environment changes and publishing configuration
remain owner actions unless explicitly authorized. The workflow refuses missing
reviewers and a candidate that is not a successful `main` push run of the
downstream CI workflow on the exact tagged commit.

Download `benchbox-sqlglot` from that successful run and review `SHA256SUMS`.
Create an immutable `benchbox-vX.Y.Z.N` tag at its source commit only after
approval. Dispatch publication with that tag, the successful candidate run ID
and both approved hashes. The workflow downloads and verifies those exact
artifacts, reruns artifact checks, and waits for environment approval. It does
not rebuild different bytes for upload. Artifacts are retained for 30 days;
expired or changed artifacts require a new reviewable candidate and approval.
No credential or branch-protection bypass is part of this procedure.

After publication, install the exact version from public PyPI in a fresh
environment, verify its provenance, hashes and coexistence behavior, then
update BenchBox with an exact version and its regenerated lockfile. A local
path or Git override is not a substitute for that install proof.

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

   Repeat `--retire PATCH_ID` for reviewed retirements. The helper applies the
   normalized carried patches in manifest order and stops on conflicts. Resolve
   conflicts explicitly, preserve one `fix: retain PATCH_ID` commit per retained
   patch, apply the remaining patches in order, and review all adaptations.
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
   the union of previously carried source paths to the verified candidate.
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

For the initial adoption, revert the complete BenchBox adoption PR, restoring
its public SQLGlot 30.18.0 dependency, lockfile and imports. Test that restoration
in isolation before adoption merges. Subsequent rollback selects the previous
tested fork version and its matching BenchBox dependency/lock change. Never
replace or delete a published release to implement rollback.
