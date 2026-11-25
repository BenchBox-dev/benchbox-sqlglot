"""Query-shape rewrites for engines that reject SQL their peers accept.

Like ``sqlglot.benchbox.rewrites``, every rewrite takes and returns an expression
(``copy=True`` by default), so the caller picks the read and write dialects. Every
gate (column names, table names, name suffixes) is an argument.
"""

from __future__ import annotations

import typing as t

from sqlglot import exp

E = t.TypeVar("E", bound=exp.Expr)

_DERIVED_SOURCE_ALIAS = "hoisted_source"
_ORDERED_ROWS_ALIAS = "ordered_rows"


def drop_empty_group_by(expression: E, copy: bool = True) -> E:
    """Remove ``()`` from every ``GROUP BY``, dropping the clause when nothing else remains.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        if group is None:
            continue
        remaining = [
            item
            for item in group.expressions
            if not (isinstance(item, exp.Tuple) and not item.expressions)
        ]
        if len(remaining) == len(group.expressions):
            continue
        if remaining or any(
            group.args.get(key) for key in ("grouping_sets", "cube", "rollup", "totals")
        ):
            group.set("expressions", remaining)
        else:
            select.set("group", None)
    return tree


def unwrap_single_grouping_set(expression: E, copy: bool = True) -> E:
    """Rewrite ``GROUP BY GROUPING SETS ((a, b))`` with one set as ``GROUP BY a, b``.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for group in list(tree.find_all(exp.Group)):
        if len(group.expressions) != 1 or not isinstance(group.expressions[0], exp.GroupingSets):
            continue
        sets = group.expressions[0].expressions
        if len(sets) != 1:
            continue
        only = sets[0]
        keys = only.expressions if isinstance(only, exp.Tuple) else [only]
        group.set("expressions", [key.copy() for key in keys])
    return tree


def add_dual_source_to_filtered_selects(expression: E, copy: bool = True) -> E:
    """Write ``FROM DUAL`` (the bare keyword) on a select that has a ``WHERE`` but no source.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for select in tree.find_all(exp.Select):
        if select.args.get("from_") is None and select.args.get("where") is not None:
            select.set("from_", exp.From(this=exp.Var(this="DUAL")))
    return tree


def inline_sourceless_exists(expression: E, copy: bool = True) -> E:
    """Replace ``EXISTS (SELECT ... WHERE cond)`` that has no source with ``(cond)``.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for node in list(tree.find_all(exp.Exists)):
        inner = node.this.this if isinstance(node.this, exp.Subquery) else node.this
        if not isinstance(inner, exp.Select) or inner.args.get("from_") is not None:
            continue
        where = inner.args.get("where")
        if where is None or any(
            inner.args.get(key) for key in ("group", "having", "limit", "joins")
        ):
            continue
        node.replace(exp.paren(where.this.copy()))
    return tree


