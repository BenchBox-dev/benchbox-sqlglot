from setuptools import setup
from setuptools_scm import get_version

version = get_version(local_scheme="no-local-version")

setup(
    extras_require={
        "dev": [
            "duckdb>=0.6",
            # mypy 2.4+ is the build dep for sqlglotc and only ships
            # for py3.10+; on py3.9 just use upstream mypy for type checking.
            "mypy >= 2.4.0; python_version >= '3.10'",
            "mypy; python_version < '3.10'",
            "setuptools_scm",
            "pandas",
            "pandas-stubs",
            "python-dateutil",
            "pytz",
            "pdoc",
            "pre-commit",
            "pyyaml",
            "types-PyYAML",
            "ruff==0.15.6",
            "types-python-dateutil",
            "types-pytz",
            "typing_extensions",
            "pyperf",
        ],
        # Engines for tests/engines.py, which run translated SQL for real. Not
        # part of the default install or of `dev`. Engines without Python 3.9
        # wheels are gated by marker; tests import every engine lazily and skip
        # (or, with BENCHBOX_ENGINES_REQUIRED, fail) when one is absent.
        "engines": [
            "duckdb>=0.6",
            "datafusion>=43; python_version >= '3.10'",
            "chdb>=3; python_version >= '3.10'",
            "pyspark>=3.5; python_version >= '3.10'",
            "psycopg[binary]>=3.1",
            "trino>=0.330",
            "pymysql>=1.1",
        ],
        # Compiles from source on the user's machine. Requires Python 3.10+
        # because the build dep (mypy 2.4+) dropped 3.9; on 3.9
        # `pip install sqlglot[c]` is a no-op and you just get pure-Python sqlglot.
        "c": [f"sqlglotc=={version}; python_version >= '3.10'"],
        # Deprecated: the Rust tokenizer has been replaced by sqlglotc.
        "rs": ["sqlglotrs==0.13.0", f"sqlglotc=={version}; python_version >= '3.10'"],
    },
)
