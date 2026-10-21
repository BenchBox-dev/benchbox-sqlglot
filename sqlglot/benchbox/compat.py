"""Engine-compatibility rewrites maintained by the BenchBox fork.

Each rewrite takes and returns an expression (``copy=True`` by default) and
leaves every gate (which query, which column, which alias) to the caller.
"""

from __future__ import annotations

import typing as t

from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.dialects.clickhouse import ClickHouse
from sqlglot.dialects.dialect import Dialect, DialectType
from sqlglot.dialects.spark2 import Spark2

E = t.TypeVar("E", bound=exp.Expr)

_NOT_SCALAR_CONTEXT = (exp.AggFunc, exp.Exists, exp.In, exp.Any, exp.All, exp.Window, exp.Select)

_CLICKHOUSE_FILTER_COMBINATORS = {
    exp.Sum: "sumOrNullIf",
    exp.Avg: "avgOrNullIf",
    exp.Min: "minOrNullIf",
    exp.Max: "maxOrNullIf",
}


def inline_select_aliases(
    expression: E,
    clauses: t.Sequence[str] = ("having", "where"),
    copy: bool = True,
) -> E:
    """Replace references to ``SELECT`` aliases in ``HAVING`` and ``WHERE`` with the aliased expression.

    Engines that resolve a projection alias only in ``ORDER BY`` reject
    ``SELECT SUM(x) AS total ... HAVING total > 1``. The rewrite turns the
    reference into ``HAVING SUM(x) > 1``. Only unqualified columns whose nearest
    enclosing ``SELECT`` is the one defining the alias are replaced, and
    ``ORDER BY`` and ``GROUP BY`` references are left alone.

    Args:
        expression: the expression to rewrite.
        clauses: the ``SELECT`` argument names (``"having"``, ``"where"``, ...) to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for select in list(expression.find_all(exp.Select)):
        aliases = {
            projection.alias: projection.this
            for projection in select.expressions
            if isinstance(projection, exp.Alias) and projection.alias
        }
        if not aliases:
            continue
        for clause_name in clauses:
            clause = select.args.get(clause_name)
            if clause is None:
                continue
            for column in list(clause.find_all(exp.Column)):
                if column.table or column.name not in aliases:
                    continue
                if column.find_ancestor(exp.Select) is select:
                    column.replace(aliases[column.name].copy())
    return expression


def _inside_clause(node: exp.Expr, clauses: t.Sequence[str]) -> bool:
    ancestor: exp.Expr | None = node
    while ancestor is not None:
        if ancestor.arg_key in clauses:
            return True
        ancestor = ancestor.parent
    return False


def qualify_columns_by_name(
    expression: E,
    tables: t.Mapping[str, str],
    clauses: t.Sequence[str] | None = None,
    quoted: bool = False,
    copy: bool = True,
) -> E:
    """Qualify every unqualified column whose name is a key of ``tables`` with the mapped table.

    Fixes columns that more than one joined relation exposes under the same name, when the
    caller knows which relation is meant. Columns that already have a qualifier are kept.

    Args:
        expression: the expression to rewrite.
        tables: column name to the table (or alias) that owns it.
        clauses: when given, only columns inside one of these ``SELECT`` argument names
            (``"having"``, ``"where"``, ...) are qualified; otherwise every column is.
        quoted: write the qualifier as a quoted identifier.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    columns: t.Iterable[exp.Column] = expression.find_all(exp.Column)
    if clauses is not None:
        columns = [column for column in columns if _inside_clause(column, clauses)]
    for column in columns:
        if not column.table and (table := tables.get(column.name)):
            column.set("table", exp.to_identifier(table, quoted=quoted or None))
    return expression


