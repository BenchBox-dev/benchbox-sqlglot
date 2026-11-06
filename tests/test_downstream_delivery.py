import copy
import hashlib
import io
import json
import re
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from downstream import delivery
from downstream.delivery import DeliveryError, NotReady

UPSTREAM = "2" * 40
VERSION = "1.2.3.1"
WORKFLOW = "jobs:\n  one:\n    runs-on: x\n  two:\n    runs-on: x\n    strategy:\n      matrix:\n        python: ['3.11', '3.12']\n"
JOBS = ["one", "two (3.11)", "two (3.12)"]
CANONICAL_WORKFLOW = "jobs:\n  build:\n    runs-on: x\n"
CANONICAL_JOBS = ["build"]


def run_git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def manifest(version=VERSION):
    return {
        "schema": 1,
        "distribution": "benchbox-sqlglot",
        "version": version,
        "upstream": {"sha": UPSTREAM},
    }


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def make_bundle(source, version=VERSION, **changes):
    provenance = {
        "schema": 1,
        "distribution": "benchbox-sqlglot",
        "namespace": "benchbox._vendor.sqlglot",
        "version": version,
        "fork_sha": source,
        "upstream_sha": UPSTREAM,
        "dirty": False,
        "manifest": manifest(version),
        **changes,
    }
    wheel = make_zip({delivery.PROVENANCE: json.dumps(provenance)})
    sdist = b"sdist"
    names = (f"benchbox_sqlglot-{version}-py3-none-any.whl", f"benchbox_sqlglot-{version}.tar.gz")
    sums = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in zip(names, (wheel, sdist))
    )
    return make_zip({names[0]: wheel, names[1]: sdist, "SHA256SUMS": sums}), names[0], wheel


class FakeApi:
    def __init__(self, source, bundle):
        self.runs = {}
        self.jobs = {}
        self.suites = {}
        self.artifacts = {}
        self.publisher_runs = []
        self.bundle = bundle
        for index, path in enumerate(delivery.REQUIRED):
            run = {
                "id": 100 + index,
                "run_number": 1,
                "run_attempt": 1,
                "check_suite_id": 900 + index,
                "path": path,
                "head_sha": source,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "pull_requests": [],
                "repository": {"id": 7, "full_name": delivery.REPOSITORY},
                "head_repository": {"full_name": delivery.REPOSITORY},
            }
            self.runs[path] = run
            jobs = CANONICAL_JOBS if path == delivery.CANONICAL else JOBS
            self.jobs[run["id"]] = [
                {"name": name, "conclusion": "success", "head_sha": source, "steps": []}
                for name in jobs
            ]
            self.suites[900 + index] = {
                "app": {"id": delivery.ACTIONS_APP},
                "head_sha": source,
                "head_branch": "main",
            }
        self.jobs[100][0]["steps"] = [
            {"name": "Set up job", "started_at": "2026-11-05T09:59:00Z"},
            {
                "name": "Run actions/upload-artifact@v4",
                "started_at": "2026-11-05T10:00:00Z",
                "completed_at": "2026-11-05T10:01:00Z",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        self.rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": name, "integration_id": delivery.ACTIONS_APP}
                        for name in sorted({*JOBS, *CANONICAL_JOBS})
                    ]
                },
            }
        ]
        self.branch = {
            "protection": {
                "required_status_checks": {
                    "enforcement_level": "off",
                    "contexts": [],
                    "checks": [],
                }
            }
        }
        canonical = self.runs[delivery.CANONICAL]
        self.artifacts[canonical["id"]] = [
            {
                "id": 55,
                "name": delivery.CANONICAL_ARTIFACT,
                "expired": False,
                "size_in_bytes": len(bundle),
                "digest": "sha256:" + hashlib.sha256(bundle).hexdigest(),
                "workflow_run": {
                    "id": canonical["id"],
                    "head_sha": source,
                    "head_branch": "main",
                    "repository_id": 7,
                },
                "created_at": "2026-11-05T10:00:30Z",
            }
        ]

    def get(self, path, params=None):
        match = re.fullmatch(r"actions/workflows/(.+)/runs", path)
        if match:
            return {
                "workflow_runs": [
                    run for run in self.runs.values() if Path(run["path"]).name == match.group(1)
                ]
            }
        match = re.fullmatch(r"actions/runs/(\d+)", path)
        if match:
            return next(r for r in self.runs.values() if r["id"] == int(match.group(1)))
        match = re.fullmatch(r"check-suites/(\d+)", path)
        if match:
            return self.suites[int(match.group(1))]
        if path == "rules/branches/main":
            return self.rules
        if path == "branches/main":
            return self.branch
        raise AssertionError(path)

    def pages(self, path, key, params=None):
        if path == f"actions/workflows/{Path(delivery.PUBLISHER).name}/runs":
            wanted = (params or {}).get("status")
            return [run for run in self.publisher_runs if wanted in (None, run["conclusion"])]
        match = re.fullmatch(r"actions/runs/(\d+)/attempts/(\d+)/jobs", path)
        if match:
            return self.jobs.get(int(match.group(1)), [])
        match = re.fullmatch(r"actions/runs/(\d+)/artifacts", path)
        return self.artifacts.get(int(match.group(1)), [])

    def artifact(self, artifact_id):
        return self.bundle

    def deliver_run(self, run_id, version, sha256, head):
        self.publisher_runs.append(
            {
                "id": run_id,
                "run_attempt": 1,
                "path": delivery.PUBLISHER,
                "head_sha": head,
                "head_branch": "main",
                "event": "workflow_run",
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": delivery.REPOSITORY},
                "head_repository": {"full_name": delivery.REPOSITORY},
            }
        )
        self.jobs[run_id] = [
            {"name": delivery.job_name(version, sha256), "conclusion": "success"},
            {"name": "verify", "conclusion": "success"},
        ]


