"""Expansion of grouping sets into a ``UNION ALL`` of plain ``GROUP BY`` queries.

For engines without ``ROLLUP``, ``CUBE``, ``GROUPING SETS`` and ``GROUPING()``. Each grouping
set becomes one ``SELECT``: the grouped expressions outside the set read ``NULL``, ``GROUPING``
becomes the integer it would have returned, and the empty set drops its ``GROUP BY`` so it
returns the one grand-total row. A query that needs the combined rows (window functions,
``ORDER BY``, ``LIMIT``, ``OFFSET``, ``DISTINCT``) runs them over the union in an outer select.

A query the rewrite cannot prove equivalent is returned unchanged.
"""

from __future__ import annotations

import itertools
import typing as t

from sqlglot import exp

E = t.TypeVar("E", bound=exp.Expr)

_CONSTRUCTS = (exp.Rollup, exp.Cube, exp.GroupingSets)
_WRAPPER_ALIAS = "_grouping_sets"
_HIDDEN_PREFIX = "_gs_"
_UNNAMED_PREFIX = "_col_"
_HAVING_ALIAS = "_gs_having"
_FILTER_ALIAS = "_gs_total"


class _Unsupported(Exception):
    pass


def expand_grouping_sets(expression: E, copy: bool = True) -> E:
    """Replace every select that groups by ROLLUP, CUBE or GROUPING SETS with a UNION ALL.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression; a select the rewrite cannot prove equivalent keeps its
        grouping sets.
    """
    if copy:
        expression = expression.copy()
    for select in reversed(list(expression.find_all(exp.Select))):
        if not _groups_by_sets(select):
            continue
        try:
            replacement = _expand(select)
        except _Unsupported:
            continue
        if isinstance(replacement, exp.SetOperation) and isinstance(
            select.parent, exp.SetOperation
        ):
            replacement = replacement.subquery()
        if select is expression:
            expression = t.cast(E, replacement)
        else:
            select.replace(replacement)
    return expression


def _groups_by_sets(select: exp.Select) -> bool:
    group = select.args.get("group")
    return bool(
        group
        and (
            group.args.get("rollup")
            or group.args.get("cube")
            or any(isinstance(item, _CONSTRUCTS) for item in group.expressions)
        )
    )


def _same(a: exp.Expr, b: exp.Expr) -> bool:
    if isinstance(a, exp.Column) and isinstance(b, exp.Column):
        if a.name.lower() != b.name.lower():
            return False
        return not a.table or not b.table or a.table.lower() == b.table.lower()
    return a == b


def _index(node: exp.Expr, universe: list[exp.Expr]) -> int | None:
    return next((i for i, item in enumerate(universe) if _same(node, item)), None)


def _members(node: exp.Expr) -> list[exp.Expr]:
    if isinstance(node, exp.Tuple):
        return list(node.expressions)
    if isinstance(node, exp.Paren):
        return [node.this]
    return [node]


def _prefixes(items: list[list[exp.Expr]]) -> list[list[exp.Expr]]:
    return [
        [member for item in items[:size] for member in item] for size in range(len(items), -1, -1)
    ]


def _subsets(items: list[list[exp.Expr]]) -> list[list[exp.Expr]]:
    return [
        [member for item in chosen for member in item]
        for size in range(len(items), -1, -1)
        for chosen in itertools.combinations(items, size)
    ]


def _options(node: exp.Expr) -> list[list[exp.Expr]]:
    if isinstance(node, exp.Rollup):
        return _prefixes([_members(item) for item in node.expressions])
    if isinstance(node, exp.Cube):
        return _subsets([_members(item) for item in node.expressions])
    if isinstance(node, exp.GroupingSets):
        options: list[list[exp.Expr]] = []
        for item in node.expressions:
            options.extend(_options(item) if isinstance(item, _CONSTRUCTS) else [_members(item)])
        return options
    return [[node]]


def _grouping_sets(select: exp.Select) -> list[list[exp.Expr]]:
    group = select.args["group"]
    if group.args.get("totals") or group.args.get("all"):
        raise _Unsupported
    rollup, cube = group.args.get("rollup"), group.args.get("cube")
    if rollup or cube:
        if (rollup and cube) or any(isinstance(item, _CONSTRUCTS) for item in group.expressions):
            raise _Unsupported
        items = [[item] for item in group.expressions]
        return _prefixes(items) if rollup else _subsets(items)
    sets: list[list[exp.Expr]] = [[]]
    for item in group.expressions:
        options = _options(item)
        sets = [existing + option for existing in sets for option in options]
    return sets


def _bind_targets(root: exp.Expr, universe: list[exp.Expr]) -> list[tuple[exp.Expr, int | None]]:
    """Grouped expressions and GROUPING calls outside aggregates and nested queries."""
    found: list[tuple[exp.Expr, int | None]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Grouping):
            found.append((node, None))
            continue
        index = _index(node, universe)
        if index is not None:
            found.append((node, index))
        elif isinstance(node, (exp.Column, exp.Window)):
            raise _Unsupported
        elif not isinstance(node, (exp.AggFunc, exp.Query, exp.Subquery)):
            stack.extend(node.iter_expressions())
    return found


def _bind(node: exp.Expr, universe: list[exp.Expr], kept: list[bool]) -> exp.Expr:
    """Copy ``node`` reading grouped expressions outside the set as NULL and GROUPING as integers."""
    node = node.copy()
    for target, index in _bind_targets(node, universe):
        if index is None:
            value = 0
            for argument in target.expressions:
                position = _index(argument, universe)
                if position is None:
                    raise _Unsupported
                value = value * 2 + (0 if kept[position] else 1)
            replacement: exp.Expr = exp.Literal.number(value)
        elif kept[index]:
            continue
        else:
            replacement = exp.Null()
        if target is node:
            node = replacement
        else:
            target.replace(replacement)
    return node


