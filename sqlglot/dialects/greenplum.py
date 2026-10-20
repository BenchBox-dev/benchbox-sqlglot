from __future__ import annotations

from sqlglot.dialects.postgres import Postgres


class Greenplum(Postgres):
    """Greenplum Database, a massively parallel PostgreSQL derivative.

    Behaves as `Postgres` until a difference is proved against the engine; its own
    class gives callers an explicit key to read and write.
    """
