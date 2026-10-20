from __future__ import annotations

from sqlglot.dialects.postgres import Postgres


class DataFusion(Postgres):
    """Apache DataFusion, whose SQL follows PostgreSQL conventions.

    Behaves as `Postgres` until a difference is proved against the engine; its own
    class gives callers an explicit key to read and write.
    """
