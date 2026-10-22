from __future__ import annotations

from sqlglot import exp
from sqlglot.generators.postgres import PostgresGenerator


class DataFusionGenerator(PostgresGenerator):
    SUPPORTS_EMPTY_GROUP_BY = False
    EXACT_DIVISION_TYPE = exp.DType.DOUBLE
