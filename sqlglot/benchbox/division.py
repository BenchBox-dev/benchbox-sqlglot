from __future__ import annotations

from sqlglot import exp

EXACT_DIVISION = "exact_division"


def mark_exact_division(node: exp.Expr | None) -> None:
    while isinstance(node, (exp.Mul, exp.Div, exp.IntDiv, exp.Mod)):
        if isinstance(node, exp.Div):
            node.meta[EXACT_DIVISION] = True
        node = node.this


def make_quotients_exact(expression: exp.Div, to: exp.DType = exp.DType.DOUBLE) -> None:
    from sqlglot.generators.sqlite import SQLiteGenerator as classify

    for operand in (expression.this, expression.expression):
        if isinstance(operand, exp.Div):
            make_quotients_exact(operand, to)
    if (
        expression.meta.get(EXACT_DIVISION)
        and expression.args.get("typed")
        and not classify._truncating_division(expression)
        and not classify._real_valued(expression.this)
        and not classify._real_valued(expression.expression)
    ):
        expression.set("this", exp.Cast(this=expression.this, to=exp.DataType.build(to)))
