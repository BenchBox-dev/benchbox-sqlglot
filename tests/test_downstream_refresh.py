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
            self.assertEqual(
                json.loads((final / "downstream/patches.json").read_text())["patches"][0]["status"],
                "upstreamed",
            )
            self.command(final, "merge-base", "--is-ancestor", base, "HEAD")

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
