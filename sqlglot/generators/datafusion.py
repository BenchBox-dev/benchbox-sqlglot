from __future__ import annotations

from sqlglot.generators.postgres import PostgresGenerator


class DataFusionGenerator(PostgresGenerator):
    SUPPORTS_EMPTY_GROUP_BY = False
