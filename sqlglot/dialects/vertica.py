from __future__ import annotations

from sqlglot.dialects.postgres import Postgres


class Vertica(Postgres):
    """OpenText Vertica, which follows PostgreSQL syntax for the subset BenchBox uses.

    Behaves as `Postgres` until a difference is proved against the engine; its own
    class gives callers an explicit key to read and write.
    """
