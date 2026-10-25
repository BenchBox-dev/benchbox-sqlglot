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
from sqlglot.dialects.presto import Presto
from sqlglot.dialects.sqlite import SQLite
from sqlglot.dialects.spark2 import Spark2
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.scope import Scope, traverse_scope

E = t.TypeVar("E", bound=exp.Expr)

_NOT_SCALAR_CONTEXT = (exp.AggFunc, exp.Exists, exp.In, exp.Any, exp.All, exp.Window, exp.Select)

# Nodes whose operands need parentheses around a lower-precedence operand.
_OPERATORS = (exp.Binary, exp.Unary, exp.Between, exp.In)

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


def inline_order_by_aliases(
    expression: E,
    names: t.Collection[str] | None = None,
    copy: bool = True,
) -> E:
    """Replace projection aliases used inside ``ORDER BY`` expressions with the aliased expression.

    PostgreSQL (and engines built on it) resolve an output alias in ``ORDER BY`` only when it is the
    whole sort key: ``ORDER BY lochierarchy DESC`` works, ``ORDER BY CASE WHEN lochierarchy = 0 THEN
    i_category END`` fails with ``column "lochierarchy" does not exist``. The rewrite turns the second
    form into ``CASE WHEN GROUPING(i_category) + GROUPING(i_class) = 0 THEN i_category END``. A sort
    key that is just the alias stays as it is (every engine reads it), qualified columns and columns
    of nested ``SELECT`` scopes are never replaced, and ``GROUP BY`` and ``HAVING`` are not touched
    (see ``inline_select_aliases``). If a source column has the alias's name, PostgreSQL would read the
    column inside an expression; the rewrite makes the alias win, which is what the query says.

    Args:
        expression: the expression to rewrite.
        names: only alias names in this collection (case-insensitive) are inlined; ``None`` inlines all.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    wanted = None if names is None else {name.lower() for name in names}
    for select in list(expression.find_all(exp.Select)):
        order = select.args.get("order")
        if order is None:
            continue
        aliases = {
            projection.alias.lower(): projection.this
            for projection in select.expressions
            if isinstance(projection, exp.Alias) and projection.alias
        }
        if wanted is not None:
            aliases = {name: value for name, value in aliases.items() if name in wanted}
        if not aliases:
            continue
        for ordered in order.expressions:
            key = ordered.this
            if isinstance(key, exp.Column):
                continue
            for column in list(key.find_all(exp.Column)):
                if (
                    not column.table
                    and column.name.lower() in aliases
                    and column.find_ancestor(exp.Select) is select
                ):
                    value = aliases[column.name.lower()].copy()
                    if isinstance(value, _OPERATORS) and isinstance(column.parent, _OPERATORS):
                        value = exp.Paren(this=value)
                    column.replace(value)
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
    is left alone. SQLite has no arrays, so the list is built as a JSON array,
    ``JSON_GROUP_ARRAY(x)``; SQLite steps every aggregate of a ``GROUP BY`` query over the same rows in
    the same order, so lists built together align, and ``order_by`` is not applied (an ordered aggregate
    needs SQLite 3.44).

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
        ordering = exp.Order(
            this=value.copy(),
            expressions=[
                exp.Ordered(this=exp.column(key), nulls_first=nulls_first) for key in order_by
            ],
        )
        if isinstance(resolved, SQLite):
            replacement = exp.JSONArrayAgg(this=value.copy())
        elif isinstance(resolved, Spark2):
            fields = [f"{key} AS _k{i}" for i, key in enumerate(order_by)]
            fields.append(f"{value.sql(dialect)} AS _v")
            replacement = exp.maybe_parse(
                f"TRANSFORM(SORT_ARRAY(COLLECT_LIST(STRUCT({', '.join(fields)}))), r -> r._v)",
                dialect=dialect,
            )
        else:
            replacement = exp.ArrayAgg(this=ordering)
        if node is expression:
            return replacement  # type: ignore[return-value]
        node.replace(replacement)
    return expression


_LIST_FOLDS = {"LIST_SUM": "sum", "LIST_AVG": "avg"}
_LIST_ARRAY_TYPES = (exp.List, exp.ArrayAgg, exp.JSONArrayAgg, exp.Array, exp.Transform)


def _list_family(dialect: DialectType) -> str | None:
    resolved = Dialect.get_or_raise(dialect)
    if isinstance(resolved, BigQuery):
        return "bigquery"
    if isinstance(resolved, Spark2):
        return "spark"
    if isinstance(resolved, Presto):
        return "presto"
    if isinstance(resolved, SQLite):
        return "sqlite"
    return None


def _build(sql: str, dialect: DialectType, **parts: exp.Expr) -> exp.Expr:
    """Parse ``sql`` and swap each unqualified column named like a key of ``parts`` for that part."""
    tree: exp.Expr = exp.maybe_parse(sql, dialect=dialect)
    for column in list(tree.find_all(exp.Column)):
        replacement = parts.get(column.name)
        if replacement is None or column.table:
            continue
        if column is tree:
            return replacement.copy()
        column.replace(replacement.copy())
    return tree


class _Mapped:
    """An array an aggregate reads: ``LIST_TRANSFORM(LIST_ZIP(a, b, ...), x -> f(x[1], x[2], ...))``.

    ``arrays`` are the zipped (or single) arrays and ``body`` the lambda body, or ``None`` when the
    aggregate reads the array itself. ``zipped`` says every use of the lambda parameter is a literal
    subscript naming one of ``arrays``.
    """

    def __init__(
        self, arrays: list[exp.Expr], body: exp.Expr | None, param: str | None, zipped: bool
    ) -> None:
        self.arrays = arrays
        self.body = body
        self.param = param
        self.zipped = zipped

    def substitute(self, ref: t.Callable[[int], exp.Expr]) -> exp.Expr:
        """Copy ``body`` with every read of the lambda parameter replaced by ``ref(position)``."""
        assert self.body is not None and self.param is not None
        body = self.body.copy()
        uses = [
            i
            for i in body.find_all(exp.Identifier)
            if i.name.lower() == self.param and not isinstance(i.parent, exp.Lambda)
        ]
        for use in uses:
            if self.zipped:
                bracket = use.parent
                assert isinstance(bracket, exp.Bracket)
                position = int(bracket.expressions[0].name)
                if bracket is body:
                    return ref(position)
                bracket.replace(ref(position))
            else:
                if use is body:
                    return ref(0)
                use.replace(ref(0))
        return body


def _zip_arguments(node: exp.Expr) -> list[exp.Expr] | None:
    if isinstance(node, exp.Anonymous) and node.name.upper() == "LIST_ZIP" and node.expressions:
        return list(node.expressions)
    return None


def _mapped_array(node: exp.Expr) -> _Mapped:
    """Describe the array ``node`` as a (possibly zipped and mapped) read; a plain array maps to itself."""
    if isinstance(node, exp.Transform) and isinstance(node.expression, exp.Lambda):
        lam = node.expression
        if len(lam.expressions) == 1:
            param = lam.expressions[0].name.lower()
            body = lam.this
            shadowed = any(
                nested.expressions and any(p.name.lower() == param for p in nested.expressions)
                for nested in body.find_all(exp.Lambda)
            )
            if not shadowed:
                uses = [
                    i
                    for i in body.find_all(exp.Identifier)
                    if i.name.lower() == param and not isinstance(i.parent, exp.Lambda)
                ]
                zipped = _zip_arguments(node.this)
                if zipped is None:
                    return _Mapped([node.this], body, param, False)
                if all(
                    isinstance(use.parent, exp.Bracket)
                    and use.parent.this is use
                    and len(use.parent.expressions) == 1
                    and use.parent.expressions[0].is_int
                    and 0 <= int(use.parent.expressions[0].name) < len(zipped)
                    for use in uses
                ):
                    return _Mapped(zipped, body, param, True)
    return _Mapped([node], None, None, False)


class _ListRewriter:
    def __init__(self, dialect: DialectType, family: str) -> None:
        self.dialect = dialect
        self.family = family
        self.used: set[str] = set()

    def scope_to(self, node: exp.Expr) -> None:
        """Pick fresh names against the identifiers ``node`` reads; the helper names live inside it."""
        self.used = {i.name.lower() for i in node.find_all(exp.Identifier)}

    def fresh(self, base: str) -> str:
        name = base
        while name.lower() in self.used:
            name += "_"
        self.used.add(name.lower())
        return name

    def parts(self, mapped: _Mapped) -> dict[str, exp.Expr]:
        return {f"__a{i}": array for i, array in enumerate(mapped.arrays)}

    def array_value(self, mapped: _Mapped) -> exp.Expr:
        """The mapped array as an array expression (Presto, Trino, Athena, Spark, Databricks, SQLite)."""
        arrays = mapped.arrays
        if mapped.body is None:
            return arrays[0]
        if self.family == "sqlite":
            return self.sqlite_select(mapped, None)
        n = len(arrays)
        parts = self.parts(mapped)
        names = [self.fresh(f"_v{i}") for i in range(n)]
        if n == 1:
            parts["__body"] = mapped.substitute(lambda _: exp.column(names[0]))
            return _build(f"TRANSFORM(__a0, {names[0]} -> __body)", self.dialect, **parts)
        if n == 2:
            parts["__body"] = mapped.substitute(lambda k: exp.column(names[k]))
            return _build(
                f"ZIP_WITH(__a0, __a1, ({names[0]}, {names[1]}) -> __body)", self.dialect, **parts
            )
        if self.family == "presto":
            if n > 5:
                raise NotImplementedError("Presto and Trino ZIP takes at most five arrays")
            row = self.fresh("_r")
            parts["__body"] = mapped.substitute(lambda k: _build(f"{row}[{k + 1}]", self.dialect))
            arguments = ", ".join(f"__a{i}" for i in range(n))
            return _build(f"TRANSFORM(ZIP({arguments}), {row} -> __body)", self.dialect, **parts)
        # Spark: ZIP_WITH pairs two arrays, so zip the tail into an array of structs first.
        row = self.fresh("_r")
        fields = [self.fresh(f"_f{i}") for i in range(n)]
        tail = self.zip_tail(n - 1, parts, names, row, fields)
        parts["__tail"] = tail
        parts["__body"] = mapped.substitute(
            lambda k: exp.column(names[0]) if k == 0 else _build(f"{row}.{fields[k]}", self.dialect)
        )
        return _build(
            f"ZIP_WITH(__a0, __tail, ({names[0]}, {row}) -> __body)", self.dialect, **parts
        )

    def zip_tail(
        self, start: int, parts: dict[str, exp.Expr], names: list[str], row: str, fields: list[str]
    ) -> exp.Expr:
        """Zip arrays ``start`` .. last into one array of structs with the fields ``fields[start:]``."""
        n = len(fields)
        tail = _build(
            f"ZIP_WITH(__a{n - 2}, __a{n - 1}, ({names[n - 2]}, {names[n - 1]}) -> "
            f"NAMED_STRUCT('{fields[n - 2]}', {names[n - 2]}, '{fields[n - 1]}', {names[n - 1]}))",
            self.dialect,
            **parts,
        )
        for i in range(n - 3, start - 1, -1):
            carried = ", ".join(f"'{fields[j]}', {row}.{fields[j]}" for j in range(i + 1, n))
            tail = _build(
                f"ZIP_WITH(__a{i}, __tail, ({names[i]}, {row}) -> "
                f"NAMED_STRUCT('{fields[i]}', {names[i]}, {carried}))",
                self.dialect,
                __tail=tail,
                **parts,
            )
        return tail

    def fold(self, kind: str, mapped: _Mapped) -> exp.Expr:
        if self.family == "bigquery":
            return self.bigquery_select(mapped, "SUM" if kind == "sum" else "AVG")
        if self.family == "sqlite":
            return self.sqlite_select(mapped, "SUM" if kind == "sum" else "AVG")
        array = self.array_value(mapped)
        acc, v, state = self.fresh("_acc"), self.fresh("_v"), self.fresh("_state")
        if kind == "sum":
            sql = (
                f"{'REDUCE' if self.family == 'presto' else 'AGGREGATE'}(__m, CAST(NULL AS DOUBLE), "
                f"({acc}, {v}) -> CASE WHEN {v} IS NULL THEN {acc} "
                f"WHEN {acc} IS NULL THEN CAST({v} AS DOUBLE) ELSE {acc} + CAST({v} AS DOUBLE) END"
                f"{f', {acc} -> {acc}' if self.family == 'presto' else ''})"
            )
        elif self.family == "presto":
            sql = (
                f"REDUCE(__m, ROW(CAST(0 AS DOUBLE), CAST(0 AS BIGINT)), ({state}, {v}) -> "
                f"IF({v} IS NULL, {state}, ROW({state}[1] + CAST({v} AS DOUBLE), {state}[2] + 1)), "
                f"{state} -> CASE WHEN {state}[2] = 0 THEN NULL ELSE {state}[1] / {state}[2] END)"
            )
        else:
            sql = (
                "AGGREGATE(__m, NAMED_STRUCT('s', CAST(0 AS DOUBLE), 'c', CAST(0 AS BIGINT)), "
                f"({state}, {v}) -> CASE WHEN {v} IS NULL THEN {state} "
                f"ELSE NAMED_STRUCT('s', {state}.s + CAST({v} AS DOUBLE), 'c', {state}.c + 1) END, "
                f"{state} -> CASE WHEN {state}.c = 0 THEN NULL ELSE {state}.s / {state}.c END)"
            )
        return _build(sql, self.dialect, __m=array)

    def sqlite_select(self, mapped: _Mapped, aggregate: str | None) -> exp.Expr:
        """``(SELECT agg(body) FROM JSON_EACH(...))``, or the mapped JSON array when ``aggregate`` is None.

        Zipped arrays are aligned by position with one grouped pass over the union of their
        ``JSON_EACH`` rows, so the cost is linear in the array length and a shorter array reads ``NULL``.
        """
        arrays = mapped.arrays
        n = len(arrays)
        parts = self.parts(mapped)
        if n == 1:
            item = self.fresh("_j0")
            parts["__body"] = (
                _build(f"{item}.value", self.dialect)
                if mapped.body is None
                else mapped.substitute(lambda _: _build(f"{item}.value", self.dialect))
            )
            source = f"JSON_EACH(__a0) AS {item}"
            order = f"{item}.key"
        else:
            zipped, source_id, union, position = (
                self.fresh("_z"),
                self.fresh("_s"),
                self.fresh("_u"),
                self.fresh("_pos"),
            )
            values = [self.fresh(f"_v{i}") for i in range(n)]
            parts["__body"] = mapped.substitute(
                lambda k: _build(f"{zipped}.{values[k]}", self.dialect)
            )
            rows = " UNION ALL ".join(
                f"SELECT {i} AS {source_id}, key, value FROM JSON_EACH(__a{i})" for i in range(n)
            )
            columns = ", ".join(
                f"MAX(CASE WHEN {source_id} = {i} THEN value END) AS {values[i]}" for i in range(n)
            )
            source = f"(SELECT key AS {position}, {columns} FROM ({rows}) AS {union} GROUP BY key) AS {zipped}"
            order = f"{zipped}.{position}"
        if aggregate is None:
            row = self.fresh("_r")
            sql = (
                f"(SELECT JSON_GROUP_ARRAY({row}) FROM "
                f"(SELECT __body AS {row} FROM {source} ORDER BY {order}))"
            )
        else:
            sql = f"(SELECT {aggregate}(__body) FROM {source})"
        return _build(sql, self.dialect, **parts)

    def bigquery_select(
        self, mapped: _Mapped, aggregate: str | None, ordered: bool = False
    ) -> exp.Expr:
        """``(SELECT agg(body) FROM UNNEST(...))``, or ``ARRAY(SELECT body ...)`` when ``aggregate`` is None."""
        arrays = mapped.arrays
        n = len(arrays)
        parts = self.parts(mapped)
        item, pos = self.fresh("_item"), self.fresh("_pos")
        if mapped.body is None or n == 1:
            parts["__body"] = (
                exp.column(item)
                if mapped.body is None
                else mapped.substitute(lambda _: exp.column(item))
            )
            offset = self.fresh("_offset")
            source = f"UNNEST(__a0) AS {item}" + (
                f" WITH OFFSET AS {offset}" if aggregate is None else ""
            )
            order = offset
        else:
            parts["__body"] = mapped.substitute(
                lambda k: _build(f"__a{k}[SAFE_OFFSET({pos})]", self.dialect, **parts)
            )
            lengths = ", ".join(f"ARRAY_LENGTH(__a{i})" for i in range(n))
            source = f"UNNEST(GENERATE_ARRAY(0, GREATEST({lengths}) - 1)) AS {pos}"
            order = pos
        if aggregate is None:
            sql = f"ARRAY(SELECT __body FROM {source} ORDER BY {order})"
        else:
            sql = f"(SELECT {aggregate}(__body) FROM {source})"
        return _build(sql, self.dialect, **parts)


def _is_list_value(node: exp.Expr, scope: t.Any, depth: int = 0) -> bool:
    while isinstance(node, (exp.Alias, exp.Paren)):
        node = node.this
    if isinstance(node, _LIST_ARRAY_TYPES):
        return True
    if not isinstance(node, exp.Column) or depth > 16:
        return False
    sources = scope.sources
    if node.table:
        source = sources.get(node.table)
    else:
        candidates = [
            s
            for s in sources.values()
            if isinstance(s, Scope)
            and isinstance(s.expression, exp.Select)
            and node.name in s.expression.named_selects
        ]
        source = candidates[0] if len(candidates) == 1 else None
    if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
        return False
    selects = source.expression.selects
    names = source.expression.named_selects
    if node.name not in names:
        return False
    return _is_list_value(selects[names.index(node.name)], source, depth + 1)


def _list_lengths(expression: exp.Expr) -> list[exp.Length]:
    """The ``LENGTH``/``LEN`` calls whose argument is provably a list (a ``LIST``/``ARRAY_AGG`` column)."""
    if not isinstance(expression, exp.Query):
        return []
    try:
        scopes = traverse_scope(expression)
    except OptimizeError:
        return []
    found: list[exp.Length] = []
    for scope in scopes:
        for length in scope.find_all(exp.Length):
            if length.this is not None and _is_list_value(length.this, scope):
                found.append(length)
    return found


def reduce_list_aggregates(expression: E, dialect: DialectType, copy: bool = True) -> E:
    """Rewrite DuckDB list aggregates for engines without DuckDB's list functions.

    DuckDB's ``LIST_SUM``, ``LIST_AVG``, ``LIST_TRANSFORM``, ``LIST_ZIP`` and ``LEN`` over a list
    column have no spelling in Presto, Trino, Athena, BigQuery, Spark, Databricks or SQLite. A bare
    ``LIST_SUM(list)`` or ``LIST_AVG(list)``, and the zipped and mapped shape
    ``LIST_SUM(LIST_TRANSFORM(LIST_ZIP(a, b, ...), x -> f(x[1], x[2], ...)))`` (TPC-Havoc ``1_v7``),
    become:

    - Presto, Trino, Athena: ``REDUCE`` over ``ZIP_WITH`` (two arrays) or ``TRANSFORM(ZIP(...))``
      (three to five arrays), written in ``DOUBLE``.
    - Spark, Databricks: ``AGGREGATE`` over ``ZIP_WITH`` (arrays beyond the second are zipped into
      structs first), written in ``DOUBLE``.
    - SQLite: the list is a JSON array (see ``order_list_aggregates``) and each fold is a scalar
      ``(SELECT SUM(...) FROM JSON_EACH(...))`` subquery; zipped arrays are aligned by their ``key``
      position in one grouped pass, so a shorter array reads ``NULL`` past its end. A bare ``LIST_ZIP`` has no
      equivalent and is left alone.
    - BigQuery: a scalar ``(SELECT SUM(...) FROM UNNEST(...))`` subquery that reads the zipped arrays
      by ``SAFE_OFFSET`` over ``GENERATE_ARRAY(0, GREATEST(ARRAY_LENGTH(...)) - 1)``, keeping the
      column type.

    ``NULL`` elements are skipped and an empty or all-``NULL`` list gives ``NULL``, as DuckDB does;
    shorter zipped arrays read ``NULL`` past their end, as ``LIST_ZIP`` pads them. A ``LIST_ZIP``
    inside ``LIST_TRANSFORM`` whose lambda uses its parameter only as ``x[<literal>]`` is rewritten; any
    other use leaves that call unchanged. A ``LIST_TRANSFORM`` over ``LIST_ZIP`` outside an aggregate
    becomes ``ZIP_WITH``/``TRANSFORM(ZIP(...))`` (Presto family, Spark family) or an ordered
    ``ARRAY(SELECT ...)`` (BigQuery); a bare ``LIST_ZIP`` becomes ``ZIP`` or ``ARRAYS_ZIP`` (BigQuery has no
    equivalent and is left alone). ``LEN``/``LENGTH`` of a column that a ``LIST`` or ``ARRAY_AGG``
    defines in a ``WITH`` or derived table becomes ``CARDINALITY``, ``SIZE``, ``ARRAY_LENGTH`` or
    ``JSON_ARRAY_LENGTH`` (SQLite).

    Run ``order_list_aggregates`` first so every list a zip reads is built in the same order. Other
    dialects, and calls the rewrite does not recognise, are left alone. Arrays that are not plain
    columns are written once per use.

    Args:
        expression: the expression to rewrite (read in a DuckDB-compatible dialect).
        dialect: the dialect the expression will be generated for.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    family = _list_family(dialect)
    if family is None:
        return expression.copy() if copy else expression
    if copy:
        expression = expression.copy()
    rewriter = _ListRewriter(dialect, family)
    lengths = _list_lengths(expression)

    def swap(node: exp.Expr, replacement: exp.Expr) -> None:
        nonlocal expression
        if node is expression:
            expression = replacement  # type: ignore[assignment]
        else:
            node.replace(replacement)

    for length in lengths:
        swap(
            length,
            exp.Anonymous(this="JSON_ARRAY_LENGTH", expressions=[length.this.copy()])
            if family == "sqlite"
            else exp.ArraySize(this=length.this.copy()),
        )

    def attached(node: exp.Expr) -> bool:
        while node.parent is not None:
            node = node.parent
        return node is expression

    changed = True
    while changed:
        changed = False
        for node in list(expression.walk(bfs=False)):
            if not attached(node):
                continue
            replacement: exp.Expr | None = None
            rewriter.scope_to(node)
            if (
                isinstance(node, exp.Anonymous)
                and node.name.upper() in _LIST_FOLDS
                and len(node.expressions) == 1
            ):
                replacement = rewriter.fold(
                    _LIST_FOLDS[node.name.upper()], _mapped_array(node.expressions[0])
                )
            elif isinstance(node, exp.Transform) and (
                family == "bigquery" or _zip_arguments(node.this) is not None
            ):
                mapped = _mapped_array(node)
                if mapped.body is not None:
                    replacement = (
                        rewriter.bigquery_select(mapped, None)
                        if family == "bigquery"
                        else rewriter.array_value(mapped)
                    )
            elif family in ("presto", "spark") and _zip_arguments(node) is not None:
                name = "ZIP" if family == "presto" else "ARRAYS_ZIP"
                replacement = exp.Anonymous(
                    this=name, expressions=[a.copy() for a in node.expressions]
                )
            if replacement is not None:
                swap(node, replacement)
                changed = True
                break
    return expression
