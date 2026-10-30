from __future__ import annotations

import datetime
import re
import typing as t

from sqlglot import exp, generator, transforms
from sqlglot.dialects.dialect import (
    any_value_to_max_sql,
    arrow_json_extract_sql,
    count_if_to_sum,
    groupconcat_sql,
    no_ilike_sql,
    no_tablesample_sql,
    no_trycast_sql,
    rename_func,
    strposition_sql,
)
from sqlglot.benchbox.grouping_sets import expand_grouping_sets
from sqlglot.benchbox.moments import SUPPORTED as MOMENT_AGGREGATES, expand_moments, is_expandable
from sqlglot.benchbox.pivot import expand_order_by_all, lower_pivots, reject_pivot_sql
from sqlglot.generator import unsupported_args
from sqlglot.helper import is_int
from sqlglot.tokens import TokenType

EXTRACT_FORMATS = {
    "YEAR": "%Y",
    "MONTH": "%m",
    "DAY": "%d",
    "HOUR": "%H",
    "MINUTE": "%M",
    "SECOND": "%S",
}
DATE_PARTS = {"YEAR", "MONTH", "DAY"}
TRUNC_FORMATS = {
    "YEAR": "%Y-01-01 00:00:00",
    "MONTH": "%Y-%m-01 00:00:00",
    "DAY": "%Y-%m-%d 00:00:00",
    "HOUR": "%Y-%m-%d %H:00:00",
    "MINUTE": "%Y-%m-%d %H:%M:00",
    "SECOND": "%Y-%m-%d %H:%M:%S",
}


_DATE_TEXT = re.compile(r"(\d{1,4})-(\d{1,2})-(\d{1,2})")
_TIMESTAMP_TEXT = re.compile(
    r"(\d{1,4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2})(\.\d+)?)?"
)


def _canonical_date(text: str) -> str | None:
    """``text`` as ``YYYY-MM-DD`` when it is a valid date written with unpadded fields, else ``None``."""
    match = _DATE_TEXT.fullmatch(text.strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _canonical_timestamp(text: str) -> str | None:
    """``text`` as ``YYYY-MM-DD[ HH:MM:SS[.f]]`` when it is a valid date or timestamp, else ``None``."""
    date = _canonical_date(text)
    if date is not None:
        return date
    match = _TIMESTAMP_TEXT.fullmatch(text.strip())
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups()[:5])
    second = int(match.group(6) or 0)
    try:
        datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    fraction = match.group(7) or ""
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}{fraction}"


def _moments_sql(self: SQLiteGenerator, expression: exp.Expr) -> str:
    if is_expandable(expression):
        return self.sql(expand_moments(expression))
    self.unsupported(
        f"SQLite has no {type(expression).__name__.upper()}; only a plain aggregate is expanded"
    )
    return self.function_fallback_sql(t.cast(exp.Func, expression))


def _transform_create(expression: exp.Expr) -> exp.Expr:
    """Move primary key to a column and place auto_increment after it."""
    schema = expression.this

    if isinstance(expression, exp.Create) and isinstance(schema, exp.Schema):
        defs = {}
        primary_key = None

        for e in schema.expressions:
            if isinstance(e, exp.ColumnDef):
                defs[e.name] = e
            elif isinstance(e, exp.PrimaryKey):
                primary_key = e

        if primary_key and len(primary_key.expressions) == 1:
            column = defs[primary_key.expressions[0].name]
            column.append(
                "constraints", exp.ColumnConstraint(kind=exp.PrimaryKeyColumnConstraint())
            )
            schema.expressions.remove(primary_key)

        for column in defs.values():
            primary_key_index = -1
            auto_increment = None
            auto_increment_index = -1

            for i, constraint in enumerate(column.constraints):
                if isinstance(constraint.kind, exp.PrimaryKeyColumnConstraint):
                    primary_key_index = i
                elif isinstance(constraint.kind, exp.AutoIncrementColumnConstraint):
                    auto_increment = constraint
                    auto_increment_index = i

            if auto_increment is not None and 0 <= auto_increment_index < primary_key_index:
                column.constraints.remove(auto_increment)
                column.constraints.insert(primary_key_index, auto_increment)

    return expression


