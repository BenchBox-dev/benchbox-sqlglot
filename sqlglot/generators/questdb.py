from __future__ import annotations

import re

from sqlglot import exp, transforms
from sqlglot.benchbox.date_literals import DateLiteralPadding
from sqlglot.generators.postgres import PostgresGenerator

# QuestDB's DATEADD period codes for the interval units that are generated as DATEADD.
_DATEADD_PERIODS = {"DAY": "d", "MONTH": "M", "YEAR": "y"}
_WHOLE_NUMBER = re.compile(r"\d+")


def _table_ref(node: exp.Expr) -> str:
    return node.alias_or_name.lower()


def _is_comma_join(join: exp.Join) -> bool:
    """A bare ``, table`` join: no kind, side, ON or USING, and a table or derived table."""
    return isinstance(join.this, (exp.Table, exp.Subquery)) and not any(
        value for key, value in join.args.items() if key != "this"
    )


def _flatten_and(node: exp.Expr) -> list[exp.Expr]:
    if isinstance(node, exp.And):
        return _flatten_and(node.left) + _flatten_and(node.right)
    return [node]


def _is_join_predicate(predicate: exp.Expr, in_scope: set[str], current: str) -> bool:
    """Whether ``predicate`` equates two columns and can be the ON clause of join ``current``.

    That is an equality that names ``current`` and a table already joined, or one with a
    column that has no qualifier next to a joined table, or only unqualified columns.
    """
    if not isinstance(predicate, exp.EQ):
        return False
    columns = list(predicate.find_all(exp.Column))
    if len(columns) != 2:
        return False

    refs = {column.table.lower() for column in columns if column.table}
    if not refs:
        return True
    if current in refs and refs & in_scope:
        return True
    return bool(refs & in_scope) and any(not column.table for column in columns)


def _expand_comma_joins(expression: exp.Expr) -> exp.Expr:
    """Rewrite ``FROM a, b WHERE a.x = b.x`` as ``FROM a JOIN b ON a.x = b.x``.

    An equality between columns of the joined table and an earlier table becomes the ON
    clause and leaves the WHERE; a comma join without one becomes a CROSS JOIN and every
    predicate stays in the WHERE.
    """
    if not isinstance(expression, exp.Select):
        return expression
    joins = expression.args.get("joins") or []
    if not any(_is_comma_join(join) for join in joins):
        return expression

    where = expression.args.get("where")
    remaining = _flatten_and(where.this) if where else []
    from_ = expression.args.get("from_")
    in_scope = {_table_ref(from_.this)} if from_ else set()

    for join in joins:
        current = _table_ref(join.this)
        if _is_comma_join(join):
            attached = next(
                (
                    predicate
                    for predicate in remaining
                    if _is_join_predicate(predicate, in_scope, current)
                ),
                None,
            )
            if attached is None:
                join.set("kind", "CROSS")
            else:
                remaining.remove(attached)
                join.set("on", attached.copy())
        in_scope.add(current)

    if where:
        if remaining:
            where.set("this", exp.and_(*[predicate.copy() for predicate in remaining]))
        else:
            expression.set("where", None)
    return expression


def _column_of(node: exp.Expr, qualifiers: set[str]) -> exp.Column | None:
    if isinstance(node, exp.Column) and node.table.lower() in qualifiers:
        return node
    return None


def _decorrelate_subqueries(expression: exp.Expr) -> exp.Expr:
    """Lower ``[NOT] EXISTS`` and ``IN (subquery)`` conjuncts of a WHERE into joins QuestDB can run.

    ``EXISTS`` with equality correlations becomes an inner join to the distinct inner keys,
    ``NOT EXISTS`` a left join to them with an ``IS NULL`` test, and ``EXISTS`` with one extra
    ``outer <> inner`` correlation a join to the per-key ``MIN``/``MAX`` of the inner column, where a
    differing row exists exactly when the outer value differs from either extreme. Any other shape
    is left as written. An ``IN`` over an uncorrelated one-column subquery becomes an inner join to its
    distinct non-NULL values. QuestDB joins NULL to NULL and compares NULL as a value, so the inner keys are
    filtered to non-NULL and the outer ``<>`` value and the inner extremes are tested for NULL.
    """
    if not isinstance(expression, exp.Select):
        return expression
    where = expression.args.get("where")
    from_ = expression.args.get("from_")
    if not where or not from_ or not isinstance(from_.this, exp.Table):
        return expression
    outer = {_table_ref(from_.this)}
    for join in expression.args.get("joins") or []:
        if not isinstance(join.this, (exp.Table, exp.Subquery)):
            return expression
        outer.add(_table_ref(join.this))

    conjuncts = _flatten_and(where.this)
    occupied = {identifier.name.lower() for identifier in expression.find_all(exp.Identifier)}
    new_joins: list[exp.Join] = []
    rewritten: list[exp.Expr] = []
    for conjunct in conjuncts:
        negated = isinstance(conjunct, exp.Not) and isinstance(conjunct.this, exp.Exists)
        exists = conjunct.this if negated else conjunct
        number = len(new_joins) + 1
        while any(
            name.startswith((f"_qx_exists_{number}", f"_qx_in_{number}")) for name in occupied
        ):
            number += 1
        if isinstance(exists, exp.Exists):
            lowered = _lower_exists(exists, negated, outer, f"_qx_exists_{number}")
        elif isinstance(conjunct, exp.In):
            lowered = _lower_in_subquery(conjunct, outer, f"_qx_in_{number}")
        else:
            lowered = None
        if lowered is None:
            rewritten.append(conjunct)
            continue
        join, predicate = lowered
        new_joins.append(join)
        occupied.add(join.this.alias.lower())
        if predicate is not None:
            rewritten.append(predicate)

    if not new_joins:
        return expression
    expression.set("joins", [*(expression.args.get("joins") or []), *new_joins])
    if rewritten:
        where.set("this", exp.and_(*rewritten))
    else:
        expression.set("where", None)
    return expression


