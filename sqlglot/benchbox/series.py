"""Row generators for engines that cannot write ``SELECT unnest(generate_series(lo, hi)) AS n``.

DuckDB and PostgreSQL write ``SELECT unnest(generate_series(1, 100)) AS n`` to produce the integers
1 to 100. The generators of the other engines either keep the call (``EXPLODE(GENERATE_SERIES(...))``
on SQLite, Doris, StarRocks and SingleStore, none of which has it) or write a form that is valid
but does not return the same rows. Each engine has its own way to count, so ``lower_series_selects``
writes the form the engine runs, in the target dialect's syntax. Every name and bound is an argument
or comes from the query; nothing here names a table, a column or a benchmark.
"""

from __future__ import annotations

import typing as t

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect, DialectType

E = t.TypeVar("E", bound=exp.Expr)

_LOW = "__series_low__"
_HIGH = "__series_high__"
_ALIAS = "__series_alias__"
_DIGIT_ROWS = " UNION ALL ".join(f"SELECT {digit} AS d" for digit in range(10))

# One select per dialect that returns the integers LOW to HIGH (inclusive) in a column named ALIAS.
# SQLite has a recursive CTE, Snowflake, BigQuery, QuestDB and StarRocks each have a table function,
# Doris has numbers() and SingleStore has neither a series nor a table function, so it cross joins
# the digits 0 to 9 four times (0 to 9999).
_TEMPLATES = {
    "sqlite": (
        f"WITH RECURSIVE _series({_ALIAS}) AS (SELECT {_LOW} UNION ALL "
        f"SELECT {_ALIAS} + 1 FROM _series WHERE {_ALIAS} < {_HIGH}) "
        f"SELECT {_ALIAS} FROM _series"
    ),
    "snowflake": (
        f"SELECT value::INT AS {_ALIAS} FROM TABLE(FLATTEN(ARRAY_GENERATE_RANGE({_LOW}, {_HIGH} + 1)))"
    ),
    "bigquery": f"SELECT {_ALIAS} FROM UNNEST(GENERATE_ARRAY({_LOW}, {_HIGH})) AS {_ALIAS}",
    "questdb": f"SELECT generate_series AS {_ALIAS} FROM generate_series({_LOW}, {_HIGH})",
    "starrocks": (
        f"SELECT {_ALIAS} FROM TABLE(generate_series({_LOW}, {_HIGH})) AS series({_ALIAS})"
    ),
    "singlestore": (
        f"SELECT n AS {_ALIAS} FROM (SELECT a.d + 10 * b.d + 100 * c.d + 1000 * e.d AS n FROM "
        f"({_DIGIT_ROWS}) a, ({_DIGIT_ROWS}) b, ({_DIGIT_ROWS}) c, ({_DIGIT_ROWS}) e) _digits "
        f"WHERE n BETWEEN {_LOW} AND {_HIGH}"
    ),
}
# Doris counts from numbers("number" = N), which takes a literal N: it needs integer bounds.
_DORIS_TEMPLATE = f'SELECT number + {_LOW} AS {_ALIAS} FROM numbers("number" = {_HIGH})'

SERIES_DIALECTS = frozenset({*_TEMPLATES, "doris", "postgres"})


def _series_of(node: exp.Expr) -> exp.GenerateSeries | None:
    """The ``GENERATE_SERIES(lo, hi)`` inside ``UNNEST`` or ``EXPLODE``, or ``None``.

    A series with a step or an exclusive end is another shape and is not matched.
    """
    if isinstance(node, exp.Explode):
        series = node.this
    elif isinstance(node, exp.Unnest) and len(node.expressions) == 1:
        series = node.expressions[0]
    else:
        return None

    if (
        isinstance(series, exp.GenerateSeries)
        and series.args.get("start") is not None
        and series.args.get("end") is not None
        and not series.args.get("step")
        and not series.args.get("is_end_exclusive")
    ):
        return series
    return None


def _series_select(node: exp.Expr) -> tuple[exp.GenerateSeries, exp.Identifier] | None:
    """The series and alias of ``SELECT unnest(generate_series(lo, hi)) AS alias``, or ``None``.

    The select must have nothing else: one aliased projection and no FROM, WHERE or other clause.
    """
    if not isinstance(node, exp.Select) or len(node.expressions) != 1:
        return None
    if any(value for key, value in node.args.items() if key != "expressions"):
        return None

    projection = node.expressions[0]
    if not isinstance(projection, exp.Alias) or not isinstance(
        projection.args.get("alias"), exp.Identifier
    ):
        return None

    series = _series_of(projection.this)
    return (series, projection.args["alias"]) if series else None


