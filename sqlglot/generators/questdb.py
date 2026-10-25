from __future__ import annotations

import re

from sqlglot import exp, transforms
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


class QuestDBGenerator(PostgresGenerator):
    EXACT_DIVISION_TYPE = exp.DType.DOUBLE
    NULL_ORDERING_SUPPORTED = None
    SUPPORTS_NULL_SORT_KEY_IN_WINDOW = False

    TRANSFORMS = {
        **PostgresGenerator.TRANSFORMS,
        exp.Select: transforms.preprocess(
            [
                transforms.eliminate_semi_and_anti_joins,
                transforms.eliminate_qualify,
                _expand_comma_joins,
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
