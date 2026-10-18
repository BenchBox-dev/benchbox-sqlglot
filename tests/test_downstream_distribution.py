import ast
import tempfile
import unittest
from pathlib import Path

from downstream.relocate import relocate, stage_suite


class TestDownstreamDistribution(unittest.TestCase):
    def test_import_bindings_and_sql_literals(self):
        source = """import sqlglot
import sqlglot.optimizer
import sqlglot.parser as parser
from sqlglot import exp
SQL = "SELECT 'sqlglot.parser', sqlglot.foo /* sqlglot.meta x */"
"""
        result = relocate(source, "fixture.py")
        self.assertIn("import benchbox_sqlglot as sqlglot", result)
        self.assertIn(
            "import benchbox_sqlglot.optimizer; import benchbox_sqlglot as sqlglot", result
        )
        self.assertIn("import benchbox_sqlglot.parser as parser", result)
        self.assertIn("from benchbox_sqlglot import exp", result)
        self.assertEqual(
            ast.parse(source).body[-1].value.value, ast.parse(result).body[-1].value.value
        )

    def test_unreviewed_dynamic_references_fail(self):
        for source in ['NAME = "sqlglot.new_module"', 'NAME = f"sqlglot.{module}"']:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "Unreviewed"):
                relocate(source, "fixture.py")

    def test_dynamic_paths_and_comment_markers(self):
        self.assertIn(
            "benchbox_sqlglot.dialects.",
            relocate('mod = f"sqlglot.dialects.{key}"', "dialects/dialect.py"),
        )
        markers = 'META = "sqlglot.meta"\nANONYMOUS = "sqlglot.anonymous"\n'
        self.assertEqual(relocate(markers, "expressions/core.py"), markers)

    def test_suite_preserves_sql_fixtures(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            stage_suite(root, destination)
            for fixture in (root / "tests/fixtures").rglob("*"):
                if fixture.is_file():
                    self.assertEqual(
                        fixture.read_bytes(),
                        (destination / "tests" / fixture.relative_to(root / "tests")).read_bytes(),
                    )
            self.assertFalse((destination / "tests/test_downstream_contract.py").exists())
            self.assertIn(
                "benchbox_sqlglot.parser.logger",
                (destination / "tests/test_parser.py").read_text(),
            )