def _lower_in_subquery(
    predicate: exp.In, outer: set[str], alias: str
) -> tuple[exp.Join, exp.Expr | None] | None:
    query = predicate.args.get("query")
    value = predicate.this
    if not isinstance(value, exp.Column) or predicate.args.get("expressions") or not query:
        return None
    if predicate.args.get("unnest") or predicate.args.get("field"):
        return None
    while isinstance(query, exp.Subquery) and not query.args.get("alias"):
        query = query.this
    if not isinstance(query, exp.Select) or len(query.expressions) != 1:
        return None
    if isinstance(query.expressions[0].unalias(), exp.Star) or any(
        query.args.get(key) for key in ("limit", "offset", "with_")
    ):
        return None
    own = {_table_ref(table) for table in query.find_all(exp.Table)}
    own |= {subquery.alias.lower() for subquery in query.find_all(exp.Subquery) if subquery.alias}
    if any(
        column.table and column.table.lower() not in own for column in query.find_all(exp.Column)
    ):
        return None

    source = query.copy()
    source.set("expressions", [exp.alias_(source.expressions[0].unalias(), f"{alias}_k0")])
    source.set("order", None)
    key = exp.column(f"{alias}_k0")
    derived = (
        exp.select(key.copy())
        .distinct()
        .from_(
            exp.Subquery(this=source, alias=exp.TableAlias(this=exp.to_identifier(f"{alias}_s")))
        )
        .where(exp.Not(this=exp.Is(this=key.copy(), expression=exp.Null())), copy=False)
    )
    join = exp.Join(
        this=exp.Subquery(this=derived, alias=exp.TableAlias(this=exp.to_identifier(alias))),
        on=exp.EQ(this=value.copy(), expression=exp.column(f"{alias}_k0", table=alias)),
    )
    return join, None


def _lower_exists(
    exists: exp.Exists, negated: bool, outer: set[str], alias: str
) -> tuple[exp.Join, exp.Expr | None] | None:
    inner = exists.this
    while isinstance(inner, exp.Subquery) and not inner.args.get("alias"):
        inner = inner.this
    if not isinstance(inner, exp.Select):
        return None
    if any(
        inner.args.get(key)
        for key in ("joins", "group", "having", "qualify", "order", "limit", "offset", "with_")
    ):
        return None
    if inner.args.get("distinct") or inner.find(exp.AggFunc) or inner.find(exp.Window):
        return None
    source = inner.args.get("from_")
    if not source or not isinstance(source.this, exp.Table):
        return None
    inner_ref = _table_ref(source.this)
    if inner_ref in outer or not inner.args.get("where"):
        return None

    inner_only = {inner_ref}
    equalities: list[tuple[exp.Column, exp.Column]] = []
    inequalities: list[tuple[exp.Column, exp.Column]] = []
    local: list[exp.Expr] = []
    for predicate in _flatten_and(inner.args["where"].this):
        columns = list(predicate.find_all(exp.Column))
        if not any(column.table.lower() in outer for column in columns):
            if predicate.find(exp.Subquery) or any(
                column.table and column.table.lower() not in inner_only for column in columns
            ):
                return None
            local.append(predicate)
            continue
        if not isinstance(predicate, (exp.EQ, exp.NEQ)) or len(columns) != 2:
            return None
        pair = (
            (_column_of(predicate.left, outer), _column_of(predicate.right, inner_only)),
            (_column_of(predicate.right, outer), _column_of(predicate.left, inner_only)),
        )
        match = next(((o, i) for o, i in pair if o is not None and i is not None), None)
        if match is None:
            return None
        (equalities if isinstance(predicate, exp.EQ) else inequalities).append(match)

    if not equalities or len(inequalities) > 1 or (negated and inequalities):
        return None

    keys = [exp.alias_(i.copy(), f"{alias}_k{n}") for n, (_, i) in enumerate(equalities)]
    projections: list[exp.Expr] = list(keys)
    if inequalities:
        differing = inequalities[0][1]
        projections.append(exp.alias_(exp.Min(this=differing.copy()), f"{alias}_lo"))
        projections.append(exp.alias_(exp.Max(this=differing.copy()), f"{alias}_hi"))
    derived = exp.select(*projections).from_(source.this.copy())
    present = [exp.Not(this=exp.Is(this=i.copy(), expression=exp.Null())) for _, i in equalities]
    derived = derived.where(exp.and_(*present, *[p.copy() for p in local]), copy=False)
    if inequalities:
        derived.set("group", exp.Group(expressions=[i.copy() for _, i in equalities]))
    else:
        derived.set("distinct", exp.Distinct())

    condition = exp.and_(
        *[
            exp.EQ(this=o.copy(), expression=exp.column(f"{alias}_k{n}", table=alias))
            for n, (o, _) in enumerate(equalities)
        ]
    )
    join = exp.Join(
        this=exp.Subquery(this=derived, alias=exp.TableAlias(this=exp.to_identifier(alias))),
        on=condition,
        side="LEFT" if negated else None,
    )
    if negated:
        return join, exp.Is(this=exp.column(f"{alias}_k0", table=alias), expression=exp.Null())
    if inequalities:
        outer_value = inequalities[0][0]
        low = exp.column(f"{alias}_lo", table=alias)
        high = exp.column(f"{alias}_hi", table=alias)
        return join, exp.and_(
            exp.Not(this=exp.Is(this=outer_value.copy(), expression=exp.Null())),
            exp.Not(this=exp.Is(this=low.copy(), expression=exp.Null())),
            exp.Paren(
                this=exp.or_(
                    exp.NEQ(this=outer_value.copy(), expression=low),
                    exp.NEQ(this=outer_value.copy(), expression=high),
                )
            ),
        )
    return join, None