def add_row_source_to_fromless_selects(
    expression: E,
    source: str,
    dialect: DialectType,
    copy: bool = True,
) -> E:
    """Give every ``SELECT`` that has a ``WHERE`` but no ``FROM`` a one-row ``FROM`` source.

    Engines such as BigQuery reject ``SELECT 1 WHERE cond`` and need a table to filter. The source the
    caller passes must produce exactly one row, for example ``UNNEST([1])`` for BigQuery, so the
    filtered result is unchanged.

    Args:
        expression: the expression to rewrite.
        source: SQL of the one-row table expression, written in ``dialect``.
        dialect: the dialect the expression and ``source`` are written in.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for select in list(expression.find_all(exp.Select)):
        if select.args.get("where") is not None and not select.args.get("from_"):
            select.from_(source, dialect=dialect, copy=False)
    return expression


def _default_nulls_first(dialect: DialectType, desc: bool) -> bool:
    ordering = Dialect.get_or_raise(dialect).NULL_ORDERING
    if ordering == "nulls_are_last":
        return False
    return (ordering == "nulls_are_small") != desc


def _filtered_aggregate(node: exp.Filter, dialect: DialectType) -> exp.Expr:
    aggregate = node.this.copy()
    condition = node.expression.this.copy()
    value = aggregate.this
    resolved = Dialect.get_or_raise(dialect)

    if isinstance(resolved, ClickHouse):
        if isinstance(aggregate, exp.Count) and isinstance(value, exp.Star):
            return exp.Anonymous(this="countIf", expressions=[condition])
        if isinstance(aggregate, exp.Count) and not isinstance(value, exp.Distinct):
            return exp.Anonymous(
                this="countIf",
                expressions=[
                    exp.and_(
                        exp.Not(this=exp.Is(this=value.copy(), expression=exp.Null())), condition
                    )
                ],
            )
        if isinstance(aggregate, exp.Count) and len(value.expressions) == 1:
            return exp.Anonymous(
                this="uniqExactIf", expressions=[value.expressions[0].copy(), condition]
            )
        combinator = _CLICKHOUSE_FILTER_COMBINATORS.get(type(aggregate))
        if combinator is None or isinstance(value, exp.Distinct):
            raise NotImplementedError(
                "ClickHouse FILTER rewrite handles COUNT, SUM, AVG, MIN and MAX; "
                f"got {aggregate.sql_name()}{' DISTINCT' if isinstance(value, exp.Distinct) else ''}."
            )
        return exp.Anonymous(this=combinator, expressions=[value.copy(), condition])

    if (
        isinstance(resolved, BigQuery)
        and isinstance(aggregate, exp.Count)
        and isinstance(value, exp.Star)
    ):
        return exp.CountIf(this=condition)

    if isinstance(aggregate, exp.Count) and isinstance(value, exp.Star):
        aggregate.set("this", exp.If(this=condition, true=exp.Literal.number(1), false=exp.Null()))
    elif isinstance(value, exp.Distinct):
        value.set(
            "expressions",
            [
                exp.If(this=condition.copy(), true=item.copy(), false=exp.Null())
                for item in value.expressions
            ],
        )
    else:
        aggregate.set("this", exp.If(this=condition, true=value.copy(), false=exp.Null()))
    return aggregate


def filtered_aggregates_to_conditional(
    expression: E,
    dialect: DialectType,
    windows_only: bool = False,
    copy: bool = True,
) -> E:
    """Replace ``agg(x) FILTER (WHERE cond)`` with the conditional form the target dialect runs.

    * ``bigquery``: ``COUNT(*) FILTER`` becomes ``COUNTIF(cond)``; any other aggregate wraps its
      argument as ``agg(IF(cond, x, NULL))``, including each expression of ``DISTINCT``.
    * ``clickhouse``: ``COUNT(*)`` becomes ``countIf(cond)``, ``COUNT(x)`` becomes
      ``countIf(x IS NOT NULL AND cond)``, ``COUNT(DISTINCT x)`` becomes ``uniqExactIf(x, cond)`` and
      ``SUM``, ``AVG``, ``MIN`` and ``MAX`` become ``sumOrNullIf``, ``avgOrNullIf``, ``minOrNullIf``
      and ``maxOrNullIf``, which keep the NULL of an empty input. Any other aggregate raises
      ``NotImplementedError``.
    * every other dialect: ``agg(IF(cond, x, NULL))``, and ``COUNT(*)`` becomes
      ``COUNT(IF(cond, 1, NULL))``.

    Args:
        expression: the expression to rewrite.
        dialect: the dialect the expression is written in and will be generated for.
        windows_only: rewrite only the aggregates inside a window (``agg(x) FILTER (...) OVER (...)``),
            for engines that accept ``FILTER`` on ordinary aggregates but not on window ones.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.

    Raises:
        NotImplementedError: for a ClickHouse aggregate without a conditional form.
    """
    if copy:
        expression = expression.copy()
    for node in list(expression.find_all(exp.Filter)):
        if windows_only and not isinstance(node.parent, exp.Window):
            continue
        replacement = _filtered_aggregate(node, dialect)
        if node is expression:
            return replacement  # type: ignore[return-value]
        node.replace(replacement)
    return expression


def drop_literal_row_source(expression: E, alias: str, copy: bool = True) -> E:
    """Drop the one-row ``(SELECT 1) AS alias`` source from a ``SELECT`` that never reads it.

    ``SELECT 0 AS r FROM (SELECT 1) AS dual WHERE FALSE`` becomes ``SELECT 0 AS r WHERE FALSE``.
    A ``FROM``-less ``SELECT`` is the one-row source every engine the fork is tested on accepts
    (PostgreSQL, DuckDB, DataFusion, Spark, ClickHouse, StarRocks, Doris and SQLite), so no
    ``VALUES`` list, ``system.one`` or ``dual`` is needed. Engines that reject ``WHERE`` without a
    ``FROM`` (BigQuery) get a source back from ``add_row_source_to_fromless_selects``.

    Only a ``SELECT`` whose single ``FROM`` item is a derived table named ``alias`` over a
    provably one-row inner ``SELECT`` (one numeric literal projection and no ``FROM``, ``WHERE``,
    ``LIMIT``, ``GROUP BY``, ``HAVING``, ``DISTINCT`` or any other clause) is rewritten, and only
    when it neither selects ``*`` nor references the derived table's column.

    Args:
        expression: the expression to rewrite.
        alias: the derived table name to match.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for select in list(expression.find_all(exp.Select)):
        source = select.args.get("from_")
        subquery = source.this if source else None
        if (
            select.args.get("joins")
            or not isinstance(subquery, exp.Subquery)
            or subquery.alias != alias
            or any(isinstance(projection, exp.Star) for projection in select.expressions)
        ):
            continue
        inner = subquery.this
        if (
            not isinstance(inner, exp.Select)
            or len(inner.expressions) != 1
            or any(value for key, value in inner.args.items() if key != "expressions")
        ):
            continue
        projection = inner.expressions[0]
        literal = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(literal, exp.Literal) or literal.is_string:
            continue
        column = projection.alias if isinstance(projection, exp.Alias) else ""
        if any(
            ref.table == alias or (column and not ref.table and ref.name == column)
            for ref in select.find_all(exp.Column)
        ):
            continue
        select.set("from_", None)
    return expression


