from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from downstream.relocate import relocate

CONSUMER_TESTS = [
    "tests/unit/utils/test_dialect_utils.py",
    "tests/unit/platforms/test_base_adapter.py",
    "tests/unit/platforms/test_sql_generation_dialects.py",
    "tests/unit/core/test_tpch_named_column_alias_translation.py",
    "tests/unit/sql_compat/test_sqlite_discount_bounds.py",
    "tests/unit/platforms/test_sqlite_adapter.py",
    "tests/unit/tpchavoc/test_cloud_compat.py",
    "tests/unit/tpchavoc/test_dialect_compat.py",
    "tests/unit/sql_compat/test_translate_sql_delegation.py",
    "tests/unit/test_base_advanced.py",
]

CONSUMER_CONTRACT = """import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import benchbox_sqlglot
import pytest
from benchbox.base import BaseBenchmark
from benchbox.platforms.base.dialect_translation import DialectTranslationMixin
from benchbox.utils.dialect_utils import translate_sql_query

pytestmark = [pytest.mark.unit, pytest.mark.fast]

class Adapter(DialectTranslationMixin):
    _dialect = "sqlite"
    logger = logging.getLogger("consumer-contract")

@pytest.mark.parametrize("query, expected", [
    ("SELECT EXTRACT(YEAR FROM DATE '2020-06-01') / 3 > 673", (1,)),
    ("WITH t AS (SELECT EXTRACT(YEAR FROM DATE '2020-06-01') AS y) SELECT CAST(y AS INTEGER) / 3 > 673, y / 3 > 673 FROM t", (0, 1)),
    ("SELECT DATE '2024-02-29' + INTERVAL '1' DAY", ("2024-03-01 00:00:00",)),
    ("SELECT CAST(NULL AS DATE) + INTERVAL '1' DAY", (None,)),
    ("SELECT -EXTRACT(YEAR FROM DATE '2020-06-01') / 3 < -673", (1,)),
])
@pytest.mark.parametrize("entrypoint", ["utility", "mixin", "base"])
def test_real_translation_entrypoint(query, expected, entrypoint):
    with patch.object(benchbox_sqlglot, "transpile", wraps=benchbox_sqlglot.transpile) as transpile:
        if entrypoint == "utility":
            translated = translate_sql_query(query, "sqlite", source_dialect="netezza", identify=True)
        elif entrypoint == "mixin":
            translated = Adapter().translate_sql(query, source_dialect="netezza")
        else:
            benchmark = SimpleNamespace(get_query=lambda _: query)
            translated = BaseBenchmark.translate_query(benchmark, "fixture", "sqlite")
        assert transpile.call_args.kwargs["read"] == "postgres"
        assert transpile.call_args.kwargs["write"] == "sqlite"
        assert transpile.call_args.kwargs["identify"] is True
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(translated).fetchone() == expected
"""


def preview(repository: Path, ref: str, wheel: Path) -> None:
    source = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", ref + "^{commit}"], text=True
    ).strip()
    print(f"Previewing merged BenchBox {source} against {wheel}", flush=True)
    with tempfile.TemporaryDirectory(prefix="benchbox-sqlglot-consumer-") as temporary:
        work = Path(temporary)
        with tempfile.TemporaryFile() as stream:
            subprocess.run(
                ["git", "-C", str(repository), "archive", source], stdout=stream, check=True
            )
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                archive.extractall(work, filter="data")
        for directory in ("benchbox", "tests"):
            for path in (work / directory).rglob("*.py"):
                text = path.read_text()
                if "sqlglot" in text:
                    text = relocate(text, path.relative_to(work).as_posix(), suite=True)
                    if path.name == "test_translate_sql_delegation.py":
                        text = text.replace(
                            'if name == "sqlglot":', 'if name == "benchbox_sqlglot":'
                        )
                    path.write_text(text)
        (work / "test_downstream_consumer.py").write_text(CONSUMER_CONTRACT)
        env = {**os.environ, "BENCHBOX_EPHEMERAL_CLONE": "1"}
        env.pop("PYTHONPATH", None)
        subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(work),
                "--no-default-groups",
                "--with",
                str(wheel.resolve()),
                "--with",
                "pytest",
                "--with",
                "pytest-mock",
                "--with",
                "pytest-xdist",
                "--with",
                "pytest-timeout",
                "--with",
                "pytest-benchmark",
                "--with",
                "pytest-cov",
                "--with",
                "duckdb",
                "--with",
                "datafusion>=54.0.0",
                "--with",
                "polars>=1.35.2",
                "--",
                "python",
                "-m",
                "pytest",
                "-n",
                "0",
                "-q",
                *CONSUMER_TESTS,
                "test_downstream_consumer.py",
            ],
            cwd=work,
            env=env,
            timeout=900,
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchbox", type=Path, required=True)
    parser.add_argument("--ref", default="origin/develop")
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    preview(args.benchbox, args.ref, args.wheel)


if __name__ == "__main__":
    main()
