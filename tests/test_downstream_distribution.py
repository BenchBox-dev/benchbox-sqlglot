import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from downstream.build import stage_runtime
from downstream.relocate import PACKAGE, relocate, stage_suite
from downstream.verify import verify_zipimport

ROOT = Path(__file__).resolve().parents[1]


class TestDownstreamDistribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.work = Path(cls.temporary.name)
        cls.root = cls.work / "source"
        cls.root.mkdir()
        shutil.copytree(
            ROOT / "sqlglot",
            cls.root / "sqlglot",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.so", "*.pyd", "_version.py"),
        )
        shutil.copyfile(ROOT / "LICENSE", cls.root / "LICENSE")

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(cls.root), *args], text=True, stderr=subprocess.DEVNULL
            ).strip()

        cls.git = staticmethod(git)
        git("init")
        git("config", "user.name", "Test Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "commit.gpgsign", "false")
        git("config", "tag.gpgsign", "false")
        git("add", ".")
        git("commit", "-m", "Fixture upstream")
        git("tag", "v30.24.0")
        manifest = json.loads((ROOT / "downstream/patches.json").read_text(encoding="utf-8"))
        manifest["upstream"]["sha"] = git("rev-parse", "HEAD")
        (cls.root / "downstream").mkdir()
        (cls.root / "downstream/patches.json").write_text(json.dumps(manifest), encoding="utf-8")
        git("add", ".")
        git("commit", "-m", "Fixture downstream")

    def test_nested_wheel_zipimport_without_installation_or_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            runtime = work / "runtime"
            stage_runtime(self.root, runtime)
            wheel = work / "benchbox_sqlglot_fixture-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(runtime.rglob("*")):
                    if path.is_file():
                        archive.write(
                            path, "benchbox/_vendor/sqlglot/" + path.relative_to(runtime).as_posix()
                        )
            verify_zipimport(wheel, Path(sys.executable), work / "host")

    def test_private_runtime_bindings_dynamic_dialects_and_plugins(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            package = work / "benchbox/_vendor/sqlglot"
            stage_runtime(self.root, package)
            for parent in (work / "benchbox", work / "benchbox/_vendor"):
                (parent / "__init__.py").write_text("", encoding="utf-8")
            source = """import sqlglot
import sqlglot.optimizer
import sqlglot.parser as parser
from sqlglot import exp
assert sqlglot.parse_one("SELECT 1").sql() == "SELECT 1"
assert parser.Parser is sqlglot.parser.Parser
folded = sqlglot.optimizer.optimize(sqlglot.parse_one("SELECT 1 + 2 AS n"))
assert folded.expressions[0].this == exp.Literal.number(3)
assert isinstance(sqlglot.parse_one("SELECT 1"), exp.Expression)
"""
            (work / "bindings.py").write_text(relocate(source, "fixture.py"), encoding="utf-8")
            (work / "fixture_plugin.py").write_text(
                f"from {PACKAGE} import Dialect\nclass FixturePlugin(Dialect):\n    pass\n",
                encoding="utf-8",
            )
            metadata = work / "fixture_plugin-1.0.dist-info"
            metadata.mkdir()
            (metadata / "METADATA").write_text(
                "Name: fixture-plugin\nVersion: 1.0\n", encoding="utf-8"
            )
            (metadata / "entry_points.txt").write_text(
                f"[{PACKAGE}.dialects]\nprivatefixture = fixture_plugin:FixturePlugin\n"
                "[sqlglot.dialects]\nupstreamfixture = fixture_plugin:FixturePlugin\n",
                encoding="utf-8",
            )
            probe = """import bindings
import importlib
import logging
import sys
from benchbox._vendor import sqlglot
for dialect in sqlglot.Dialects:
    assert sqlglot.Dialect.get_or_raise(dialect).__class__.__module__.startswith("benchbox._vendor.sqlglot.")
assert sqlglot.Dialect.get("privatefixture").__module__ == "fixture_plugin"
assert sqlglot.Dialect.get("upstreamfixture") is None
assert importlib.import_module("benchbox._vendor.sqlglot.parser").logger.name == "sqlglot"
marked = "SELECT 'sqlglot.parser', a /* sqlglot.meta x */ FROM t"
rendered = sqlglot.parse_one(marked).sql()
assert "'sqlglot.parser'" in rendered and "/* sqlglot.meta x */" in rendered
assert not any(n == "sqlglot" or n.startswith("sqlglot.") for n in sys.modules)
"""
            (work / "probe.py").write_text(probe, encoding="utf-8")
            subprocess.run([sys.executable, "-B", "probe.py"], cwd=work, check=True)

    def test_deterministic_staging_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary) / "first", Path(temporary) / "second"
            provenance = stage_runtime(self.root, first)
            self.assertEqual(provenance, stage_runtime(self.root, second))
            payload = {
                p.relative_to(first).as_posix(): p.read_bytes()
                for p in first.rglob("*")
                if p.is_file()
            }
            self.assertEqual(
                payload,
                {
                    p.relative_to(second).as_posix(): p.read_bytes()
                    for p in second.rglob("*")
                    if p.is_file()
                },
            )
            self.assertEqual(
                provenance["repository"], "https://github.com/BenchBox-dev/benchbox-sqlglot"
            )
            self.assertEqual(provenance["namespace"], PACKAGE)
            self.assertEqual(provenance["fork_sha"], self.git("rev-parse", "HEAD"))
            self.assertFalse(provenance["dirty"])
            self.assertEqual(
                provenance["files"],
                {
                    name: hashlib.sha256(data).hexdigest()
                    for name, data in payload.items()
                    if name != "benchbox_provenance.json"
                },
            )
            for name in ("LICENSE", "py.typed", "_version.py", "__init__.py"):
                self.assertIn(name, provenance["files"])
            self.assertFalse(any(name.endswith((".pyc", ".so", ".pyd")) for name in payload))
            self.assertEqual(payload["LICENSE"], (ROOT / "LICENSE").read_bytes())
            with self.assertRaisesRegex(ValueError, "must not exist"):
                stage_runtime(self.root, first)

    def test_dirty_tracked_and_untracked_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            for path in (self.root / "LICENSE", self.root / "untracked.txt"):
                original = path.read_bytes() if path.exists() else None
                try:
                    path.write_bytes(b"dirty")
                    destination = Path(temporary) / "runtime"
                    with self.assertRaisesRegex(ValueError, "clean tracked and untracked"):
                        stage_runtime(self.root, destination)
                    self.assertFalse(destination.exists())
                finally:
                    if original is None:
                        path.unlink()
                    else:
                        path.write_bytes(original)

    def test_unreviewed_dynamic_references_fail(self):
        for source in ['NAME = "sqlglot.new_module"', 'NAME = f"sqlglot.{module}"']:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "Unreviewed"):
                relocate(source, "fixture.py")

    def test_target_namespace_is_parameterized(self):
        for namespace in (PACKAGE, "fixture.runtime"):
            scope = {}
            exec(
                relocate(
                    'PATH = f"sqlglot.dialects.{key}"',
                    "dialects/dialect.py",
                    namespace=namespace,
                ),
                {"key": "sqlite"},
                scope,
            )
            self.assertEqual(scope["PATH"], namespace + ".dialects.sqlite")

    def test_suite_preserves_sql_fixtures(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            stage_suite(ROOT, destination, namespace=PACKAGE)
            for fixture in (ROOT / "tests/fixtures").rglob("*"):
                if fixture.is_file():
                    self.assertEqual(
                        fixture.read_bytes(),
                        (destination / "tests" / fixture.relative_to(ROOT / "tests")).read_bytes(),
                    )
            self.assertFalse((destination / "tests/test_downstream_contract.py").exists())
            self.assertFalse((destination / "tests/test_downstream_distribution.py").exists())
