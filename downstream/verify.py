from __future__ import annotations

import argparse
import email
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from downstream.build import ROOT, build
from downstream.relocate import PACKAGE, stage_suite


def run(command: list[str], cwd: Path, *, env: dict | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def wheel_payload(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if name.startswith(PACKAGE + "/")
        }


def inspect_artifacts(directory: Path, *, development: bool = False) -> tuple[Path, Path, dict]:
    wheels, sdists = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("Expected one wheel and one sdist")
    wheel, sdist = wheels[0], sdists[0]
    expected = (directory / "SHA256SUMS").read_text()
    actual = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in sorted([wheel, sdist])
    )
    if expected != actual:
        raise ValueError("Artifact SHA256SUMS do not match")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if not wheel.name.endswith("-py3-none-any.whl"):
            raise ValueError("Downstream wheels must be pure Python")
        if any(not (n.startswith(PACKAGE + "/") or n.startswith(PACKAGE + "-")) for n in names):
            raise ValueError("Unexpected wheel namespace")
        if any(n.endswith((".so", ".pyd", "/entry_points.txt")) for n in names):
            raise ValueError("Compiled modules and entry points are not admitted")
        metadata = email.message_from_bytes(
            archive.read(next(n for n in names if n.endswith("/METADATA")))
        )
        provenance = json.loads(archive.read(PACKAGE + "/benchbox_provenance.json"))
        if (
            provenance["schema"] != 1
            or provenance["distribution"] != "benchbox-sqlglot"
            or provenance["version"] != provenance["manifest"]["version"]
            or provenance["upstream_sha"] != provenance["manifest"]["upstream"]["sha"]
        ):
            raise ValueError("Invalid downstream provenance")
        if metadata["Name"] != "benchbox-sqlglot" or metadata["Version"] != provenance["version"]:
            raise ValueError("Distribution metadata and provenance disagree")
        if metadata.get_all("Provides-Extra") or metadata.get_all("Requires-Dist"):
            raise ValueError("The initial downstream runtime has no extras or dependencies")
        if provenance["dirty"] and not development:
            raise ValueError("Development artifacts cannot be published")
        assert PACKAGE + "/py.typed" in names
        license_path = next(n for n in names if n.endswith("/licenses/LICENSE"))
        assert archive.read(license_path) == (ROOT / "LICENSE").read_bytes()
    return wheel, sdist, provenance


def environment(destination: Path, cwd: Path) -> Path:
    run(["uv", "venv", "--python", sys.executable, str(destination)], cwd)
    return destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install(python: Path, cwd: Path, *packages: str | Path) -> None:
    run(["uv", "pip", "install", "--python", str(python), *map(str, packages)], cwd)


def runtime(python: Path, cwd: Path, *args: str) -> None:
    run([str(python), "-I", str(cwd / "smoke.py"), *args], cwd)


def fixture(root: Path, version: str) -> Path:
    project = root / "plugin-fixture"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        f"""[build-system]
requires = ["setuptools==80.9.0"]
build-backend = "setuptools.build_meta"
[project]
name = "sqlglot-coexistence-fixture"
version = "1.0"
dependencies = ["sqlglot==30.23.0", "benchbox-sqlglot=={version}"]
[project.entry-points."sqlglot.dialects"]
upstreamfixture = "upstream_fixture:Fixture"
[project.entry-points."benchbox_sqlglot.dialects"]
forkfixture = "fork_fixture:Fixture"
[tool.setuptools]
py-modules = ["upstream_fixture", "fork_fixture"]
"""
    )
    for name, package in [("upstream", "sqlglot"), ("fork", PACKAGE)]:
        (project / f"{name}_fixture.py").write_text(
            f"from {package} import Dialect\nclass Fixture(Dialect):\n    pass\n"
        )
    return project


def verify(
    directory: Path, *, full: bool = False, sdist_check: bool = False, development: bool = False
) -> None:
    wheel, sdist, provenance = inspect_artifacts(directory, development=development)
    with tempfile.TemporaryDirectory(prefix="benchbox-sqlglot-verify-") as temporary:
        work = Path(temporary)
        shutil.copyfile(ROOT / "downstream/smoke.py", work / "smoke.py")
        python = environment(work / "venv-wheel", work)
        install(python, work, wheel)
        runtime(python, work)
        run([str(python), "-I", "-m", PACKAGE, "--version"], work)
        if full:
            install(python, work, "duckdb>=0.6", "pandas", "pytz", "typing_extensions")
            stage_suite(ROOT, work)
            env = {
                **os.environ,
                "SKIP_INTEGRATION": "1",
                "PATH": str(python.parent) + os.pathsep + os.environ["PATH"],
            }
            env.pop("PYTHONPATH", None)
            run(
                [str(python), "-m", "unittest", "discover", "-s", "tests", "-t", "."], work, env=env
            )
        if sdist_check:
            unpacked = work / "unpacked"
            unpacked.mkdir()
            with tarfile.open(sdist) as archive:
                archive.extractall(unpacked, filter="data")
            projects = list(unpacked.iterdir())
            assert len(projects) == 1 and not (projects[0] / ".git").exists()
            rebuilt = work / "rebuilt"
            env = {**os.environ, "SOURCE_DATE_EPOCH": str(provenance["source_date_epoch"])}
            run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--wheel",
                    "--out-dir",
                    str(rebuilt),
                    str(projects[0]),
                ],
                work,
                env=env,
            )
            rebuilt_wheel = next(rebuilt.glob("*.whl"))
            assert wheel_payload(wheel) == wheel_payload(rebuilt_wheel), "Sdist runtime differs"
            assert wheel.read_bytes() == rebuilt_wheel.read_bytes(), (
                "Sdist wheel is not reproducible"
            )
            repeated = work / "repeated"
            build(ROOT, repeated, allow_dirty=development)
            assert wheel.read_bytes() == next(repeated.glob("*.whl")).read_bytes(), (
                "Clean assembly is not reproducible"
            )
            plugin = fixture(work, provenance["version"])
            install(python, work, wheel, plugin)
            for order in [[], ["--upstream-first"]]:
                runtime(python, work, "--coexist", *order)
            run(["uv", "pip", "uninstall", "--python", str(python), "sqlglot"], work)
            runtime(python, work)
            sdist_python = environment(work / "venv-sdist", work)
            install(sdist_python, work, rebuilt_wheel, plugin)
            runtime(sdist_python, work, "--coexist", "--upstream-first")
            run(["uv", "pip", "uninstall", "--python", str(sdist_python), "benchbox-sqlglot"], work)
            run(
                [
                    str(sdist_python),
                    "-I",
                    "-c",
                    "import sqlglot; assert sqlglot.parse_one('SELECT 1').sql() == 'SELECT 1'",
                ],
                work,
            )
    print("Downstream artifact verification passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--sdist", action="store_true")
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    verify(
        args.dist_dir.resolve(),
        full=args.full,
        sdist_check=args.sdist,
        development=args.development,
    )


if __name__ == "__main__":
    main()
