import json
import re
import unittest
from pathlib import Path


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
        for patch in manifest["patches"]:
            with self.subTest(patch=patch["id"]):
                self.assertRegex(patch["source"], r"^[a-f0-9]{40}$")
                self.assertIn(patch["status"], {"carried", "upstreamed", "excluded"})
                self.assertTrue(patch["tests"])
                self.assertTrue(patch["reason"])
                if patch["status"] == "carried":
                    self.assertTrue(set(patch["depends"]) <= admitted)
                    self.assertTrue(all((root / path).is_file() for path in patch["paths"]))
                    admitted.add(patch["id"])
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
        for path in (root / ".github" / "workflows").glob("*.yml"):
            with self.subTest(workflow=path.name):
                content = path.read_text()
                self.assertNotRegex(content, r"twine\s+upload|pypa/gh-action-pypi-publish")
                self.assertNotIn("owner: fivetran", content)
                self.assertIsNone(re.search(r"(?:contents|pull-requests):\s*write", content))
