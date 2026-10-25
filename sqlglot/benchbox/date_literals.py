from __future__ import annotations

import datetime
import re

from sqlglot import exp
from sqlglot.generator import Generator

_DATE_TEXT = re.compile(r"(\d{1,4})-(\d{1,2})-(\d{1,2})")
_TIMESTAMP_TEXT = re.compile(
    r"(\d{1,4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2})(\.\d+)?)?"
)


def canonical_date(text: str) -> str | None:
    match = _DATE_TEXT.fullmatch(text.strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def canonical_timestamp(text: str) -> str | None:
    date = canonical_date(text)
    if date is not None:
        return date
    match = _TIMESTAMP_TEXT.fullmatch(text.strip())
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups()[:5])
    second = int(match.group(6) or 0)
    try:
        datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    fraction = match.group(7) or ""
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}{fraction}"


def canonical_literal(node: exp.Expr, timestamp: bool = False) -> exp.Expr:
    if isinstance(node, exp.Literal) and node.is_string:
        canonical = (canonical_timestamp if timestamp else canonical_date)(node.name)
        if canonical is not None and canonical != node.name:
            return exp.Literal.string(canonical)
    return node


class DateLiteralPadding(Generator):
    PAD_DATE_LITERALS = True
    PAD_TIMESTAMP_LITERALS = True

    def _typed(self, node: exp.Expr) -> exp.Expr:
        if node.type:
            return node
        from sqlglot.optimizer.annotate_types import annotate_types

        return annotate_types(node.copy(), dialect=self.dialect, overwrite_types=False)

    def _is_known_date(self, node: exp.Expr) -> bool:
        return self._typed(node).is_type(exp.DType.DATE)

    def _pad_literals(self, *operands: exp.Expr) -> None:
        if not self.PAD_DATE_LITERALS:
            return
        literals = [n for n in operands if isinstance(n, exp.Literal) and n.is_string]
        if not literals:
            return
        others = [n for n in operands if not any(n is literal for literal in literals)]
        if not any(self._is_known_date(n) for n in others):
            return
        for literal in literals:
            canonical = canonical_literal(literal)
            if canonical is not literal:
                literal.replace(canonical)

    def cast_sql(self, expression: exp.Cast, safe_prefix: str | None = None) -> str:
        timestamp = expression.is_type(
            exp.DType.TIMESTAMP, exp.DType.TIMESTAMPNTZ, exp.DType.DATETIME
        )
        if (self.PAD_DATE_LITERALS and expression.is_type(exp.DType.DATE)) or (
            timestamp and self.PAD_TIMESTAMP_LITERALS
        ):
            canonical = canonical_literal(expression.this, timestamp=timestamp)
            if canonical is not expression.this:
                expression = expression.copy()
                expression.set("this", canonical)
        return super().cast_sql(expression, safe_prefix=safe_prefix)

    def eq_sql(self, expression: exp.EQ) -> str:
        self._pad_literals(expression.this, expression.expression)
        return super().eq_sql(expression)

    def neq_sql(self, expression: exp.NEQ) -> str:
        self._pad_literals(expression.this, expression.expression)
        return super().neq_sql(expression)

    def lt_sql(self, expression: exp.LT) -> str:
        self._pad_literals(expression.this, expression.expression)
        return super().lt_sql(expression)

    def lte_sql(self, expression: exp.LTE) -> str:
        self._pad_literals(expression.this, expression.expression)
        return super().lte_sql(expression)

    def gt_sql(self, expression: exp.GT) -> str:
        self._pad_literals(expression.this, expression.expression)
        return super().gt_sql(expression)

    def gte_sql(self, expression: exp.GTE) -> str:
        self._pad_literals(expression.this, expression.expression)
        return super().gte_sql(expression)

    def between_sql(self, expression: exp.Between) -> str:
        self._pad_literals(expression.this, expression.args["low"], expression.args["high"])
        return super().between_sql(expression)

    def in_sql(self, expression: exp.In) -> str:
        self._pad_literals(expression.this, *expression.expressions)
        return super().in_sql(expression)
