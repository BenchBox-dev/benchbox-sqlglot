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
]


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
                    path.write_text(relocate(text, path.relative_to(work).as_posix(), suite=True))
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