def flatten_count_of_count(expression: E, copy: bool = True) -> E:
    """Count the rows of the source instead of the one row a nested ``COUNT(*)`` returns.

    ``SELECT COUNT(*) FROM (SELECT COUNT(*) AS c FROM t WHERE p)`` counts the single row of the
    inner aggregate, so it always returns 1. When the author meant the number of source rows, as in
    a "source records" column next to a target count, this rewrite replaces it with the inner
    ``SELECT COUNT(*) FROM t WHERE p``. **The returned value changes** (1 becomes the row count),
    so apply it only where the row count is the intended answer.

    Only an outer ``SELECT COUNT(*)`` over exactly one derived table, with nothing else, whose inner
    ``SELECT`` is a single ungrouped ``COUNT(*)`` with a ``FROM``, is rewritten. The outer projection
    (and its alias) is kept.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for outer in list(expression.find_all(exp.Select)):
        source = outer.args.get("from_")
        subquery = source.this if source else None
        if (
            len(outer.expressions) != 1
            or not _is_count_star(outer.expressions[0])
            or not isinstance(subquery, exp.Subquery)
            or not isinstance(subquery.this, exp.Select)
            or any(
                outer.args.get(arg)
                for arg in (
                    "where",
                    "group",
                    "having",
                    "joins",
                    "with_",
                    "distinct",
                    "limit",
                    "qualify",
                )
            )
        ):
            continue
        inner = subquery.this
        if (
            len(inner.expressions) != 1
            or not _is_count_star(inner.expressions[0])
            or not inner.args.get("from_")
            or any(
                inner.args.get(arg)
                for arg in ("group", "having", "with_", "distinct", "limit", "qualify")
            )
        ):
            continue
        replacement = inner.copy()
        replacement.set("expressions", [outer.expressions[0].copy()])
        if outer is expression:
            return replacement  # type: ignore[return-value]
        outer.replace(replacement)
    return expression


def _is_count_star(projection: exp.Expr) -> bool:
    count = projection.this if isinstance(projection, exp.Alias) else projection
    return isinstance(count, exp.Count) and isinstance(count.this, exp.Star)


def name_literal_subquery_columns(expression: E, column: str, copy: bool = True) -> E:
    """Alias the unnamed numeric literal projection of a one-column derived table as ``column``.

    ``(SELECT 1)`` becomes ``(SELECT 1 AS column)``, for engines that need every derived-table
    column to be named. A derived table with an aliased projection is left alone.

    Args:
        expression: the expression to rewrite.
        column: the alias given to the literal.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for subquery in list(expression.find_all(exp.Subquery)):
        inner = subquery.this
        if not isinstance(inner, exp.Select) or len(inner.expressions) != 1:
            continue
        literal = inner.expressions[0]
        if isinstance(literal, exp.Literal) and not literal.is_string:
            literal.replace(exp.alias_(literal.copy(), column))
    return expression


