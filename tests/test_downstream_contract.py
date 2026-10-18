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
        self.assertEqual(manifest["import_package"], "benchbox_sqlglot")
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
                workflow = yaml.safe_load(path.read_text())
                triggers = workflow.get("on", workflow.get(True))
                self.assertNotIn("pull_request_target", triggers)
                for scope in ("contents", "pull-requests"):
                    self.assertNotEqual(workflow.get("permissions", {}).get(scope), "write")
                for name, job in workflow["jobs"].items():
                    for scope in ("contents", "pull-requests"):
                        self.assertNotEqual(job.get("permissions", {}).get(scope), "write")
                    for step in job.get("steps", []):
                        self.assertNotIn("owner: fivetran", str(step))
                        self.assertNotRegex(step.get("run", ""), r"twine\s+upload|git\s+push")
                        if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@"):
                            publishers.append(path.name)
                            self.assertEqual(path.name, "benchbox-release.yml")
                            self.assertEqual(set(triggers), {"workflow_dispatch"})
                            self.assertEqual(name, "publish")
                            self.assertEqual(job["needs"], "verify")
                            self.assertEqual(job["environment"], "benchbox-sqlglot-pypi")
                            self.assertEqual(step["with"]["packages-dir"], "upload/")
                            self.assertEqual(
                                set(triggers["workflow_dispatch"]["inputs"]),
                                {"tag", "candidate_run", "wheel_sha256", "sdist_sha256"},
                            )
                            verifier = workflow["jobs"]["verify"]["steps"]
                            self.assertTrue(
                                any("-m downstream.release" in s.get("run", "") for s in verifier)
                            )
                            self.assertTrue(
                                any(s.get("run") == "make benchbox-check" for s in verifier)
                            )
        self.assertEqual(publishers, ["benchbox-release.yml"])
