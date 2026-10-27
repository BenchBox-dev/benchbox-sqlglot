import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from downstream.build import git
from downstream.refresh import candidate, integrate


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
