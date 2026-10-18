from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from downstream.build import ROOT, git


def command(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], check=check)


def manifest(root: Path) -> dict:
    return json.loads((root / "downstream/patches.json").read_text())


def clean(root: Path) -> None:
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Refresh operations require a clean source tree")


def carried(data: dict, retired: set[str]) -> list[dict]:
    patches = [p for p in data["patches"] if p["status"] == "carried"]
    if not retired <= {p["id"] for p in patches}:
        raise ValueError("Only currently carried patches may be retired")
    return [p for p in patches if p["id"] not in retired]


def owned_paths(data: dict) -> list[str]:
    return sorted(
        {path for p in data["patches"] if p["status"] == "carried" for path in p["paths"]}
    )


def validate_tag(root: Path, data: dict, tag: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Select an explicit upstream X.Y.Z tag")
    if tuple(map(int, tag[1:].split("."))) <= tuple(
        map(int, data["upstream"]["tag"][1:].split("."))
    ):
        raise ValueError("Refresh tag must be newer than the recorded upstream base")
    return git(root, "rev-parse", "refs/tags/" + tag + "^{commit}")


def candidate(root: Path, tag: str, destination: Path, branch: str, retired: set[str]) -> None:
    clean(root)
    data = manifest(root)
    sha = validate_tag(root, data, tag)
    patches = carried(data, retired)
    command(root, "worktree", "add", "-b", branch, str(destination), sha)
    for patch in patches:
        patch_path = root / patch["patch"]
        if not patch_path.is_file():
            raise ValueError(f"Missing normalized patch: {patch_path}")
        command(destination, "apply", "--3way", "--index", str(patch_path))
        command(destination, "commit", "-m", f"fix: retain {patch['id']}")
    print(
        f"Candidate source ready at {destination}; resolve any conflicts explicitly and run source checks"
    )


def integrate(
    root: Path,
    tag: str,
    candidate_ref: str,
    destination: Path,
    branch: str,
    retirements: dict[str, str],
) -> None:
    clean(root)
    data = manifest(root)
    sha = validate_tag(root, data, tag)
    candidate_sha = git(root, "rev-parse", candidate_ref + "^{commit}")
    command(root, "merge-base", "--is-ancestor", sha, candidate_sha)
    paths = owned_paths(data)
    changed = set(git(root, "diff", "--name-only", sha, candidate_sha).splitlines())
    if not changed <= set(paths):
        raise ValueError("Candidate includes changes outside the reviewed patch source paths")
    patches = carried(data, set(retirements))
    if any(not reason.strip() for reason in retirements.values()):
        raise ValueError("Retirement requires a reviewed reason and upstream evidence")
    commits = git(
        root, "rev-list", "--reverse", "--no-merges", f"{sha}..{candidate_sha}"
    ).splitlines()
    if len(commits) != len(patches):
        raise ValueError("Candidate must contain exactly one reviewed commit per retained patch")
    for patch, commit in zip(patches, commits):
        if git(root, "show", "-s", "--format=%s", commit) != f"fix: retain {patch['id']}":
            raise ValueError("Candidate patch order does not match the manifest")
    command(root, "worktree", "add", "-b", branch, str(destination), "HEAD")
    merge = command(destination, "merge", "--no-ff", "--no-commit", sha, check=False)
    conflicts = set(git(destination, "diff", "--name-only", "--diff-filter=U").splitlines())
    if conflicts - set(paths) or (merge.returncode and not conflicts):
        raise ValueError(
            "Upstream merge needs manual resolution outside the patch-owned source paths"
        )
    if paths:
        command(
            destination,
            "restore",
            "--source",
            candidate_sha,
            "--staged",
            "--worktree",
            "--",
            *paths,
        )
        command(destination, "diff", "--exit-code", "--cached", candidate_sha, "--", *paths)
    data["upstream"]["tag"] = tag
    data["upstream"]["sha"] = sha
    data["version"] = tag[1:] + ".1"
    for patch, commit in zip(patches, commits):
        content = subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--binary",
                commit + "^",
                commit,
                "--",
                *patch["paths"],
            ]
        )
        (destination / patch["patch"]).write_bytes(content)
        command(destination, "add", "--", patch["patch"])
    for patch in data["patches"]:
        if patch["id"] in retirements:
            reason = retirements[patch["id"]].strip()
            patch["status"] = "upstreamed"
            patch["reason"] = reason
    (destination / "downstream/patches.json").write_text(json.dumps(data, indent=2) + "\n")
    command(destination, "add", "--", "downstream/patches.json")
    command(destination, "commit", "-m", f"chore: refresh SQLGlot from upstream {tag}")
    print(
        f"Integration source equals candidate on {len(paths)} patch-owned paths; run all artifact and consumer checks"
    )


def latest(root: Path) -> None:
    data = manifest(root)
    response = subprocess.check_output(
        ["git", "ls-remote", "--tags", data["upstream"]["repository"], "refs/tags/v*"], text=True
    )
    tags = []
    for line in response.splitlines():
        sha, ref = line.split()
        match = re.fullmatch(r"refs/tags/v(\d+)\.(\d+)\.(\d+)", ref)
        if match:
            tags.append((tuple(map(int, match.groups())), ref.removeprefix("refs/tags/"), sha))
    version, tag, sha = max(tags)
    current = tuple(map(int, data["upstream"]["tag"][1:].split(".")))
    print(
        json.dumps(
            {
                "current": data["upstream"],
                "latest_tag": tag,
                "tag_object": sha,
                "refresh_available": version > current,
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("latest")
    for action in ("candidate", "integrate"):
        sub = commands.add_parser(action)
        sub.add_argument("--tag", required=True)
        sub.add_argument("--path", type=Path, required=True)
        sub.add_argument("--branch", required=True)
        sub.add_argument("--retire", action="append", default=[])
        if action == "integrate":
            sub.add_argument("--candidate", required=True)
    args = parser.parse_args()
    if args.action == "latest":
        latest(ROOT)
    elif args.action == "candidate":
        candidate(ROOT, args.tag, args.path.resolve(), args.branch, set(args.retire))
    else:
        retirements = dict(value.split("=", 1) for value in args.retire)
        integrate(ROOT, args.tag, args.candidate, args.path.resolve(), args.branch, retirements)


if __name__ == "__main__":
    main()
