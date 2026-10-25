from __future__ import annotations

from sqlglot import exp
from sqlglot.benchbox.date_literals import DateLiteralPadding
from sqlglot.generators.postgres import PostgresGenerator


class DataFusionGenerator(DateLiteralPadding, PostgresGenerator):
    PAD_DATE_LITERALS = False
    SUPPORTS_EMPTY_GROUP_BY = False
    EXACT_DIVISION_TYPE = exp.DType.DOUBLE