def wrap_grouped_scalar_subqueries(
    expression: E,
    aggregate: type[exp.AggFunc] = exp.First,
    copy: bool = True,
) -> E:
    """Wrap each scalar subquery projected by a grouped ``SELECT`` in an aggregate.

    Engines that insist every projection of a ``GROUP BY`` query is a grouping key or an aggregate
    reject an uncorrelated scalar subquery there. ``FIRST`` of a single-row subquery is the
    subquery, so the result is unchanged. Subqueries already inside an aggregate, ``EXISTS``,
    ``IN``, ``ANY``, ``ALL`` or a window are left alone, so applying the rewrite twice is safe.

    Args:
        expression: the expression to rewrite.
        aggregate: the aggregate function class that wraps the subquery.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for select in list(expression.find_all(exp.Select)):
        if not select.args.get("group"):
            continue
        for projection in select.expressions:
            subqueries = [
                node
                for node in projection.dfs(
                    prune=lambda n: isinstance(n, (*_NOT_SCALAR_CONTEXT, exp.Subquery))
                )
                if isinstance(node, exp.Subquery)
            ]
            for subquery in subqueries:
                subquery.replace(aggregate(this=subquery.copy()))
    return expression


def order_list_aggregates(
    expression: E,
    order_by: t.Sequence[str],
    dialect: DialectType,
    copy: bool = True,
) -> E:
    """Turn each list-building aggregate into an ``ARRAY_AGG`` ordered by ``order_by``.

    ``LIST(x)`` (DuckDB) and an unordered ``ARRAY_AGG(x)`` build arrays in arbitrary order; the
    rewrite makes them ``ARRAY_AGG(x ORDER BY key, ...)`` so every engine builds the same array.
    Spark, Spark 2 and Databricks have no ordered collect, so the array is built as
    ``TRANSFORM(SORT_ARRAY(COLLECT_LIST(STRUCT(key AS _k0, ..., x AS _v))), r -> r._v)``, which sorts
    by the keys and keeps NULL elements, as ``ARRAY_AGG`` does. An ``ARRAY_AGG`` that already orders
    is left alone.

    Args:
        expression: the expression to rewrite.
        order_by: the ascending ordering key columns, as names.
        dialect: the dialect the expression will be generated for; it decides the NULL ordering.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    resolved = Dialect.get_or_raise(dialect)
    nulls_first = _default_nulls_first(dialect, False)
    for node in list(expression.find_all(exp.List, exp.ArrayAgg)):
        if isinstance(node, exp.ArrayAgg):
            value = node.this
            if isinstance(value, exp.Order):
                continue
        elif len(node.expressions) == 1:
            value = node.expressions[0]
        else:
            continue
        replacement: exp.Expr
        if isinstance(resolved, Spark2):
            fields = [f"{key} AS _k{i}" for i, key in enumerate(order_by)]
            fields.append(f"{value.sql(dialect)} AS _v")
            replacement = exp.maybe_parse(
                f"TRANSFORM(SORT_ARRAY(COLLECT_LIST(STRUCT({', '.join(fields)}))), r -> r._v)",
                dialect=dialect,
            )
        else:
            replacement = exp.ArrayAgg(
                this=exp.Order(
                    this=value.copy(),
                    expressions=[
                        exp.Ordered(this=exp.column(key), nulls_first=nulls_first)
                        for key in order_by
                    ],
                )
            )
        if node is expression:
            return replacement  # type: ignore[return-value]
        node.replace(replacement)
    return expression
