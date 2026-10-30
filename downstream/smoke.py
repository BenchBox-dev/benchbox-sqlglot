from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.resources
import json
import multiprocessing
import pickle
import sqlite3
import sys
from pathlib import Path


def spawned_roundtrip(expression, queue) -> None:
    queue.put((expression.sql(), expression.__class__.__module__))


def smoke(*, coexist: bool = False, upstream_first: bool = False) -> None:
    if coexist and upstream_first:
        importlib.import_module("sqlglot")
    fork = importlib.import_module("benchbox._vendor.sqlglot")
    assert fork.__file__ is not None
    assert "site-packages" in Path(fork.__file__).parts, fork.__file__
    distribution = importlib.metadata.distribution("benchbox-sqlglot")
    provenance = json.loads(
        importlib.resources.files("benchbox._vendor.sqlglot")
        .joinpath("benchbox_provenance.json")
        .read_text()
    )
    assert fork.__version__ == distribution.version == provenance["version"]
    assert not distribution.entry_points
    assert not distribution.metadata.get_all("Provides-Extra")
    assert importlib.resources.files("benchbox._vendor.sqlglot").joinpath("py.typed").is_file()
    for dialect in fork.Dialects:
        assert fork.Dialect.get_or_raise(dialect).__class__.__module__.startswith(
            "benchbox._vendor.sqlglot."
        )
    optimizer = importlib.import_module("benchbox._vendor.sqlglot.optimizer")
    expression = fork.parse_one("SELECT 1 + 2 AS n")
    assert optimizer.optimize(expression).sql() == 'SELECT 3 AS "n"'
    serde = importlib.import_module("benchbox._vendor.sqlglot.serde")
    assert serde.load(serde.dump(expression)) == expression
    assert pickle.loads(pickle.dumps(expression)) == expression
    for name in ("postgres", "sqlite", "singlestore", "duckdb", "bigquery"):
        assert fork.parse_one("SELECT 1", read=name).sql(name) == "SELECT 1"
    sql = fork.transpile(
        "SELECT EXTRACT(YEAR FROM DATE '2024-02-29') / 2, DATE '2024-02-29' + INTERVAL '1' DAY",
        read="postgres",
        write="sqlite",
        unsupported_level=fork.ErrorLevel.RAISE,
    )[0]
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(sql).fetchone() == (1012.0, "2024-03-01")
    marked = "SELECT 'sqlglot.parser', a /* sqlglot.meta x */ FROM t"
    assert "/* sqlglot.meta x */" in fork.parse_one(marked).sql()
    assert "'sqlglot.parser'" in fork.parse_one(marked).sql()
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=spawned_roundtrip, args=(expression, queue))
    process.start()
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join()
        raise AssertionError("Spawn round trip timed out")
    assert process.exitcode == 0
    assert queue.get(timeout=5) == (expression.sql(), expression.__class__.__module__)
    queue.close()
    if coexist:
        upstream = importlib.import_module("sqlglot")
        assert fork is not upstream
        assert sys.modules["sqlglot"] is upstream
        assert fork.exp.Expression is not upstream.exp.Expression
        assert not isinstance(expression, upstream.exp.Expression)
        assert not isinstance(upstream.parse_one("SELECT 1"), fork.exp.Expression)
        assert upstream.parse_one("SELECT 1").__class__.__module__.startswith("sqlglot.")
        assert importlib.metadata.version("sqlglot") == "30.27.0"
        assert fork.Dialect.get("forkfixture") is not None
        assert fork.Dialect.get("upstreamfixture") is None
        assert upstream.Dialect.get("upstreamfixture") is not None
        assert upstream.Dialect.get("forkfixture") is None
    else:
        assert not any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules)
    print(f"Installed {distribution.metadata['Name']} {distribution.version}: runtime smoke passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--coexist", action="store_true")
    parser.add_argument("--upstream-first", action="store_true")
    arguments = parser.parse_args()
    smoke(coexist=arguments.coexist, upstream_first=arguments.upstream_first)
