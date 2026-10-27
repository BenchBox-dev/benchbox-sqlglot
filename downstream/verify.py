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
            if name.startswith(PACKAGE.replace(".", "/") + "/")
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
        runtime_prefix = PACKAGE.replace(".", "/") + "/"
        if any(not (n.startswith("benchbox/") or n.startswith("benchbox_sqlglot-")) for n in names):
            raise ValueError("Unexpected verification fixture namespace")
        if any(n.endswith((".so", ".pyd", "/entry_points.txt")) for n in names):
            raise ValueError("Compiled modules and entry points are not admitted")
        metadata = email.message_from_bytes(
            archive.read(next(n for n in names if n.endswith("/METADATA")))
        )
        provenance = json.loads(archive.read(runtime_prefix + "benchbox_provenance.json"))
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
            raise ValueError("Development artifacts require explicit verification opt-in")
        assert runtime_prefix + "py.typed" in names
        payload = {
            name[len(runtime_prefix) :]: hashlib.sha256(archive.read(name)).hexdigest()
            for name in names
            if name.startswith(runtime_prefix) and not name.endswith("/benchbox_provenance.json")
        }
        assert provenance["namespace"] == PACKAGE
        assert provenance["files"] == payload
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
dependencies = ["sqlglot==30.25.0", "benchbox-sqlglot=={version}"]
[project.entry-points."sqlglot.dialects"]
upstreamfixture = "upstream_fixture:Fixture"
[project.entry-points."benchbox._vendor.sqlglot.dialects"]
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


def verify_zipimport(wheel: Path, python: Path, destination: Path) -> None:
    """Exercise the built wheel as nested package data, without installation or extraction."""
    vendor = destination / "benchbox/_vendor"
    vendor.mkdir(parents=True)
    (vendor.parent / "__init__.py").write_text("", encoding="utf-8")
    (vendor / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"__path__.append(str(Path(__file__).parent / {wheel.name!r}) + '/benchbox/_vendor')\n",
        encoding="utf-8",
    )
    shutil.copyfile(wheel, vendor / wheel.name)
    (destination / "private_plugin.py").write_text(
        f"from {PACKAGE} import Dialect\nclass PrivatePlugin(Dialect):\n    pass\n",
        encoding="utf-8",
    )
    metadata = destination / "private_plugin-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: private-plugin\nVersion: 1.0\n", encoding="utf-8")
    (metadata / "entry_points.txt").write_text(
        f"[{PACKAGE}.dialects]\nzipfixture = private_plugin:PrivatePlugin\n",
        encoding="utf-8",
    )
    probe = destination / "probe.py"
    probe.write_text(
        """import importlib
import importlib.resources
import json
import pickle
import sys
import zipimport

original_path = list(sys.path)
from benchbox._vendor import sqlglot
assert sys.path == original_path
assert isinstance(sqlglot.__loader__, zipimport.zipimporter)
assert ".whl/benchbox/_vendor/sqlglot/" in sqlglot.__file__
import benchbox._vendor.sqlglot.optimizer
folded = sqlglot.optimizer.optimize(sqlglot.parse_one("SELECT 1 + 2 AS n"))
assert folded.expressions[0].this == sqlglot.exp.Literal.number(3)
for dialect in sqlglot.Dialects:
    assert sqlglot.Dialect.get_or_raise(dialect).__class__.__module__.startswith(
        "benchbox._vendor.sqlglot."
    )
assert sqlglot.Dialect.get("zipfixture").__module__ == "private_plugin"
resources = importlib.resources.files(sqlglot)
assert resources.joinpath("LICENSE").read_text(encoding="utf-8")
assert resources.joinpath("py.typed").is_file()
assert json.loads(resources.joinpath("benchbox_provenance.json").read_text())["namespace"] == sqlglot.__name__
assert importlib.import_module(sqlglot.__name__ + ".parser").logger.name == "sqlglot"
expression = sqlglot.parse_one("SELECT 1")
assert pickle.loads(pickle.dumps(expression)) == expression
assert not any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules)
""",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    run([str(python), "-B", str(probe)], destination, env=env)


def verify(
    directory: Path, *, full: bool = False, sdist_check: bool = False, development: bool = False
) -> None:
    wheel, sdist, provenance = inspect_artifacts(directory, development=development)
    with tempfile.TemporaryDirectory(prefix="benchbox-sqlglot-verify-") as temporary:
        work = Path(temporary)
        shutil.copyfile(ROOT / "downstream/smoke.py", work / "smoke.py")
        python = environment(work / "venv-wheel", work)
        verify_zipimport(wheel, python, work / "zipimport-host")
        install(python, work, wheel)
        runtime(python, work)
        run([str(python), "-I", "-m", PACKAGE, "--version"], work)
        if full:
            install(
                python,
                work,
                "duckdb>=2; python_version >= '3.11'",
                "duckdb<2; python_version < '3.11'",
                "pandas",
                "pytz",
                "typing_extensions",
            )
            stage_suite(ROOT, work, namespace=PACKAGE)
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