def exists_to_derived_join(expression: E, copy: bool = True) -> E:
    """Rewrite a correlated ``EXISTS`` conjunct as a join to a distinct derived table.

    The ``EXISTS`` must be a top-level conjunct of the ``WHERE``, its subquery a
    single table with equality correlations (one outer column per inner column).
    The derived table keeps the inner table's alias and exposes the correlated
    columns as ``<alias>_<column>``. An ``EXISTS`` that does not fit is left alone.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for node in list(tree.find_all(exp.Exists)):
        outer = node.find_ancestor(exp.Select)
        inner = node.this.this if isinstance(node.this, exp.Subquery) else node.this
        if outer is None or not isinstance(inner, exp.Select) or outer.args.get("where") is None:
            continue
        conjuncts = list(outer.args["where"].this.flatten())
        if not any(item is node for item in conjuncts):
            continue
        source = inner.args.get("from_")
        if (
            source is None
            or not isinstance(source.this, exp.Table)
            or inner.args.get("joins")
            or any(inner.args.get(key) for key in ("group", "having", "limit", "order", "distinct"))
            or inner.args.get("where") is None
        ):
            continue
        inner_alias = source.this.alias_or_name
        local: list[exp.Expr] = []
        correlated: list[tuple[exp.Expr, exp.Column]] = []
        join_columns: dict[str, exp.Column] = {}
        convertible = True
        for conjunct in inner.args["where"].this.flatten():
            if not any(
                column.table not in ("", inner_alias) for column in conjunct.find_all(exp.Column)
            ):
                local.append(conjunct)
                continue
            if not isinstance(conjunct, exp.EQ):
                convertible = False
                break
            sides = [conjunct.this, conjunct.expression]
            mine = [
                side for side in sides if isinstance(side, exp.Column) and side.table == inner_alias
            ]
            theirs = [
                side
                for side in sides
                if isinstance(side, exp.Column) and side.table not in ("", inner_alias)
            ]
            if len(mine) != 1 or len(theirs) != 1:
                convertible = False
                break
            join_columns.setdefault(mine[0].name, mine[0])
            correlated.append((conjunct, mine[0]))
        if not convertible or not correlated:
            continue
        derived = exp.Select(
            expressions=[
                exp.alias_(column.copy(), f"{inner_alias}_{name}", quoted=column.this.quoted)
                for name, column in join_columns.items()
            ],
            distinct=exp.Distinct(),
        )
        derived.set("from_", exp.From(this=source.this.copy()))
        if local:
            derived.set("where", exp.Where(this=exp.and_(*[item.copy() for item in local])))
        outer.append(
            "joins",
            exp.Join(
                this=exp.Subquery(
                    this=derived, alias=exp.TableAlias(this=exp.to_identifier(inner_alias))
                )
            ),
        )
        kept = [item for item in conjuncts if item is not node]
        for conjunct, column in correlated:
            joined = conjunct.copy()
            for candidate in joined.find_all(exp.Column):
                if candidate.table == inner_alias:
                    candidate.replace(
                        exp.column(f"{inner_alias}_{candidate.name}", table=inner_alias)
                    )
            kept.append(joined)
        outer.set("where", exp.Where(this=exp.and_(*kept)))
    return tree


def drop_correlated_scalar_group_by(
    expression: E, columns: t.Sequence[str], copy: bool = True
) -> E:
    """Drop the ``GROUP BY`` of a scalar subquery whose keys are pinned to outer columns.

    The subquery must be an operand of a binary expression, group by exactly
    ``columns`` (case-insensitive) and equate each of them to a column with a
    different name in its ``WHERE``, so it yields one group. Run it before
    ``fold_scalar_having_into_case``, which needs the ``GROUP BY`` gone.

    Args:
        expression: the expression to rewrite.
        columns: the names of the grouping columns that qualify.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    wanted = {name.lower() for name in columns}
    for subquery in list(tree.find_all(exp.Subquery)):
        inner = subquery.this
        if not isinstance(inner, exp.Select) or not isinstance(subquery.parent, exp.Binary):
            continue
        group = inner.args.get("group")
        where = inner.args.get("where")
        if group is None or where is None:
            continue
        keys = group.expressions
        if not keys or not all(isinstance(key, exp.Column) for key in keys):
            continue
        if {key.name.lower() for key in keys} != wanted:
            continue
        if all(_pinned_to_other_column(key, where) for key in keys):
            inner.set("group", None)
    return tree


def fold_scalar_having_into_case(expression: E, copy: bool = True) -> E:
    """Move the ``HAVING`` of an ungrouped scalar subquery into a ``CASE`` around its value.

    ``(SELECT SUM(x) FROM t HAVING SUM(x) > 1)`` becomes
    ``(SELECT CASE WHEN SUM(x) > 1 THEN SUM(x) END FROM t)``.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for subquery in list(tree.find_all(exp.Subquery)):
        inner = subquery.this
        if not isinstance(inner, exp.Select) or not isinstance(subquery.parent, exp.Binary):
            continue
        having = inner.args.get("having")
        if having is None or inner.args.get("group") is not None or len(inner.expressions) != 1:
            continue
        projection = inner.expressions[0]
        value = projection.this if isinstance(projection, exp.Alias) else projection
        guarded = exp.Case(ifs=[exp.If(this=having.this.copy(), true=value.copy())])
        value.replace(guarded)
        inner.set("having", None)
    return tree


def _pinned_to_other_column(key: exp.Expr, where: exp.Where) -> bool:
    for conjunct in where.this.flatten():
        if not isinstance(conjunct, exp.EQ):
            continue
        left, right = conjunct.this, conjunct.expression
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            continue
        for mine, other in ((left, right), (right, left)):
            if mine == key and other.name.lower() != key.name.lower():
                return True
    return False


def qualify_aggregated_columns(expression: E, tables: t.Mapping[str, str], copy: bool = True) -> E:
    """Qualify the unqualified columns inside aggregate functions.

    Only the columns named in ``tables`` (matched case-insensitively) are
    qualified, with the table it maps to. Columns outside an aggregate, such as
    in ``ORDER BY``, keep their spelling.

    Args:
        expression: the expression to rewrite.
        tables: maps a column name to the table that qualifies it.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    wanted = {name.lower(): table for name, table in tables.items()}
    for aggregate in list(tree.find_all(exp.AggFunc)):
        for column in aggregate.find_all(exp.Column):
            table = wanted.get(column.name.lower())
            if table is not None and not column.table:
                column.set("table", exp.to_identifier(table))
    return tree


