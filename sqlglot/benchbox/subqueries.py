"""Subquery rewrites maintained by the BenchBox fork.

Each rewrite takes and returns an expression (``copy=True`` by default) and leaves every gate
(which columns are known not to be NULL, which schema) to the caller.
"""

from __future__ import annotations

import typing as t

from sqlglot import exp
from sqlglot.dialects.dialect import DialectType
from sqlglot.helper import name_sequence
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope
from sqlglot.optimizer.unnest_subqueries import decorrelate, unnest

E = t.TypeVar("E", bound=exp.Expr)

_POSITIVE_CONTEXT = (exp.And, exp.Or, exp.Paren, exp.Where, exp.Having, exp.Join)
_NOT_PLAIN_SELECT = (
    "group",
    "having",
    "limit",
    "offset",
    "qualify",
    "windows",
    "with_",
    "laterals",
)


def _single_projection(select: exp.Select) -> exp.Expr | None:
    if len(select.expressions) != 1:
        return None
    projection = select.expressions[0]
    return projection.this if isinstance(projection, exp.Alias) else projection


def _plain_select(query: exp.Expr | None) -> exp.Select | None:
    select = query.unnest() if query is not None else None
    if (
        not isinstance(select, exp.Select)
        or not select.args.get("from_")
        or any(select.args.get(key) for key in _NOT_PLAIN_SELECT)
    ):
        return None
    return select


def _correlated_by_equality(scope: Scope) -> bool:
    return all(isinstance(column.parent, exp.EQ) for column in scope.external_columns)


def not_in_subquery_to_not_exists(
    expression: E,
    non_null_columns: t.Iterable[str],
    copy: bool = True,
) -> E:
    """Rewrite ``x NOT IN (SELECT y FROM ... WHERE p)`` into ``NOT EXISTS (SELECT 1 FROM ... WHERE p AND y = x)``.

    ``NOT IN`` is three-valued (a NULL in the subquery makes it NULL) while ``NOT EXISTS`` is not, and
    engines that plan the first poorly or incorrectly can take the second. The two agree only when
    neither side can be NULL, so a predicate is rewritten only when both the outer expression and the
    subquery's projection are plain columns named in ``non_null_columns``; every other ``NOT IN`` is
    left alone. Only a subquery that is a plain ``SELECT ... FROM ... [WHERE ...]`` (no ``GROUP BY``,
    ``HAVING``, ``LIMIT``, window or ``WITH``) qualifies, and one whose outer and inner column have the
    same unqualified name is skipped, because the correlation would resolve to the inner table.

    The caller asserts that column names are unambiguous: an unqualified outer column is correlated
    by name, so it must not also be a column of a table inside the subquery.

    Args:
        expression: the expression to rewrite.
        non_null_columns: names (case-insensitive, qualifier ignored) of columns that are never NULL.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    names = {name.lower() for name in non_null_columns}
    if copy:
        expression = expression.copy()
    for negation in list(expression.find_all(exp.Not)):
        predicate = negation.this
        if not isinstance(predicate, exp.In):
            continue
        outer = predicate.this
        select = _plain_select(predicate.args.get("query"))
        inner = _single_projection(select) if select is not None else None
        if (
            select is None
            or not isinstance(outer, exp.Column)
            or not isinstance(inner, exp.Column)
            or outer.name.lower() not in names
            or inner.name.lower() not in names
            or (not outer.table and not inner.table and outer.name.lower() == inner.name.lower())
        ):
            continue
        correlated = select.copy()
        correlated.set("expressions", [exp.Literal.number(1)])
        correlated.where(exp.EQ(this=inner.copy(), expression=outer.copy()), copy=False)
        replacement = exp.Not(this=exp.Exists(this=correlated))
        if negation is expression:
            return replacement  # type: ignore[return-value]
        negation.replace(replacement)
    return expression


def in_aggregate_subquery_to_exists(
    expression: E, alias: str = "_in_subquery", copy: bool = True
) -> E:
    """Rewrite ``x IN (SELECT k ... GROUP BY ... HAVING ...)`` into an ``EXISTS`` over that subquery.

    The aggregate subquery becomes a derived table named ``alias`` and the membership test a correlated
    equality: ``EXISTS (SELECT 1 FROM (SELECT k ...) AS alias WHERE alias.k = x)``. Used where an engine
    decorrelates an ``IN`` over a grouped, filtered subquery incorrectly. The two predicates agree
    wherever a NULL result and a false one filter the same rows, so only an ``IN`` reached through
    ``AND``, ``OR`` and parentheses from a ``WHERE``, ``HAVING`` or ``ON`` is rewritten, never one under
    ``NOT`` or inside a function or ``CASE``. The subquery needs one projection with an output name and a
    ``GROUP BY`` or ``HAVING``; every other ``IN`` is left alone.

    Args:
        expression: the expression to rewrite.
        alias: the name of the derived table that wraps the subquery.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for predicate in list(expression.find_all(exp.In)):
        subquery = predicate.args.get("query")
        select = subquery.unnest() if subquery is not None else None
        if (
            not isinstance(select, exp.Select)
            or len(select.expressions) != 1
            or not (select.args.get("group") or select.args.get("having"))
            or not select.expressions[0].alias_or_name
        ):
            continue
        parent = predicate.parent
        while isinstance(parent, (exp.And, exp.Or, exp.Paren)):
            parent = parent.parent
        if not isinstance(parent, (exp.Where, exp.Having, exp.Join)):
            continue
        key = select.expressions[0].alias_or_name
        correlated = exp.select(exp.Literal.number(1)).from_(
            exp.Subquery(this=select.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias)))
        )
        correlated.where(
            exp.EQ(this=exp.column(key, table=alias), expression=predicate.this.copy()), copy=False
        )
        replacement = exp.Exists(this=correlated)
        if predicate is expression:
            return replacement  # type: ignore[return-value]
        predicate.replace(replacement)
    return expression