def _integer(node: exp.Expr) -> int:
    value = node.this if isinstance(node, exp.Paren) else node
    if isinstance(value, exp.Neg):
        return -_integer(value.this)
    if isinstance(value, exp.Literal) and not value.is_string and value.name.lstrip("-").isdigit():
        return int(value.name)
    raise ValueError(f"Doris numbers() needs integer literal bounds, got {node.sql()}")


_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv)


def _operand(node: exp.Expr) -> exp.Expr:
    # a bound is written next to ``+ 1``, ``<`` and ``BETWEEN``: keep a comparison or boolean whole
    if isinstance(node, exp.Binary) and not isinstance(node, _ARITHMETIC):
        return exp.paren(node.copy())
    return node.copy()


def _series_query(dialect: str, series: exp.GenerateSeries, alias: exp.Identifier) -> exp.Expr:
    low, high = series.args["start"], series.args["end"]
    if dialect == "doris":
        first, last = _integer(low), _integer(high)
        template, low, high = (
            _DORIS_TEMPLATE,
            exp.Literal.number(first),
            exp.Literal.number(last - first + 1),
        )
    else:
        template = _TEMPLATES[dialect]

    query = sqlglot.parse_one(template, read=dialect)
    for identifier in list(query.find_all(exp.Identifier)):
        if identifier.name == _ALIAS:
            identifier.replace(alias.copy())
    for column in list(query.find_all(exp.Column)):
        if column.name == _LOW:
            column.replace(_operand(low))
        elif column.name == _HIGH:
            column.replace(_operand(high))
    return query


def lower_series_selects(expression: E, dialect: DialectType, copy: bool = True) -> E:
    """Write ``SELECT unnest(generate_series(lo, hi)) AS n`` in the form ``dialect`` runs.

    The select is replaced, wherever it sits (a derived table, a CTE, a set operation), by one that
    returns the integers ``lo`` to ``hi`` inclusive in the same column:

    * ``sqlite``: ``WITH RECURSIVE _series(n) AS (SELECT lo UNION ALL SELECT n + 1 FROM _series
      WHERE n < hi) SELECT n FROM _series``.
    * ``doris``: ``SELECT number + lo AS n FROM numbers("number" = hi - lo + 1)``; Doris reads a
      literal there, so integer literal bounds are required (``ValueError`` otherwise).
    * ``singlestore``: the digits 0 to 9 cross joined four times and filtered to ``n BETWEEN lo AND
      hi``, so ``hi`` can be at most 9999.
    * ``snowflake``: ``FLATTEN(ARRAY_GENERATE_RANGE(lo, hi + 1))``; ``bigquery``:
      ``UNNEST(GENERATE_ARRAY(lo, hi))``; ``questdb`` and ``starrocks``: ``generate_series(lo, hi)``
      as a table.
    * ``postgres``: the select list keeps the select and drops the ``UNNEST``, so ``SELECT
      generate_series(lo, hi) AS n``. Every ``UNNEST(GENERATE_SERIES(...))`` in a select list is
      written that way, not only a select of that one column.

    A series with a step, an exclusive end, no alias or other columns beside it is not the shape
    and is left unchanged. Any other dialect raises ``ValueError``.

    Args:
        expression: the expression to rewrite.
        dialect: the dialect the expression will be generated for.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    resolved = type(Dialect.get_or_raise(dialect))
    name = next(
        (name for name in sorted(SERIES_DIALECTS) if type(Dialect.get_or_raise(name)) is resolved),
        None,
    )
    if name is None:
        raise ValueError(
            f"No series rewrite for dialect {dialect!r}; supported: {sorted(SERIES_DIALECTS)}"
        )

    if copy:
        expression = expression.copy()

    if name == "postgres":
        for unnest in list(expression.find_all(exp.Explode, exp.Unnest)):
            series = _series_of(unnest)
            if series is not None and isinstance(unnest.parent, (exp.Select, exp.Alias)):
                unnest.replace(series.copy())
        return expression

    for select in list(expression.find_all(exp.Select)):
        match = _series_select(select)
        if match is None:
            continue
        query = _series_query(name, *match)
        if select is expression:
            return query  # type: ignore[return-value]
        select.replace(query)
    return expression
