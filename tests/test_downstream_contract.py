import json
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
        admitted = set()
        available = {p["id"] for p in manifest["patches"] if p["status"] == "upstreamed"}
        for patch in manifest["patches"]:
            with self.subTest(patch=patch["id"]):
                self.assertRegex(patch["source"], r"^[a-f0-9]{40}$")
                self.assertIn(patch["status"], {"carried", "upstreamed", "excluded"})
                self.assertTrue(patch["tests"])
                self.assertTrue(patch["reason"])
                if patch["status"] == "carried":
                    self.assertTrue(set(patch["depends"]) <= available)
                    self.assertTrue(all((root / path).is_file() for path in patch["paths"]))
                    admitted.add(patch["id"])
                    available.add(patch["id"])
        self.assertLessEqual(
            admitted,
            {
                "postgres-extract-return-types",
                "sqlite-date-extraction",
                "sqlite-date-interval-arithmetic",
            },
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
