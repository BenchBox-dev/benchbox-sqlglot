from __future__ import annotations

from sqlglot.dialects.postgres import Postgres
from sqlglot.generators.datafusion import DataFusionGenerator


class DataFusion(Postgres):
    """Apache DataFusion, whose SQL follows PostgreSQL conventions.

    Behaves as `Postgres` except where the engine differs: it rejects `GROUP BY ()`, so
    the generator drops that clause from a global aggregate.
    """

    Generator = DataFusionGenerator
