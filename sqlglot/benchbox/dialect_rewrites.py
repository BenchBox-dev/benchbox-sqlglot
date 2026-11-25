"""Expression rewrites that rewrite a query's shape for engines with narrower SQL.

Like ``sqlglot.benchbox.rewrites``, every rewrite takes and returns an expression
(``copy=True`` by default), so the caller picks the read and write dialects, and
every gate (table names, aliases) arrives as an argument.
"""

from __future__ import annotations

import logging
import typing as t

from sqlglot import exp, transforms

logger = logging.getLogger("sqlglot")

E = t.TypeVar("E", bound=exp.Expr)


def alias_outer_table_references(expression: E, table: str, alias: str, copy: bool = True) -> E:
    """Alias the unaliased ``table`` of the outermost select and requalify its columns.

    Only a table that belongs directly to the outermost select is aliased, so a
    subquery that reads the same table keeps its own unaliased reference. Nothing
    changes when the outer select does not read ``table``.

    Args:
        expression: the expression to rewrite.
        table: the name of the table to alias.
        alias: the alias to give it.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    outer_tables = [
        node
        for node in tree.find_all(exp.Table)
        if node.name == table and not node.alias and node.find_ancestor(exp.Select) is tree
    ]
    for node in outer_tables:
        node.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
    if outer_tables:
        for column in tree.find_all(exp.Column):
            if column.table == table:
                column.set("table", exp.to_identifier(alias))
    return tree


def alias_derived_tables(expression: E, prefix: str = "_dt", copy: bool = True) -> E:
    """Give every unaliased derived table in a FROM or JOIN the name ``<prefix><n>``.

    Numbering starts at 1 in breadth-first order and skips names the query already
    uses as a table or subquery alias.

    Args:
        expression: the expression to rewrite.
        prefix: the alias prefix.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    taken = {node.alias for node in tree.find_all(exp.Subquery) if node.alias}
    taken.update(node.alias for node in tree.find_all(exp.Table) if node.alias)
    counter = 0
    for subquery in tree.find_all(exp.Subquery):
        if subquery.alias or not isinstance(subquery.parent, (exp.From, exp.Join)):
            continue
        if not isinstance(subquery.this, exp.Query):
            continue
        counter += 1
        while f"{prefix}{counter}" in taken:
            counter += 1
        subquery.set("alias", exp.TableAlias(this=exp.to_identifier(f"{prefix}{counter}")))
        taken.add(f"{prefix}{counter}")
    return tree


def coalesce_count_subqueries(expression: E, copy: bool = True) -> E:
    """Wrap an ungrouped scalar ``COUNT`` subquery in ``COALESCE(..., 0)``.

    Engines that turn a correlated scalar subquery into a join return NULL, not 0,
    for a group with no rows.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for subquery in list(tree.find_all(exp.Subquery)):
        inner = subquery.this
        if (
            not isinstance(inner, exp.Select)
            or inner.args.get("group")
            or len(inner.expressions) != 1
        ):
            continue
        if not isinstance(subquery.parent, (exp.Alias, exp.Select)):
            continue
        if isinstance(inner.expressions[0], exp.Count):
            subquery.replace(
                exp.Coalesce(this=subquery.copy(), expressions=[exp.Literal.number(0)])
            )
    return tree


def trim_char_case_comparisons(expression: E, copy: bool = True) -> E:
    """Compare ``CASE`` results with ``=`` after trimming trailing blanks from both sides.

    A fixed-width character value is blank padded, and a ``CASE`` that returns the
    column or a string literal compares differently on engines that do not pad.
    Applies to ``column = CASE ... ELSE column END`` and to ``'literal' = CASE WHEN
    ... THEN column ... END``.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for node in list(tree.find_all(exp.EQ)):
        column, case = node.this, node.expression
        if isinstance(column, exp.Case):
            column, case = case, column
        if not isinstance(case, exp.Case):
            continue
        self_defaulting = isinstance(column, exp.Column) and case.args.get("default") == column
        column_guarded_literal = (
            isinstance(column, exp.Literal)
            and column.is_string
            and any(
                isinstance(branch.args.get("true"), exp.Column)
                for branch in case.args.get("ifs") or []
            )
        )
        if not self_defaulting and not column_guarded_literal:
            continue
        node.replace(
            exp.EQ(
                this=exp.Trim(this=column.copy(), position="TRAILING"),
                expression=exp.Trim(this=case.copy(), position="TRAILING"),
            )
        )
    return tree