def wrap_order_by_subquery_aliases(expression: E, copy: bool = True) -> E:
    """Sort by an aliased scalar subquery in an outer select.

    When a select orders by the alias of a projected subquery, the select becomes
    a derived table named ``ordered_rows`` and ``ORDER BY``, ``LIMIT`` and
    ``OFFSET`` move to ``SELECT * FROM`` it.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.

    Raises:
        ValueError: when the select orders by a column it does not project.
    """
    tree = expression.copy() if copy else expression
    if not isinstance(tree, exp.Select):
        return tree
    order = tree.args.get("order")
    if order is None:
        return tree
    projected = {projection.alias_or_name for projection in tree.expressions}
    subquery_aliases = {
        projection.alias
        for projection in tree.expressions
        if isinstance(projection, exp.Alias) and projection.find(exp.Subquery) is not None
    }
    ordered = {column.name for column in order.find_all(exp.Column) if not column.table}
    if not ordered & subquery_aliases:
        return tree
    if not ordered <= projected:
        raise ValueError("ORDER BY references a column that is not projected")
    limit = tree.args.get("limit")
    offset = tree.args.get("offset")
    tree.set("order", None)
    tree.set("limit", None)
    tree.set("offset", None)
    outer = exp.select("*").from_(tree.subquery(_ORDERED_ROWS_ALIAS))
    outer.set("order", order)
    if limit is not None:
        outer.set("limit", limit)
    if offset is not None:
        outer.set("offset", offset)
    return t.cast(E, outer)


def alias_order_by_projections(expression: E, copy: bool = True) -> E:
    """Order by the alias of a projected subquery or window instead of repeating it.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for select in tree.find_all(exp.Select):
        order = select.args.get("order")
        if order is None:
            continue
        projected = {
            projection.this.sql(): projection.args["alias"]
            for projection in select.expressions
            if isinstance(projection, exp.Alias)
            and projection.this.find(exp.Subquery, exp.Window) is not None
        }
        if not projected:
            continue
        for node in list(order.walk()):
            alias = projected.get(node.sql())
            if alias is not None:
                node.replace(exp.Column(this=alias.copy()))
    return tree


def date_column_integer_arithmetic(expression: E, suffix: str, copy: bool = True) -> E:
    """Rewrite ``<column> +/- <integer>`` as ``DATE_ADD``/``DATE_SUB`` in days.

    Only a column whose name ends with ``suffix`` (case-insensitive) and an
    unsigned integer literal are rewritten; the caller names the convention.

    Args:
        expression: the expression to rewrite.
        suffix: the ending of the names of the date columns.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    suffix = suffix.lower()
    for node in list(tree.find_all(exp.Add, exp.Sub)):
        column, amount = node.this, node.expression
        if not (isinstance(column, exp.Column) and column.name.lower().endswith(suffix)):
            continue
        if not (isinstance(amount, exp.Literal) and not amount.is_string and amount.name.isdigit()):
            continue
        builder = exp.DateAdd if isinstance(node, exp.Add) else exp.DateSub
        node.replace(builder(this=column.copy(), expression=amount.copy(), unit=exp.var("DAY")))
    return tree


def _empty_input_projection(select: exp.Select, keys: set[str]) -> exp.Select:
    empty = select.copy()
    for clause in (
        "from_",
        "joins",
        "laterals",
        "where",
        "group",
        "order",
        "limit",
        "offset",
        "with_",
    ):
        empty.set(clause, None)
    for index, projection in enumerate(list(empty.expressions)):
        if isinstance(projection, exp.Column) and projection.sql() in keys:
            empty.expressions[index] = exp.alias_(exp.Null(), projection.name)
            continue
        if isinstance(projection, exp.Alias) and isinstance(projection.this, exp.Column):
            if projection.this.sql() in keys:
                projection.set("this", exp.Null())
                continue
        for aggregate in list(projection.find_all(exp.AggFunc)):
            aggregate.replace(
                exp.Cast(this=exp.Literal.number(0), to=exp.DataType.build("bigint"))
                if isinstance(aggregate, exp.Count)
                else exp.Null()
            )
        for column in list(projection.find_all(exp.Column)):
            column.replace(exp.Null())
    return empty