class TestVerifiedDelivery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        (self.root / "downstream").mkdir(parents=True)
        (self.root / ".github/workflows").mkdir(parents=True)
        run_git(self.root, "init", "-q", "-b", "main")
        run_git(self.root, "config", "user.name", "Fixture")
        run_git(self.root, "config", "user.email", "fixture@example.invalid")
        for path in delivery.REQUIRED:
            (self.root / path).write_text(
                CANONICAL_WORKFLOW if path == delivery.CANONICAL else WORKFLOW
            )
        self.commit("0.0.0.1", "old")
        self.release = self.commit(VERSION, "release")
        self.tooling = self.commit(VERSION, "tooling")
        bundle, self.wheel_name, wheel = make_bundle(self.release)
        self.wheel_sha = hashlib.sha256(wheel).hexdigest()
        self.api = FakeApi(self.release, bundle)
        self.out = Path(self.temporary.name) / "out"
        self.run = {"id": 9, "attempt": 1, "event": "workflow_run"}

    def commit(self, version, message):
        (self.root / "downstream/patches.json").write_text(json.dumps(manifest(version)))
        (self.root / "note").write_text(message)
        run_git(self.root, "add", "downstream/patches.json", "note", *delivery.REQUIRED)
        run_git(self.root, "commit", "-qm", message)
        return run_git(self.root, "rev-parse", "HEAD")

    def deliver(self, source=None, expected=None):
        return delivery.deliver(
            self.api, self.root, source or self.release, self.tooling, self.run, self.out, expected
        )

    def test_publishes_verified_record_and_exact_bytes(self):
        record = self.deliver(
            expected=(delivery.CANONICAL, 100, 1),
        )
        self.assertEqual(
            (record["version"], record["forkSha"], record["upstreamSha"]),
            (VERSION, self.release, UPSTREAM),
        )
        self.assertEqual(record["wheel"]["sha256"], self.wheel_sha)
        self.assertEqual(record["source"]["runId"], 100)
        self.assertEqual(record["source"]["artifact"]["id"], 55)
        self.assertEqual(record["delivery"]["commit"], self.tooling)
        self.assertEqual(record["delivery"]["jobName"], delivery.job_name(VERSION, self.wheel_sha))
        self.assertEqual({c["workflow"] for c in record["requiredChecks"]}, set(delivery.REQUIRED))
        written = (self.out / self.wheel_name).read_bytes()
        self.assertEqual(hashlib.sha256(written).hexdigest(), self.wheel_sha)
        self.assertEqual(json.loads((self.out / delivery.RECORD).read_text()), record)

    def test_tooling_only_main_commit_is_not_a_delivery(self):
        self.assertEqual(
            delivery.release_source(self.root, self.tooling, self.tooling), (VERSION, self.release)
        )
        with self.assertRaises(NotReady):
            self.deliver(source=self.tooling)

    def test_source_must_be_first_parent_of_trusted_main(self):
        with self.assertRaises(DeliveryError):
            delivery.release_source(self.root, "f" * 40, self.tooling)

    def test_pending_required_check_waits(self):
        self.api.runs[delivery.REQUIRED[1]]["status"] = "in_progress"
        with self.assertRaises(NotReady):
            self.deliver()

    def test_missing_required_run_waits(self):
        del self.api.runs[delivery.REQUIRED[2]]
        with self.assertRaises(NotReady):
            self.deliver()

    def test_failed_required_check_is_refused(self):
        self.api.runs[delivery.REQUIRED[1]]["conclusion"] = "failure"
        with self.assertRaises(DeliveryError):
            self.deliver()

    def test_skipped_or_missing_required_job_is_refused(self):
        run_id = self.api.runs[delivery.REQUIRED[2]]["id"]
        self.api.jobs[run_id][1]["conclusion"] = "skipped"
        with self.assertRaises(DeliveryError):
            self.deliver()
        self.api.jobs[run_id][1]["conclusion"] = "success"
        del self.api.jobs[run_id][2]
        with self.assertRaises(DeliveryError):
            self.deliver()

    def test_runs_from_other_events_or_publishers_do_not_count(self):
        for change in ({"event": "pull_request"}, {"path": ".github/workflows/other.yml"}):
            with self.subTest(change=change):
                self.api.runs[delivery.CANONICAL].update(change)
                with self.assertRaises(NotReady):
                    self.deliver()
                self.api.runs[delivery.CANONICAL].update(
                    {"event": "push", "path": delivery.CANONICAL}
                )
        self.api.runs[delivery.CANONICAL]["repository"]["full_name"] = "someone/fork"
        with self.assertRaises(NotReady):
            self.deliver()

    def test_head_attempt_and_run_must_match_trigger(self):
        with self.assertRaises(DeliveryError):
            self.deliver(expected=(delivery.CANONICAL, 100, 2))
        with self.assertRaises(DeliveryError):
            self.deliver(expected=(delivery.CANONICAL, 101, 1))
        other = copy.deepcopy(self.api.artifacts[100])
        other[0]["workflow_run"]["head_sha"] = self.tooling
        self.api.artifacts[100] = other
        with self.assertRaises(DeliveryError):
            self.deliver()

    def test_canonical_attempt_cannot_change_after_check_verification(self):
        get = self.api.get

        def get_after_rerun(path, params=None):
            result = get(path, params)
            return {**result, "run_attempt": 2} if path == "actions/runs/100" else result

        self.api.get = get_after_rerun
        with self.assertRaisesRegex(DeliveryError, "attempt changed"):
            self.deliver()
        self.assertFalse(self.out.exists())

    def test_required_run_rerun_after_check_verification_is_refused(self):
        get = self.api.get

        def get_after_rerun(path, params=None):
            result = get(path, params)
            if path == "actions/runs/101":
                return {**result, "run_attempt": 2, "status": "in_progress", "conclusion": None}
            return result

        self.api.get = get_after_rerun
        with self.assertRaisesRegex(DeliveryError, "attempt changed"):
            self.deliver()
        self.assertFalse(self.out.exists())

    def test_rerunning_publisher_keeps_its_earlier_receipt(self):
        self.api.deliver_run(500, VERSION, "a" * 64, self.release)
        self.api.publisher_runs[0].update(
            {"run_attempt": 2, "status": "in_progress", "conclusion": None}
        )
        with self.assertRaisesRegex(DeliveryError, "new fork version"):
            self.deliver()

    def test_enforced_classic_protection_context_must_be_covered(self):
        self.api.branch["protection"]["required_status_checks"].update(
            {"enforcement_level": "everyone", "contexts": ["classic-only"]}
        )
        with self.assertRaisesRegex(DeliveryError, "classic-only"):
            self.deliver()
        self.api.branch = {}
        with self.assertRaisesRegex(DeliveryError, "Classic protection"):
            self.deliver()

    def test_listing_shorter_than_total_count_is_incomplete(self):
        class Truncated(delivery.GitHub):
            def get(self, path, params=None):
                return {"total_count": 1500, "workflow_runs": []}

        with self.assertRaisesRegex(DeliveryError, "incomplete"):
            Truncated("token").pages("actions/workflows/x/runs", "workflow_runs")

    def test_live_required_set_change_is_refused(self):
        self.api.rules[0]["parameters"]["required_status_checks"].append(
            {"context": "new-required", "integration_id": delivery.ACTIONS_APP}
        )
        with self.assertRaisesRegex(DeliveryError, "new-required"):
            self.deliver()

    def test_required_check_from_another_publisher_is_refused(self):
        self.api.rules[0]["parameters"]["required_status_checks"][0]["integration_id"] = 99
        with self.assertRaises(DeliveryError):
            self.deliver()

    def test_every_required_publisher_constraint_must_match(self):
        for app in (None, delivery.ACTIONS_APP):
            with self.subTest(app=app):
                self.api.rules[0]["parameters"]["required_status_checks"][0]["integration_id"] = app
                self.api.rules.append(
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "required_status_checks": [{"context": "build", "integration_id": 99}]
                        },
                    }
                )
                with self.assertRaisesRegex(DeliveryError, "build"):
                    self.deliver()
                self.api.rules.pop()

    def test_missing_live_rules_are_refused(self):
        self.api.rules.clear()
        with self.assertRaises(DeliveryError):
            self.deliver()

    def test_check_suite_must_be_actions_on_main_at_source(self):
        for change in ({"app": {"id": 99}}, {"head_sha": self.tooling}, {"head_branch": "x"}):
            with self.subTest(change=change):
                suite = self.api.suites[900]
                original = dict(suite)
                suite.update(change)
                with self.assertRaises(DeliveryError):
                    self.deliver()
                suite.update(original)

    def test_artifact_from_previous_attempt_is_not_selected(self):
        stale = dict(self.api.artifacts[100][0], id=54, created_at="2026-11-05T09:00:00Z")
        self.api.artifacts[100].insert(0, stale)
        self.assertEqual(self.deliver()["source"]["artifact"]["id"], 55)

    def test_only_previous_attempt_artifact_is_refused(self):
        self.api.artifacts[100][0]["created_at"] = "2026-11-05T09:00:00Z"
        with self.assertRaises(DeliveryError):
            self.deliver()

    def test_attempt_without_single_upload_step_is_refused(self):
        self.api.jobs[100][0]["steps"] = []
        with self.assertRaises(DeliveryError):
            self.deliver()

    def test_failed_upload_cannot_certify_a_successful_build_job(self):
        self.api.jobs[100][0]["steps"][1]["conclusion"] = "failure"
        with self.assertRaises(DeliveryError):
            self.deliver()
        self.assertFalse(self.out.exists())

    def test_artifact_digest_and_expiry(self):
        self.api.artifacts[100][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(DeliveryError):
            self.deliver()
        self.api.artifacts[100][0]["expired"] = True
        with self.assertRaisesRegex(DeliveryError, "rerun the canonical"):
            self.deliver()
        self.assertFalse(self.out.exists())

    def test_same_version_different_hash_is_refused(self):
        self.api.deliver_run(500, VERSION, "a" * 64, self.release)
        with self.assertRaisesRegex(DeliveryError, "new fork version"):
            self.deliver()

    def test_same_hash_redelivery_is_recovery_not_conflict(self):
        self.api.deliver_run(500, VERSION, self.wheel_sha, self.release)
        self.assertEqual(self.deliver()["wheel"]["sha256"], self.wheel_sha)

    def test_hash_cannot_be_delivered_under_another_version(self):
        self.api.deliver_run(500, "0.0.0.1", self.wheel_sha, self.release)
        with self.assertRaises(DeliveryError):
            self.deliver()

    def test_receipts_from_untrusted_runs_are_ignored(self):
        self.api.deliver_run(500, VERSION, "a" * 64, self.release)
        self.api.publisher_runs[0]["head_branch"] = "feature"
        self.assertEqual(self.deliver()["version"], VERSION)

    def test_bundle_rejects_unsafe_or_inconsistent_archives(self):
        good, wheel_name, wheel = make_bundle(self.release)
        digest = "sha256:" + hashlib.sha256(good).hexdigest()
        data = delivery.read_bundle(good, digest, VERSION, self.release, manifest())
        self.assertEqual(data["wheel"], wheel_name)

        def bundle(files):
            data = make_zip(files)
            return data, "sha256:" + hashlib.sha256(data).hexdigest()

        sums = zipfile.ZipFile(io.BytesIO(good)).read("SHA256SUMS")
        sdist_name = f"benchbox_sqlglot-{VERSION}.tar.gz"
        base = {wheel_name: wheel, sdist_name: b"sdist", "SHA256SUMS": sums}
        cases = {
            "traversal": {**base, "../evil": b"x"},
            "extra": {**base, "extra": b"x"},
            "missing": {k: v for k, v in base.items() if k != sdist_name},
            "tampered": {**base, sdist_name: b"other"},
            "nested": {f"dir/{wheel_name}": wheel, sdist_name: b"sdist", "SHA256SUMS": sums},
        }
        for name, files in cases.items():
            with self.subTest(name):
                data, digest = bundle(files)
                with self.assertRaises(DeliveryError):
                    delivery.read_bundle(data, digest, VERSION, self.release, manifest())
        with self.assertRaises(DeliveryError):
            delivery.read_bundle(good, "sha256:" + "0" * 64, VERSION, self.release, manifest())
        with self.assertRaises(DeliveryError):
            delivery.read_bundle(
                b"not a zip",
                "sha256:" + hashlib.sha256(b"not a zip").hexdigest(),
                VERSION,
                self.release,
                manifest(),
            )

    def test_bundle_provenance_must_bind_to_source(self):
        for change in ({"fork_sha": self.tooling}, {"dirty": True}, {"upstream_sha": "3" * 40}):
            with self.subTest(change=change):
                data, _, _ = make_bundle(self.release, **change)
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                with self.assertRaises(DeliveryError):
                    delivery.read_bundle(data, digest, VERSION, self.release, manifest())

    def test_oversized_member_is_refused_before_inflating(self):
        data = make_zip({"SHA256SUMS": b"0" * 1024})
        archive = zipfile.ZipFile(io.BytesIO(data))
        with self.assertRaises(DeliveryError):
            delivery.bounded_read(archive, archive.infolist()[0], 10)
