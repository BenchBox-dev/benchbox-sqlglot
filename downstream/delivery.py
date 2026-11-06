from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

SCHEMA = 1
KIND = "benchbox-sqlglot-verified-delivery"
REPOSITORY = "BenchBox-dev/benchbox-sqlglot"
API = "https://api.github.com"
CANONICAL = ".github/workflows/benchbox-candidate.yml"
REQUIRED = (
    CANONICAL,
    ".github/workflows/benchbox-engines.yml",
    ".github/workflows/package-test.yml",
)
PUBLISHER = ".github/workflows/benchbox-delivery.yml"
CANONICAL_ARTIFACT = "benchbox-sqlglot"
DELIVERY_ARTIFACT = "benchbox-sqlglot-delivery-a"
RECORD = "delivery.json"
PROVENANCE = "benchbox/_vendor/sqlglot/benchbox_provenance.json"
VERSION = re.compile(r"^\d+\.\d+\.\d+\.[1-9]\d*$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
JOB = re.compile(r"^delivered benchbox-sqlglot (\d+\.\d+\.\d+\.[1-9]\d*) sha256:([0-9a-f]{64})$")
MAX_FILE = 64 * 1024 * 1024
MAX_ZIP = 192 * 1024 * 1024
MAX_PROVENANCE = 8 * 1024 * 1024
MAX_WHEEL_ENTRIES = 20000
MAX_PAGES = 30
ACTIONS_APP = 15368
PUBLISHER_ORIGINS = {"workflow_run", "workflow_dispatch"}


class DeliveryError(Exception):
    pass


class NotReady(Exception):
    pass


class Api(Protocol):
    def get(self, path: str, params: dict | None = None) -> Any: ...

    def pages(self, path: str, key: str, params: dict | None = None) -> list: ...

    def artifact(self, artifact_id: int) -> bytes: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHub:
    def __init__(self, token: str, repository: str = REPOSITORY):
        self.token = token
        self.repository = repository

    def _request(self, url: str, authorized: bool = True) -> urllib.request.Request:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if authorized:
            headers["Authorization"] = "Bearer " + self.token
        return urllib.request.Request(url, headers=headers)

    def get(self, path: str, params: dict | None = None) -> Any:
        url = f"{API}/repos/{self.repository}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(self._request(url), timeout=60) as response:
                return json.load(response)
        except urllib.error.URLError as error:
            raise DeliveryError(f"GitHub API read failed for {path}: {error}") from error

    def pages(self, path: str, key: str, params: dict | None = None) -> list:
        items: list = []
        for page in range(1, MAX_PAGES + 1):
            response = self.get(path, {**(params or {}), "per_page": 100, "page": page})
            batch = response[key]
            items.extend(batch)
            if len(batch) < 100:
                total = response.get("total_count")
                if total is not None and total != len(items):
                    raise DeliveryError(
                        f"{path} listed {len(items)} of {total} items; history is incomplete"
                    )
                return items
        raise DeliveryError(f"{path} exceeds {MAX_PAGES} pages; history is incomplete")

    def artifact(self, artifact_id: int) -> bytes:
        url = f"{API}/repos/{self.repository}/actions/artifacts/{artifact_id}/zip"
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(self._request(url), timeout=60)
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location")
            if error.code != 302 or not location:
                raise DeliveryError(f"Artifact {artifact_id} download refused: {error}") from error
        else:
            raise DeliveryError(f"Artifact {artifact_id} download did not redirect")
        if urllib.parse.urlsplit(location).scheme != "https":
            raise DeliveryError("Artifact download location is not HTTPS")
        try:
            with urllib.request.urlopen(
                self._request(location, authorized=False), timeout=300
            ) as r:
                data = r.read(MAX_ZIP + 1)
        except urllib.error.URLError as error:
            raise DeliveryError(f"Artifact {artifact_id} download failed: {error}") from error
        if len(data) > MAX_ZIP:
            raise DeliveryError(f"Artifact {artifact_id} exceeds {MAX_ZIP} bytes")
        return data


def git(root: Path, *args: str, check: bool = True) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        if check:
            raise DeliveryError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return None
    return result.stdout.strip()


def manifest_at(root: Path, commit: str) -> dict | None:
    text = git(root, "show", f"{commit}:downstream/patches.json", check=False)
    return json.loads(text) if text else None


def job_name(version: str, sha256: str) -> str:
    return f"delivered benchbox-sqlglot {version} sha256:{sha256}"


def release_source(root: Path, source: str, head: str) -> tuple[str, str]:
    if not SHA.fullmatch(source):
        raise DeliveryError("Source must be a full lowercase commit SHA")
    line = (git(root, "rev-list", "--first-parent", head) or "").split()
    if source not in line:
        raise DeliveryError(f"{source} is not a first-parent commit of the trusted main {head}")
    manifest = manifest_at(root, source)
    if not manifest or not VERSION.fullmatch(str(manifest.get("version"))):
        raise DeliveryError(f"{source} has no valid downstream manifest version")
    version = manifest["version"]
    origin = source
    for parent in line[line.index(source) + 1 :]:
        parent_manifest = manifest_at(root, parent)
        if not parent_manifest or parent_manifest.get("version") != version:
            break
        origin = parent
    return version, origin


def expected_jobs(workflow: str) -> set[str]:
    import yaml

    names: set[str] = set()
    for key, job in yaml.safe_load(workflow)["jobs"].items():
        name = job.get("name", key)
        matrix = job.get("strategy", {}).get("matrix")
        if "if" in job or "${{" in name:
            raise DeliveryError(f"Job {key} cannot be reduced to a fixed required check")
        if matrix is None:
            names.add(name)
            continue
        if not all(isinstance(v, list) for v in matrix.values()) or {"include", "exclude"} & set(
            matrix
        ):
            raise DeliveryError(f"Job {key} has a matrix the delivery cannot expand")
        for combo in itertools.product(*matrix.values()):
            names.add(f"{name} ({', '.join(map(str, combo))})")
    return names


def live_required_contexts(api: Api) -> dict[str, set]:
    rules = api.get("rules/branches/main", {"per_page": 100})
    if len(rules) >= 100:
        raise DeliveryError("Branch rules exceed one page; the required set is incomplete")
    required: dict[str, set] = {}
    for rule in rules:
        if rule["type"] == "required_status_checks":
            for check in rule["parameters"]["required_status_checks"]:
                required.setdefault(check["context"], set()).add(check.get("integration_id"))
    protection = api.get("branches/main").get("protection")
    if protection is None:
        raise DeliveryError("Classic protection of main is unreadable; the required set is unknown")
    classic = protection["required_status_checks"]
    if classic["enforcement_level"] != "off":
        for context in classic["contexts"]:
            required.setdefault(context, set()).add(None)
        for check in classic["checks"]:
            required.setdefault(check["context"], set()).add(check.get("app_id"))
    if not required:
        raise DeliveryError("main has no live required status checks; refusing to deliver")
    return required


def required_checks(
    api: Api,
    root: Path,
    source: str,
    expected: tuple[str, int, int] | None = None,
) -> list[dict]:
    live = live_required_contexts(api)
    covered: dict[str, int] = {}
    evidence = []
    for path in REQUIRED:
        runs = [
            run
            for run in api.get(
                f"actions/workflows/{Path(path).name}/runs",
                {"head_sha": source, "event": "push", "branch": "main", "per_page": 100},
            )["workflow_runs"]
            if run["path"] == path
            and run["head_sha"] == source
            and run["event"] == "push"
            and run["head_branch"] == "main"
            and run["repository"]["full_name"] == REPOSITORY
            and run["head_repository"]["full_name"] == REPOSITORY
            and not run.get("pull_requests")
        ]
        if not runs:
            raise NotReady(f"{path} has no push/main run at {source} yet")
        run = max(runs, key=lambda item: item["run_number"])
        if expected and expected[0] == path and (run["id"], run["run_attempt"]) != expected[1:]:
            raise DeliveryError(
                f"{path} run {run['id']} attempt {run['run_attempt']} is not the triggering "
                f"run {expected[1]} attempt {expected[2]}"
            )
        if run["status"] != "completed":
            raise NotReady(f"{path} run {run['id']} is {run['status']}")
        if run["conclusion"] != "success":
            raise DeliveryError(f"{path} run {run['id']} concluded {run['conclusion']}")
        jobs = api.pages(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
        required = expected_jobs(git(root, "show", f"{source}:{path}") or "")
        names = {job["name"] for job in jobs}
        bad = sorted(
            job["name"]
            for job in jobs
            if job["conclusion"] != "success" or job["head_sha"] != source
        )
        if bad or len(jobs) != len(names) or names != required:
            raise DeliveryError(
                f"{path} run {run['id']} jobs do not exactly match the required successful "
                f"set; not successful: {bad}; missing: {sorted(required - names)}; "
                f"unexpected: {sorted(names - required)}"
            )
        suite = api.get(f"check-suites/{run['check_suite_id']}")
        if (
            suite["app"]["id"] != ACTIONS_APP
            or suite["head_sha"] != source
            or suite["head_branch"] != "main"
        ):
            raise DeliveryError(f"{path} run {run['id']} check suite is not GitHub Actions on main")
        for name in names:
            covered[name] = suite["app"]["id"]
        evidence.append(
            {
                "workflow": path,
                "runId": run["id"],
                "runAttempt": run["run_attempt"],
                "checkSuiteId": run["check_suite_id"],
                "conclusion": "success",
                "jobs": sorted(names),
            }
        )
    uncovered = sorted(
        context
        for context, apps in live.items()
        if context not in covered
        or any(app is not None and app != covered[context] for app in apps)
    )
    if uncovered:
        raise DeliveryError(
            f"Live required checks {uncovered} are not satisfied by successful Actions jobs "
            "of the canonical workflows; the required set or its publishers changed"
        )
    return evidence


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_artifact(api: Api, run: dict, source: str) -> dict:
    jobs = [
        job
        for job in api.pages(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
        if job["name"] == "build"
    ]
    uploads = [
        step
        for job in jobs
        for step in job["steps"]
        if step["name"].startswith("Run actions/upload-artifact@")
    ]
    if (
        len(jobs) != 1
        or len(uploads) != 1
        or uploads[0].get("status") != "completed"
        or uploads[0].get("conclusion") != "success"
        or not uploads[0].get("completed_at")
    ):
        raise DeliveryError(
            f"Run {run['id']} attempt {run['run_attempt']} has no single completed build upload step"
        )
    start, end = moment(uploads[0]["started_at"]), moment(uploads[0]["completed_at"])
    artifacts = [
        item
        for item in api.pages(f"actions/runs/{run['id']}/artifacts", "artifacts")
        if item["name"] == CANONICAL_ARTIFACT and start <= moment(item["created_at"]) <= end
    ]
    if len(artifacts) != 1:
        raise DeliveryError(
            f"Expected one {CANONICAL_ARTIFACT} artifact uploaded by attempt "
            f"{run['run_attempt']} of run {run['id']}, found {len(artifacts)}; rerun the "
            f"canonical distribution workflow at {source}"
        )
    artifact = artifacts[0]
    if artifact["expired"]:
        raise DeliveryError(
            f"Artifact {artifact['id']} of run {run['id']} expired; rerun the canonical "
            f"distribution workflow at {source} and deliver the new attempt"
        )
    origin = artifact["workflow_run"]
    if (
        origin["id"] != run["id"]
        or origin["head_sha"] != source
        or origin["head_branch"] != "main"
        or origin["repository_id"] != run["repository"]["id"]
        or not DIGEST.fullmatch(artifact.get("digest") or "")
    ):
        raise DeliveryError(f"Artifact {artifact['id']} does not belong to run {run['id']}")
    return artifact


def bounded_read(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit or info.flag_bits & 1:
        raise DeliveryError(f"Archive member {info.filename} is oversized or encrypted")
    with archive.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) != info.file_size:
        raise DeliveryError(f"Archive member {info.filename} size disagrees with its header")
    return data


def read_bundle(data: bytes, digest: str, version: str, source: str, manifest: dict) -> dict:
    if hashlib.sha256(data).hexdigest() != DIGEST.fullmatch(digest).group(1):
        raise DeliveryError("Downloaded artifact does not match the digest GitHub recorded")
    wheel = f"benchbox_sqlglot-{version}-py3-none-any.whl"
    sdist = f"benchbox_sqlglot-{version}.tar.gz"
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise DeliveryError("Canonical artifact is not a zip archive") from error
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if sorted(names) != sorted([wheel, sdist, "SHA256SUMS"]):
        raise DeliveryError(
            f"Canonical artifact must contain exactly {wheel}, {sdist} and SHA256SUMS"
        )
    files = {}
    for info in infos:
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise DeliveryError(f"Archive member {info.filename} is not a regular file")
        files[info.filename] = bounded_read(archive, info, MAX_FILE)
    sums = {}
    for line in files["SHA256SUMS"].decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
        if not match or match.group(2) in sums:
            raise DeliveryError("SHA256SUMS is malformed")
        sums[match.group(2)] = match.group(1)
    if set(sums) != {wheel, sdist}:
        raise DeliveryError("SHA256SUMS must list exactly the wheel and sdist")
    for name, expected in sums.items():
        if hashlib.sha256(files[name]).hexdigest() != expected:
            raise DeliveryError(f"{name} does not match SHA256SUMS")
    try:
        with zipfile.ZipFile(io.BytesIO(files[wheel])) as inner:
            if len(inner.infolist()) > MAX_WHEEL_ENTRIES:
                raise DeliveryError("Wheel has too many entries")
            matches = [i for i in inner.infolist() if i.filename == PROVENANCE]
            if len(matches) != 1:
                raise DeliveryError("Wheel must carry exactly one provenance record")
            provenance = json.loads(bounded_read(inner, matches[0], MAX_PROVENANCE))
    except zipfile.BadZipFile as error:
        raise DeliveryError("Wheel is not a valid zip archive") from error
    if (
        provenance.get("schema") != 1
        or provenance.get("distribution") != "benchbox-sqlglot"
        or provenance.get("namespace") != "benchbox._vendor.sqlglot"
        or provenance.get("version") != version
        or provenance.get("fork_sha") != source
        or provenance.get("upstream_sha") != manifest["upstream"]["sha"]
        or provenance.get("dirty") is not False
        or provenance.get("manifest") != manifest
    ):
        raise DeliveryError("Wheel provenance does not match the merged source commit")
    return {"wheel": wheel, "sdist": sdist, "files": files, "sums": sums}


def prior_deliveries(api: Api) -> dict[str, dict[str, dict]]:
    runs = api.pages(f"actions/workflows/{Path(PUBLISHER).name}/runs", "workflow_runs")
    receipts: dict[str, dict[str, dict]] = {}
    for run in runs:
        if (
            run["path"] != PUBLISHER
            or run["head_branch"] != "main"
            or run["event"] not in PUBLISHER_ORIGINS
            or run["repository"]["full_name"] != REPOSITORY
            or run["head_repository"]["full_name"] != REPOSITORY
        ):
            continue
        for attempt in range(1, run["run_attempt"] + 1):
            for job in api.pages(f"actions/runs/{run['id']}/attempts/{attempt}/jobs", "jobs"):
                match = JOB.fullmatch(job["name"])
                if match and job["conclusion"] == "success":
                    receipts.setdefault(match.group(1), {}).setdefault(
                        match.group(2), {"runId": run["id"], "runAttempt": attempt}
                    )
    return receipts


def reject_conflicts(receipts: dict, version: str, sha256: str) -> None:
    other = {v for v, hashes in receipts.items() if sha256 in hashes and v != version}
    if other:
        raise DeliveryError(f"Wheel hash {sha256} was already delivered as {sorted(other)}")
    differing = sorted(set(receipts.get(version, {})) - {sha256})
    if differing:
        raise DeliveryError(
            f"Version {version} was already delivered with a different wheel hash "
            f"{differing}; a changed product needs a new fork version"
        )


def current_run(api: Api, check: dict) -> dict:
    run = api.get(f"actions/runs/{check['runId']}")
    if (
        run["run_attempt"] != check["runAttempt"]
        or run["status"] != "completed"
        or run["conclusion"] != "success"
    ):
        raise DeliveryError(
            f"{check['workflow']} run {check['runId']} attempt changed after required checks; "
            "rerun delivery"
        )
    return run


def deliver(
    api: Api,
    root: Path,
    source: str,
    head: str,
    run: dict[str, Any],
    out: Path,
    expected: tuple[str, int, int] | None = None,
) -> dict:
    version, origin = release_source(root, source, head)
    if origin != source:
        raise NotReady(
            f"{source} only changes tooling after {version} was introduced at {origin}; "
            "deliver the version from its release source"
        )
    manifest = manifest_at(root, source)
    checks = required_checks(api, root, source, expected)
    canonical = next(c for c in checks if c["workflow"] == CANONICAL)
    artifact = canonical_artifact(api, current_run(api, canonical), source)
    bundle = read_bundle(
        api.artifact(artifact["id"]), artifact["digest"], version, source, manifest
    )
    sha256 = bundle["sums"][bundle["wheel"]]
    reject_conflicts(prior_deliveries(api), version, sha256)
    for check in checks:
        current_run(api, check)
    name = job_name(version, sha256)
    record = {
        "schema": SCHEMA,
        "kind": KIND,
        "version": version,
        "forkSha": source,
        "upstreamSha": manifest["upstream"]["sha"],
        "wheel": {
            "filename": bundle["wheel"],
            "sha256": sha256,
            "size": len(bundle["files"][bundle["wheel"]]),
        },
        "sdist": {"filename": bundle["sdist"], "sha256": bundle["sums"][bundle["sdist"]]},
        "source": {
            "repository": REPOSITORY,
            "workflowPath": CANONICAL,
            "event": "push",
            "ref": "refs/heads/main",
            "runId": canonical["runId"],
            "runAttempt": canonical["runAttempt"],
            "artifact": {
                "id": artifact["id"],
                "name": artifact["name"],
                "digest": artifact["digest"],
                "size": artifact["size_in_bytes"],
            },
        },
        "requiredChecks": checks,
        "delivery": {
            "repository": REPOSITORY,
            "workflowPath": PUBLISHER,
            "ref": "refs/heads/main",
            "event": run["event"],
            "commit": head,
            "runId": run["id"],
            "runAttempt": run["attempt"],
            "jobName": name,
            "artifactName": DELIVERY_ARTIFACT + str(run["attempt"]),
            "evidence": {
                "run": f"{API}/repos/{REPOSITORY}/actions/runs/{run['id']}",
                "jobs": f"{API}/repos/{REPOSITORY}/actions/runs/{run['id']}"
                f"/attempts/{run['attempt']}/jobs",
            },
        },
    }
    out.mkdir(parents=True)
    for filename in (bundle["wheel"], bundle["sdist"]):
        (out / filename).write_bytes(bundle["files"][filename])
    (out / "SHA256SUMS").write_bytes(bundle["files"]["SHA256SUMS"])
    (out / RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def emit(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(lines)
    print(lines, end="")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expect", help="workflow-path:run-id:attempt of the triggering run")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    env = os.environ
    root = Path.cwd()
    if env["GITHUB_REPOSITORY"] != REPOSITORY or env["GITHUB_REF"] != "refs/heads/main":
        raise DeliveryError("Delivery runs only from main of " + REPOSITORY)
    expected = None
    if args.expect:
        path, run_id, attempt = args.expect.rsplit(":", 2)
        expected = (path.split("@")[0], int(run_id), int(attempt))
    run = {
        "id": int(env["GITHUB_RUN_ID"]),
        "attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "event": env["GITHUB_EVENT_NAME"],
    }
    try:
        record = deliver(
            GitHub(env["GH_TOKEN"]), root, args.source, env["GITHUB_SHA"], run, args.out, expected
        )
    except NotReady as reason:
        if args.strict:
            print(f"::error::Not deliverable: {reason}")
            return 1
        print(f"::notice::No delivery yet: {reason}")
        emit({"verified": "false"})
        return 0
    except DeliveryError as error:
        print(f"::error::Delivery refused; fix in the fork: {error}")
        return 1
    emit(
        {
            "verified": "true",
            "version": record["version"],
            "sha256": record["wheel"]["sha256"],
            "wheel": record["wheel"]["filename"],
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
