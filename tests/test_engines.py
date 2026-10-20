"""Seed tests proving the real-engine harness: each engine executes SQL that
was translated from Postgres by ``sqlglot.transpile``.

Engines that are not installed (or, for services, not running) are skipped
with a named reason unless ``BENCHBOX_ENGINES_REQUIRED`` lists them.
"""

import os
import unittest
from unittest import mock

import sqlglot
from tests import engines
from tests.engines import ENGINES, requires_engine

# Postgres-only syntax (the `::` cast) plus a date extraction, which several
# targets spell differently.
POSTGRES_SQL = (
    "SELECT 'ab'::text || 'c' AS s, EXTRACT(YEAR FROM CAST('2024-03-05' AS DATE))::int AS y"
)
EXPECTED = ("abc", "2024")


def normalize(rows):
    return [tuple(str(value) for value in row) for row in rows]


class EngineSeedMixin:
    engine = ""

    def transpile(self, sql=POSTGRES_SQL):
        return sqlglot.transpile(sql, read="postgres", write=ENGINES[self.engine].dialect)[0]

    def test_arithmetic(self):
        rows = engines.execute(self.engine, self.transpile("SELECT 1 + 1"))
        self.assertEqual(normalize(rows), [("2",)])

    def test_transpiled_from_postgres(self):
        rows = engines.execute(self.engine, self.transpile())
        self.assertEqual(normalize(rows), [EXPECTED])


@requires_engine("sqlite")
class TestSqliteEngine(EngineSeedMixin, unittest.TestCase):
    engine = "sqlite"


@requires_engine("duckdb")
class TestDuckDBEngine(EngineSeedMixin, unittest.TestCase):
    engine = "duckdb"


@requires_engine("datafusion")
class TestDataFusionEngine(EngineSeedMixin, unittest.TestCase):
    engine = "datafusion"


@requires_engine("chdb")
class TestChdbEngine(EngineSeedMixin, unittest.TestCase):
    engine = "chdb"


@requires_engine("pyspark")
class TestPySparkEngine(EngineSeedMixin, unittest.TestCase):
    engine = "pyspark"


@requires_engine("postgres")
class TestPostgresEngine(EngineSeedMixin, unittest.TestCase):
    engine = "postgres"


@requires_engine("trino")
class TestTrinoEngine(EngineSeedMixin, unittest.TestCase):
    engine = "trino"


@requires_engine("starrocks")
class TestStarRocksEngine(EngineSeedMixin, unittest.TestCase):
    engine = "starrocks"


@requires_engine("doris")
class TestDorisEngine(EngineSeedMixin, unittest.TestCase):
    engine = "doris"


@requires_engine("questdb")
class TestQuestDBEngine(EngineSeedMixin, unittest.TestCase):
    engine = "questdb"


@requires_engine("singlestore")
class TestSingleStoreEngine(EngineSeedMixin, unittest.TestCase):
    engine = "singlestore"


class TestEngineHelper(unittest.TestCase):
    def test_stdlib_engine_always_available(self):
        self.assertTrue(engines.is_available("sqlite"))
        self.assertIn("sqlite", engines.available_engines())

    def test_missing_package_reason_is_named(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            reason = engines.unavailable_reason("duckdb")
        self.assertIn("duckdb", reason)
        self.assertIn("not installed", reason)

    def test_service_without_endpoint_is_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BENCHBOX_ENGINE_TRINO", None)
            with mock.patch("importlib.util.find_spec", return_value=object()):
                reason = engines.unavailable_reason("trino")
        self.assertIn("BENCHBOX_ENGINE_TRINO", reason)

    def test_required_engines_parsing(self):
        with mock.patch.dict(os.environ, {engines.REQUIRED_ENV: "1"}):
            self.assertEqual(engines.required_engines(), set(ENGINES))
        with mock.patch.dict(os.environ, {engines.REQUIRED_ENV: "duckdb, sqlite"}):
            self.assertEqual(engines.required_engines(), {"duckdb", "sqlite"})
        with mock.patch.dict(os.environ, {engines.REQUIRED_ENV: "nonesuch"}):
            with self.assertRaises(ValueError):
                engines.required_engines()
        with mock.patch.dict(os.environ, {engines.REQUIRED_ENV: ""}):
            self.assertEqual(engines.required_engines(), set())

    def test_decorator_skips_locally_and_fails_when_required(self):
        @requires_engine("duckdb")
        def body():
            return "ran"

        with mock.patch("importlib.util.find_spec", return_value=None):
            with mock.patch.dict(os.environ, {engines.REQUIRED_ENV: ""}):
                with self.assertRaises(unittest.SkipTest) as skipped:
                    body()
            self.assertIn("duckdb", str(skipped.exception))
            with mock.patch.dict(os.environ, {engines.REQUIRED_ENV: "duckdb"}):
                with self.assertRaises(AssertionError) as failed:
                    body()
            self.assertIn("Required engine is unavailable", str(failed.exception))
            # A requirement for another engine does not affect this one.
            with mock.patch.dict(os.environ, {engines.REQUIRED_ENV: "sqlite"}):
                with self.assertRaises(unittest.SkipTest):
                    body()

    def test_unknown_engine_is_rejected(self):
        with self.assertRaises(ValueError):
            requires_engine("nonesuch")


if __name__ == "__main__":
    unittest.main()