def in_subquery_or_to_left_join(expression: E, copy: bool = True) -> E:
    """Rewrite an ``IN (subquery)`` under an ``OR`` as a ``LEFT JOIN`` to a distinct derived table.

    The predicate becomes ``in_value_<n> IS NOT NULL``, so the rows an ``OR`` keeps
    through its other branch survive. Only a select with a single source and no
    joins is rewritten, and only an uncorrelated ``IN`` (one that does not read the
    outer table) outside ``NOT``.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    root: exp.Expr = tree
    select = root if isinstance(root, exp.Select) else root.find(exp.Select)
    if select is None or select.args.get("joins"):
        return tree
    source = select.args.get("from_")
    source_table = source.this if source is not None else None
    outer_names = {source_table.alias_or_name} if isinstance(source_table, exp.Table) else set()
    position = 0
    for node in list(select.find_all(exp.In)):
        query = node.args.get("query")
        if (
            not isinstance(query, exp.Subquery)
            or not isinstance(query.this, exp.Select)
            or len(query.this.expressions) != 1
            or node.parent_select is not select
            or node.find_ancestor(exp.Not)
            or not node.find_ancestor(exp.Or)
        ):
            continue
        inner = query.this.copy()
        inner_names = {table.alias_or_name for table in inner.find_all(exp.Table)}
        if any(column.table in outer_names - inner_names for column in inner.find_all(exp.Column)):
            continue
        position += 1
        table_alias = f"in_subquery_{position}"
        value_alias = f"in_value_{position}"
        inner.set("distinct", exp.Distinct())
        inner.set("expressions", [exp.alias_(inner.expressions[0], value_alias)])
        select.append(
            "joins",
            exp.Join(
                this=exp.Subquery(
                    this=inner, alias=exp.TableAlias(this=exp.to_identifier(table_alias))
                ),
                side="LEFT",
                on=exp.EQ(this=node.this.copy(), expression=exp.column(value_alias, table_alias)),
            ),
        )
        node.replace(
            exp.Not(this=exp.Is(this=exp.column(value_alias, table_alias), expression=exp.Null()))
        )
    return tree


def factor_common_conjuncts(expression: E, copy: bool = True) -> E:
    """Factor the conjuncts shared by every branch of an ``OR`` out of it.

    ``(a AND b) OR (a AND c)`` becomes ``a AND (b OR c)``; when a branch consists
    only of the shared conjuncts the whole ``OR`` collapses to them.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression

    def disjuncts(node: exp.Expr) -> list[exp.Expr]:
        if isinstance(node, exp.Or):
            return disjuncts(node.this) + disjuncts(node.expression)
        return [node]

    def conjuncts(node: exp.Expr) -> list[exp.Expr]:
        if isinstance(node, exp.Paren) and isinstance(node.this, exp.And):
            return conjuncts(node.this)
        if isinstance(node, exp.And):
            return conjuncts(node.this) + conjuncts(node.expression)
        return [node]

    def conjoin(parts: list[exp.Expr]) -> exp.Expr:
        result = parts[0]
        for part in parts[1:]:
            result = exp.And(this=result, expression=part)
        return result

    def disjoin(parts: list[exp.Expr]) -> exp.Expr:
        result = parts[0]
        for part in parts[1:]:
            result = exp.Or(this=result, expression=part)
        return result

    for node in list(tree.find_all(exp.Or)):
        if isinstance(node.parent, exp.Or):
            continue
        terms = [
            conjuncts(term.this if isinstance(term, exp.Paren) else term)
            for term in disjuncts(node)
        ]
        if len(terms) < 2:
            continue
        common = [part for part in terms[0] if all(part in other for other in terms[1:])]
        if not common:
            continue
        remainders: list[exp.Expr | None] = []
        for term in terms:
            rest = [part for part in term if part not in common]
            remainders.append(conjoin([part.copy() for part in rest]) if rest else None)
        if any(rest is None for rest in remainders):
            replacement = conjoin([part.copy() for part in common])
        else:
            residual = exp.Paren(
                this=disjoin([exp.Paren(this=rest) for rest in remainders if rest is not None])
            )
            replacement = conjoin([*(part.copy() for part in common), residual])
        node.replace(
            replacement
            if isinstance(node.parent, (exp.Where, exp.Paren))
            else exp.Paren(this=replacement)
        )
    return tree