def _name_projections(query: exp.Expr | None, columns: list[exp.Identifier]) -> bool:
    """Alias the projections of ``query`` with ``columns``; False if its shape does not allow it."""
    while isinstance(query, (exp.Subquery, exp.SetOperation)):
        # The first branch of a set operation names its columns.
        query = query.this

    if not isinstance(query, exp.Select) or len(query.expressions) != len(columns):
        return False
    if any(isinstance(projection.unalias(), exp.Star) for projection in query.expressions):
        return False

    query.set(
        "expressions",
        [
            exp.alias_(projection.unalias(), exp.to_identifier(column.name, quoted=column.quoted))
            for projection, column in zip(query.expressions, columns)
        ],
    )
    return True


def _substring_sql(self: QuestDBGenerator, expression: exp.Substring) -> str:
    # QuestDB's SUBSTRING needs all three arguments, so a missing length is the string's own.
    this = expression.this
    length = expression.args.get("length") or exp.Length(this=this.copy())
    return self.func("SUBSTRING", this, expression.args.get("start"), length)


class QuestDBGenerator(DateLiteralPadding, PostgresGenerator):
    EXACT_DIVISION_TYPE = exp.DType.DOUBLE
    NULL_ORDERING_SUPPORTED: bool | None = None
    SUPPORTS_NULL_SORT_KEY_IN_WINDOW = False

    TRANSFORMS = {
        **PostgresGenerator.TRANSFORMS,
        exp.Select: transforms.preprocess(
            [
                transforms.eliminate_semi_and_anti_joins,
                transforms.eliminate_qualify,
                _expand_comma_joins,
                _decorrelate_subqueries,
            ]
        ),
        exp.Substring: _substring_sql,
    }

    def _dateadd_sql(self, expression: exp.Add | exp.Sub, sign: int) -> str | None:
        interval = expression.expression
        if not isinstance(interval, exp.Interval):
            return None

        unit = interval.text("unit").upper()
        period = _DATEADD_PERIODS.get(unit[:-1] if unit.endswith("S") else unit)
        amount = interval.this
        if (
            period is None
            or not isinstance(amount, exp.Literal)
            or not _WHOLE_NUMBER.fullmatch(amount.name)
        ):
            return None

        return self.func(
            "DATEADD",
            exp.Literal.string(period),
            exp.Literal.number(sign * int(amount.name)),
            expression.this,
        )

    def add_sql(self, expression: exp.Add) -> str:
        return self._dateadd_sql(expression, 1) or super().add_sql(expression)

    def sub_sql(self, expression: exp.Sub) -> str:
        return self._dateadd_sql(expression, -1) or super().sub_sql(expression)

    def cte_sql(self, expression: exp.CTE) -> str:
        # QuestDB has no CTE column list, so the names move into the query's projections.
        alias = expression.args.get("alias")
        columns = alias.args.get("columns") if alias else None
        if alias and columns:
            if _name_projections(expression.this, columns):
                alias.set("columns", None)
            else:
                self.unsupported("QuestDB cannot name the columns of this CTE query")
        return super().cte_sql(expression)
