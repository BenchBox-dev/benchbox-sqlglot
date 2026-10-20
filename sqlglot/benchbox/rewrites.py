"""Engine-neutral expression rewrites maintained by the BenchBox fork.

Rewrites take and return expressions, so the caller picks the read and write
dialects. Every gate (which column, which table) is an argument.
"""

from __future__ import annotations

import math
import re
import typing as t
from decimal import Context, Decimal, DecimalException, Inexact

from sqlglot import exp

E = t.TypeVar("E", bound=exp.Expr)

# Fold only short sums: at most this many literals per bound.
_MAX_LEAVES = 4
# Fold only when the exact result and the double-precision result agree to this
# distance. A larger gap means the double result is the meaningful one.
_NOISE = 1e-12
_NUMBER = re.compile(r"\d{1,2}\.\d{1,6}")


def fold_between_literal_arithmetic(
    expression: E,
    columns: t.Iterable[str],
    tables: t.Iterable[str] | None = None,
    copy: bool = True,
) -> E:
    """Replace literal ``+``/``-`` bounds of ``BETWEEN`` with their exact decimal value.

    Engines that evaluate decimal literals as binary floating point turn
    ``col BETWEEN 0.06 - 0.01 AND 0.06 + 0.01`` into
    ``col BETWEEN 0.049999999999999996 AND 0.06999999999999999``, which drops
    rows equal to a bound. Folding the bounds to ``0.05`` and ``0.07`` restores
    the exact decimal result.

    A bound is folded only when it is a sum or difference of at most four
    non-negative decimal literals (one or two integer digits, one to six
    fractional digits), the exact result is non-negative, and the double
    precision result is within ``1e-12`` of it. Both bounds of a ``BETWEEN``
    must qualify, otherwise neither changes. Anything else is left untouched.

    Args:
        expression: the expression to rewrite.
        columns: names (case-insensitive, qualifier ignored) of the columns whose
            ``BETWEEN`` predicates are folded.
        tables: when given, the rewrite applies only if the expression reads at
            least one table with one of these names (case-insensitive). When
            omitted there is no table gate.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    column_names = {name.lower() for name in columns}
    if copy:
        expression = expression.copy()
    if not column_names:
        return expression
    if tables is not None:
        table_names = {name.lower() for name in tables}
        if not any(table.name.lower() in table_names for table in expression.find_all(exp.Table)):
            return expression

    context = Context(prec=60, traps=[Inexact])
    leaves = 0

    def evaluate(node: exp.Expr) -> tuple[Decimal, float] | None:
        nonlocal leaves
        node = node.unnest()
        if isinstance(node, exp.Literal):
            leaves += 1
            if leaves > _MAX_LEAVES or not node.is_number or not _NUMBER.fullmatch(node.name):
                return None
            return Decimal(node.name), float(node.name)
        if isinstance(node, (exp.Add, exp.Sub)):
            left, right = evaluate(node.this), evaluate(node.expression)
            if left is None or right is None:
                return None
            try:
                if isinstance(node, exp.Add):
                    return context.add(left[0], right[0]), left[1] + right[1]
                return context.subtract(left[0], right[0]), left[1] - right[1]
            except DecimalException:
                return None
        return None

    for between in list(expression.find_all(exp.Between)):
        column = between.this
        if not isinstance(column, exp.Column) or column.name.lower() not in column_names:
            continue
        folded: list[Decimal] = []
        for bound in (between.args["low"], between.args["high"]):
            leaves = 0
            value = evaluate(bound) if isinstance(bound.unnest(), (exp.Add, exp.Sub)) else None
            if value is None:
                break
            exact, real = value
            if exact < 0 or not math.isfinite(real) or abs(real - float(exact)) > _NOISE:
                break
            folded.append(exact)
        else:
            between.set("low", exp.Literal.number(format(folded[0], "f")))
            between.set("high", exp.Literal.number(format(folded[1], "f")))
    return expression
