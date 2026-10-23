"""Expression rewrites for engines that reject SQL their peers accept.

Like ``sqlglot.benchbox.rewrites``, every rewrite takes and returns an
expression (``copy=True`` by default) and every gate is an argument, so the
caller picks the read and write dialects and supplies any names the rewrite
must match.
"""

from __future__ import annotations

import re
import typing as t
from decimal import Decimal

from sqlglot import exp

E = t.TypeVar("E", bound=exp.Expr)


def lowercase_identifiers(
    expression: E,
    patterns: t.Iterable[str | t.Pattern[str]],
    copy: bool = True,
) -> E:
    """Lowercase every identifier whose whole name matches one of ``patterns``.

    ClickHouse compares identifiers case-sensitively, so ``D_YEAR`` does not find a
    column created as ``d_year``. Patterns match the exact spelling (case-sensitive,
    anchored at both ends); a quoted identifier is lowercased too. String literals,
    function names and keywords are never touched.

    Args:
        expression: the expression to rewrite.
        patterns: regular expressions (or compiled patterns) for the names to lowercase.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    compiled = [re.compile(p) if isinstance(p, str) else p for p in patterns]
    if copy:
        expression = expression.copy()
    for identifier in expression.find_all(exp.Identifier):
        name = identifier.name
        if name != name.lower() and any(p.fullmatch(name) for p in compiled):
            identifier.set("this", name.lower())
    return expression


_REAL_TYPES = (exp.DType.DOUBLE, exp.DType.FLOAT)


def _branch_slots(node: exp.Expr) -> list[tuple[exp.Expr, str]]:
    if isinstance(node, exp.Case):
        slots = [(branch, "true") for branch in node.args.get("ifs") or []]
        slots.append((node, "default"))
    elif isinstance(node, exp.If) and not isinstance(node.parent, exp.Case):
        slots = [(node, "true"), (node, "false")]
    else:
        return []
    return [(owner, key) for owner, key in slots if owner.args.get(key) is not None]


def _is_real_quotient(node: exp.Expr) -> bool:
    bare = node.unnest()
    if not isinstance(bare, exp.Div):
        return False
    dividend = bare.this.unnest()
    return isinstance(dividend, exp.Cast) and dividend.to.this in _REAL_TYPES


def _has_real_quotient_branch(node: exp.Expr) -> bool:
    return any(_is_real_quotient(owner.args[key]) for owner, key in _branch_slots(node))


def _is_zero_decimal_literal(node: exp.Expr | None) -> bool:
    return (
        isinstance(node, exp.Literal)
        and node.is_number
        and "." in node.name
        and Decimal(node.name) == 0
    )


def _is_zero_decimal_cast(node: exp.Expr | None) -> bool:
    return (
        isinstance(node, exp.Cast)
        and node.to.this == exp.DType.DECIMAL
        and isinstance(node.this, exp.Literal)
        and node.this.is_number
        and Decimal(node.this.name) == 0
    )


def decimal_zero_literals(
    expression: E,
    precision: int = 15,
    scale: int = 2,
    copy: bool = True,
) -> E:
    """Replace a zero decimal literal (``0.0``, ``0.00``) with ``CAST(0 AS DECIMAL(p, s))``.

    A bare ``0.0`` is a ``Float64`` in ClickHouse, so ``COALESCE(decimal_col, 0.0)`` and
    ``CASE WHEN ... THEN 0.0 ELSE decimal_col END`` mix a float with a decimal and either
    fail or lose exactness. The literal is replaced where it is an argument of
    ``COALESCE``, an operand of ``+``, ``-``, ``*`` or ``/`` whose other operand is a
    column, or a ``THEN``/``ELSE`` result.

    A zero that shares its ``CASE``/``IF`` with a quotient whose dividend is already cast to
    ``DOUBLE`` (``decimal_division_to_double``) stays ``0.0``, a ``Float64``, because ClickHouse
    has no common type for ``Float64`` and ``Decimal`` without the Variant type.

    Args:
        expression: the expression to rewrite.
        precision: precision of the decimal type.
        scale: scale of the decimal type.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()

    def zero() -> exp.Cast:
        return exp.cast(
            exp.Literal.number(0),
            exp.DataType.build(
                exp.DType.DECIMAL,
                expressions=[
                    exp.DataTypeParam(this=exp.Literal.number(precision)),
                    exp.DataTypeParam(this=exp.Literal.number(scale)),
                ],
                nullable=False,
            ),
        )

    for coalesce in list(expression.find_all(exp.Coalesce)):
        if _is_zero_decimal_literal(coalesce.this):
            coalesce.set("this", zero())
        coalesce.set(
            "expressions",
            [zero() if _is_zero_decimal_literal(item) else item for item in coalesce.expressions],
        )
    for binary in list(expression.find_all(exp.Add, exp.Sub, exp.Mul, exp.Div)):
        left, right = binary.this, binary.expression
        if _is_zero_decimal_literal(right) and isinstance(left, exp.Column):
            binary.set("expression", zero())
        elif _is_zero_decimal_literal(left) and isinstance(right, exp.Column):
            binary.set("this", zero())
    for branch in list(expression.find_all(exp.If)):
        if _has_real_quotient_branch(
            branch.parent if isinstance(branch.parent, exp.Case) else branch
        ):
            continue
        if _is_zero_decimal_literal(branch.args.get("true")):
            branch.set("true", zero())
        if _is_zero_decimal_literal(branch.args.get("false")):
            branch.set("false", zero())
    for case in list(expression.find_all(exp.Case)):
        if _has_real_quotient_branch(case):
            continue
        if _is_zero_decimal_literal(case.args.get("default")):
            case.set("default", zero())
    return expression


