import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from downstream import refresh
from downstream.build import git
from downstream.refresh import auto, blank_only_hunks, candidate, integrate, probe

BASE_RUNTIME = "VALUE = 'upstream'\nMIDDLE = 1\nOTHER = 'upstream'\n"
DOWNSTREAM_RUNTIME = "VALUE = 'downstream'\nMIDDLE = 1\nOTHER = 'upstream'\n"
LONG_RUNTIME = "".join(f"LINE_{i} = {i}\n" for i in range(14))


class TestDownstreamRefresh(unittest.TestCase):
    def command(self, root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL)

    def commit(self, root, paths, message):
        self.command(root, "add", "--", *paths)
        self.command(root, "commit", "-qm", message)

    def test_two_refreshes_do_not_resurrect_retired_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "source"
            root.mkdir()
            self.command(root, "init", "-q", "-b", "main")
            self.command(root, "config", "user.name", "Test Fixture")
            self.command(root, "config", "user.email", "fixture@example.invalid")
            source = root / "runtime.py"
            source.write_text("VALUE = 'upstream'\n")
            self.commit(root, ["runtime.py"], "Record upstream runtime")
            self.command(root, "tag", "v1.0.0")
            base = git(root, "rev-parse", "HEAD")
            source.write_text("VALUE = 'downstream'\n")
            patch = subprocess.check_output(["git", "-C", str(root), "diff", "--binary"])
            self.commit(root, ["runtime.py"], "Preserve downstream behavior")
            downstream = root / "downstream"
            downstream.mkdir()
            (downstream / "runtime.patch").write_bytes(patch)
            data = {
                "schema": 1,
                "version": "1.0.0.1",
                "upstream": {"tag": "v1.0.0", "sha": base},
                "patches": [
                    {
                        "id": "runtime",
                        "status": "carried",
                        "paths": ["runtime.py"],
                        "patch": "downstream/runtime.patch",
                        "upstream_state": "not_offered",
                        "upstream_reason": "Fixture patch",
                    }
                ],
            }
            (downstream / "patches.json").write_text(json.dumps(data))
            self.commit(
                root,
                ["downstream/runtime.patch", "downstream/patches.json"],
                "Record downstream patch",
            )
            self.command(root, "switch", "-qc", "upstream", "v1.0.0")
            (root / "other.py").write_text("OTHER = 1\n")
            self.commit(root, ["other.py"], "Extend upstream runtime")
            self.command(root, "tag", "v1.1.0")
            self.command(root, "switch", "-q", "main")
            first = directory / "candidate-one"
            candidate(root, "v1.1.0", first, "candidate-one", set())
            integration = directory / "integration-one"
            integrate(root, "v1.1.0", "candidate-one", integration, "integration-one", {})
            self.assertEqual(
                (integration / "runtime.py").read_bytes(), (first / "runtime.py").read_bytes()
            )
            self.command(root, "merge", "--ff-only", "integration-one")
            self.command(root, "switch", "-q", "upstream")
            source.write_text("VALUE = 'upstream replacement'\n")
            self.commit(root, ["runtime.py"], "Replace runtime with upstream fix")
            self.command(root, "tag", "v1.2.0")
            self.command(root, "switch", "-q", "main")
            second = directory / "candidate-two"
            candidate(root, "v1.2.0", second, "candidate-two", {"runtime"})
            final = directory / "integration-two"
            integrate(
                root,
                "v1.2.0",
                "candidate-two",
                final,
                "integration-two",
                {"runtime": "Upstream replacement passes equivalent behavior checks"},
            )
            self.assertEqual((final / "runtime.py").read_text(), "VALUE = 'upstream replacement'\n")
            self.assertEqual(
                (final / "runtime.py").read_bytes(), (second / "runtime.py").read_bytes()
            )
            retired = json.loads((final / "downstream/patches.json").read_text())["patches"][0]
            self.assertEqual(retired["status"], "upstreamed")
            self.assertNotIn("upstream_state", retired)
            self.assertNotIn("upstream_reason", retired)
            self.command(final, "merge-base", "--is-ancestor", base, "HEAD")

    def test_original_files_survive_refresh_with_carried_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "source"
            root.mkdir()
            self.command(root, "init", "-q", "-b", "main")
            self.command(root, "config", "user.name", "Test Fixture")
            self.command(root, "config", "user.email", "fixture@example.invalid")
            (root / "registry.py").write_text("NAMES = ['base']\n")
            self.commit(root, ["registry.py"], "Record upstream registry")
            self.command(root, "tag", "v1.0.0")
            base = git(root, "rev-parse", "HEAD")
            (root / "registry.py").write_text("NAMES = ['base', 'feature']\n")
            patch = subprocess.check_output(["git", "-C", str(root), "diff", "--binary"])
            (root / "feature.py").write_text("FEATURE = 1\n")
            self.commit(root, ["registry.py", "feature.py"], "Add feature")
            downstream = root / "downstream"
            downstream.mkdir()
            (downstream / "registry.patch").write_bytes(patch)
            data = {
                "schema": 1,
                "version": "1.0.0.1",
                "upstream": {"tag": "v1.0.0", "sha": base},
                "patches": [
                    {
                        "id": "feature-module",
                        "status": "original",
                        "depends": [],
                        "paths": ["feature.py"],
                    },
                    {
                        "id": "feature-registry",
                        "status": "carried",
                        "depends": ["feature-module"],
                        "paths": ["registry.py"],
                        "patch": "downstream/registry.patch",
                    },
                ],
            }
            (downstream / "patches.json").write_text(json.dumps(data))
            self.commit(
                root,
                ["downstream/registry.patch", "downstream/patches.json"],
                "Record downstream entries",
            )
            self.command(root, "switch", "-qc", "upstream", "v1.0.0")
            (root / "other.py").write_text("OTHER = 1\n")
            self.commit(root, ["other.py"], "Extend upstream runtime")
            self.command(root, "tag", "v1.1.0")
            self.command(root, "switch", "-q", "main")
            first = directory / "candidate"
            candidate(root, "v1.1.0", first, "candidate", set())
            self.assertEqual((first / "feature.py").read_text(), "FEATURE = 1\n")
            self.assertEqual((first / "registry.py").read_text(), "NAMES = ['base', 'feature']\n")
            self.assertEqual(
                git(first, "log", "--format=%s", "v1.1.0..HEAD").splitlines(),
                ["fix: retain feature-registry", "feat: retain feature-module"],
            )
            integration = directory / "integration"
            integrate(root, "v1.1.0", "candidate", integration, "integration", {})
            for name in ("feature.py", "registry.py", "other.py"):
                self.assertTrue((integration / name).is_file(), name)
            self.assertEqual(
                (integration / "feature.py").read_bytes(), (root / "feature.py").read_bytes()
            )
            self.assertEqual(
                (integration / "registry.py").read_bytes(), (first / "registry.py").read_bytes()
            )
            refreshed = json.loads((integration / "downstream/patches.json").read_text())
            self.assertEqual([p["status"] for p in refreshed["patches"]], ["original", "carried"])
            self.assertEqual(refreshed["upstream"]["tag"], "v1.1.0")
            self.assertEqual((integration / "downstream/registry.patch").read_bytes(), patch)
            self.command(integration, "merge-base", "--is-ancestor", "v1.1.0", "HEAD")

            self.command(root, "switch", "-q", "upstream")
            (root / "feature.py").write_text("FEATURE = 'upstream'\n")
            self.commit(root, ["feature.py"], "Upstream adds the same path")
            self.command(root, "tag", "v1.2.0")
            self.command(root, "switch", "-q", "main")
            with self.assertRaises(ValueError):
                candidate(root, "v1.2.0", directory / "candidate-two", "candidate-two", set())

    def test_original_entries_sharing_a_path_each_keep_a_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "source"
            root.mkdir()
            self.command(root, "init", "-q", "-b", "main")
            self.command(root, "config", "user.name", "Test Fixture")
            self.command(root, "config", "user.email", "fixture@example.invalid")
            (root / "base.py").write_text("BASE = 1\n")
            self.commit(root, ["base.py"], "Record upstream base")
            self.command(root, "tag", "v1.0.0")
            base = git(root, "rev-parse", "HEAD")
            (root / "shared.py").write_text("SHARED = 1\n")
            self.commit(root, ["shared.py"], "Add shared module")
            downstream = root / "downstream"
            downstream.mkdir()
            data = {
                "schema": 1,
                "version": "1.0.0.1",
                "upstream": {"tag": "v1.0.0", "sha": base},
                "patches": [
                    {"id": "first", "status": "original", "depends": [], "paths": ["shared.py"]},
                    {"id": "second", "status": "original", "depends": [], "paths": ["shared.py"]},
                ],
            }
            (downstream / "patches.json").write_text(json.dumps(data))
            self.commit(root, ["downstream/patches.json"], "Record downstream entries")
            self.command(root, "switch", "-qc", "upstream", "v1.0.0")
            (root / "other.py").write_text("OTHER = 1\n")
            self.commit(root, ["other.py"], "Extend upstream runtime")
            self.command(root, "tag", "v1.1.0")
            self.command(root, "switch", "-q", "main")
            first = directory / "candidate"
            candidate(root, "v1.1.0", first, "candidate", set())
            self.assertEqual(
                git(first, "log", "--format=%s", "v1.1.0..HEAD").splitlines(),
                ["feat: retain second", "feat: retain first"],
            )
            integrate(root, "v1.1.0", "candidate", directory / "integration", "integration", {})

    def automatic_fixture(
        self,
        directory,
        upstream_runtime,
        upstream_files=None,
        base_runtime=BASE_RUNTIME,
        downstream_runtime=DOWNSTREAM_RUNTIME,
    ):
        root = directory / "source"
        root.mkdir()
        self.command(root, "init", "-q", "-b", "main")
        self.command(root, "config", "user.name", "Test Fixture")
        self.command(root, "config", "user.email", "fixture@example.invalid")
        (root / "runtime.py").write_text(base_runtime)
        self.commit(root, ["runtime.py"], "Record upstream runtime")
        self.command(root, "tag", "v1.0.0")
        base = git(root, "rev-parse", "HEAD")
        (root / "runtime.py").write_text(downstream_runtime)
        patch = subprocess.check_output(["git", "-C", str(root), "diff", "--binary"])
        downstream = root / "downstream"
        downstream.mkdir()
        (downstream / "runtime.patch").write_bytes(patch)
        (downstream / "README.md").write_text("The base is upstream 1.0.0 and the entries.\n")
        data = {
            "schema": 1,
            "version": "1.0.0.1",
            "upstream": {"repository": str(root), "tag": "v1.0.0", "sha": base},
            "patches": [
                {
                    "id": "runtime",
                    "status": "carried",
                    "paths": ["runtime.py"],
                    "patch": "downstream/runtime.patch",
                    "upstream_state": "not_offered",
                    "upstream_reason": "Fixture patch",
                }
            ],
        }
        (downstream / "patches.json").write_text(json.dumps(data))
        self.commit(
            root,
            [
                "runtime.py",
                "downstream/runtime.patch",
                "downstream/README.md",
                "downstream/patches.json",
            ],
            "Record downstream patch",
        )
        self.command(root, "switch", "-qc", "upstream", "v1.0.0")
        (root / "runtime.py").write_text(upstream_runtime)
        for name, content in (upstream_files or {}).items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(content)
        self.commit(root, ["runtime.py", *(upstream_files or {})], "Extend upstream runtime")
        self.command(root, "tag", "v1.1.0")
        self.command(root, "switch", "-q", "main")
        return root

    def test_auto_refreshes_when_every_patch_applies(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory, "VALUE = 'upstream'\nMIDDLE = 1\nOTHER = 'changed'\n"
            )
            report = auto(root, "v1.1.0", directory / "work")
            self.assertEqual(report["status"], "refreshed")
            self.assertEqual(report["version"], "1.1.0.1")
            self.assertEqual(report["entries"], 1)
            integration = Path(report["worktree"])
            self.assertEqual(
                (integration / "runtime.py").read_text(),
                "VALUE = 'downstream'\nMIDDLE = 1\nOTHER = 'changed'\n",
            )
            self.assertEqual(
                (integration / "downstream/README.md").read_text(),
                "The base is upstream 1.1.0 and the entries.\n",
            )
            self.assertEqual(git(integration, "status", "--porcelain"), "")
            self.assertFalse((directory / "work" / "candidate").exists())
            self.assertEqual(git(root, "branch", "--list", "refresh/candidate-1.1.0"), "")

    def test_auto_reports_the_conflicting_patch_and_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory, "VALUE = 'rewritten upstream'\nMIDDLE = 1\nOTHER = 'upstream'\n"
            )
            report = auto(root, "v1.1.0", directory / "work")
            self.assertEqual(report["status"], "conflict")
            self.assertEqual(report["step"], "candidate")
            self.assertEqual(report["entry"], "runtime")
            self.assertEqual(report["paths"], ["runtime.py"])
            self.assertEqual(git(root, "branch", "--list", "refresh/*"), "")
            self.assertEqual(git(root, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_auto_refuses_a_tag_that_is_not_newer(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory, "VALUE = 'upstream'\nMIDDLE = 1\nOTHER = 'changed'\n"
            )
            report = auto(root, "v1.0.0", directory / "work")
            self.assertEqual(report["status"], "failed")
            self.assertIn("newer", report["detail"])

    def test_auto_refuses_to_start_when_its_branches_or_paths_exist_and_removes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory, "VALUE = 'upstream'\nMIDDLE = 1\nOTHER = 'changed'\n"
            )
            self.command(root, "branch", "refresh/upstream-1.1.0", "main")
            self.command(root, "branch", "refresh/candidate-1.1.0", "main")
            report = auto(root, "v1.1.0", directory / "work")
            self.assertEqual((report["status"], report["step"]), ("failed", "preflight"))
            self.assertIn("refresh/upstream-1.1.0", report["detail"])
            self.assertEqual(
                git(root, "branch", "--list", "refresh/*").split(),
                ["refresh/candidate-1.1.0", "refresh/upstream-1.1.0"],
            )
            (directory / "work" / "integration").mkdir(parents=True)
            (directory / "work" / "integration" / "keep.txt").write_text("mine")
            self.command(root, "branch", "-D", "refresh/upstream-1.1.0", "refresh/candidate-1.1.0")
            report = auto(root, "v1.1.0", directory / "work")
            self.assertEqual(report["step"], "preflight")
            self.assertEqual((directory / "work" / "integration" / "keep.txt").read_text(), "mine")

    def test_auto_reports_a_carried_patch_that_upstream_already_contains(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory, "VALUE = 'downstream'\nMIDDLE = 1\nOTHER = 'upstream'\n"
            )
            report = auto(root, "v1.1.0", directory / "work")
            self.assertEqual((report["status"], report["entry"]), ("conflict", "runtime"))
            self.assertIn("already contain", report["detail"])
            self.assertEqual(git(root, "branch", "--list", "refresh/*"), "")

    def test_auto_stops_when_upstream_adds_an_active_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory,
                "VALUE = 'upstream'\nMIDDLE = 1\nOTHER = 'changed'\n",
                {".github/workflows/new.yml": "name: new\n"},
            )
            report = auto(root, "v1.1.0", directory / "work")
            self.assertEqual((report["status"], report["step"]), ("failed", "inherited-workflows"))
            self.assertIn(".github/workflows/new.yml", report["detail"])
            self.assertEqual(git(root, "branch", "--list", "refresh/*"), "")
            self.assertFalse((directory / "work" / "integration").exists())

    def test_auto_names_the_unowned_files_an_upstream_merge_cannot_resolve(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory,
                "VALUE = 'upstream'\nMIDDLE = 1\nOTHER = 'changed'\n",
                {"downstream/README.md": "Upstream wrote this file too.\n"},
            )
            report = auto(root, "v1.1.0", directory / "work")
            self.assertEqual((report["status"], report["step"]), ("failed", "integrate"))
            self.assertIn("downstream/README.md", report["detail"])
            self.assertEqual(git(root, "branch", "--list", "refresh/*"), "")
            self.assertFalse((directory / "work" / "integration").exists())

    def test_auto_keeps_the_apply_error_when_a_patch_cannot_be_merged(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory, "VALUE = 'rewritten'\nMIDDLE = 1\nOTHER = 'upstream'\n"
            )
            report = auto(root, "v1.1.0", directory / "work")
            self.assertEqual(report["status"], "conflict")
            self.assertIn("runtime.py", report["detail"])

    def test_normalized_series_reconstructs_current_source(self):
        root = Path(__file__).resolve().parents[1]
        data = json.loads((root / "downstream/patches.json").read_text())
        patches = [p for p in data["patches"] if p["status"] == "carried"]
        paths = sorted({path for p in patches for path in p["paths"]})
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            self.command(work, "init", "-q")
            for path in paths:
                destination = work / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(
                    subprocess.check_output(
                        ["git", "-C", str(root), "show", data["upstream"]["sha"] + ":" + path]
                    )
                )
            for patch in patches:
                self.command(work, "apply", str(root / patch["patch"]))
            for path in paths:
                self.assertEqual((work / path).read_bytes(), (root / path).read_bytes(), path)

    def test_probe_reports_a_conflicting_patch_and_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory, "VALUE = 'rewritten upstream'\nMIDDLE = 1\nOTHER = 'upstream'\n"
            )
            upstream = git(root, "rev-parse", "v1.1.0^{commit}")
            report = probe(root, upstream, directory / "work")
            self.assertEqual(
                (report["status"], report["entry"], report["paths"], report["upstream"]),
                ("conflict", "runtime", ["runtime.py"], upstream),
            )
            clean = probe(root, git(root, "rev-parse", "v1.0.0"), directory / "work")
            self.assertEqual(clean["status"], "applies")
            self.assertEqual(git(root, "worktree", "list", "--porcelain").count("worktree "), 1)
            self.assertEqual(git(root, "status", "--porcelain"), "")
            self.assertEqual(list((directory / "work").iterdir()), [])

    def test_probe_reports_a_patch_that_upstream_already_contains_as_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(directory, DOWNSTREAM_RUNTIME)
            report = probe(root, git(root, "rev-parse", "v1.1.0^{commit}"), directory / "work")
            self.assertEqual((report["status"], report["entry"]), ("empty", "runtime"))

    def test_blank_only_hunks_flags_padding_and_keeps_real_changes(self):
        padded = (
            b"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            b"@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n"
            b"@@ -287,6 +381,10 @@ class SQLiteGenerator(generator.Generator):\n"
            b"         return x\n \n+\n+\n+    \n+\n     def cast_sql(self):\n"
            b"diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n"
            b"@@ -1,2 +1,4 @@\n a\n+\n+e\n b\n"
        )
        self.assertEqual(
            blank_only_hunks(padded),
            ["@@ -287,6 +381,10 @@ class SQLiteGenerator(generator.Generator):"],
        )

    def test_integrate_refuses_a_candidate_commit_that_adds_only_blank_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory,
                LONG_RUNTIME.replace("LINE_13 = 13", "LINE_13 = 'upstream'"),
                base_runtime=LONG_RUNTIME,
                downstream_runtime=LONG_RUNTIME.replace("LINE_0 = 0", "LINE_0 = 'downstream'"),
            )
            work = directory / "candidate"
            candidate(root, "v1.1.0", work, "candidate", set())
            text = (work / "runtime.py").read_text()
            (work / "runtime.py").write_text(text.replace("LINE_7 = 7\n", "LINE_7 = 7\n\n\n\n"))
            self.command(work, "commit", "-qa", "--amend", "--no-edit")
            integration = directory / "integration"
            with self.assertRaisesRegex(ValueError, "only blank lines.*runtime"):
                integrate(root, "v1.1.0", "candidate", integration, "integration", {})
            self.assertFalse(integration.exists())
            self.assertEqual(git(root, "branch", "--list", "integration"), "")

    def test_candidate_refuses_an_existing_path_or_branch_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.automatic_fixture(
                directory, "VALUE = 'upstream'\nMIDDLE = 1\nOTHER = 'changed'\n"
            )
            branch = "refresh/candidate-1.1.0"
            existing = directory / "existing"
            existing.mkdir()
            (existing / "keep.txt").write_text("mine")
            with self.assertRaisesRegex(ValueError, "already exist.*existing"):
                candidate(root, "v1.1.0", existing, branch, set())
            self.assertEqual((existing / "keep.txt").read_text(), "mine")
            self.assertEqual(git(root, "branch", "--list", "refresh/*"), "")
            self.command(root, "branch", branch, "main")
            with self.assertRaisesRegex(ValueError, f"already exist.*{branch}"):
                candidate(root, "v1.1.0", directory / "fresh", branch, set())
            self.assertEqual(git(root, "rev-parse", branch), git(root, "rev-parse", "main"))
            self.assertFalse((directory / "fresh").exists())
            self.command(root, "branch", "-D", branch)
            blocker = directory / "blocker"
            blocker.write_text("a file, not a directory")
            with self.assertRaisesRegex(ValueError, "Could not create the worktree"):
                candidate(root, "v1.1.0", blocker / "nested", branch, set())
            self.assertEqual(git(root, "branch", "--list", "refresh/*"), "")
            self.assertEqual(blocker.read_text(), "a file, not a directory")

    def test_command_line_reports_a_stopped_candidate_without_a_traceback(self):
        argv = ["refresh", "candidate", "--tag", "v1.1.0", "--path", "x", "--branch", "b"]
        refusal = ValueError("Refusing to start: these already exist: x")
        with (
            mock.patch("sys.argv", argv),
            mock.patch.object(refresh, "candidate", side_effect=refusal),
        ):
            with self.assertRaises(SystemExit) as stopped:
                refresh.main()
        self.assertEqual(stopped.exception.code, f"candidate stopped: {refusal}")
