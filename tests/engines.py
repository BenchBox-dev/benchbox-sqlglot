"""Real-engine helpers for tests that execute translated SQL.

Everything here is lazy: engine packages are imported only when an engine is
connected to, so importing this module (and test collection) never requires
an optional engine. The fork uses ``unittest``, so skipping is exposed as a
decorator rather than a pytest marker.

Install the engine packages with
``pip install -r downstream/engines-requirements.txt``.

Environment:

``BENCHBOX_ENGINES_REQUIRED``
    ``1``/``all`` makes every engine mandatory; a comma-separated list of
    engine names makes just those mandatory. A required engine that is missing
    fails the test instead of skipping it. CI sets this so a broken install
    cannot silently turn into a green run. Unset (the default), a missing
    engine skips with a named reason.

``BENCHBOX_ENGINE_<NAME>``
    ``host:port`` of a running service for the service engines (``POSTGRES``,
    ``TRINO``, ``STARROCKS``, ``DORIS``, ``QUESTDB``, ``SINGLESTORE``).
    Unset means the service is not available. Locally, start a container and
    export the variable to run the matching tests.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import socket
import time
import typing as t
import unittest
from dataclasses import dataclass

REQUIRED_ENV = "BENCHBOX_ENGINES_REQUIRED"
SERVICE_ENV_PREFIX = "BENCHBOX_ENGINE_"

Rows = list[tuple[t.Any, ...]]


@dataclass(frozen=True)
class Engine:
    name: str
    # SQLGlot dialect that produces this engine's SQL.
    dialect: str
    # Import names of the packages needed to talk to the engine.
    modules: tuple[str, ...]
    # Extras-group package names, shown in skip and failure messages.
    packages: tuple[str, ...]
    # True when the engine is a running service reached over the network.
    service: bool = False
    # Default user/password/database for services.
    user: str = ""
    password: str = ""
    database: str = ""


ENGINES: dict[str, Engine] = {
    e.name: e
    for e in (
        Engine("sqlite", "sqlite", (), ()),
        Engine("duckdb", "duckdb", ("duckdb",), ("duckdb",)),
        Engine("datafusion", "datafusion", ("datafusion",), ("datafusion",)),
        Engine("chdb", "clickhouse", ("chdb",), ("chdb",)),
        Engine("pyspark", "spark", ("pyspark",), ("pyspark",)),
        Engine(
            "postgres",
            "postgres",
            ("psycopg",),
            ("psycopg[binary]",),
            True,
            "postgres",
            "postgres",
            "postgres",
        ),
        Engine("trino", "trino", ("trino",), ("trino",), True, "test", "", "memory"),
        Engine("starrocks", "starrocks", ("pymysql",), ("pymysql",), True, "root", "", ""),
        Engine("doris", "doris", ("pymysql",), ("pymysql",), True, "root", "", ""),
        # QuestDB speaks the Postgres wire protocol, so it connects like Postgres.
        Engine(
            "questdb", "questdb", ("psycopg",), ("psycopg[binary]",), True, "admin", "quest", "qdb"
        ),
        Engine("singlestore", "singlestore", ("pymysql",), ("pymysql",), True, "root", "", ""),
    )
}


def required_engines() -> set[str]:
    """Engines that must be present, as configured by ``BENCHBOX_ENGINES_REQUIRED``."""
    value = os.environ.get(REQUIRED_ENV, "").strip().lower()
    if value in ("", "0", "false", "no"):
        return set()
    if value in ("1", "all", "true", "yes"):
        return set(ENGINES)
    names = {n.strip() for n in value.split(",") if n.strip()}
    unknown = names - set(ENGINES)
    if unknown:
        raise ValueError(f"{REQUIRED_ENV} names unknown engines: {sorted(unknown)}")
    return names


def service_address(name: str) -> tuple[str, int] | None:
    value = os.environ.get(SERVICE_ENV_PREFIX + name.upper())
    if not value:
        return None
    host, _, port = value.rpartition(":")
    return host or "127.0.0.1", int(port)


def unavailable_reason(name: str) -> str | None:
    """Why ``name`` cannot be used here, or ``None`` when it can."""
    engine = ENGINES[name]
    for module in engine.modules:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            return f"{name}: Python package {module!r} is not installed (pip install {' '.join(engine.packages)})"
    if engine.service:
        address = service_address(name)
        if address is None:
            return (
                f"{name}: no service configured (set {SERVICE_ENV_PREFIX}{name.upper()}=host:port)"
            )
        try:
            with socket.create_connection(address, timeout=3):
                pass
        except OSError as e:
            return f"{name}: service at {address[0]}:{address[1]} is unreachable ({e})"
    return None


def is_available(name: str) -> bool:
    return unavailable_reason(name) is None


def available_engines() -> list[str]:
    return [name for name in ENGINES if is_available(name)]


def requires_engine(name: str) -> t.Callable[[t.Any], t.Any]:
    """``unittest`` decorator for a test or ``TestCase`` needing engine ``name``.

    Skips with a named reason when the engine is missing, unless the engine is
    listed in ``BENCHBOX_ENGINES_REQUIRED``, in which case the test fails.
    The check runs when the test runs, so the environment is read late.
    """
    if name not in ENGINES:
        raise ValueError(f"Unknown engine {name!r}; known: {sorted(ENGINES)}")

    def check() -> None:
        reason = unavailable_reason(name)
        if reason is None:
            return
        if name in required_engines():
            raise AssertionError(f"Required engine is unavailable: {reason}")
        raise unittest.SkipTest(reason)

    def decorate(target: t.Any) -> t.Any:
        if isinstance(target, type):
            if not issubclass(target, unittest.TestCase):
                raise TypeError("requires_engine decorates TestCase classes or test functions")
            original_setup = target.setUp

            @functools.wraps(original_setup)
            def setUp(self: unittest.TestCase) -> None:
                check()
                original_setup(self)

            target.setUp = setUp  # type: ignore[method-assign]
            return target

        @functools.wraps(target)
        def wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
            check()
            return target(*args, **kwargs)

        return wrapper

    return decorate


class Connection:
    """Minimal uniform handle: ``execute(sql) -> list of row tuples``."""

    def __init__(self, execute: t.Callable[[str], Rows], close: t.Callable[[], None]) -> None:
        self._execute = execute
        self._close = close

    def execute(self, sql: str) -> Rows:
        return self._execute(sql)

    def close(self) -> None:
        self._close()

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: t.Any) -> None:
        self.close()


def _rows(cursor: t.Any) -> Rows:
    return [tuple(row) for row in cursor.fetchall()] if cursor.description else []


def _connect_sqlite(engine: Engine) -> Connection:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    return Connection(lambda sql: _rows(conn.execute(sql)), conn.close)


def _connect_duckdb(engine: Engine) -> Connection:
    import duckdb

    conn = duckdb.connect(":memory:")
    return Connection(lambda sql: _rows(conn.execute(sql)), conn.close)


def _connect_datafusion(engine: Engine) -> Connection:
    import datafusion

    ctx = datafusion.SessionContext()

    def execute(sql: str) -> Rows:
        table = ctx.sql(sql).to_arrow_table()
        columns = [column.to_pylist() for column in table.columns]
        return [tuple(row) for row in zip(*columns)]

    return Connection(execute, lambda: None)


def _connect_chdb(engine: Engine) -> Connection:
    import chdb

    def execute(sql: str) -> Rows:
        text = str(chdb.query(sql, "TabSeparated")).rstrip("\n")
        return [tuple(line.split("\t")) for line in text.split("\n")] if text else []

    return Connection(execute, lambda: None)


_SPARK: t.Any = None


def _connect_pyspark(engine: Engine) -> Connection:
    global _SPARK
    if _SPARK is None:
        from pyspark.sql import SparkSession

        _SPARK = (
            SparkSession.builder.master("local[1]")
            .appName("sqlglot-engine-tests")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.shuffle.partitions", "1")
            .getOrCreate()
        )
    spark = _SPARK
    # The session is shared across tests because starting one costs seconds;
    # closing a connection therefore leaves it running.
    return Connection(lambda sql: [tuple(row) for row in spark.sql(sql).collect()], lambda: None)


def _connect_postgres_wire(engine: Engine) -> Connection:
    import psycopg

    host, port = t.cast(tuple[str, int], service_address(engine.name))
    conn = psycopg.connect(
        host=host,
        port=port,
        user=engine.user,
        password=engine.password,
        dbname=engine.database,
        autocommit=True,
        connect_timeout=30,
    )

    def execute(sql: str) -> Rows:
        with conn.cursor() as cursor:
            cursor.execute(sql)  # type: ignore[call-overload]
            return _rows(cursor)

    return Connection(execute, conn.close)


def _connect_trino(engine: Engine) -> Connection:
    import trino

    host, port = t.cast(tuple[str, int], service_address(engine.name))
    conn = trino.dbapi.connect(
        host=host, port=port, user=engine.user, catalog=engine.database, schema="default"
    )

    def execute(sql: str) -> Rows:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]

    return Connection(execute, conn.close)


def _connect_mysql_wire(engine: Engine) -> Connection:
    import pymysql

    host, port = t.cast(tuple[str, int], service_address(engine.name))
    conn = pymysql.connect(
        host=host,
        port=port,
        user=engine.user,
        password=engine.password,
        autocommit=True,
        connect_timeout=30,
    )

    def execute(sql: str) -> Rows:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            return _rows(cursor)

    return Connection(execute, conn.close)


_CONNECTORS: dict[str, t.Callable[[Engine], Connection]] = {
    "sqlite": _connect_sqlite,
    "duckdb": _connect_duckdb,
    "datafusion": _connect_datafusion,
    "chdb": _connect_chdb,
    "pyspark": _connect_pyspark,
    "postgres": _connect_postgres_wire,
    "questdb": _connect_postgres_wire,
    "trino": _connect_trino,
    "starrocks": _connect_mysql_wire,
    "doris": _connect_mysql_wire,
    "singlestore": _connect_mysql_wire,
}


def connect(name: str) -> Connection:
    """Open a connection to engine ``name``; raises if it is unavailable."""
    reason = unavailable_reason(name)
    if reason is not None:
        raise RuntimeError(reason)
    return _CONNECTORS[name](ENGINES[name])


def execute(name: str, sql: str) -> Rows:
    """Run one statement on a fresh connection and return its rows."""
    with connect(name) as conn:
        return conn.execute(sql)


def wait_until_ready(name: str, timeout: float = 300.0, interval: float = 5.0) -> None:
    """Block until ``SELECT 1`` runs on a service engine; raise on timeout.

    Containers accept connections well before they can execute queries, so
    readiness is judged by running a statement, not by an open port.
    """
    deadline = time.monotonic() + timeout
    while True:
        last: BaseException
        try:
            rows = execute(name, "SELECT 1")
            if rows and str(rows[0][0]) == "1":
                return
            last = AssertionError(f"unexpected result {rows!r}")
        except Exception as e:  # noqa: BLE001 - any failure means "not ready yet"
            last = e
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{name} not ready after {timeout:.0f}s: {last}")
        time.sleep(interval)


def installed_versions() -> dict[str, str]:
    """Installed version of every engine package, ``not installed`` when missing."""
    from importlib import metadata

    names = sorted({p.split("[")[0] for e in ENGINES.values() for p in e.packages})
    versions = {}
    for package in names:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not installed"
    return versions


def main(argv: list[str]) -> int:
    if argv[:1] == ["status"]:
        for name in ENGINES:
            print(f"{name}: {unavailable_reason(name) or 'available'}")
        return 0
    if argv[:1] == ["versions"]:
        for package, version in installed_versions().items():
            print(f"{package}=={version}" if version[0].isdigit() else f"{package}: {version}")
        return 0
    if len(argv) >= 2 and argv[0] == "wait" and argv[1] in ENGINES:
        wait_until_ready(argv[1], float(argv[2]) if len(argv) > 2 else 300.0)
        print(f"{argv[1]} is ready")
        return 0
    print("usage: python -m tests.engines status | versions | wait ENGINE [TIMEOUT_SECONDS]")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