def _last_select(expression: exp.Expr) -> exp.Select | None:
    while isinstance(expression, exp.SetOperation):
        expression = expression.expression
    return expression if isinstance(expression, exp.Select) else None


def add_query_settings(
    expression: E,
    settings: t.Mapping[str, str | int | float | bool],
    copy: bool = True,
) -> E:
    """Append ``SETTINGS name = value, ...`` to a top-level ClickHouse query.

    Applies to a ``SELECT`` (including one with a ``WITH``) and to the last ``SELECT`` of a
    ``UNION``/``INTERSECT``/``EXCEPT``, where ClickHouse reads it as the setting of the whole
    query. A setting the query already carries is replaced, not repeated. Any other
    expression (DDL, inserts, parenthesized queries) is returned unchanged.

    Args:
        expression: the expression to rewrite.
        settings: setting names mapped to values; strings become string literals and booleans
            become ``1`` or ``0``.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    select = _last_select(expression)
    if select is None or not settings:
        return expression

    def literal(value: str | int | float | bool) -> exp.Expr:
        if isinstance(value, str):
            return exp.Literal.string(value)
        return exp.Literal.number(int(value) if isinstance(value, bool) else value)

    new = {
        name.lower(): exp.EQ(this=exp.var(name), expression=literal(v))
        for name, v in settings.items()
    }
    kept = [
        item
        for item in select.args.get("settings") or []
        if not (isinstance(item, exp.EQ) and item.this.name.lower() in new)
    ]
    select.set("settings", [*kept, *new.values()])
    return expression


def _conjuncts(node: exp.Expr) -> list[exp.Expr]:
    node = node.unnest()
    if isinstance(node, exp.And):
        return [*_conjuncts(node.this), *_conjuncts(node.expression)]
    return [node]


def _source_names(select: exp.Select) -> set[str]:
    names: set[str] = set()
    sources = [select.args["from_"].this] if select.args.get("from_") else []
    sources.extend(join.this for join in select.args.get("joins") or [])
    for source in sources:
        if source.alias_or_name:
            names.add(source.alias_or_name.lower())
    return names


def _outer_references(node: exp.Expr, inner: set[str]) -> list[exp.Column]:
    return [
        column
        for column in node.find_all(exp.Column)
        if column.table and column.table.lower() not in inner
    ]


def _is_positive_filter(node: exp.Expr) -> bool:
    parent = node.parent
    while isinstance(parent, (exp.And, exp.Or, exp.Paren)):
        parent = parent.parent
    return isinstance(parent, exp.Where)


def correlated_exists_to_in(
    expression: E,
    expected: int | None = None,
    copy: bool = True,
) -> E:
    """Turn ``EXISTS (SELECT ... WHERE outer.k = inner.k AND ...)`` into ``outer.k IN (SELECT inner.k ...)``.

    ClickHouse cannot plan a correlated ``EXISTS`` that is combined with ``OR``; the ``IN``
    form is a semi-join it can. An ``EXISTS`` is rewritten when it sits in a ``WHERE``
    under ``AND``/``OR`` only (a ``NOT`` anywhere above it would turn ``NULL`` into a
    different result), is a plain ``SELECT`` with a ``WHERE`` (no ``DISTINCT``, ``GROUP BY``,
    ``HAVING``, window, ``LIMIT``, aggregate or set operation), and has exactly one
    correlated predicate: an equality between a qualified column of an outer source and an
    expression of the inner sources. All other references must be to inner sources. Outer
    columns are recognized by a qualifier that names no inner source.

    Args:
        expression: the expression to rewrite.
        expected: when given, the number of rewrites required; a different count raises
            ``ValueError`` so a query of an unexpected shape fails closed instead of running
            in a form the engine cannot plan.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    count = 0
    for node in reversed(list(expression.find_all(exp.Exists, bfs=False))):
        inner = node.this
        if not isinstance(inner, exp.Select) or not _is_positive_filter(node):
            continue
        if (
            any(
                inner.args.get(k)
                for k in ("distinct", "group", "having", "qualify", "limit", "offset", "windows")
            )
            or not inner.args.get("where")
            or inner.find(exp.AggFunc, exp.Window)
        ):
            continue
        names = _source_names(inner)
        conjuncts = _conjuncts(inner.args["where"].this)
        correlated = [c for c in conjuncts if _outer_references(c, names)]
        if len(correlated) != 1:
            continue
        predicate = correlated[0]
        if not isinstance(predicate, exp.EQ):
            continue
        sides = [predicate.this, predicate.expression]
        outer_sides = [
            s for s in sides if isinstance(s, exp.Column) and _outer_references(s, names)
        ]
        inner_sides = [s for s in sides if not _outer_references(s, names)]
        if len(outer_sides) != 1 or len(inner_sides) != 1:
            continue
        other = [c for c in inner.args.get("joins") or []] + [e for e in inner.expressions]
        if any(_outer_references(o, names) for o in other):
            continue
        rest = [c for c in conjuncts if c is not predicate]
        inner.set("expressions", [inner_sides[0].copy()])
        if rest:
            inner.set("where", exp.Where(this=exp.and_(*[c.copy() for c in rest], copy=False)))
        else:
            inner.set("where", None)
        node.replace(exp.In(this=outer_sides[0].copy(), query=exp.Subquery(this=inner.copy())))
        count += 1
    if expected is not None and count != expected:
        raise ValueError(f"expected {expected} correlated EXISTS rewrites, applied {count}")
    return expression


