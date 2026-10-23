"""Engine-neutral expression rewrites maintained by the BenchBox fork.

Rewrites take and return expressions, so the caller picks the read and write
dialects. Every gate (which column, which table) is an argument.
"""

from __future__ import annotations

import math
import re
import typing as t
from decimal import Context, Decimal, DecimalException, Inexact

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect, DialectType

E = t.TypeVar("E", bound=exp.Expr)

# Fold only short sums: at most this many literals per bound.
_MAX_LEAVES = 4
# Fold only when the exact result and the double-precision result agree to this
# distance. A larger gap means the double result is the meaningful one.
_NOISE = 1e-12
_NUMBER = re.compile(r"\d{1,2}\.\d{1,6}")


def fold_between_literal_arithmetic(
    expression: E,
    columns: t.Iterable[str],
    tables: t.Iterable[str] | None = None,
    copy: bool = True,
) -> E:
    """Replace literal ``+``/``-`` bounds of ``BETWEEN`` with their exact decimal value.

    Engines that evaluate decimal literals as binary floating point turn
    ``col BETWEEN 0.06 - 0.01 AND 0.06 + 0.01`` into
    ``col BETWEEN 0.049999999999999996 AND 0.06999999999999999``, which drops
    rows equal to a bound. Folding the bounds to ``0.05`` and ``0.07`` restores
    the exact decimal result.

    A bound is folded only when it is a sum or difference of at most four
    non-negative decimal literals (one or two integer digits, one to six
    fractional digits), the exact result is non-negative, and the double
    precision result is within ``1e-12`` of it. Both bounds of a ``BETWEEN``
    must qualify, otherwise neither changes. Anything else is left untouched.

    Args:
        expression: the expression to rewrite.
        columns: names (case-insensitive, qualifier ignored) of the columns whose
            ``BETWEEN`` predicates are folded.
        tables: when given, the rewrite applies only if the expression reads at
            least one table with one of these names (case-insensitive). When
            omitted there is no table gate.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    column_names = {name.lower() for name in columns}
    if copy:
        expression = expression.copy()
    if not column_names:
        return expression
    if tables is not None:
        table_names = {name.lower() for name in tables}
        if not any(table.name.lower() in table_names for table in expression.find_all(exp.Table)):
            return expression

    context = Context(prec=60, traps=[Inexact])
    leaves = 0

    def evaluate(node: exp.Expr) -> tuple[Decimal, float] | None:
        nonlocal leaves
        node = node.unnest()
        if isinstance(node, exp.Literal):
            leaves += 1
            if leaves > _MAX_LEAVES or not node.is_number or not _NUMBER.fullmatch(node.name):
                return None
            return Decimal(node.name), float(node.name)
        if isinstance(node, (exp.Add, exp.Sub)):
            left, right = evaluate(node.this), evaluate(node.expression)
            if left is None or right is None:
                return None
            try:
                if isinstance(node, exp.Add):
                    return context.add(left[0], right[0]), left[1] + right[1]
                return context.subtract(left[0], right[0]), left[1] - right[1]
            except DecimalException:
                return None
        return None

    for between in list(expression.find_all(exp.Between)):
        column = between.this
        if not isinstance(column, exp.Column) or column.name.lower() not in column_names:
            continue
        folded: list[Decimal] = []
        for bound in (between.args["low"], between.args["high"]):
            leaves = 0
            value = evaluate(bound) if isinstance(bound.unnest(), (exp.Add, exp.Sub)) else None
            if value is None:
                break
            exact, real = value
            if exact < 0 or not math.isfinite(real) or abs(real - float(exact)) > _NOISE:
                break
            folded.append(exact)
        else:
            between.set("low", exp.Literal.number(format(folded[0], "f")))
            between.set("high", exp.Literal.number(format(folded[1], "f")))
    return expression


def date_integer_arithmetic_to_interval(
    expression: E,
    date_columns: t.Iterable[str],
    copy: bool = True,
) -> E:
    """Rewrite ``date_column +/- <integer>`` into ``date_column +/- INTERVAL '<integer>' DAY``.

    Netezza adds and subtracts a number of days with a plain integer. Other
    engines, such as PostgreSQL when the column is a timestamp or DataFusion
    for any date, need an explicit day interval. Only the pattern
    ``<date column> + <integer literal>`` or ``<date column> - <integer literal>``
    is rewritten; the integer must be the whole right operand, so
    ``d + 5 * 2`` and ``d - n`` are left untouched, as is any expression whose
    left operand is not one of the named columns.

    Which columns hold dates is data the caller owns, for example from a schema.

    Args:
        expression: the expression to rewrite.
        date_columns: names (case-insensitive, qualifier ignored) of the columns
            known to hold dates.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    column_names = {name.lower() for name in date_columns}
    if copy:
        expression = expression.copy()
    if not column_names:
        return expression

    for node in list(expression.find_all(exp.Add, exp.Sub)):
        column, days = node.this, node.expression
        if (
            isinstance(column, exp.Column)
            and column.name.lower() in column_names
            and isinstance(days, exp.Literal)
            and days.is_int
        ):
            node.set(
                "expression", exp.Interval(this=exp.Literal.string(days.name), unit=exp.var("DAY"))
            )
    return expression


