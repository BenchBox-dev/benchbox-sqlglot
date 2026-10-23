import json
import subprocess
import unittest
from pathlib import Path

import yaml


class TestDownstreamContract(unittest.TestCase):
    def test_manifest_source_boundaries(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "downstream" / "patches.json").read_text())
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["distribution"], "benchbox-sqlglot")
        self.assertEqual(manifest["import_package"], "benchbox._vendor.sqlglot")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+\.[1-9]\d*$")
        self.assertEqual(manifest["upstream"]["tag"], "v" + manifest["version"].rsplit(".", 1)[0])
        available = {p["id"] for p in manifest["patches"] if p["status"] == "upstreamed"}
        ids = [p["id"] for p in manifest["patches"]]
        self.assertEqual(len(ids), len(set(ids)))
        owners = {}
        for patch in manifest["patches"]:
            with self.subTest(patch=patch["id"]):
                self.assertRegex(patch["source"], r"^[a-f0-9]{40}$")
                self.assertIn(patch["status"], {"carried", "original", "upstreamed", "excluded"})
                self.assertTrue(patch["tests"])
                self.assertTrue(patch["reason"])
                if patch["status"] in {"carried", "original"}:
                    self.assertTrue(set(patch["depends"]) <= available)
                    self.assertTrue(patch["paths"])
                    self.assertTrue(all((root / path).is_file() for path in patch["paths"]))
                    for path in patch["paths"]:
                        owners.setdefault(path, set()).add(patch["status"])
                    available.add(patch["id"])
                if patch["status"] == "carried":
                    self.assertTrue((root / patch["patch"]).is_file())
                    self.assertIn(patch["upstream_state"], {"offered", "not_offered", "declined"})
                    self.assertTrue(patch.get("upstream_reason", "").strip())
                    links = patch.get("upstream", [])
                    if patch["upstream_state"] == "offered":
                        self.assertTrue(links)
                    for link in links:
                        self.assertRegex(
                            link, r"^https://github\.com/tobymao/sqlglot/(pull|issues)/\d+$"
                        )
                else:
                    self.assertNotIn("upstream_state", patch)
                    self.assertNotIn("upstream_reason", patch)
                if patch["status"] == "original":
                    self.assertNotIn("patch", patch)
                    for path in patch["paths"]:
                        self.assertNotEqual(
                            subprocess.run(
                                [
                                    "git",
                                    "-C",
                                    str(root),
                                    "cat-file",
                                    "-e",
                                    manifest["upstream"]["sha"] + ":" + path,
                                ],
                                capture_output=True,
                            ).returncode,
                            0,
                            f"{path} exists upstream; original entries are limited to new files",
                        )
        self.assertTrue(all(len(kinds) == 1 for kinds in owners.values()))

    def test_every_changed_path_has_an_owner(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "downstream" / "patches.json").read_text())
        owned = {
            path
            for entry in manifest["patches"]
            if entry["status"] in {"carried", "original"}
            for path in entry["paths"]
        }
        infrastructure = set()
        for entry in manifest["infrastructure"]:
            with self.subTest(infrastructure=entry["id"]):
                self.assertTrue(entry["reason"])
                self.assertTrue(entry["paths"])
                self.assertTrue(all((root / path).is_file() for path in entry["paths"]))
                self.assertFalse(any((root / path).exists() for path in entry.get("removed", [])))
                infrastructure |= set(entry["paths"]) | set(entry.get("removed", []))
        self.assertFalse(owned & infrastructure)
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--name-only",
                "--no-renames",
                manifest["upstream"]["sha"],
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        unowned = [
            path
            for path in changed
            if path not in owned
            and path not in infrastructure
            and not path.startswith("downstream/patches/")
        ]
        self.assertEqual(
            unowned, [], "Changed files need a carried or original entry, or an infrastructure entry"
        )

    def test_upstream_automation_is_inactive(self):
        root = Path(__file__).resolve().parents[1]
        publishers = []
        for path in (root / ".github" / "workflows").glob("*.yml"):
            with self.subTest(workflow=path.name):
                self.assertNotIn(
                    path.name,
                    {"package-publish.yml", "run-integration-tests.yml", "benchmark-sqlglot.yml"},
                )
                workflow = yaml.safe_load(path.read_text())
                triggers = workflow.get("on", workflow.get(True))
                self.assertNotIn("pull_request_target", triggers)
                for scope in ("contents", "pull-requests"):
                    self.assertNotEqual(workflow.get("permissions", {}).get(scope), "write")
                for name, job in workflow["jobs"].items():
                    for scope in ("contents", "pull-requests"):
                        self.assertNotEqual(job.get("permissions", {}).get(scope), "write")
                    for step in job.get("steps", []):
                        self.assertNotEqual(step.get("with", {}).get("owner"), "fivetran")
                        self.assertNotRegex(
                            step.get("run", ""), r"twine\s+upload|uv\s+publish|git\s+push"
                        )
                        if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@"):
                            publishers.append(path.name)
        self.assertEqual(publishers, [])

    def test_artifact_ci_is_bounded_and_retained(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/benchbox-candidate.yml").read_text())
        jobs = workflow["jobs"]
        self.assertEqual(
            set(jobs), {"build", "wheel-smoke", "installed-suite", "sdist-coexistence"}
        )
        self.assertEqual(
            jobs["wheel-smoke"]["strategy"]["matrix"]["python"], ["3.11", "3.12", "3.13", "3.14"]
        )
        for name in ("wheel-smoke", "installed-suite", "sdist-coexistence"):
            self.assertEqual(jobs[name]["needs"], "build")
        self.assertTrue(
            any("--full" in step.get("run", "") for step in jobs["installed-suite"]["steps"])
        )
        self.assertTrue(
            any("--sdist" in step.get("run", "") for step in jobs["sdist-coexistence"]["steps"])
        )
        uploads = [
            step
            for step in jobs["build"]["steps"]
            if step.get("uses", "").startswith("actions/upload-artifact@")
        ]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["with"]["retention-days"], 30)