def _hoist_targets(root: exp.Expr, universe: list[exp.Expr], outputs: set[str]) -> list[exp.Expr]:
    """Aggregates, GROUPING calls and grouped expressions the outer select reads from the union."""
    found: list[exp.Expr] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Window):
            stack.extend(node.this.iter_expressions())
            stack.extend(child for key, child in _children(node) if key != "this")
        elif isinstance(node, exp.Column) and not node.table and node.name.lower() in outputs:
            continue
        elif isinstance(node, exp.AggFunc) or _index(node, universe) is not None:
            found.append(node)
        elif isinstance(node, exp.Column):
            raise _Unsupported
        elif not isinstance(node, (exp.Query, exp.Subquery)):
            stack.extend(node.iter_expressions())
    return found


def _children(node: exp.Expr) -> list[tuple[str, exp.Expr]]:
    children = []
    for key, value in node.args.items():
        for child in value if isinstance(value, list) else [value]:
            if isinstance(child, exp.Expr):
                children.append((key, child))
    return children


def _filter_grand_total(branch: exp.Select, condition: exp.Expr) -> exp.Select:
    """Apply HAVING to a grand-total row as a WHERE over it, which SQLite before 3.39 requires."""
    outputs = [exp.column(projection.alias) for projection in branch.expressions]
    branch.set(
        "expressions",
        [*branch.expressions, exp.alias_(condition.copy(), _HAVING_ALIAS, copy=False)],
    )
    return (
        exp.select(*outputs)
        .from_(branch.subquery(_FILTER_ALIAS, copy=False), copy=False)
        .where(exp.column(_HAVING_ALIAS))
    )


def _expand(select: exp.Select) -> exp.Expr:
    if select.args.get("qualify") or select.args.get("windows") or select.args.get("laterals"):
        raise _Unsupported
    order = select.args.get("order")
    if order and order.find(exp.Window):
        raise _Unsupported

    sets = _grouping_sets(select)
    universe: list[exp.Expr] = []
    for grouping_set in sets:
        for item in grouping_set:
            if _index(item, universe) is None:
                universe.append(item)

    needs_outer = bool(
        any(p.find(exp.Window) for p in select.expressions)
        or order
        or select.args.get("limit")
        or select.args.get("offset")
        or select.args.get("distinct")
    )

    inner: list[exp.Expr] = []
    outer: list[exp.Expr] = []
    hidden: list[tuple[str, exp.Expr]] = []
    outputs = {p.alias_or_name.lower() for p in select.expressions if p.alias_or_name}

    def hoist(root: exp.Expr, allow_outputs: bool) -> exp.Expr:
        root = root.copy()
        for target in _hoist_targets(root, universe, outputs if allow_outputs else set()):
            key = target.sql()
            name = next((n for n, known in hidden if known.sql() == key), None)
            if name is None:
                name = f"{_HIDDEN_PREFIX}{len(hidden)}"
                hidden.append((name, target.copy()))
            column = exp.column(name, table=_WRAPPER_ALIAS)
            if target is root:
                root = column
            else:
                target.replace(column)
        return root

    for position, projection in enumerate(select.expressions):
        if projection.find(exp.Window):
            outer.append(hoist(projection, False))
            continue
        name = projection.alias_or_name or f"{_UNNAMED_PREFIX}{position}"
        value = projection.this if isinstance(projection, exp.Alias) else projection
        inner.append(exp.alias_(value.copy(), name))
        outer.append(exp.column(name, table=_WRAPPER_ALIAS))
    outer_order = hoist(order, True) if order else None
    inner.extend(exp.alias_(value, name, copy=False) for name, value in hidden)

    having = select.args.get("having")
    branches: list[exp.Select] = []
    for grouping_set in sets:
        kept = [any(_same(item, member) for member in grouping_set) for item in universe]
        branch = select.copy()
        for arg in ("with_", "order", "limit", "offset", "distinct", "group", "having"):
            branch.set(arg, None)
        branch.set("expressions", [_bind(p, universe, kept) for p in inner])
        bound_having = _bind(having, universe, kept) if having else None
        present = [item for item, keep in zip(universe, kept) if keep]
        if present:
            branch.set("having", bound_having)
            branch.set("group", exp.Group(expressions=[item.copy() for item in present]))
        elif not any(
            node.find(exp.AggFunc) for node in (*branch.expressions, bound_having) if node
        ):
            raise _Unsupported
        elif bound_having:
            branch = _filter_grand_total(branch, bound_having.this)
        branches.append(branch)

    combined: exp.Expr = branches[0]
    for branch in branches[1:]:
        combined = exp.Union(this=combined, expression=branch, distinct=False)

    with_ = select.args.get("with_")
    if not needs_outer:
        if with_:
            combined.set("with_", with_.copy())
        return combined

    wrapped = exp.select(*outer).from_(
        exp.Subquery(this=combined, alias=exp.TableAlias(this=exp.to_identifier(_WRAPPER_ALIAS)))
    )
    if with_:
        wrapped.set("with_", with_.copy())
    for arg in ("distinct", "limit", "offset"):
        if select.args.get(arg):
            wrapped.set(arg, select.args[arg].copy())
    if outer_order is not None:
        wrapped.set("order", outer_order)
    return wrapped