def decorrelate_subqueries(
    expression: E,
    schema: t.Mapping[str, t.Mapping[str, str]],
    dialect: DialectType = None,
    copy: bool = True,
) -> E:
    """Turn scalar, correlated and ``IN`` subqueries into joins against derived tables.

    A scalar subquery becomes a cross join (an uncorrelated one) or a left join on its correlation keys
    to a derived table grouped by them, and a correlated or aggregated ``IN`` or ``EXISTS`` becomes a
    join with a null check, which is what an engine without decorrelation needs. This is SQLGlot's own
    ``unnest_subqueries`` after two preparations: columns are qualified from ``schema`` (the
    optimizer cannot tell what a subquery correlates with otherwise) and comma joins in selects that
    contain subqueries become ``CROSS JOIN``, because a left join added after ``FROM a, b`` binds
    to ``b`` only. Projections keep their output names as explicit aliases and table references get
    aliases; identifiers are not quoted or case-normalized.

    ``NOT IN`` is left as it is (it is three-valued; see ``not_in_subquery_to_not_exists``), and so is a
    correlated subquery that correlates through anything but equality (``l.k <> o.k``), which SQLGlot
    decorrelates with array functions that not every engine has and some evaluate differently on an
    empty array.

    Args:
        expression: the expression to rewrite.
        schema: table name to ``{column: type}``, covering every table the expression reads.
        dialect: the dialect the expression is written in, used for name resolution.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    expression = qualify(
        expression,
        dialect=dialect,
        schema=dict(schema),
        expand_stars=False,
        validate_qualify_columns=False,
        quote_identifiers=False,
        identify=False,
    )
    for select in expression.find_all(exp.Select):
        if sum(1 for _ in select.find_all(exp.Select)) > 1:
            for join in select.args.get("joins") or []:
                if not any(
                    join.args.get(key) for key in ("kind", "side", "on", "using", "method")
                ) and isinstance(join.this, (exp.Table, exp.Subquery)):
                    join.set("kind", "CROSS")
    next_alias_name = name_sequence("_u_")
    for scope in traverse_scope(expression):
        inner = scope.expression
        parent = inner.parent_select
        if not parent:
            continue
        if scope.external_columns:
            if scope.scope_type != ScopeType.SET_OPERATION and _correlated_by_equality(scope):
                decorrelate(inner, parent, scope.external_columns, next_alias_name)
        elif scope.scope_type == ScopeType.SUBQUERY:
            unnest(inner, parent, next_alias_name)
    return expression
