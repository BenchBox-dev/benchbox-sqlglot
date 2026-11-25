"""Recognise the ``EXTRACT`` shapes that generators write differently from the base form.

``EXTRACT(DOW FROM x)`` counts days from Sunday = 0 and ``EXTRACT(EPOCH FROM (a - b))`` is the number
of seconds between two timestamps. Several engines have no such field (BigQuery, Spark, Databricks,
SingleStore, SQLite) or read the subtraction as a number, so each generator rewrites the two shapes
with its own functions. The helpers here only find the shapes; they name no table or column.
"""

from __future__ import annotations

from sqlglot import exp


def is_day_of_week(expression: exp.Extract) -> bool:
    """True for ``EXTRACT(DOW FROM x)``."""
    return expression.name.upper() == "DOW"


def epoch_difference(expression: exp.Extract) -> tuple[exp.Expr, exp.Expr] | None:
    """Return ``(a, b)`` for ``EXTRACT(EPOCH FROM (a - b))``, or ``None`` for any other shape."""
    if expression.name.upper() != "EPOCH":
        return None

    operand = expression.expression
    while isinstance(operand, exp.Paren):
        operand = operand.this

    if isinstance(operand, exp.Sub) and not expression.args.get("date_part"):
        return operand.this, operand.expression
    return None