def add_derived_table_aliases(
    expression: E,
    prefix: str = "_sq",
    quoted: bool = True,
    copy: bool = True,
) -> E:
    """Give every unaliased derived table in ``FROM`` or a join an alias ``<prefix><n>``.

    StarRocks and Doris require every derived table to have an alias. Numbering starts at
    ``0`` and follows the order the tables appear in the text, outer queries first. Tables
    that already have an alias, ``IN``/``EXISTS`` subqueries and scalar subqueries are left
    alone.

    Args:
        expression: the expression to rewrite.
        prefix: alias prefix.
        quoted: quote the generated alias.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    number = 0
    for node in expression.walk(bfs=False):
        if (
            isinstance(node, exp.Subquery)
            and isinstance(node.parent, (exp.From, exp.Join))
            and node.parent.this is node
            and not node.alias
            and isinstance(node.this, (exp.Query, exp.With))
        ):
            node.set(
                "alias",
                exp.TableAlias(this=exp.to_identifier(f"{prefix}{number}", quoted=quoted)),
            )
            number += 1
    return expression


def qualify_order_by_columns(
    expression: E,
    qualifiers: t.Mapping[str, str],
    copy: bool = True,
) -> E:
    """Qualify unqualified ``ORDER BY`` columns with the source that ``qualifiers`` names.

    When several sources of a query expose a column of the same name (``item_id`` in three
    CTEs joined together), an unqualified ``ORDER BY item_id`` is ambiguous on engines that
    resolve it against the sources rather than the select list. For each ``SELECT`` whose
    ``FROM``/``JOIN`` sources include the table ``qualifiers[column]``, an unqualified
    ``ORDER BY`` reference to ``column`` becomes ``table.column``. A reference is left alone
    when a select-list alias of the same name defines something other than that column.

    Args:
        expression: the expression to rewrite.
        qualifiers: column name mapped to the source (table, CTE or alias) to qualify it with;
            names compare case-insensitively.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    wanted = {column.lower(): table for column, table in qualifiers.items()}
    for select in expression.find_all(exp.Select):
        order = select.args.get("order")
        if not order or not wanted:
            continue
        sources = _source_names(select)
        redefined = {
            item.alias.lower()
            for item in select.expressions
            if isinstance(item, exp.Alias)
            and not (
                isinstance(item.this, exp.Column) and item.this.name.lower() == item.alias.lower()
            )
        }
        for column in order.find_all(exp.Column):
            table = wanted.get(column.name.lower())
            if (
                column.table
                or table is None
                or table.lower() not in sources
                or column.name.lower() in redefined
            ):
                continue
            column.set("table", exp.to_identifier(table, quoted=column.this.args.get("quoted")))
    return expression