def mark_date_columns(
    expression: E,
    date_columns: t.Iterable[str],
    copy: bool = True,
) -> E:
    """Record that the named columns hold dates, so a generator that needs types can use them.

    SQLite keeps dates as text, so ``d_date + 5`` adds 5 to a number read from the text
    and ``DATE_TRUNC('month', d) = date_col`` compares a timestamp text with a date text.
    The SQLite generator writes the intended whole-day offsets and comparisons only for
    operands it knows to be dates; without a schema that knowledge is this column set.
    Source engines that need no type are unaffected, and the marking lives on the tree, so
    pass the returned expression (not its SQL text) to the generator.

    Which columns hold dates is data the caller owns, for example from a schema.

    Args:
        expression: the expression to mark.
        date_columns: names (case-insensitive, qualifier ignored) of the columns known
            to hold dates.
        copy: mark a copy instead of mutating ``expression``.

    Returns:
        The expression with every matching column typed ``DATE``.
    """
    column_names = {name.lower() for name in date_columns}
    if copy:
        expression = expression.copy()
    for column in expression.find_all(exp.Column):
        if column.name.lower() in column_names:
            column.type = exp.DataType.build("date")
    return expression


def _division_form(dialect: DialectType) -> str:
    from sqlglot.dialects.bigquery import BigQuery
    from sqlglot.dialects.clickhouse import ClickHouse
    from sqlglot.dialects.databricks import Databricks
    from sqlglot.dialects.snowflake import Snowflake

    resolved = Dialect.get_or_raise(dialect)
    if isinstance(resolved, (BigQuery, Databricks)):
        return "function"
    if isinstance(resolved, Snowflake):
        return "nullif"
    if isinstance(resolved, ClickHouse):
        return "nullif_skip_literals"
    raise ValueError(
        f"safe_division supports bigquery, snowflake, databricks and clickhouse, not {dialect!r}"
    )


def _is_zero_guard(node: exp.Expr) -> bool:
    node = node.unnest()
    if not isinstance(node, exp.Nullif):
        return False
    zero = node.expression
    return isinstance(zero, exp.Literal) and zero.is_number and zero.name in {"0", "0.0"}


def _is_nonzero_number(node: exp.Expr) -> bool:
    node = node.unnest()
    if isinstance(node, exp.Neg):
        node = node.this.unnest()
    return isinstance(node, exp.Literal) and node.is_number and Decimal(node.name) != 0


def nullif_zero_divisors(expression: E, columns: t.Iterable[str], copy: bool = True) -> E:
    """Make a division by one of the named columns return NULL instead of failing on zero.

    ``a / CAST(pmc AS DECIMAL(15, 4))`` becomes ``a / NULLIF(CAST(pmc AS DECIMAL(15, 4)), 0)`` for every
    division whose divisor is, once parentheses and casts are removed, an unqualified or qualified
    column named in ``columns`` (compared case-insensitively). PostgreSQL, ClickHouse, SQL Server and
    other engines raise on a zero divisor; DuckDB and SQLite return NULL, so the rewrite gives every
    engine the same answer. A divisor that is already ``NULLIF(x, 0)`` or that wraps one is left alone.

    The rewrite changes results (an error becomes NULL), so it is opt-in and takes the columns as an
    argument: nothing else is guarded. Unlike ``safe_division`` it works for every dialect.

    Args:
        expression: the expression to rewrite.
        columns: column names whose use as a divisor is guarded.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    names = {column.lower() for column in columns}
    if copy:
        expression = expression.copy()
    for node in expression.find_all(exp.Div):
        divisor = node.expression
        if _is_zero_guard(divisor):
            continue
        bare = divisor.unnest()
        while isinstance(bare, exp.Cast):
            bare = bare.this.unnest()
        if isinstance(bare, exp.Column) and bare.name.lower() in names:
            node.set("expression", exp.Nullif(this=divisor, expression=exp.Literal.number(0)))
    return expression


def safe_division(expression: E, dialect: DialectType, copy: bool = True) -> E:
    """Make every ``/`` return NULL instead of failing or producing infinity on a zero divisor.

    The form depends on the dialect the expression is written in:

    * ``bigquery`` and ``databricks``: ``a / b`` becomes ``SAFE_DIVIDE(a, b)`` and
      ``TRY_DIVIDE(a, b)`` respectively (both are ``exp.SafeDivide``).
    * ``snowflake``: ``a / b`` becomes ``a / NULLIF(b, 0)``; a divisor that is already
      ``NULLIF(x, 0)`` is left alone.
    * ``clickhouse``: ``a / b`` becomes ``a / NULLIF(b, 0)``, which also covers the
      ``Nullable(Decimal(p, s))`` divisors ClickHouse rejects at zero; a non-zero numeric
      literal divisor and a divisor that is already ``NULLIF(x, 0)`` are left alone.

    The rewrite changes results (a zero divisor yields NULL), so it is opt-in: nothing
    calls it unless the caller asks. Any other dialect raises ``ValueError``.

    Args:
        expression: the expression to rewrite.
        dialect: the dialect the expression is written in and will be generated for.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    form = _division_form(dialect)
    if copy:
        expression = expression.copy()
    for node in reversed(list(expression.find_all(exp.Div, bfs=False))):
        divisor = node.expression
        if form == "function":
            replacement = exp.SafeDivide(this=node.this, expression=divisor)
            if node is expression:
                return replacement  # type: ignore[return-value]
            node.replace(replacement)
        elif not (
            _is_zero_guard(divisor)
            or (form == "nullif_skip_literals" and _is_nonzero_number(divisor))
        ):
            node.set("expression", exp.Nullif(this=divisor, expression=exp.Literal.number(0)))
    return expression


