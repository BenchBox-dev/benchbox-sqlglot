"""Exact rewrites for engines without ``PIVOT`` and ``UNPIVOT``, and ``ORDER BY ALL``.

``lower_pivots`` turns a ``PIVOT`` or ``UNPIVOT`` over a derived table into conditional
aggregation or a ``UNION ALL`` when the output columns follow from the select list. A shape it
cannot prove equivalent keeps its pivot, and ``reject_pivot_sql`` makes the generator raise
``UnsupportedError`` for it, so a query is never written without its pivot.

``expand_order_by_all`` writes ``ORDER BY ALL [ASC|DESC] [NULLS ...]`` (a ``Var`` read by DuckDB, or the unquoted column named
``ALL`` that a dialect without the keyword reads, which no such dialect accepts as a column) as one ordinal key per
selected column with the same direction and null placement.
"""

from __future__ import annotations

import typing as t

from sqlglot import exp
from sqlglot.errors import UnsupportedError

if t.TYPE_CHECKING:
    from sqlglot.generator import Generator

E = t.TypeVar("E", bound=exp.Expr)

_PIVOT_AGGREGATES = (exp.Sum, exp.Min, exp.Max, exp.Avg, exp.Count)
_SOURCE_ALIAS = "_pivot_src"


def reject_pivot_sql(self: Generator, expression: exp.Pivot) -> str:
    direction = "UNPIVOT" if expression.unpivot else "PIVOT"
    raise UnsupportedError(
        f"{self.dialect.__class__.__name__} has no {direction}, and this one is not a shape "
        "that can be written with conditional aggregation or UNION ALL without changing its rows"
    )


def lower_pivots(expression: E, copy: bool = True) -> E:
    """Replace each provable ``PIVOT`` or ``UNPIVOT`` over a derived table.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression; a pivot the rewrite cannot prove equivalent is kept.
    """
    if copy:
        expression = expression.copy()
    for source in reversed(list(expression.find_all(exp.Subquery))):
        pivots = source.args.get("pivots")
        if not pivots or len(pivots) != 1:
            continue
        pivot = pivots[0]
        lowered = _lower_unpivot(source, pivot) if pivot.unpivot else _lower_pivot(source, pivot)
        if lowered is None:
            continue
        alias = pivot.args.get("alias") or source.args.get("alias")
        if alias:
            lowered.set("alias", alias.copy())
        if source is expression:
            return t.cast(E, lowered)
        source.replace(lowered)
    return expression


def expand_order_by_all(expression: E, copy: bool = True) -> E:
    """Replace ``ORDER BY ALL`` with one ordinal key per selected column.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.

    Raises:
        UnsupportedError: the select list is unknown (``*``), or ``ALL`` shares its ``ORDER BY``
            with other keys.
    """
    if copy:
        expression = expression.copy()
    for order in list(expression.find_all(exp.Order)):
        keys = order.expressions
        if not any(_is_all(key) for key in keys):
            continue
        query = order.parent
        if len(keys) != 1 or not isinstance(query, (exp.Select, exp.SetOperation)):
            raise UnsupportedError("ORDER BY ALL is only rewritten as the single ORDER BY key of a query")
        projections = query.selects
        if not projections or any(isinstance(item.unnest(), exp.Star) or item.is_star for item in projections):
            raise UnsupportedError("ORDER BY ALL needs a select list without * to expand")
        template = keys[0]
        expanded = []
        for position in range(1, len(projections) + 1):
            key = template.copy()
            key.set("this", exp.Literal.number(position))
            expanded.append(key)
        order.set("expressions", expanded)
    return expression


def _is_all(key: exp.Expr) -> bool:
    if not isinstance(key, exp.Ordered):
        return False
    target = key.this
    if isinstance(target, exp.Var):
        return target.name.upper() == "ALL"
    return (
        isinstance(target, exp.Column)
        and not target.table
        and isinstance(target.this, exp.Identifier)
        and not target.this.quoted
        and target.name.upper() == "ALL"
    )


def _projection_identifiers(select: exp.Expr) -> list[exp.Identifier] | None:
    if not isinstance(select, exp.Select):
        return None
    identifiers: list[exp.Identifier] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Alias):
            identifier = projection.args.get("alias")
        elif isinstance(projection, exp.Column) and not projection.table:
            identifier = projection.this
        else:
            return None
        if not isinstance(identifier, exp.Identifier):
            return None
        identifiers.append(identifier)
    names = [identifier.name for identifier in identifiers]
    if len(set(names)) != len(names):
        return None
    return identifiers


