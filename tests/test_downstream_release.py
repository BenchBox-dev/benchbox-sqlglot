import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from downstream.release import publishing_environment, validate


class TestDownstreamRelease(unittest.TestCase):
    def test_publication_rejects_changed_approval_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "artifact.whl"
            wheel.write_bytes(b"changed bytes")
            provenance = {"version": "1.0.0.1", "fork_sha": "a" * 40}
            with (
                patch(
                    "downstream.release.inspect_artifacts", return_value=(wheel, wheel, provenance)
                ),
                patch("downstream.release.git", return_value="a" * 40),
                self.assertRaisesRegex(ValueError, "approved hashes"),
            ):
                validate(Path(temporary), "benchbox-v1.0.0.1", "0" * 64, "0" * 64)

    def test_publication_requires_owner_environment_reviewers(self):
        import json

        run = {
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "path": ".github/workflows/benchbox-candidate.yml",
        }
        with (
            patch.dict("os.environ", {"GITHUB_REPOSITORY": "joeharris76/sqlglot"}),
            patch("downstream.release.git", return_value="a" * 40),
            patch(
                "downstream.release.subprocess.check_output",
                side_effect=[json.dumps(run), '{"protection_rules": []}'],
            ),
            self.assertRaisesRegex(ValueError, "required reviewers"),
        ):
            publishing_environment("123")