def _deduplicate_select(select: exp.Select) -> None:
    selects = select.expressions
    if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and e.is_star) for e in selects):
        return

    def output_name(node: exp.Expr) -> str:
        if isinstance(node, (exp.Alias, exp.Column)):
            return node.output_name
        return ""

    totals: dict[str, int] = {}
    for node in selects:
        name = output_name(node).lower()
        if name:
            totals[name] = totals.get(name, 0) + 1
    repeated = {name for name, total in totals.items() if total > 1}
    if not repeated:
        return

    ordering: list[exp.Expr] = []
    for key in ("order", "group"):
        clause = select.args.get(key)
        if clause:
            ordering.extend(clause.expressions)
    if any(
        not column.table and column.name.lower() in repeated
        for node in ordering
        for column in node.find_all(exp.Column)
    ):
        return

    seen: dict[str, int] = {}
    for node in list(selects):
        name = output_name(node)
        if name.lower() not in repeated:
            continue
        seen[name.lower()] = seen.get(name.lower(), 0) + 1
        if seen[name.lower()] == 1:
            continue
        new_name = f"{name}_{seen[name.lower()]}"
        if isinstance(node, exp.Alias):
            node.set(
                "alias", exp.to_identifier(new_name, quoted=node.args["alias"].args.get("quoted"))
            )
        else:
            node.replace(exp.alias_(node.copy(), new_name))


def _is_set_operand(select: exp.Select) -> bool:
    parent = select.parent
    while isinstance(parent, exp.Subquery):
        parent = parent.parent
    return isinstance(parent, exp.SetOperation)


