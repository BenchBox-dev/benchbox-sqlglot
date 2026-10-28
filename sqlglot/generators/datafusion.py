from __future__ import annotations

from sqlglot import exp
from sqlglot.benchbox.date_literals import DateLiteralPadding
from sqlglot.benchbox.pivot import expand_order_by_all, lower_pivots, reject_pivot_sql
from sqlglot.generators.postgres import PostgresGenerator


class DataFusionGenerator(DateLiteralPadding, PostgresGenerator):
    PAD_DATE_LITERALS = False
    SUPPORTS_EMPTY_GROUP_BY = False
    EXACT_DIVISION_TYPE = exp.DType.DOUBLE

    TRANSFORMS = {
        **PostgresGenerator.TRANSFORMS,
        exp.Pivot: reject_pivot_sql,
    }

    def preprocess(self, expression: exp.Expr) -> exp.Expr:
        expression = lower_pivots(expression, copy=False)
        return super().preprocess(expand_order_by_all(expression, copy=False))