def positive_count_subquery_to_exists(expression: E, copy: bool = True) -> E:
    """Rewrite ``(SELECT COUNT(*) ...) > 0`` as ``EXISTS (SELECT 1 ...)``.

    Only a subquery with a single ``COUNT(*)`` projection and no ``DISTINCT``,
    ``GROUP BY``, ``HAVING``, ``QUALIFY``, ``LIMIT`` or ``OFFSET`` is rewritten.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    for node in list(tree.find_all(exp.GT)):
        subquery, zero = node.this, node.expression
        if (
            not isinstance(subquery, exp.Subquery)
            or not isinstance(zero, exp.Literal)
            or zero.name != "0"
        ):
            continue
        inner = subquery.this
        if (
            not isinstance(inner, exp.Select)
            or len(inner.expressions) != 1
            or any(
                inner.args.get(key)
                for key in ("distinct", "group", "having", "qualify", "limit", "offset")
            )
        ):
            continue
        projection = inner.expressions[0]
        count = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(count, exp.Count) or not isinstance(count.this, exp.Star):
            continue
        inner.set("expressions", [exp.Literal.number(1)])
        node.replace(exp.Exists(this=inner.copy()))
    return tree


def _outer_order_key(
    key: exp.Expr,
    outputs: t.Sequence[tuple[exp.Expr, exp.Expr]],
    nested: bool = False,
) -> exp.Expr | None:
    if isinstance(key, exp.Literal) and not key.is_string:
        return key.copy()
    if isinstance(key, exp.Column):
        if not key.table and not nested:
            for _, projection in outputs:
                if projection.name.lower() == key.name.lower():
                    return projection.copy()
        for source, projection in outputs:
            if source == key:
                return projection.copy()
        return None
    for source, projection in outputs:
        if source == key:
            return projection.copy()
    if key.find(exp.Window, exp.AggFunc) is not None:
        return None
    resolved = key.copy()
    for column in list(resolved.find_all(exp.Column)):
        replacement = _outer_order_key(column, outputs, nested=True)
        if replacement is None:
            return None
        column.replace(replacement)
    return resolved


def _hoistable_distinct(select: exp.Select) -> bool:
    distinct = select.args.get("distinct")
    return isinstance(distinct, exp.Distinct) and not distinct.args.get("on")


def hoist_qualify_outer_clauses(expression: E, copy: bool = True) -> E:
    """Eliminate ``QUALIFY`` and keep ``ORDER BY``, ``LIMIT``, ``OFFSET`` and ``DISTINCT`` outside it.

    ``transforms.eliminate_qualify`` leaves these clauses on the inner select, where
    the order of the final result is lost. They move to the outer select, with the
    sort keys rewritten to the projections the subquery exposes. A sort key that is
    not projected leaves the order in place and logs a warning, because the result
    order is then undefined. The expression is returned unchanged when no select
    combines ``QUALIFY`` with one of those clauses.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    tree = expression.copy() if copy else expression
    targets = [
        select
        for select in tree.find_all(exp.Select)
        if select.args.get("qualify") is not None
        and (
            select.args.get("order") is not None
            or select.args.get("limit") is not None
            or select.args.get("offset") is not None
            or _hoistable_distinct(select)
        )
    ]
    for select in reversed(targets):
        order = select.args.get("order")
        sources = [item.this if isinstance(item, exp.Alias) else item for item in select.selects]
        eliminated = t.cast(exp.Select, transforms.eliminate_qualify(select.copy()))
        inner = eliminated.args["from_"].this.this
        outputs = list(zip(sources, eliminated.selects))
        keys = (
            [_outer_order_key(item.this, outputs) for item in order.expressions]
            if order is not None
            else []
        )
        order_hoisted = order is not None and all(key is not None for key in keys)
        if order is not None and not order_hoisted:
            logger.warning(
                "QUALIFY elimination cannot hoist %s to the outer select; its sort keys are not "
                "projected, so the result order is undefined",
                order.sql(),
            )
        if order is not None and order_hoisted:
            ordered = []
            for item, key in zip(order.expressions, keys):
                hoisted = item.copy()
                hoisted.set("this", key)
                ordered.append(hoisted)
            eliminated.set("order", exp.Order(expressions=ordered))
            if not inner.args.get("distinct") or _hoistable_distinct(inner):
                inner.set("order", None)
        if order is None or order_hoisted:
            for clause in ("limit", "offset"):
                if inner.args.get(clause) is not None:
                    eliminated.set(clause, inner.args[clause].copy())
                    inner.set(clause, None)
        if _hoistable_distinct(inner):
            eliminated.set("distinct", inner.args["distinct"].copy())
            inner.set("distinct", None)
        if select.parent is None:
            tree = t.cast(E, eliminated)
        else:
            select.replace(eliminated)
    return tree