def deduplicate_output_aliases(expression: E, nested: bool = True, copy: bool = True) -> E:
    """Suffix repeated output names of ``SELECT`` projections as ``name_2``, ``name_3``...

    Spark and Databricks reject or mis-resolve a query whose projection names two
    columns alike once it feeds a ``CREATE TABLE AS`` or an outer query. In each
    ``SELECT`` the first occurrence keeps its name (case-insensitive); later ones are
    aliased, and a plain column that needs a new name gains an alias. A ``SELECT`` is left
    unchanged when its projection contains ``*``, or when an unqualified column of its
    ``ORDER BY`` or ``GROUP BY`` names a repeated output (renaming could change what it
    resolves to). Operands of ``UNION``, ``INTERSECT`` and ``EXCEPT`` are never renamed,
    since the first operand names the result.

    Args:
        expression: the expression to rewrite.
        nested: also rewrite ``SELECT`` statements in subqueries, CTEs and DML sources;
            when false only ``expression`` itself, if it is a ``SELECT``, is rewritten.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    selects = expression.find_all(exp.Select) if nested else [expression]
    for select in list(selects):
        if isinstance(select, exp.Select) and not _is_set_operand(select):
            _deduplicate_select(select)
    return expression


def _is_cte_reference(table: exp.Table) -> bool:
    name = table.name.lower()
    node: exp.Expr = table
    while node.parent is not None:
        parent = node.parent
        if isinstance(parent, exp.With):
            for cte in parent.expressions:
                if cte is node:
                    if parent.args.get("recursive") and cte.alias_or_name.lower() == name:
                        return True
                    break
                if cte.alias_or_name.lower() == name:
                    return True
        elif isinstance(parent, exp.Query) and node is not parent.args.get("with_"):
            with_ = parent.args.get("with_")
            if with_ and any(cte.alias_or_name.lower() == name for cte in with_.expressions):
                return True
        node = parent
    return False


def unqualified_tables(expression: exp.Expr) -> list[exp.Table]:
    """Return the ``Table`` nodes that name a table without a schema or catalog.

    Names that resolve to a CTE visible at the reference (an earlier sibling of the same
    ``WITH``, or any CTE of an enclosing query's ``WITH``) are skipped, as are table
    functions. The nodes belong to ``expression`` and are in depth-first order, so a caller
    holding a dataset or project can qualify them in place with ``table.set("db", ...)``.

    Args:
        expression: the expression to scan.

    Returns:
        Every unqualified, non-CTE table node, duplicates included.
    """
    return [
        table
        for table in expression.find_all(exp.Table, bfs=False)
        if isinstance(table.this, exp.Identifier)
        and not table.db
        and not table.catalog
        and not _is_cte_reference(table)
    ]


def flag_comparisons_to_boolean(
    expression: E,
    columns: t.Iterable[str],
    use_is: bool = False,
    copy: bool = True,
) -> E:
    """Rewrite ``flag = 1`` and ``flag = 0`` into boolean comparisons for the named columns.

    Schemas that store flags as ``BOOLEAN`` can arrive with query text that
    compares them with ``1`` and ``0``, which engines with a strict boolean type reject.
    ``flag = 1`` becomes ``flag = TRUE`` (or ``flag IS TRUE`` with ``use_is``) and
    ``flag = 0`` becomes ``flag = FALSE`` (``flag IS FALSE``). The literal may be on
    either side; only the integers 0 and 1 are rewritten.

    Args:
        expression: the expression to rewrite.
        columns: names (case-insensitive, qualifier ignored) of the flag columns.
        use_is: emit ``IS TRUE`` / ``IS FALSE`` instead of ``= TRUE`` / ``= FALSE``.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    column_names = {name.lower() for name in columns}
    if copy:
        expression = expression.copy()
    if not column_names:
        return expression

    for node in list(expression.find_all(exp.EQ)):
        column, literal = node.this, node.expression
        if isinstance(literal, exp.Column):
            column, literal = literal, column
        if (
            isinstance(column, exp.Column)
            and column.name.lower() in column_names
            and isinstance(literal, exp.Literal)
            and literal.is_int
            and literal.name in {"0", "1"}
        ):
            value = exp.Boolean(this=literal.name == "1")
            column = column.copy()
            node.replace(
                exp.Is(this=column, expression=value)
                if use_is
                else exp.EQ(this=column, expression=value)
            )
    return expression


def round_inputs_to_decimal(expression: E, copy: bool = True) -> E:
    """Cast the input of every ``ROUND`` to an unconstrained ``DECIMAL``.

    ``ROUND`` on a double rounds half away from zero in DuckDB (``ROUND(2.5)`` is 3) but
    half to even on PostgreSQL (2), and PostgreSQL has no ``ROUND(double precision, integer)``
    at all. The PostgreSQL generator casts the input of a two-argument ``ROUND`` only when the
    type annotator knows it is a double, so ``ROUND(delay, 2)`` over an unknown ``DOUBLE`` column
    is an error there and no ``ROUND`` with one argument is cast. PostgreSQL rounds ``numeric`` half away from zero and accepts a scale, so ``ROUND(CAST(x AS DECIMAL), n)``
    gives the DuckDB answer for every value that is a decimal tie.

    Use it for engines where a bare ``DECIMAL`` is arbitrary precision, such as the PostgreSQL
    family. Do not use it for engines whose bare ``DECIMAL`` has a default scale: DuckDB's is
    ``DECIMAL(18,3)`` and would truncate the value. A double is converted to ``numeric`` through
    its shortest decimal form, so a value such as ``0.285`` rounds to ``0.29`` where a DuckDB
    double, whose binary value is just below the tie, gives ``0.28``.

    An input that is already a cast to a decimal type is left alone, so the rewrite is idempotent.

    Args:
        expression: the expression to rewrite.
        copy: rewrite a copy instead of mutating ``expression``.

    Returns:
        The rewritten expression.
    """
    if copy:
        expression = expression.copy()
    for node in list(expression.find_all(exp.Round)):
        operand = node.this
        if isinstance(operand, exp.Cast) and operand.to.is_type(exp.DType.DECIMAL):
            continue
        node.set("this", exp.cast(operand, exp.DType.DECIMAL, copy=False))
    return expression
