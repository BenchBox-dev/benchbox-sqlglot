"""Variance and standard deviation written with COUNT, SUM and SQRT, for engines without them.

``VAR_SAMP = (n * sum(x*x) - sum(x)^2) / (n * (n - 1))`` and ``VAR_POP`` divides by ``n * n``;
the standard deviations are the square roots. ``n`` is ``COUNT(x)``, so NULL inputs are skipped as
the aggregates skip them, and a group too small to have a sample variance (``n <= 1``) divides by
zero, which engines such as SQLite answer with NULL, as ``STDDEV_SAMP`` does. The numerator is
clamped at zero so rounding cannot make a nonnegative variance negative, which would turn its
square root NULL.
"""

from __future__ import annotations

from sqlglot import exp

_SAMPLE = "(COUNT(:x) * SUM(:x * :x) - SUM(:x) * SUM(:x)) / (COUNT(:x) * (COUNT(:x) - 1))"
_POPULATION = "(COUNT(:x) * SUM(:x * :x) - SUM(:x) * SUM(:x)) / (COUNT(:x) * COUNT(:x))"

_SAMPLE_TYPES = (exp.Stddev, exp.StddevSamp, exp.Variance)
_ROOT_TYPES = (exp.Stddev, exp.StddevSamp, exp.StddevPop)
SUPPORTED = (exp.Stddev, exp.StddevSamp, exp.StddevPop, exp.Variance, exp.VariancePop)


def is_expandable(node: exp.Expr) -> bool:
    """Whether ``node`` is a plain aggregate: not DISTINCT, FILTERed or windowed."""
    return isinstance(node, SUPPORTED) and not (
        isinstance(node.this, exp.Distinct) or isinstance(node.parent, (exp.Window, exp.Filter))
    )


def expand_moments(node: exp.Expr) -> exp.Expr:
    """The COUNT/SUM/SQRT form of a variance or standard deviation aggregate."""
    template = _SAMPLE if isinstance(node, _SAMPLE_TYPES) else _POPULATION
    value = exp.cast(node.this.copy(), exp.DType.DOUBLE, copy=False)
    variance: exp.Expr = exp.maybe_parse(template).transform(
        lambda n: value.copy() if isinstance(n, exp.Placeholder) else n
    )
    clamped = exp.Greatest(this=exp.Literal.number("0.0"), expressions=[variance])
    return exp.Sqrt(this=clamped) if isinstance(node, _ROOT_TYPES) else clamped