def alias_qualified_projections(expression: E, top_level: bool = False, copy: bool = True) -> E:
    """Name every qualified column projected by a nested ``SELECT`` after the column.

    ClickHouse can name the output of ``SELECT v1.d_year FROM v1, v1 AS v1_lag`` ``v1.d_year`` when the
    same table appears more than once in the ``FROM`` clause, so an outer ``WHERE d_year = 2001`` fails
    with an unknown identifier. SQL names the column ``d_year``. The rewrite writes ``v1.d_year AS
    d_year`` in every ``SELECT`` that sits in a ``WITH`` or a subquery. A projection is left alone when
    the name would repeat another output name of the same ``SELECT`` (the engine then keeps the
    qualified names apart) and ``*`` projections are never touched.

    Args:
        expression: the expression to rewrite.
        top_level: also alias the projections of the outermost query, which only changes the result
            column names a client reads.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for select in list(expression.find_all(exp.Select)):
        if not top_level and select.find_ancestor(exp.CTE, exp.Subquery) is None:
            continue
        names = [projection.alias_or_name.lower() for projection in select.expressions]
        for projection in list(select.expressions):
            if (
                isinstance(projection, exp.Column)
                and projection.table
                and not isinstance(projection.this, exp.Star)
                and names.count(projection.name.lower()) == 1
            ):
                projection.replace(exp.Alias(this=projection.copy(), alias=projection.this.copy()))
    return expression


def _query_output_names(query: exp.Expr) -> set[str] | None:
    """Return the lowercase output names of a query, or ``None`` when a ``*`` hides them."""
    node = query
    while isinstance(node, (exp.Subquery, exp.SetOperation)):
        node = node.this
    if not isinstance(node, exp.Select):
        return None
    names: set[str] = set()
    for projection in node.expressions:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            return None
        names.add(projection.alias_or_name.lower())
    return names


def _derived_sources(select: exp.Select) -> dict[str, tuple[exp.Identifier, set[str]]] | None:
    """Map each ``FROM``/``JOIN`` source of ``select`` to its alias and output names.

    Only derived tables and references to a ``WITH`` entry have output names that can be read
    from the query, so any other source (a base table, a table function) makes the result ``None``.
    """
    ctes: dict[str, exp.Expr] = {}
    ancestor: exp.Expr | None = select
    while ancestor is not None:
        clause = ancestor.args.get("with_")
        if clause is not None:
            for cte in clause.expressions:
                ctes.setdefault(cte.alias.lower(), cte.this)
        ancestor = ancestor.parent
    sources = [select.args["from_"].this] if select.args.get("from_") else []
    sources.extend(join.this for join in select.args.get("joins") or [])
    found: dict[str, tuple[exp.Identifier, set[str]]] = {}
    for source in sources:
        alias = source.args.get("alias")
        if isinstance(source, exp.Subquery) and alias is not None:
            names = _query_output_names(source.this)
        elif isinstance(source, exp.Table) and not source.db and source.name.lower() in ctes:
            names = _query_output_names(ctes[source.name.lower()])
        else:
            return None
        if names is None:
            return None
        found[source.alias_or_name.lower()] = (
            alias.this.copy() if alias is not None else source.this.copy(),
            names,
        )
    return found


def qualify_alias_shadowed_columns(expression: E, copy: bool = True) -> E:
    """Qualify a column that a projection alias of the same name hides, outside ``ORDER BY``.

    ClickHouse resolves a name to a projection alias before a column of the same name, so
    ``SELECT SUM(jan_sales) AS jan_sales, SUM(jan_sales / sq_ft) AS jan_per_sq_foot FROM (...) AS x``
    reads the second ``jan_sales`` as the first aggregate and fails with ``ILLEGAL_AGGREGATION``; in a
    ``WHERE`` or ``GROUP BY`` the alias can silently replace the column. SQL resolves those clauses
    against the ``FROM`` sources, and the rewrite writes that reading out: ``SUM(x.jan_sales / sq_ft)``.

    A column is qualified only when its nearest ``SELECT`` projects an alias of that name (other than
    the same column under its own name), every ``FROM`` and ``JOIN`` source is a derived table or a
    ``WITH`` entry with a readable output list, and exactly one source outputs that name. ``ORDER BY``
    keeps the alias, as SQL does, and a ``SELECT`` with any other source is left alone because its
    columns are unknown.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for select in list(expression.find_all(exp.Select)):
        aliases = {
            projection.alias.lower()
            for projection in select.expressions
            if isinstance(projection, exp.Alias)
            and not (
                isinstance(projection.this, exp.Column)
                and projection.this.name.lower() == projection.alias.lower()
            )
        }
        if not aliases:
            continue
        sources = _derived_sources(select)
        if not sources:
            continue
        for column in list(select.find_all(exp.Column)):
            name = column.name.lower()
            if column.table or name not in aliases:
                continue
            if column.find_ancestor(exp.Select) is not select or column.find_ancestor(exp.Order):
                continue
            owners = [alias for alias, names in sources.values() if name in names]
            if len(owners) == 1:
                column.set("table", owners[0].copy())
    return expression