def _generated_to_auto_increment(expression: exp.Expr) -> exp.Expr:
    if not isinstance(expression, exp.ColumnDef):
        return expression

    generated = expression.find(exp.GeneratedAsIdentityColumnConstraint)

    # Only rewrite true identity columns. Expression-bearing forms are computed
    # columns (GENERATED ALWAYS AS (expr)) and must keep their expression.
    if generated and generated.expression is None:
        t.cast(exp.ColumnConstraint, generated.parent).pop()

        not_null = expression.find(exp.NotNullColumnConstraint)
        if not_null:
            t.cast(exp.ColumnConstraint, not_null.parent).pop()

        expression.append(
            "constraints", exp.ColumnConstraint(kind=exp.AutoIncrementColumnConstraint())
        )

    return expression


def _global_having_to_subquery(expression: exp.Expr) -> exp.Expr:
    if not isinstance(expression, exp.Select):
        return expression

    group = expression.args.get("group")
    having = expression.args.get("having")
    if (
        group is None
        or having is None
        or len(group.expressions) != 1
        or not isinstance(group.expressions[0], exp.Tuple)
        or group.expressions[0].expressions
        or any(group.args.get(key) for key in ("all", "grouping_sets", "cube", "rollup", "totals"))
        or any(
            expression.args.get(key)
            for key in ("order", "limit", "offset", "distinct", "qualify", "windows", "into")
        )
        or any(isinstance(projection, exp.Star) for projection in expression.expressions)
        or any(projection.find(exp.Window) for projection in expression.expressions)
    ):
        return expression

    names: list[str] = []
    inner = expression.copy()
    inner.set("group", None)
    inner.set("having", None)
    projections: list[exp.Expr] = []
    for position, projection in enumerate(inner.expressions):
        if isinstance(projection, exp.Alias) and projection.alias:
            name = projection.alias
        else:
            name = f"_col_{position}"
            projection = exp.alias_(projection, name)
        names.append(name)
        projections.append(projection)
    flag = "_having"
    while flag in names:
        flag += "_"
    projections.append(exp.alias_(having.this.copy(), flag))
    inner.set("expressions", projections)
    return (
        exp.select(*(exp.column(name) for name in names))
        .from_(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier("_global"))))
        .where(exp.column(flag))
    )