def _derived(select: exp.Expr, alias: str = _SOURCE_ALIAS) -> exp.Subquery:
    return exp.Subquery(this=select.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias)))


def _lower_pivot(source: exp.Subquery, pivot: exp.Pivot) -> exp.Subquery | None:
    inner = source.this
    identifiers = _projection_identifiers(inner)
    if identifiers is None:
        return None
    if (
        len(pivot.expressions) != 1
        or len(pivot.fields) != 1
        or pivot.args.get("group")
        or pivot.args.get("default_on_null")
        or pivot.args.get("include_nulls") is not None
    ):
        return None
    aggregate = pivot.expressions[0]
    function = aggregate.this if isinstance(aggregate, exp.Alias) else aggregate
    if (
        not isinstance(function, _PIVOT_AGGREGATES)
        or function.args.get("distinct")
        or isinstance(function.this, (exp.Star, exp.Distinct))
        or function.this is None
        or any(value for key, value in function.args.items() if key not in ("this", "big_int"))
    ):
        return None

    field = pivot.fields[0]
    column = field.this if isinstance(field, exp.In) else None
    if not isinstance(column, exp.Column) or column.table or field.args.get("query"):
        return None
    values = []
    for value in field.expressions:
        literal = value.this if isinstance(value, exp.PivotAlias) else value
        if not isinstance(literal, exp.Literal):
            return None
        values.append(literal)
    output = pivot.args.get("columns") or []
    if not values or len(output) != len(values):
        return None

    names = {identifier.name for identifier in identifiers}
    consumed = {item.name for item in function.find_all(exp.Column)} | {column.name}
    if not consumed <= names or any(item.table for item in function.find_all(exp.Column)):
        return None
    keys = [identifier for identifier in identifiers if identifier.name not in consumed]
    if any(identifier.name in {item.name for item in output} for identifier in keys):
        return None

    projections: list[exp.Expr] = [exp.column(identifier.copy()) for identifier in keys]
    for literal, name in zip(values, output):
        condition = exp.EQ(this=column.copy(), expression=literal.copy())
        aggregated = function.copy()
        aggregated.set("this", exp.Case(ifs=[exp.If(this=condition, true=function.this.copy())]))
        projections.append(exp.alias_(aggregated, name.copy()))
    select = exp.select(*projections).from_(_derived(inner))
    if keys:
        select = select.group_by(*[exp.column(identifier.copy()) for identifier in keys])
    return exp.Subquery(this=select)


def _lower_unpivot(source: exp.Subquery, pivot: exp.Pivot) -> exp.Subquery | None:
    inner = source.this
    identifiers = _projection_identifiers(inner)
    if identifiers is None:
        return None
    if (
        len(pivot.expressions) != 1
        or len(pivot.fields) != 1
        or pivot.args.get("group")
        or pivot.args.get("default_on_null")
    ):
        return None
    value = pivot.expressions[0]
    field = pivot.fields[0]
    label = field.this if isinstance(field, exp.In) else None
    if not isinstance(value, exp.Identifier) or not isinstance(label, exp.Identifier) or field.args.get("query"):
        return None

    names = {identifier.name for identifier in identifiers}
    listed: list[tuple[exp.Column, str]] = []
    for item in field.expressions:
        member = item.this if isinstance(item, exp.PivotAlias) else item
        if not isinstance(member, exp.Column) or member.table:
            return None
        text = item.alias if isinstance(item, exp.PivotAlias) else member.name
        if isinstance(item, exp.PivotAlias) and not text:
            return None
        listed.append((member, text))
    if not listed or any(member.name not in names for member, _ in listed):
        return None
    consumed = {member.name for member, _ in listed}
    if len(consumed) != len(listed):
        return None
    keys = [identifier for identifier in identifiers if identifier.name not in consumed]
    if {label.name, value.name} & {identifier.name for identifier in keys} or label.name == value.name:
        return None

    keep_nulls = pivot.args.get("include_nulls") is True
    branches: list[exp.Expr] = []
    for member, text in listed:
        branch = exp.select(
            *[exp.column(identifier.copy()) for identifier in keys],
            exp.alias_(exp.Literal.string(text), label.copy()),
            exp.alias_(member.copy(), value.copy()),
        ).from_(_derived(inner))
        if not keep_nulls:
            branch = branch.where(exp.Not(this=exp.Is(this=member.copy(), expression=exp.Null())))
        branches.append(branch)
    union = branches[0]
    for branch in branches[1:]:
        union = exp.union(union, branch, distinct=False)
    return exp.Subquery(this=union)
