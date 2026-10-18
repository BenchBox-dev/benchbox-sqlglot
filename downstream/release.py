from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from downstream.build import ROOT, git
from downstream.verify import inspect_artifacts


def validate_sequence(version: str, tags: list[str]) -> None:
    current = tuple(map(int, version.split(".")))
    previous = [
        tuple(map(int, tag.removeprefix("benchbox-v").split(".")))
        for tag in tags
        if re.fullmatch(r"benchbox-v\d+\.\d+\.\d+\.[1-9]\d*", tag) and tag != "benchbox-v" + version
    ]
    if not previous:
        if current[-1] != 1:
            raise ValueError(
                "The first release on an upstream base starts at downstream revision one"
            )
        return
    latest = max(previous)
    if current[:3] < latest[:3]:
        raise ValueError("Published upstream bases cannot regress")
    expected = latest[-1] + 1 if current[:3] == latest[:3] else 1
    if current[-1] != expected:
        raise ValueError(
            "Increment the downstream revision on the same base; reset it on a new base"
        )


def validate(directory: Path, tag: str, wheel_hash: str, sdist_hash: str) -> None:
    wheel, sdist, provenance = inspect_artifacts(directory)
    if tag != "benchbox-v" + provenance["version"]:
        raise ValueError("Release tag does not match the downstream version")
    if git(ROOT, "rev-parse", "refs/tags/" + tag + "^{commit}") != provenance["fork_sha"]:
        raise ValueError("Release tag must identify the exact verified source commit")
    if git(ROOT, "rev-parse", "HEAD") != provenance["fork_sha"]:
        raise ValueError("Release checkout and artifact provenance disagree")
    validate_sequence(provenance["version"], git(ROOT, "tag", "--list", "benchbox-v*").splitlines())
    for artifact, expected in [(wheel, wheel_hash), (sdist, sdist_hash)]:
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Approval must name both exact SHA256 hashes")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
            raise ValueError("Artifact bytes differ from the approved hashes")


def publishing_environment(run_id: str) -> None:
    repository = os.environ["GITHUB_REPOSITORY"]
    if repository != "joeharris76/sqlglot" or not run_id.isdigit():
        raise ValueError("Unexpected publishing repository or candidate run")

    def api(path: str) -> dict:
        return json.loads(subprocess.check_output(["gh", "api", path], text=True))

    run = api(f"repos/{repository}/actions/runs/{run_id}")
    if (
        run["conclusion"] != "success"
        or run["event"] != "push"
        or run["head_branch"] != "main"
        or run["head_sha"] != git(ROOT, "rev-parse", "HEAD")
        or run["path"] != ".github/workflows/benchbox-candidate.yml"
    ):
        raise ValueError(
            "Candidate must come from successful downstream CI on this exact main commit"
        )
    environment = api(f"repos/{repository}/environments/benchbox-sqlglot-pypi")
    if not any(
        rule["type"] == "required_reviewers" and rule.get("reviewers")
        for rule in environment["protection_rules"]
    ):
        raise ValueError("Owner must configure required reviewers on the publishing environment")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--sdist-sha256", required=True)
    parser.add_argument("--candidate-run", required=True)
    args = parser.parse_args()
    validate(args.dist_dir, args.tag, args.wheel_sha256, args.sdist_sha256)
    publishing_environment(args.candidate_run)
    print("Exact approved downstream artifacts and owner publishing controls verified")


if __name__ == "__main__":
    main()
