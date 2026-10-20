from __future__ import annotations

from sqlglot.dialects.postgres import Postgres


class Netezza(Postgres):
    """IBM Netezza Performance Server, whose SQL descends from PostgreSQL.

    Behaves as `Postgres` until a difference is proved against the engine; its own
    class gives callers an explicit key to read and write.
    """