def decimal_division_to_double(expression: E, copy: bool = True) -> E:
    """Divide as ``DOUBLE`` so ClickHouse keeps the fractional digits of a decimal quotient.

    ClickHouse gives ``Decimal(p, s) / Decimal`` and ``Decimal(p, s) / Int`` the scale ``s`` of the
    dividend and truncates the rest (``CAST(75 AS Decimal(9, 2)) / 7`` is ``10.71``, not
    ``10.714285714285714``), where DuckDB, SQLite and DataFusion divide in double precision.
    The rewrite casts the dividend of every ``/`` to ``DOUBLE`` (``CAST(x AS Nullable(Float64))`` on
    ClickHouse, which keeps NULL), so the quotient is ``Float64`` whatever the operand types are.
    Integer quotients are already ``Float64`` in ClickHouse and do not change. A dividend that is
    already ``DOUBLE``/``FLOAT``, a literal with a decimal point or exponent (ClickHouse reads it as
    ``Float64``) or another division is left alone, so applying the rewrite twice changes nothing.

    A zero divisor then yields ``inf`` (as in DuckDB) instead of ClickHouse's decimal division error;
    combine with ``safe_division`` or ``nullif_zero_divisors`` for NULL.

    ClickHouse has no common type for ``Float64`` and ``Decimal`` unless the experimental Variant
    type is enabled, so a ``CASE``/``IF`` that returns a quotient in one branch and a
    ``CAST(0 AS DECIMAL(p, s))`` zero (what ``decimal_zero_literals`` writes) in another gets
    ``CAST(0 AS DOUBLE)`` for that zero (TPC-DS Q11). Other branch types are left alone.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for node in expression.find_all(exp.Div):
        dividend = node.this
        bare = dividend.unnest()
        if isinstance(bare, exp.Div):
            continue
        if isinstance(bare, exp.Literal) and bare.is_number and any(c in bare.name for c in ".eE"):
            continue
        if isinstance(bare, exp.Cast) and bare.to.this in _REAL_TYPES:
            continue
        node.set("this", exp.Cast(this=dividend, to=exp.DataType.build(exp.DType.DOUBLE)))
    for node in list(expression.find_all(exp.Case, exp.If)):
        if not _has_real_quotient_branch(node):
            continue
        for owner, key in _branch_slots(node):
            if _is_zero_decimal_cast(owner.args[key]):
                owner.set(
                    key,
                    exp.Cast(this=exp.Literal.number(0), to=exp.DataType.build(exp.DType.DOUBLE)),
                )
    return expression


def use_nulls_for_rollups(expression: E, copy: bool = True) -> E:
    """Add ``SETTINGS group_by_use_nulls = 1`` to a ClickHouse query that has ``ROLLUP``, ``CUBE`` or ``GROUPING SETS``.

    ClickHouse fills a rolled-up key with the default value of its type (``''`` for a string, ``0`` for
    a number) unless the key column is ``Nullable``, so the grand-total row of
    ``GROUP BY ROLLUP (channel, id)`` over literal branches (TPC-DS Q5, Q14, Q77, Q80) carries ``''``
    and ``0`` where SQL, and every other engine, has NULL. The setting makes ClickHouse write NULL.
    Queries without a rollup, cube or grouping set are returned unchanged.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    if expression.find(exp.Rollup, exp.Cube, exp.GroupingSets) is None:
        return expression
    return add_query_settings(expression, {"group_by_use_nulls": 1}, copy=False)
