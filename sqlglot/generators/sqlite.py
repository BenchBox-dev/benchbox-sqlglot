from __future__ import annotations

import re
import typing as t

from sqlglot import exp, generator, transforms
from sqlglot.dialects.dialect import (
    any_value_to_max_sql,
    arrow_json_extract_sql,
    count_if_to_sum,
    groupconcat_sql,
    no_ilike_sql,
    no_pivot_sql,
    no_tablesample_sql,
    no_trycast_sql,
    rename_func,
    strposition_sql,
)
from sqlglot.generator import unsupported_args
from sqlglot.errors import OptimizeError
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


def _offset_to_limit(expression: exp.Expr) -> exp.Expr:
    if not isinstance(expression, exp.Select):
        return expression

    offset = expression.args.get("offset")
    limit = expression.args.get("limit")

    if offset and (not limit or (isinstance(limit, exp.Limit) and limit.is_limit_all)):
        expression.limit(-1, copy=False)

    return expression


class SQLiteGenerator(generator.Generator):
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
        exp.DateStrToDate: lambda self, e: self.sql(e, "this"),
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
        exp.Pivot: no_pivot_sql,
        exp.Rand: rename_func("RANDOM"),
        exp.RegexpLike: lambda self, e: self.binary(e, "REGEXP"),
        exp.Select: transforms.preprocess(
            [
                _offset_to_limit,
                transforms.eliminate_distinct_on,
                transforms.eliminate_qualify,
                transforms.eliminate_semi_and_anti_joins,
            ]
        ),
        exp.StrPosition: lambda self, e: strposition_sql(self, e, func_name="INSTR"),
        exp.TableSample: no_tablesample_sql,
        exp.TimeStrToTime: lambda self, e: self.sql(e, "this"),
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

    def autoincrementcolumnconstraint_sql(
        self, expression: exp.AutoIncrementColumnConstraint
    ) -> str:
        column = expression.find_ancestor(exp.ColumnDef)
        if column and column.find(exp.PrimaryKeyColumnConstraint):
            return super().autoincrementcolumnconstraint_sql(expression)

        self.unsupported("SQLite AUTOINCREMENT requires an INTEGER PRIMARY KEY")
        return ""

    _extraction_division_casts: dict[int, bool | None] | None = None

    def preprocess(self, expression: exp.Expr) -> exp.Expr:
        self._extraction_division_casts = None
        return super().preprocess(expression)

    def _resolve_extraction_divisions(self, expression: exp.Expr) -> dict[int, bool | None]:
        root = expression.root()
        if not any(
            extract.args.get("return_type")
            and extract.args["return_type"].is_type(exp.DType.DECIMAL)
            for extract in root.find_all(exp.Extract)
        ):
            return {}

        from sqlglot.optimizer.annotate_types import annotate_types
        from sqlglot.optimizer.qualify_columns import qualify_columns

        # Whole date parts fit SQLite INTEGER, but PostgreSQL's decimal EXTRACT
        # still requires fractional division, including through CTE aliases.
        # Resolve on a copy so qualification cannot alter emitted column names.
        annotated = root.copy()
        divisions = {
            id(original): copied
            for original, copied in zip(root.find_all(exp.Div), annotated.find_all(exp.Div))
        }
        try:
            if annotated.find(exp.Column):
                qualify_columns(annotated, schema={}, expand_stars=False, dialect=self.dialect)
            annotate_types(annotated, dialect=self.dialect, overwrite_types=False)
        except OptimizeError as error:
            self.unsupported(f"Cannot resolve SQLite extraction division types: {error}")
            return {}

        casts: dict[int, bool | None] = {}
        for key, division in divisions.items():
            left, right = division.this, division.expression
            if left.is_type(exp.DType.DECIMAL) or right.is_type(exp.DType.DECIMAL):
                casts[key] = True
            elif (
                not left.type
                or not right.type
                or left.is_type(exp.DType.UNKNOWN)
                or right.is_type(exp.DType.UNKNOWN)
            ):
                casts[key] = None
            else:
                casts[key] = False
        return casts

    def div_sql(self, expression: exp.Div) -> str:
        if expression.args.get("typed"):
            if self._extraction_division_casts is None:
                self._extraction_division_casts = self._resolve_extraction_divisions(expression)
            if self._extraction_division_casts:
                needs_cast = self._extraction_division_casts.get(id(expression))
                if needs_cast:
                    expression.set(
                        "this",
                        exp.Cast(this=expression.this, to=exp.DataType.build(exp.DType.DOUBLE)),
                    )
                elif needs_cast is None:
                    self.unsupported("SQLite extraction division requires known operand types")
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
        modifier = f"'{modifier} {unit.name}'" if unit else f"'{modifier}'"
        return self.func("DATE", expression.this, modifier)

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
            return self.func("DATETIME", date, modifier)
        if unit in ("MONTH", "YEAR") and isinstance(date, (exp.Cast, exp.Date)):
            # SQLite carries an overflowing month end into the next month, while the
            # source engines clamp it, so only inputs that cannot overflow are lowered:
            # a NULL, or a literal whose day of month is at most 28.
            literal = date.this
            if isinstance(literal, exp.Null) or (
                isinstance(literal, exp.Literal)
                and literal.is_string
                and re.fullmatch(r"\d{4}-\d{2}-(0[1-9]|1\d|2[0-8])", literal.name)
            ):
                modifier = exp.Literal.string(f"{amount:+d} {unit.lower()}")
                return self.func("DATE", literal, modifier)
        return None

    def add_sql(self, expression: exp.Add) -> str:
        return self._date_interval_sql(expression) or super().add_sql(expression)

    def sub_sql(self, expression: exp.Sub) -> str:
        return self._date_interval_sql(expression) or super().sub_sql(expression)

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

    def datetrunc_sql(self, expression: exp.DateTrunc) -> str:
        return self._truncate_sql(expression)

    def timestamptrunc_sql(self, expression: exp.TimestampTrunc) -> str:
        return self._truncate_sql(expression)

    def cast_sql(self, expression: exp.Cast, safe_prefix: str | None = None) -> str:
        if expression.is_type("date"):
            return self.func("DATE", expression.this)

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

        if unit == "MONTH":
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
