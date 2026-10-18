from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from downstream.relocate import PACKAGE, relocate

BUILD_REQUIREMENTS = ["setuptools==80.9.0", "wheel==0.45.1"]
ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def fetch_upstream_tag(root: Path) -> None:
    manifest = json.loads((root / "downstream/patches.json").read_text())
    repository, tag = manifest["upstream"]["repository"], manifest["upstream"]["tag"]
    if repository != "https://github.com/tobymao/sqlglot" or not re.fullmatch(
        r"v\d+\.\d+\.\d+", tag
    ):
        raise ValueError("Unexpected upstream repository or tag")
    ref = "refs/tags/" + tag
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            repository,
            f"{ref}:{ref}",
        ],
        check=True,
    )
    if git(root, "rev-parse", ref + "^{commit}") != manifest["upstream"]["sha"]:
        raise ValueError("Fetched upstream tag does not match the recorded SHA")


def stage(root: Path, destination: Path, *, allow_dirty: bool = False) -> dict:
    manifest = json.loads((root / "downstream/patches.json").read_text())
    if (
        manifest["schema"] != 1
        or manifest["distribution"] != "benchbox-sqlglot"
        or manifest["import_package"] != PACKAGE
        or not re.fullmatch(r"\d+\.\d+\.\d+\.[1-9]\d*", manifest["version"])
        or manifest["upstream"]["tag"] != "v" + manifest["version"].rsplit(".", 1)[0]
    ):
        raise ValueError("Invalid downstream release manifest")
    dirty = bool(git(root, "status", "--porcelain", "--untracked-files=all"))
    if dirty and not allow_dirty:
        raise ValueError("Release assembly requires a clean tracked and untracked source tree")
    upstream = manifest["upstream"]["sha"]
    if git(root, "rev-parse", manifest["upstream"]["tag"] + "^{commit}") != upstream:
        raise ValueError("Upstream tag and manifest SHA disagree")
    subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", upstream, "HEAD"], check=True
    )
    if destination.exists():
        raise ValueError("Assembly destination must not exist")
    destination.mkdir(parents=True)
    package = destination / PACKAGE
    for path in sorted((root / "sqlglot").rglob("*.py")):
        relative = path.relative_to(root / "sqlglot")
        if relative.as_posix() == "_version.py":
            continue
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relocate(path.read_text(), relative.as_posix()))
    shutil.copyfile(root / "sqlglot/py.typed", package / "py.typed")
    version = manifest["version"]
    (package / "_version.py").write_text(
        f"__version__ = {version!r}\n__version_tuple__ = {tuple(map(int, version.split('.')))!r}\n"
    )
    provenance = {
        "schema": 1,
        "distribution": manifest["distribution"],
        "version": version,
        "upstream_sha": upstream,
        "fork_sha": git(root, "rev-parse", "HEAD"),
        "source_date_epoch": int(git(root, "show", "-s", "--format=%ct", "HEAD")),
        "dirty": dirty,
        "manifest": manifest,
    }
    (package / "benchbox_provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    shutil.copyfile(root / "LICENSE", destination / "LICENSE")
    shutil.copyfile(root / "downstream/README.md", destination / "README.md")
    requirements = json.dumps(BUILD_REQUIREMENTS)
    (destination / "pyproject.toml").write_text(
        f'''[build-system]
requires = {requirements}
build-backend = "setuptools.build_meta"

[project]
name = "benchbox-sqlglot"
version = "{version}"
description = "BenchBox maintained SQLGlot with bounded downstream SQL fixes"
readme = "README.md"
requires-python = ">=3.11"
license = "MIT"
license-files = ["LICENSE"]
authors = [{{name = "Toby Mao and SQLGlot contributors"}}]

[project.urls]
Repository = "https://github.com/joeharris76/sqlglot"
Upstream = "https://github.com/tobymao/sqlglot"
Issues = "https://github.com/joeharris76/sqlglot/issues"

[tool.setuptools.packages.find]
include = ["benchbox_sqlglot", "benchbox_sqlglot.*"]

[tool.setuptools.package-data]
benchbox_sqlglot = ["py.typed", "benchbox_provenance.json"]
'''
    )
    return provenance


def build(root: Path, output: Path, *, allow_dirty: bool = False) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError("Build output must be empty; existing artifacts are never overwritten")
    with tempfile.TemporaryDirectory(prefix="benchbox-sqlglot-build-") as temporary:
        project = Path(temporary) / "project"
        provenance = stage(root, project, allow_dirty=allow_dirty)
        env = {**os.environ, "SOURCE_DATE_EPOCH": str(provenance["source_date_epoch"])}
        subprocess.run(
            ["uv", "build", "--sdist", "--wheel", "--out-dir", str(output.resolve()), str(project)],
            env=env,
            check=True,
        )
    artifacts = sorted([*output.glob("*.whl"), *output.glob("*.tar.gz")])
    if len(artifacts) != 2:
        raise ValueError(
            "Expected exactly one downstream wheel and sdist; use an empty output directory"
        )
    (output / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in artifacts)
    )
    return provenance


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist/benchbox")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--fetch-upstream-tag", action="store_true")
    args = parser.parse_args()
    if args.fetch_upstream_tag:
        fetch_upstream_tag(ROOT)
        return
    build(ROOT, args.out_dir, allow_dirty=args.allow_dirty)


if __name__ == "__main__":
    main()