def rollup_empty_input_row(expression: E, copy: bool = True) -> E:
    """Make a ``ROLLUP`` select return its grand-total row on empty input.

    Engines whose ``ROLLUP`` returns no rows for empty input get a ``UNION ALL``
    with one row (``NULL`` keys, ``0`` counts, ``NULL`` for other aggregates) that
    only appears when the rollup produced none. ``ORDER BY``, ``LIMIT`` and
    ``OFFSET`` move to the outer select. A select with ``HAVING`` or ``GROUPING()``,
    or one that orders by a qualified column, is left alone.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    taken = {name.lower() for name in (cte.alias for cte in tree.find_all(exp.CTE))}
    taken |= {table.name.lower() for table in tree.find_all(exp.Table)}
    for select in reversed(list(tree.find_all(exp.Select))):
        group = select.args.get("group")
        if (
            group is None
            or len(group.expressions) != 1
            or not isinstance(group.expressions[0], exp.Rollup)
        ):
            continue
        order = select.args.get("order")
        if select.args.get("having") is not None or select.find(exp.Grouping) is not None:
            continue
        if order is not None and any(column.table for column in order.find_all(exp.Column)):
            continue
        keys = {key.sql() for key in group.expressions[0].expressions}
        empty = _empty_input_projection(select, keys)
        name = "rollup_rows"
        while name in taken:
            name += "_"
        taken.add(name)
        limit, offset = select.args.get("limit"), select.args.get("offset")
        with_clause = select.args.get("with_")
        for clause in ("order", "limit", "offset", "with_"):
            select.set(clause, None)
        empty.set(
            "from_",
            exp.From(
                this=exp.Subquery(
                    this=exp.select("1"),
                    alias=exp.TableAlias(this=exp.to_identifier("rollup_dual")),
                )
            ),
        )
        empty.set(
            "where",
            exp.Where(
                this=exp.Not(this=exp.Exists(this=exp.select("1").from_(exp.to_table(name))))
            ),
        )
        union = exp.Union(
            this=exp.select("*").from_(exp.to_table(name)), expression=empty, distinct=False
        )
        wrapper = exp.select("*").from_(union.subquery("rollup_union"))
        for clause, value in (("order", order), ("limit", limit), ("offset", offset)):
            if value is not None:
                wrapper.set(clause, value)
        wrapper = wrapper.with_(name, as_=select.copy())
        if with_clause is not None:
            for cte in reversed(with_clause.expressions):
                wrapper.args["with_"].expressions.insert(0, cte.copy())
        if select is tree:
            return t.cast(E, wrapper)
        select.replace(wrapper)
    return tree


def _is_hoistable(node: exp.Expr) -> bool:
    if isinstance(node, (exp.Exists, exp.Subquery)):
        return True
    return isinstance(node, exp.In) and node.args.get("query") is not None


def _outermost_subqueries(aggregate: exp.Expr) -> list[exp.Expr]:
    found: list[exp.Expr] = []
    stack = [aggregate]
    while stack:
        node = stack.pop()
        for child in node.iter_expressions():
            if _is_hoistable(child):
                found.append(child)
            else:
                stack.append(child)
    return found


def _hoist_aggregate_subqueries(select: exp.Select) -> None:
    aggregates = [
        agg for agg in select.find_all(exp.AggFunc) if agg.find_ancestor(exp.Select) is select
    ]
    hoisted = [node for agg in aggregates for node in _outermost_subqueries(agg)]
    source = select.args.get("from_")
    if not hoisted or source is None:
        return
    joins = select.args.get("joins") or []
    single_table = not joins and isinstance(source.this, exp.Table)
    source_alias = source.this.alias_or_name if single_table else _DERIVED_SOURCE_ALIAS
    columns: dict[str, str] = {}
    projections: list[exp.Expr] = [exp.Star()]
    for node in hoisted:
        key = node.sql()
        if key not in columns:
            columns[key] = f"hoisted_subquery_{len(columns) + 1}"
            projections.append(exp.alias_(node.copy(), columns[key]))
        node.replace(exp.column(columns[key]))
    if not single_table and any(
        column.table
        for column in select.find_all(exp.Column)
        if column.find_ancestor(exp.Select) is select
        and column.find_ancestor(exp.Where, exp.Join) is None
    ):
        raise ValueError(
            "Cannot hoist subqueries from a multi-source select with qualified column references"
        )
    inner = exp.Select(expressions=projections)
    inner.set("from_", source)
    if joins:
        inner.set("joins", joins)
    if select.args.get("where") is not None:
        inner.set("where", select.args["where"])
    select.set("joins", None)
    select.set("where", None)
    select.set("from_", exp.From(this=inner.subquery(source_alias)))


def hoist_aggregate_subqueries(expression: E, copy: bool = True) -> E:
    """Compute the subqueries inside aggregate arguments in a derived table below the aggregate.

    ``SUM((SELECT ...))`` becomes a ``hoisted_subquery_<n>`` column of a derived
    table that replaces the select's source, for engines that reject a subquery
    in an aggregate. The derived table keeps the source table's alias, or is
    named ``hoisted_source`` when the select has several sources.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.

    Raises:
        ValueError: when the select has several sources and a column outside
            ``WHERE`` and ``JOIN`` is qualified.
    """
    tree = expression.copy() if copy else expression
    for select in list(tree.find_all(exp.Select)):
        _hoist_aggregate_subqueries(select)
    return tree