class SQLiteGenerator(generator.Generator):
    SUPPORTS_EMPTY_GROUP_BY = False
    # SQLite's indexed-column grammar (CREATE INDEX) has no NULLS FIRST/LAST
    # clause, so index key definitions drop the null ordering ORDER BY keeps.
    INDEX_NULL_ORDERING_SUPPORTED = False
    SELECT_KINDS: tuple[str, ...] = ()
    TRY_SUPPORTED = False
    SUPPORTS_UESCAPE = False
    SUPPORTS_DECODE_CASE = False
    SET_OP_PARENTHESIZED_OPERANDS = False

    AFTER_HAVING_MODIFIER_TRANSFORMS = generator.AFTER_HAVING_MODIFIER_TRANSFORMS

    JOIN_HINTS = False
    TABLE_HINTS = False
    QUERY_HINTS = False
    NVL2_SUPPORTED = False
    JSON_PATH_BRACKETED_KEY_SUPPORTED = False
    SUPPORTS_CREATE_TABLE_LIKE = False
    SUPPORTS_TABLE_ALIAS_COLUMNS = False
    SUPPORTS_TO_NUMBER = False
    SUPPORTS_WINDOW_EXCLUDE = True
    EXCEPT_INTERSECT_SUPPORT_ALL_CLAUSE = False
    SUPPORTS_MEDIAN = False
    JSON_KEY_VALUE_PAIR_SEP = ","
    PARSE_JSON_NAME: str | None = None

    SUPPORTED_JSON_PATH_PARTS = {
        exp.JSONPathKey,
        exp.JSONPathRoot,
        exp.JSONPathSubscript,
    }

    TYPE_MAPPING = {
        **{k: v for k, v in generator.Generator.TYPE_MAPPING.items() if k != exp.DType.BLOB},
        exp.DType.BOOLEAN: "INTEGER",
        exp.DType.TINYINT: "INTEGER",
        exp.DType.SMALLINT: "INTEGER",
        exp.DType.INT: "INTEGER",
        exp.DType.BIGINT: "INTEGER",
        exp.DType.FLOAT: "REAL",
        exp.DType.DOUBLE: "REAL",
        exp.DType.DECIMAL: "REAL",
        exp.DType.CHAR: "TEXT",
        exp.DType.NCHAR: "TEXT",
        exp.DType.VARCHAR: "TEXT",
        exp.DType.NVARCHAR: "TEXT",
        exp.DType.BINARY: "BLOB",
        exp.DType.VARBINARY: "BLOB",
    }

    TOKEN_MAPPING = {
        TokenType.AUTO_INCREMENT: "AUTOINCREMENT",
    }

    TRANSFORMS = {
        **generator.Generator.TRANSFORMS,
        exp.AnyValue: any_value_to_max_sql,
        exp.Chr: rename_func("CHAR"),
        exp.CountIf: count_if_to_sum,
        exp.Create: transforms.preprocess([_transform_create]),
        exp.CurrentDate: lambda *_: "CURRENT_DATE",
        exp.CurrentTime: lambda *_: "CURRENT_TIME",
        exp.CurrentTimestamp: lambda *_: "CURRENT_TIMESTAMP",
        exp.CurrentVersion: lambda *_: "SQLITE_VERSION()",
        exp.ColumnDef: transforms.preprocess([_generated_to_auto_increment]),
        exp.If: rename_func("IIF"),
        exp.ILike: no_ilike_sql,
        exp.JSONArrayAgg: unsupported_args("order", "null_handling", "return_type", "strict")(
            rename_func("JSON_GROUP_ARRAY")
        ),
        exp.JSONObjectAgg: lambda self, e: self._jsonobject_sql(e, name="JSON_GROUP_OBJECT"),
        exp.Levenshtein: unsupported_args("ins_cost", "del_cost", "sub_cost", "max_dist")(
            rename_func("EDITDIST3")
        ),
        exp.LogicalOr: rename_func("MAX"),
        exp.LogicalAnd: rename_func("MIN"),
        exp.Pivot: reject_pivot_sql,
        exp.Rand: rename_func("RANDOM"),
        exp.RegexpLike: lambda self, e: self.binary(e, "REGEXP"),
        **{kind: _moments_sql for kind in MOMENT_AGGREGATES},
        exp.Select: transforms.preprocess(
            [
                _global_having_to_subquery,
                transforms.eliminate_distinct_on,
                transforms.eliminate_qualify,
                transforms.eliminate_semi_and_anti_joins,
            ]
        ),
        exp.StrPosition: lambda self, e: strposition_sql(self, e, func_name="INSTR"),
        exp.TableSample: no_tablesample_sql,
        exp.TimeToStr: lambda self, e: self.func("STRFTIME", e.args.get("format"), e.this),
        exp.TryCast: no_trycast_sql,
        exp.TsOrDsToTimestamp: lambda self, e: self.sql(e, "this"),
    }

    # SQLite doesn't generally support CREATE TABLE .. properties
    # https://www.sqlite.org/lang_createtable.html
    PROPERTIES_LOCATION = {
        **{
            prop: exp.Properties.Location.UNSUPPORTED
            for prop in generator.Generator.PROPERTIES_LOCATION
        },
        # There are a few exceptions (e.g. temporary tables) which are supported or
        # can be transpiled to SQLite, so we explicitly override them accordingly
        exp.LikeProperty: exp.Properties.Location.POST_SCHEMA,
        exp.ModuleProperty: exp.Properties.Location.POST_SCHEMA,
        exp.TemporaryProperty: exp.Properties.Location.POST_CREATE,
        exp.VirtualProperty: exp.Properties.Location.POST_CREATE,
    }

    LIMIT_FETCH = "LIMIT"

    def offset_limit_modifiers(
        self, expression: exp.Expr, fetch: bool, limit: exp.Fetch | exp.Limit | None
    ) -> list[str]:
        if expression.args.get("offset") and (
            not limit or (isinstance(limit, exp.Limit) and limit.is_limit_all)
        ):
            limit = exp.Limit(expression=exp.Literal.number(-1))
        return super().offset_limit_modifiers(expression, fetch, limit)

    def subquery_sql(self, expression: exp.Subquery, sep: str = " AS ") -> str:
        if any(expression.args.get(key) for key in ("order", "limit", "offset")):
            query = expression.this
            keys = ("order", "limit", "offset", "with_")
            # Merge grouping modifiers unless doing so would overwrite an inner clause.
            if any(expression.args.get(key) and query.args.get(key) for key in keys):
                query = exp.select("*").from_(query.subquery(copy=False), copy=False)
            for key in keys:
                if value := expression.args.get(key):
                    query.set(key, value.pop())
            return self.sql(query)

        return super().subquery_sql(expression, sep=sep)

    def autoincrementcolumnconstraint_sql(
        self, expression: exp.AutoIncrementColumnConstraint
    ) -> str:
        column = expression.find_ancestor(exp.ColumnDef)
        if column and column.find(exp.PrimaryKeyColumnConstraint):
            return super().autoincrementcolumnconstraint_sql(expression)

        self.unsupported("SQLite AUTOINCREMENT requires an INTEGER PRIMARY KEY")
        return ""

    @staticmethod
    def _explicit_integer(node: exp.Expr) -> bool:
        """
        Whether `node` is integer-valued by its own syntax: an integer literal, a CAST to an
        integer type, or arithmetic over them.
        """
        while isinstance(node, (exp.Paren, exp.Neg)):
            node = node.this
        if isinstance(node, exp.Literal):
            return node.is_int
        if isinstance(node, exp.Cast):
            return node.to.is_type(*exp.DataType.INTEGER_TYPES)
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Mod, exp.IntDiv, exp.Div)):
            if isinstance(node, exp.Div) and not node.args.get("typed"):
                return False
            return SQLiteGenerator._explicit_integer(
                node.this
            ) and SQLiteGenerator._explicit_integer(node.expression)
        return False

    @staticmethod
    def _real_valued(node: exp.Expr) -> bool:
        """Whether SQLite evaluates `node` to REAL by its own syntax."""
        while isinstance(node, (exp.Paren, exp.Neg)):
            node = node.this
        if isinstance(node, exp.Literal):
            return node.is_number and not node.is_int
        if isinstance(node, exp.Cast):
            return node.to.is_type(*exp.DataType.REAL_TYPES)
        if isinstance(node, exp.Avg):
            return True
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Mod)):
            return SQLiteGenerator._real_valued(node.this) or SQLiteGenerator._real_valued(
                node.expression
            )
        if isinstance(node, exp.Div):
            # A division is REAL unless it is an explicit integer division (see div_sql).
            return not SQLiteGenerator._truncating_division(node)
        return False

    @staticmethod
    def _truncating_division(expression: exp.Div) -> bool:
        if not expression.args.get("typed"):
            return False
        # SQLite divides two INTEGER values with truncation, and a column or aggregate that
        # looks decimal can still hold INTEGER storage, so without a schema only operands that
        # are integer by syntax (literals, CASTs to an integer type) keep the source's
        # truncating division; `2020 / 3` from PostgreSQL stays 673.
        return SQLiteGenerator._explicit_integer(
            expression.this
        ) and SQLiteGenerator._explicit_integer(expression.expression)

    def _exact_quotients(self, expression: exp.Div) -> None:
        # `binary` writes directly nested divisions (a / b / c) without calling div_sql for the
        # inner ones, so the whole chain is resolved here, innermost first.
        for operand in (expression.this, expression.expression):
            if isinstance(operand, exp.Div):
                self._exact_quotients(operand)
        # A source `/` that may truncate integers (typed) writes a quotient SQLite computes
        # exactly: the dividend becomes REAL unless an operand already is REAL or both operands
        # are integer by syntax (integer literals, CASTs to an integer type), which keep the
        # source's truncating division.
        if (
            expression.args.get("typed")
            and not self._truncating_division(expression)
            and not self._real_valued(expression.this)
            and not self._real_valued(expression.expression)
        ):
            expression.set(
                "this",
                exp.Cast(this=expression.this, to=exp.DataType.build(exp.DType.DOUBLE)),
            )

    def div_sql(self, expression: exp.Div) -> str:
        self._exact_quotients(expression)
        return super().div_sql(expression)

    def _annotated(self, node: exp.Expr) -> exp.Expr:
        if node.type:
            return node
        from sqlglot.optimizer.annotate_types import annotate_types

        return annotate_types(node.copy(), dialect=self.dialect, overwrite_types=False)

    def _strftime_input(self, node: exp.Expr, date_ok: bool) -> bool:
        # SQLite keeps temporal values as text, which STRFTIME reads for a column or
        # expression of unknown type and for a DATE. A CAST to TIMESTAMP yields a
        # number instead, so a value typed as anything else is not lowered.
        node = self._annotated(node)
        return (
            not node.type
            or node.is_type(exp.DType.UNKNOWN)
            or (date_ok and node.is_type(exp.DType.DATE))
        )

    def extract_sql(self, expression: exp.Extract) -> str:
        part = expression.name.upper()
        format_string = EXTRACT_FORMATS.get(part)
        return_type = expression.args.get("return_type")
        if (
            format_string
            and (
                not return_type
                or return_type.is_type(exp.DType.DECIMAL, *exp.DataType.INTEGER_TYPES)
            )
            and self._strftime_input(expression.expression, part in DATE_PARTS)
        ):
            return self.sql(
                exp.cast(
                    exp.TimeToStr(
                        this=expression.expression.copy(), format=exp.Literal.string(format_string)
                    ),
                    exp.DType.INT,
                )
            )
        self.unsupported(
            "SQLite EXTRACT requires a YEAR to SECOND field of an input of unknown type "
            "(YEAR/MONTH/DAY for a DATE) with integer or decimal return semantics"
        )
        return super().extract_sql(expression)

    def insert_sql(self, expression: exp.Insert) -> str:
        if expression.args.get("ignore"):
            expression.set("ignore", False)
            expression.set("alternative", "IGNORE")

        return super().insert_sql(expression)

    def bitwiseandagg_sql(self, expression: exp.BitwiseAndAgg) -> str:
        self.unsupported("BITWISE_AND aggregation is not supported in SQLite")
        return self.function_fallback_sql(expression)

    def bitwiseoragg_sql(self, expression: exp.BitwiseOrAgg) -> str:
        self.unsupported("BITWISE_OR aggregation is not supported in SQLite")
        return self.function_fallback_sql(expression)

    def bitwisexoragg_sql(self, expression: exp.BitwiseXorAgg) -> str:
        self.unsupported("BITWISE_XOR aggregation is not supported in SQLite")
        return self.function_fallback_sql(expression)

    def jsonextract_sql(self, expression: exp.JSONExtract) -> str:
        if expression.expressions:
            return self.function_fallback_sql(expression)
        return arrow_json_extract_sql(self, expression)

    def jsonextractscalar_sql(self, expression: exp.JSONExtractScalar) -> str:
        if expression.args.get("json_subtype"):
            # json_extract() keeps the JSON subtype on object/array results;
            # ->> strips it, observable when the result feeds another JSON function
            return self.func("JSON_EXTRACT", expression.this, expression.expression)
        return arrow_json_extract_sql(self, expression)

    def dateadd_sql(self, expression: exp.DateAdd) -> str:
        modifier = expression.expression
        unit = expression.args.get("unit")
        # An INTERVAL amount carries its own unit, e.g. DATE_ADD(d, INTERVAL 1 DAY);
        # unwrap it so the unit is not left inside the quoted modifier string.
        if isinstance(modifier, exp.Interval):
            unit = unit or modifier.unit
            modifier = modifier.this

        modifier = modifier.unnest()
        if (
            unit
            and not isinstance(modifier, exp.Literal)
            and not (isinstance(modifier, exp.Neg) and modifier.this.is_number)
        ):
            return self.func(
                "DATE",
                expression.this,
                exp.DPipe(
                    this=exp.paren(modifier),
                    expression=exp.Literal.string(f" {unit.name}"),
                ),
            )
        modifier = modifier.name if modifier.is_string else self.sql(modifier)
        modifier = f"{modifier} {unit.name}" if unit else modifier
        return self.func("DATE", expression.this, exp.Literal.string(modifier))

    def datesub_sql(self, expression: exp.DateSub) -> str:
        return self.dateadd_sql(
            exp.DateAdd(
                this=expression.this,
                expression=exp.Neg(this=expression.expression),
                unit=expression.args.get("unit"),
            )
        )

    def _date_interval_sql(self, expression: exp.Add | exp.Sub) -> str | None:
        date, interval = expression.this, expression.expression
        if isinstance(expression, exp.Add) and isinstance(date, exp.Interval):
            date, interval = interval, date
        if not (
            isinstance(interval, exp.Interval)
            and interval.unit
            and isinstance(interval.this, exp.Literal)
            and is_int(interval.this.name)
            and self._annotated(date).is_type(exp.DType.DATE)
        ):
            return None
        amount = int(interval.this.name)
        if isinstance(expression, exp.Sub):
            amount = -amount
        unit = interval.unit.name.upper()
        if unit in ("DAY", "HOUR", "MINUTE", "SECOND"):
            modifier = exp.Literal.string(f"{amount} {unit}")
            # A whole-day offset of a DATE is a DATE: DATETIME() would return
            # 'YYYY-MM-DD 00:00:00', which sorts after the date text it is compared with.
            return self.func("DATE" if unit == "DAY" else "DATETIME", date, modifier)
        if unit in ("MONTH", "YEAR") and isinstance(date, (exp.Cast, exp.Date)):
            # SQLite carries an overflowing month end into the next month, while the
            # source engines clamp it, so only inputs that cannot overflow are lowered:
            # a NULL, or a literal whose day of month is at most 28.
            literal = self._canonical_literal(date.this)
            if isinstance(literal, exp.Null) or (
                isinstance(literal, exp.Literal)
                and literal.is_string
                and re.fullmatch(r"\d{4}-\d{2}-(0[1-9]|1\d|2[0-8])", literal.name)
            ):
                modifier = exp.Literal.string(f"{amount:+d} {unit.lower()}")
                return self.func("DATE", literal, modifier)
        return None

    def _is_date(self, node: exp.Expr) -> bool:
        return self._annotated(node).is_type(exp.DType.DATE)

    def _whole_days(self, node: exp.Expr) -> bool:
        node = node.unnest()
        if isinstance(node, exp.Neg):
            node = node.this.unnest()
        if isinstance(node, exp.Literal):
            return node.is_int
        return self._annotated(node).is_type(*exp.DataType.INTEGER_TYPES)

    def _date_integer_sql(self, expression: exp.Add | exp.Sub) -> str | None:
        # SQLite adds numbers to text: '2000-01-01' + 5 is 2005 and a difference of two date
        # texts is a year difference, whereas the source engines count whole days.
        left, right = expression.this, expression.expression
        if self._is_date(left) and self._whole_days(right):
            days = right
        elif isinstance(expression, exp.Add) and self._is_date(right) and self._whole_days(left):
            left, days = right, left
        elif isinstance(expression, exp.Sub) and self._is_date(left) and self._is_date(right):
            return self.datediff_sql(exp.DateDiff(this=left, expression=right, unit=exp.var("DAY")))
        else:
            return None
        day = exp.var("DAY")
        if isinstance(expression, exp.Sub):
            return self.datesub_sql(exp.DateSub(this=left, expression=days, unit=day))
        return self.dateadd_sql(exp.DateAdd(this=left, expression=days, unit=day))

    def add_sql(self, expression: exp.Add) -> str:
        return (
            self._date_interval_sql(expression)
            or self._date_integer_sql(expression)
            or super().add_sql(expression)
        )

    def sub_sql(self, expression: exp.Sub) -> str:
        return (
            self._date_interval_sql(expression)
            or self._date_integer_sql(expression)
            or super().sub_sql(expression)
        )

    def interval_sql(self, expression: exp.Interval) -> str:
        self.unsupported("SQLite does not support this INTERVAL expression")
        return super().interval_sql(expression)

    def _truncate_sql(self, expression: exp.DateTrunc | exp.TimestampTrunc) -> str:
        unit = expression.args.get("unit")
        format_string = TRUNC_FORMATS.get(unit.name.upper()) if unit else None
        if (
            format_string
            and not expression.args.get("zone")
            and self._strftime_input(expression.this, True)
        ):
            return self.func("STRFTIME", exp.Literal.string(format_string), expression.this)
        self.unsupported(
            "SQLite date truncation requires a YEAR to SECOND unit, no time zone and an input "
            "of unknown type or a DATE"
        )
        return self.function_fallback_sql(expression)

    def _align_dates_with_truncation(self, *operands: exp.Expr) -> None:
        # A truncation is written as timestamp text ('2020-03-01 00:00:00'), while a DATE is
        # date text ('2020-03-01'); the source engines compare a DATE with a timestamp as the
        # DATE's midnight, but SQLite compares the texts, so equality fails and a range bound
        # on the same day is mis-ordered.
        if not any(isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)) for node in operands):
            return
        for node in operands:
            if not isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)) and self._is_date(node):
                node.replace(exp.Anonymous(this="DATETIME", expressions=[node.copy()]))

    def eq_sql(self, expression: exp.EQ) -> str:
        self._align_operands(expression.this, expression.expression)
        return super().eq_sql(expression)

    def neq_sql(self, expression: exp.NEQ) -> str:
        self._align_operands(expression.this, expression.expression)
        return super().neq_sql(expression)

    def lt_sql(self, expression: exp.LT) -> str:
        self._align_operands(expression.this, expression.expression)
        return super().lt_sql(expression)

    def lte_sql(self, expression: exp.LTE) -> str:
        self._align_operands(expression.this, expression.expression)
        return super().lte_sql(expression)

    def gt_sql(self, expression: exp.GT) -> str:
        self._align_operands(expression.this, expression.expression)
        return super().gt_sql(expression)

    def gte_sql(self, expression: exp.GTE) -> str:
        self._align_operands(expression.this, expression.expression)
        return super().gte_sql(expression)

    def between_sql(self, expression: exp.Between) -> str:
        self._align_operands(expression.this, expression.args["low"], expression.args["high"])
        return super().between_sql(expression)

    def datetrunc_sql(self, expression: exp.DateTrunc) -> str:
        return self._truncate_sql(expression)

    def timestamptrunc_sql(self, expression: exp.TimestampTrunc) -> str:
        return self._truncate_sql(expression)

    @staticmethod
    def _canonical_literal(node: exp.Expr, timestamp: bool = False) -> exp.Expr:
        # SQLite parses date text only when every field is zero padded, so DATE('1999-5-01')
        # is NULL and '1999-5-01' sorts after '1999-12-01'; the source engines read both.
        # A string that is not a valid date is left for the engine to reject or ignore.
        if isinstance(node, exp.Literal) and node.is_string:
            canonical = (_canonical_timestamp if timestamp else _canonical_date)(node.name)
            if canonical is not None and canonical != node.name:
                return exp.Literal.string(canonical)
        return node

    def _align_date_literals(self, *operands: exp.Expr) -> None:
        # The source engines coerce a string compared with a DATE to a date; SQLite compares
        # the texts, so an unpadded literal must be written in the stored zero-padded form.
        literals = [n for n in operands if isinstance(n, exp.Literal) and n.is_string]
        if not literals:
            return
        if not any(
            self._is_date(n) for n in operands if not any(n is literal for literal in literals)
        ):
            return
        for literal in literals:
            canonical = self._canonical_literal(literal)
            if canonical is not literal:
                literal.replace(canonical)

    def _align_operands(self, *operands: exp.Expr) -> None:
        self._align_date_literals(*operands)
        self._align_dates_with_truncation(*operands)

    def in_sql(self, expression: exp.In) -> str:
        self._align_date_literals(expression.this, *expression.expressions)
        return super().in_sql(expression)

    def datestrtodate_sql(self, expression: exp.DateStrToDate) -> str:
        return self.sql(self._canonical_literal(expression.this))

    def timestrtotime_sql(self, expression: exp.TimeStrToTime) -> str:
        return self.sql(self._canonical_literal(expression.this, timestamp=True))

    def cast_sql(self, expression: exp.Cast, safe_prefix: str | None = None) -> str:
        if expression.is_type("date"):
            return self.func("DATE", self._canonical_literal(expression.this))
        if expression.is_type("datetime", "timestamp", "timestampntz"):
            return self.func("DATETIME", self._canonical_literal(expression.this, timestamp=True))
        if expression.is_type("time"):
            return self.func("TIME", expression.this)

        return super().cast_sql(expression)

    # https://www.sqlite.org/gencol.html
    # Inline unsupported check: mypyc cannot compile @unsupported_args on an
    # override of an undecorated base-class method.
    def computedcolumnconstraint_sql(self, expression: exp.ComputedColumnConstraint) -> str:
        if expression.args.get("data_type"):
            self.unsupported("SQLite generated columns do not support a data type")

        this = expression.this
        this_sql = self.sql(this) if isinstance(this, exp.Paren) else f"({self.sql(this)})"
        storage = " STORED" if expression.args.get("persisted") else ""
        not_null = " NOT NULL" if expression.args.get("not_null") else ""
        return f"AS {this_sql}{storage}{not_null}"

    # Note: SQLite's TRUNC always returns REAL (e.g., trunc(10.99) -> 10.0), not INTEGER.
    # This creates a transpilation gap affecting division semantics, similar to Presto.
    # Unlike Presto where this only affects decimals=0, SQLite has no decimals parameter
    # so every use of TRUNC is affected. Modeling precisely would require exp.FloatTrunc.
    @unsupported_args("decimals")
    def trunc_sql(self, expression: exp.Trunc) -> str:
        return self.func("TRUNC", expression.this)

    def generateseries_sql(self, expression: exp.GenerateSeries) -> str:
        parent = expression.parent
        alias = parent and parent.args.get("alias")

        if isinstance(alias, exp.TableAlias) and alias.columns:
            column_alias = alias.columns[0]
            alias.set("columns", None)
            sql = self.sql(
                exp.select(exp.alias_("value", column_alias)).from_(expression).subquery()
            )
        else:
            sql = self.function_fallback_sql(expression)

        return sql

    def datediff_sql(self, expression: exp.DateDiff) -> str:
        unit = expression.args.get("unit")
        unit = unit.name.upper() if unit else "DAY"

        sql = f"(JULIANDAY({self.sql(expression, 'this')}) - JULIANDAY({self.sql(expression, 'expression')}))"

        if unit == "DAY":
            pass
        elif unit == "MONTH":
            sql = f"{sql} / 30.0"
        elif unit == "YEAR":
            sql = f"{sql} / 365.0"
        elif unit == "HOUR":
            sql = f"{sql} * 24.0"
        elif unit == "MINUTE":
            sql = f"{sql} * 1440.0"
        elif unit == "SECOND":
            sql = f"{sql} * 86400.0"
        elif unit == "MILLISECOND":
            sql = f"{sql} * 86400000.0"
        elif unit == "MICROSECOND":
            sql = f"{sql} * 86400000000.0"
        elif unit == "NANOSECOND":
            sql = f"{sql} * 86400000000000.0"
        else:
            self.unsupported(f"DATEDIFF unsupported for '{unit}'.")

        return f"CAST({sql} AS INTEGER)"

    def groupconcat_sql(self, expression: exp.GroupConcat) -> str:
        node = expression.parent if isinstance(expression.parent, exp.Filter) else expression
        window = node.parent

        if (
            isinstance(expression.this, exp.Order)
            and isinstance(window, exp.Window)
            and window.this is node
        ):
            self.unsupported(
                "SQLite GROUP_CONCAT window functions do not support argument ORDER BY"
            )
            expression.set("this", expression.this.this)

        return groupconcat_sql(
            self, expression, func_name="GROUP_CONCAT", sep=None, within_group=False
        )

    def _greatest_least_sql(self, expression: exp.Greatest | exp.Least) -> str:
        if not expression.expressions:
            return self.sql(expression, "this")

        name = "MAX" if isinstance(expression, exp.Greatest) else "MIN"

        if not expression.args.get("ignore_nulls"):
            return rename_func(name)(self, expression)

        # SQLite's multi-argument MAX/MIN return NULL if any argument is NULL.
        # GREATEST(a, b, c) -> MAX(COALESCE(a, b, c), COALESCE(b, c, a), COALESCE(c, a, b)).
        args = [expression.this, *expression.expressions]

        coalesces = []
        for i in range(len(args)):
            rotated = args[i:] + args[:i]
            coalesces.append(exp.Coalesce(this=rotated[0], expressions=rotated[1:]))

        return self.func(name, *coalesces)

    def preprocess(self, expression: exp.Expr) -> exp.Expr:
        expression = expand_grouping_sets(expression, copy=False)
        expression = lower_pivots(expression, copy=False)
        return super().preprocess(expand_order_by_all(expression, copy=False))

    def greatest_sql(self, expression: exp.Greatest) -> str:
        return self._greatest_least_sql(expression)

    def least_sql(self, expression: exp.Least) -> str:
        return self._greatest_least_sql(expression)

    def transaction_sql(self, expression: exp.Transaction) -> str:
        this = expression.this
        this = f" {this}" if this else ""
        return f"BEGIN{this} TRANSACTION"

    def isascii_sql(self, expression: exp.IsAscii) -> str:
        return f"(NOT {self.sql(expression.this)} GLOB CAST(x'2a5b5e012d7f5d2a' AS TEXT))"

    @unsupported_args("this")
    def currentschema_sql(self, expression: exp.CurrentSchema) -> str:
        return "'main'"

    def ignorenulls_sql(self, expression: exp.IgnoreNulls) -> str:
        self.unsupported("SQLite does not support IGNORE NULLS.")
        return self.sql(expression.this)

    def respectnulls_sql(self, expression: exp.RespectNulls) -> str:
        return self.sql(expression.this)

    def windowspec_sql(self, expression: exp.WindowSpec) -> str:
        if (
            expression.text("kind").upper() == "RANGE"
            and expression.text("start").upper() == "CURRENT ROW"
            and expression.args.get("end") is None
        ):
            return "RANGE CURRENT ROW"

        return super().windowspec_sql(expression)
