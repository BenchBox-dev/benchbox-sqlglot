"""Engine-specific table clauses for ``transpile_ddl``.

``apply_layout`` turns a ``TableLayout`` into the clauses the target dialect writes. Each
clause group is written in the target dialect's own syntax, parsed back with that dialect and
attached to the ``CREATE`` statement's properties, so SQLGlot's generator spells and places it.
A group the dialect's parser cannot read is returned as text for ``transpile_ddl`` to append to
the statement.
"""

from __future__ import annotations

import logging
import re
import typing as t
from collections.abc import Sequence

import sqlglot
from sqlglot import exp
from sqlglot.dialects.clickhouse import ClickHouse
from sqlglot.dialects.dialect import Dialect
from sqlglot.dialects.doris import Doris
from sqlglot.dialects.athena import Athena
from sqlglot.dialects.hive import Hive
from sqlglot.dialects.postgres import Postgres
from sqlglot.dialects.questdb import QuestDB
from sqlglot.dialects.presto import Presto
from sqlglot.dialects.spark import Spark
from sqlglot.dialects.tsql import TSQL
from sqlglot.dialects.redshift import Redshift
from sqlglot.dialects.starrocks import StarRocks
from sqlglot.benchbox.layout import TableLayout

# Column type names SQLGlot spells with a length or without a type Doris and StarRocks accept
# as a column of an OLAP table; pass as ``DdlOptions.type_map``.
DORIS_TYPE_MAP: dict[str, str] = {
    "TIME": "VARCHAR(8)",
    "TEXT": "VARCHAR(65533)",
    "SMALLINT": "INT",
}
STARROCKS_TYPE_MAP: dict[str, str] = {
    "TIME": "VARCHAR(10)",
    "TEXT": "VARCHAR(65533)",
    "SMALLINT": "INT",
    "BLOB": "VARCHAR(65533)",
}
# SingleStore recommends ``DATETIME`` over ``TIMESTAMP``, which only holds dates through
# 2038-01-19 (``DATETIME`` goes to 9999-12-31). Pass as ``DdlOptions.type_map``.
SINGLESTORE_TYPE_MAP: dict[str, str] = {"TIMESTAMP": "DATETIME"}

_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_NON_KEY_TYPES = frozenset(
    {
        "time",
        "json",
        "jsonb",
        "blob",
        "binary",
        "varbinary",
        "hll",
        "bitmap",
        "array",
        "map",
        "struct",
    }
)
_PASSIVE = (exp.ExternalProperty,)


class _Raw(str):
    """A clause group written verbatim after the statement, not parsed."""


_DISTSTYLES = {"key", "even", "all", "auto"}


class _Context:
    """The statement, target and options a layout is applied with."""

    def __init__(
        self,
        create: exp.Create,
        layout: TableLayout,
        target: Dialect,
        opts: dict[str, t.Any],
        source_primary_key: Sequence[str],
    ) -> None:
        self.create = create
        self.layout = layout
        self.target = target
        self.opts = opts
        self.source_primary_key = tuple(source_primary_key)
        schema = create.this
        self.columns: list[exp.ColumnDef] = (
            list(schema.expressions) if isinstance(schema, exp.Schema) else []
        )
        self.columns = [c for c in self.columns if isinstance(c, exp.ColumnDef)]

    @property
    def defined(self) -> dict[str, exp.ColumnDef]:
        return {c.name.lower(): c for c in self.columns}

    def name(self, text: str) -> str:
        """A column name or expression as the target writes it."""
        if _PLAIN_NAME.match(text):
            return exp.to_identifier(text).sql(self.target, **self.opts)
        return text

    def names(self, values: Sequence[str]) -> str:
        return ", ".join(self.name(v) for v in values)

    def literal(self, value: str) -> str:
        """A string literal, escaped the way the target's tokenizer reads it."""
        escapes = self.target.tokenizer_class.STRING_ESCAPES
        if "\\" in escapes:
            value = value.replace("\\", "\\\\")
        return "'" + value.replace("'", "''" if "'" in escapes else "\\'") + "'"

    def value(self, value: str) -> str:
        """A property value: numbers, booleans and arrays as they are, everything else quoted."""
        text = str(value)
        if re.fullmatch(
            r"-?\d+(\.\d+)?|true|false", text, re.IGNORECASE
        ) or text.upper().startswith("ARRAY["):
            return text
        return self.literal(text)


def _key_type_ok(column: exp.ColumnDef) -> bool:
    kind = column.args.get("kind")
    if not isinstance(kind, exp.DataType):
        return True
    name = kind.this.value if isinstance(kind.this, exp.DType) else str(kind.this)
    return name.split("(")[0].strip().lower() not in _NON_KEY_TYPES


# -- ClickHouse -----------------------------------------------------------------------


def _mentions(expression: str, column: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(column)}(?![A-Za-z0-9_])", expression, re.I)
        is not None
    )


def drop_array_sizes(columns: Sequence[exp.ColumnDef], *, plain_elements: bool = False) -> None:
    """``FLOAT[3]`` is a plain array in engines without fixed-size arrays: drop the size."""
    for column in columns:
        kind = column.args.get("kind")
        if not isinstance(kind, exp.DataType):
            continue
        for array in [d for d in kind.find_all(exp.DataType) if d.is_type(exp.DType.ARRAY)]:
            if array.args.get("values"):
                array.set("values", None)
                if plain_elements:
                    for inner in array.expressions:
                        if isinstance(inner, exp.DataType):
                            inner.set("nullable", False)


def _drop_array_sizes(ctx: _Context, *, plain_elements: bool = False) -> None:
    drop_array_sizes(ctx.columns, plain_elements=plain_elements)


def _clickhouse_types(ctx: _Context) -> None:
    layout = ctx.layout
    source_pk = {c.lower() for c in ctx.source_primary_key}
    nullable = layout.nullable_columns
    if nullable is not None:
        tuned = [*layout.partition_by, *layout.sort_by, *layout.primary_key]
        bad = sorted(c for c in nullable if any(_mentions(e, c) for e in tuned))
        if bad:
            raise ValueError(
                f"ClickHouse key columns cannot be Nullable: {', '.join(bad)} appear in "
                "partition_by, sort_by or primary_key and are listed in nullable_columns"
            )

    _drop_array_sizes(ctx, plain_elements=True)
    for column in ctx.columns:
        kind = column.args.get("kind")
        if not isinstance(kind, exp.DataType):
            continue
        has_not_null = any(
            isinstance(c.args.get("kind"), exp.NotNullColumnConstraint)
            for c in column.args.get("constraints") or []
        )
        name = column.name.lower()
        if nullable is None:
            if has_not_null:
                kind.set("nullable", False)
            continue
        make_nullable = name in nullable and name not in source_pk
        if make_nullable and has_not_null:
            raise ValueError(f"source-nullable column {name!r} is declared NOT NULL")
        kind.set("nullable", make_nullable)


def _clickhouse(ctx: _Context) -> list[str]:
    layout = ctx.layout
    source_pk = ctx.source_primary_key
    _clickhouse_types(ctx)

    groups: list[str] = []
    if layout.engine:
        groups.append(f"ENGINE = {layout.engine}")
    if layout.partition_by:
        groups.append(f"PARTITION BY ({ctx.names(layout.partition_by)})")

    properties = ctx.create.args.get("properties")
    has_order = isinstance(properties, exp.Properties) and any(
        isinstance(p, exp.Order) for p in properties.expressions
    )
    if layout.sort_by:
        order = f"ORDER BY ({ctx.names(layout.sort_by)})"
        if layout.primary_key:
            order += f" PRIMARY KEY ({ctx.names(layout.primary_key)})"
        if not has_order:
            _strip_primary_key(ctx)
    elif source_pk:
        order = f"ORDER BY ({ctx.names(source_pk)})"
    else:
        order = "ORDER BY tuple()"
    groups.append(order)

    if layout.properties:
        settings = ", ".join(f"{k} = {ctx.value(v)}" for k, v in layout.properties.items())
        groups.append(f"SETTINGS {settings}")
    return groups


def _strip_primary_key(ctx: _Context) -> None:
    from sqlglot.benchbox.ddl import _strip_constraints

    _strip_constraints(ctx.create, (exp.PrimaryKeyColumnConstraint, exp.PrimaryKey))


# -- Doris and StarRocks --------------------------------------------------------------

_KEY_MODELS = {
    "duplicate": "DUPLICATE KEY",
    "unique": "UNIQUE KEY",
    "aggregate": "AGGREGATE KEY",
    "primary": "PRIMARY KEY",
}


def _olap_keys(ctx: _Context) -> tuple[str, list[exp.ColumnDef]] | None:
    layout = ctx.layout
    model = layout.key_model
    if model not in _KEY_MODELS:
        raise ValueError(
            f"key_model must be one of {', '.join(_KEY_MODELS)}, not {layout.key_model!r}"
        )
    columns = ctx.columns
    keyable = [c for c in columns if _key_type_ok(c)]

    if model != "duplicate":
        required = list(layout.key_columns) or list(ctx.source_primary_key)
        if not required:
            raise ValueError(
                f"key_model {model!r} needs key_columns or a primary key in the statement; "
                "a narrower key would merge or replace rows"
            )
        wanted = {c.lower() for c in required}
        leading = columns[: len(wanted)]
        if {c.name.lower() for c in leading} != wanted or not all(map(_key_type_ok, leading)):
            raise ValueError(
                f"key_model {model!r} needs the key columns ({', '.join(required)}) to be defined, "
                "keyable and the leading columns of the table; a narrower key would merge or "
                "replace rows"
            )
        return str(model), leading

    if not keyable:
        return None
    wanted_columns = [c.lower() for c in layout.key_columns]
    prefix: list[exp.ColumnDef] = []
    for column in columns:
        if column.name.lower() in wanted_columns and _key_type_ok(column):
            prefix.append(column)
        else:
            break
    return "duplicate", prefix or keyable[:1]


def _olap(ctx: _Context, *, starrocks: bool) -> list[str]:
    from sqlglot.benchbox.ddl import _strip_constraints

    layout = ctx.layout
    _strip_constraints(ctx.create, (exp.PrimaryKeyColumnConstraint, exp.PrimaryKey))
    _strip_constraints(ctx.create, (exp.Reference, exp.ForeignKey))
    _drop_array_sizes(ctx)

    parts: list[str] = []
    keys: list[exp.ColumnDef] = []
    if layout.key_model:
        resolved = _olap_keys(ctx)
        if resolved:
            model, keys = resolved
            parts.append(f"{_KEY_MODELS[model]}({ctx.names([c.name for c in keys])})")

    if layout.partition_clause:
        parts.append(layout.partition_clause)
    elif layout.partition_by:
        if not starrocks:
            raise ValueError("Doris partitioning needs partition_clause (RANGE or LIST)")
        parts.append(f"PARTITION BY ({ctx.names(layout.partition_by)})")

    wants_distribution = (
        layout.distribution or layout.distribute_by or layout.buckets or layout.key_model
    )
    if wants_distribution:
        defined = ctx.defined
        buckets = f" BUCKETS {layout.buckets}" if layout.buckets else ""
        columns = [defined[c.lower()].name for c in layout.distribute_by if c.lower() in defined]
        if len(columns) != len(layout.distribute_by):
            columns = []
        if layout.distribution == "random":
            parts.append(f"DISTRIBUTED BY RANDOM{buckets}")
        elif columns or keys:
            hashed = columns or [keys[0].name]
            parts.append(f"DISTRIBUTED BY HASH({ctx.names(hashed)}){buckets}")
        else:
            parts.append(f"DISTRIBUTED BY RANDOM{buckets}")

    if starrocks and layout.sort_by:
        parts.append(f"ORDER BY ({ctx.names(layout.sort_by)})")
    if layout.properties:
        props = ", ".join(
            f"{ctx.literal(k)} = {ctx.literal(v)}" for k, v in layout.properties.items()
        )
        parts.append(f"PROPERTIES ({props})")
    return [" ".join(parts)] if parts else []


# -- Redshift -------------------------------------------------------------------------


def _redshift(ctx: _Context) -> list[str]:
    layout = ctx.layout
    groups: list[str] = []
    style = layout.distribution or ("key" if layout.distribute_by else None)
    if style is not None and style not in _DISTSTYLES:
        raise ValueError(
            f"Redshift distribution must be one of {sorted(_DISTSTYLES)}, not {style!r}"
        )
    if style == "key":
        if not layout.distribute_by:
            raise ValueError("Redshift DISTSTYLE KEY needs distribute_by")
        groups.append(f"DISTSTYLE KEY DISTKEY({ctx.name(layout.distribute_by[0])})")
    elif style:
        groups.append(f"DISTSTYLE {style.upper()}")

    properties = ctx.create.args.get("properties")
    # A statement's own SORTKEY is kept, with no second one beside it.
    has_sort_key = isinstance(properties, exp.Properties) and any(
        isinstance(p, exp.SortKeyProperty) for p in properties.expressions
    )
    if layout.sort_by and not has_sort_key:
        groups.append(f"SORTKEY({ctx.names(layout.sort_by)})")
    elif layout.sort_auto and not layout.sort_by and not has_sort_key:
        groups.append("SORTKEY AUTO")

    if layout.external:
        ctx.create.set("kind", "EXTERNAL TABLE")
        file_format = layout.stored_as or layout.format
        if file_format:
            groups.append(_Raw(f"STORED AS {file_format.upper()}"))
        if layout.location:
            groups.append(_Raw(f"LOCATION {ctx.literal(layout.location)}"))
        if layout.properties:
            props = ", ".join(
                f"{ctx.literal(k)}={ctx.literal(v)}" for k, v in layout.properties.items()
            )
            groups.append(_Raw(f"TABLE PROPERTIES ({props})"))
    return groups


# -- Spark, Databricks, Hive, Athena --------------------------------------------------


def _bucketing(ctx: _Context) -> str | None:
    layout = ctx.layout
    if not layout.distribute_by:
        return None
    if not layout.buckets:
        raise ValueError("bucketing by distribute_by needs buckets")
    clause = f"CLUSTERED BY ({ctx.names(layout.distribute_by)})"
    if layout.sort_by:
        clause += f" SORTED BY ({ctx.names(layout.sort_by)})"
    return f"{clause} INTO {layout.buckets} BUCKETS"


def _table_properties(ctx: _Context) -> str | None:
    if not ctx.layout.properties:
        return None
    props = ", ".join(
        f"{ctx.literal(k)} = {ctx.literal(v)}" for k, v in ctx.layout.properties.items()
    )
    return f"TBLPROPERTIES ({props})"


def _replace_file_format(ctx: _Context) -> None:
    """The layout's file format is the one written; the statement's own ``USING`` or ``STORED AS`` goes."""
    properties = ctx.create.args.get("properties")
    if not isinstance(properties, exp.Properties):
        return
    kept = [p for p in properties.expressions if not isinstance(p, exp.FileFormatProperty)]
    properties.set("expressions", kept)
    if not kept:
        ctx.create.set("properties", None)


def _spark(ctx: _Context) -> list[str]:
    layout = ctx.layout
    groups: list[str | None] = []
    if layout.stored_as:
        _replace_file_format(ctx)
        groups.append(f"STORED AS {layout.stored_as.upper()}")
    elif layout.format:
        _replace_file_format(ctx)
        groups.append(f"USING {layout.format.upper()}")
    if layout.partition_by:
        groups.append(f"PARTITIONED BY ({ctx.names(layout.partition_by)})")
    if layout.cluster_by_auto:
        groups.append("CLUSTER BY AUTO")
    elif layout.cluster_by:
        groups.append(f"CLUSTER BY ({ctx.names(layout.cluster_by)})")
    groups.append(_bucketing(ctx))
    if layout.location:
        groups.append(f"LOCATION {ctx.literal(layout.location)}")
    groups.append(_table_properties(ctx))
    return [g for g in groups if g]


_ESCAPES = {"\n": "\\n", "\t": "\\t", "\r": "\\r", "'": "\\'", "\\": "\\\\"}


def _delimiter(value: str) -> str:
    return "'" + "".join(_ESCAPES.get(c, c) for c in value) + "'"


def _hive(ctx: _Context) -> list[str]:
    layout = ctx.layout
    groups: list[str | None] = []
    if layout.partition_by:
        defined = ctx.defined
        typed: list[str] = []
        for entry in layout.partition_by:
            column = defined.get(entry.lower())
            if column is None:
                typed.append(entry if " " in entry.strip() else f"{ctx.name(entry)} STRING")
                continue
            kind = column.args.get("kind")
            column.pop()
            typed.append(f"{ctx.name(column.name)} {kind.sql(ctx.target) if kind else 'STRING'}")
        groups.append(f"PARTITIONED BY ({', '.join(typed)})")
    groups.append(_bucketing(ctx))
    if layout.field_delimiter is not None or layout.line_delimiter is not None:
        row = "ROW FORMAT DELIMITED"
        if layout.field_delimiter is not None:
            row += f" FIELDS TERMINATED BY {_delimiter(layout.field_delimiter)}"
        if layout.line_delimiter is not None:
            row += f" LINES TERMINATED BY {_delimiter(layout.line_delimiter)}"
        groups.append(row)
    file_format = layout.stored_as or layout.format
    if file_format:
        _replace_file_format(ctx)
        groups.append(f"STORED AS {file_format.upper()}")
    if layout.location:
        groups.append(f"LOCATION {ctx.literal(layout.location)}")
    groups.append(_table_properties(ctx))
    return [g for g in groups if g]


# -- Trino, Presto --------------------------------------------------------------------


def _trino(ctx: _Context) -> list[str]:
    layout = ctx.layout

    def array(values: Sequence[str]) -> str:
        return "ARRAY[" + ", ".join(ctx.literal(v) for v in values) + "]"

    settings: list[tuple[str, str]] = []
    if layout.format:
        settings.append(("format", ctx.literal(layout.format.upper())))
    if layout.partition_by:
        key = "partitioning" if (layout.format or "").lower() == "iceberg" else "partitioned_by"
        settings.append((key, array(layout.partition_by)))
    if layout.distribute_by:
        if not layout.buckets:
            raise ValueError("bucketing by distribute_by needs buckets")
        settings.append(("bucketed_by", array(layout.distribute_by)))
        settings.append(("bucket_count", str(layout.buckets)))
    if layout.sort_by:
        settings.append(("sorted_by", array(layout.sort_by)))
    if layout.location:
        key = "external_location" if layout.external else "location"
        settings.append((key, ctx.literal(layout.location)))
    settings.extend((k, ctx.value(v)) for k, v in layout.properties.items())
    return [f"WITH ({key} = {value})" for key, value in settings]


# -- T-SQL (Synapse, Fabric) and PostgreSQL ---------------------------------------------

_TSQL_DISTRIBUTIONS = {"round_robin": "ROUND_ROBIN", "replicate": "REPLICATE", "hash": "HASH"}


def _tsql(ctx: _Context) -> list[str]:
    layout = ctx.layout
    style = layout.distribution or ("hash" if layout.distribute_by else None)
    options: list[str] = []
    if style is not None:
        if style not in _TSQL_DISTRIBUTIONS:
            raise ValueError(
                f"T-SQL distribution must be one of {sorted(_TSQL_DISTRIBUTIONS)}, not {style!r}"
            )
        value = _TSQL_DISTRIBUTIONS[style]
        if style == "hash":
            if not layout.distribute_by:
                raise ValueError("T-SQL hash distribution needs distribute_by")
            value = f"HASH({ctx.names(layout.distribute_by)})"
        options.append(f"DISTRIBUTION = {value}")
    options.extend(f"{k} = {ctx.value(v)}" for k, v in layout.properties.items())
    return [f"WITH ({', '.join(options)})"] if options else []


def _postgres(ctx: _Context) -> list[str]:
    """``format`` is a table access method; it is written after the column list."""
    return [_Raw(f"USING {ctx.layout.format}")] if ctx.layout.format else []


# -- QuestDB ---------------------------------------------------------------------------

_TIME_TYPES = {exp.DType.DATE, exp.DType.TIMESTAMP}


def _questdb(ctx: _Context) -> list[str]:
    """Symbols, timestamp columns, the designated timestamp and its partitioning.

    The clause written after the column list is ``timestamp(col) PARTITION BY <unit>``.
    """
    layout = ctx.layout
    defined = ctx.defined

    for name in layout.symbol_columns:
        column = defined.get(name.lower())
        if column is None:
            continue
        kind = column.args.get("kind")
        if isinstance(kind, exp.DataType) and kind.is_type(*exp.DataType.TEXT_TYPES):
            column.set("kind", exp.DataType(this="SYMBOL"))
            # A SYMBOL is nullable by construction.
            for constraint in list(column.args.get("constraints") or []):
                if isinstance(constraint.args.get("kind"), exp.NotNullColumnConstraint):
                    constraint.pop()

    designated = layout.designated_timestamp
    for name in dict.fromkeys([*layout.timestamp_columns, *([designated] if designated else [])]):
        column = defined.get(name.lower())
        if column is None:
            if name == designated:
                raise ValueError(f"designated_timestamp column {name!r} is not defined")
            continue
        kind = column.args.get("kind")
        if not (isinstance(kind, exp.DataType) and kind.is_type(*_TIME_TYPES)):
            if name == designated:
                raise ValueError(f"designated_timestamp column {name!r} is not a DATE or TIMESTAMP")
            continue
        column.set("kind", exp.DataType(this=exp.DType.TIMESTAMP))

    groups: list[str] = []
    unit = (layout.time_partition or "none").upper()
    if designated:
        clause = f"timestamp({ctx.name(designated)})"
        if unit != "NONE":
            clause += f" PARTITION BY {unit}"
        groups.append(_Raw(clause))
    elif unit != "NONE":
        raise ValueError("QuestDB time_partition needs a designated_timestamp")
    return groups


def mooncake_mirror_statements(
    table: str, staging_table: str, schema: str | None = None
) -> list[str]:
    """The statements that promote a loaded heap table to a ``pg_mooncake`` mirror.

    The table is created and loaded as a plain heap table, because ``COPY`` into a
    ``pg_mooncake`` table fails. ``table`` is renamed to ``staging_table`` and
    ``mooncake.create_table`` creates ``table`` from it.
    """

    def quoted(name: str) -> str:
        return exp.to_identifier(name, quoted=True).sql("postgres")

    def qualified(name: str) -> str:
        return f"{quoted(schema)}.{quoted(name)}" if schema else quoted(name)

    def reference(name: str) -> str:
        return "'" + (f"{schema}.{name}" if schema else name).replace("'", "''") + "'"

    return [
        f"ALTER TABLE {qualified(table)} RENAME TO {quoted(staging_table)}",
        f"CALL mooncake.create_table({reference(table)}, {reference(staging_table)})",
    ]


# -- Grafting -------------------------------------------------------------------------

_Builder = t.Callable[[_Context], "list[str]"]


def _family(target: Dialect, layout: TableLayout) -> _Builder | None:
    if isinstance(target, ClickHouse):
        return _clickhouse
    if isinstance(target, Doris):
        return lambda ctx: _olap(ctx, starrocks=False)
    if isinstance(target, StarRocks):
        return lambda ctx: _olap(ctx, starrocks=True)
    if isinstance(target, Redshift):
        return _redshift
    if isinstance(target, Spark):
        return _spark
    if isinstance(target, (Hive, Athena)):
        return _hive
    if isinstance(target, Presto):
        return _trino
    if isinstance(target, TSQL):
        return _tsql
    if isinstance(target, QuestDB):
        return _questdb
    if isinstance(target, Postgres):
        return _postgres
    return None


def _property_key(prop: exp.Expr) -> str:
    if type(prop) is exp.Property:
        return f"property:{prop.name.lower()}"
    return type(prop).__name__


class _Quiet:
    """Keep SQLGlot's "unsupported syntax" warning for a probe parse out of the logs."""

    def __enter__(self) -> None:
        self.logger = logging.getLogger("sqlglot")
        self.level = self.logger.level
        self.logger.setLevel(logging.CRITICAL)

    def __exit__(self, *exc: t.Any) -> None:
        self.logger.setLevel(self.level)


def _parse_group(
    target: Dialect, external: bool, text: str
) -> tuple[list[exp.Expr], list[exp.Expr]] | None:
    sql = f"CREATE {'EXTERNAL ' if external else ''}TABLE _t (_c INT) {text}"
    try:
        with _Quiet():
            tree = sqlglot.parse_one(sql, read=target)
    except sqlglot.errors.SqlglotError:
        return None
    if not isinstance(tree, exp.Create):
        return None
    properties = tree.args.get("properties")
    schema = tree.this
    extras = (
        [e for e in schema.expressions if not isinstance(e, exp.ColumnDef)]
        if isinstance(schema, exp.Schema)
        else []
    )
    props = list(properties.expressions) if isinstance(properties, exp.Properties) else []
    if not props and not extras:
        return None
    return props, extras


def apply_layout(
    create: exp.Create,
    layout: TableLayout,
    target: Dialect,
    opts: dict[str, t.Any],
    source_primary_key: Sequence[str] = (),
) -> list[str]:
    """Apply ``layout`` to a ``CREATE TABLE``; return the clauses to append to its SQL text.

    Clauses the target's parser reads become properties of ``create``; a clause group whose
    property kind the statement already has is dropped, so a statement that says
    ``ENGINE = ...`` or ``DISTSTYLE ...`` keeps its own. The exception is the file format of
    Spark, Databricks, Hive and Athena: a layout's ``format`` or ``stored_as`` is what the table
    is declared to be, so it replaces the statement's own ``USING`` or ``STORED AS``.

    Raises:
        ValueError: ``target`` has no layout support, or the layout is invalid for it.
    """
    builder = _family(target, layout)
    if builder is None:
        raise ValueError(f"table layouts are not supported for the {type(target).__name__} dialect")

    ctx = _Context(create, layout, target, opts, source_primary_key)
    groups = builder(ctx)

    properties = create.args.get("properties")
    if not isinstance(properties, exp.Properties):
        properties = exp.Properties(expressions=[])
    existing = {_property_key(p) for p in properties.expressions}
    tail: list[str] = []
    external = layout.external and not isinstance(target, Redshift)
    schema = create.this if isinstance(create.this, exp.Schema) else None
    for group in groups:
        read = None if isinstance(group, _Raw) else _parse_group(target, external, group)
        if read is None:
            tail.append(group)
            continue
        parsed, extras = read
        active = [p for p in parsed if not isinstance(p, _PASSIVE)]
        if any(_property_key(p) in existing for p in active) or (
            schema is not None
            and any(isinstance(e, type(x)) for e in extras for x in schema.expressions)
        ):
            continue
        for extra in extras:
            if schema is not None:
                schema.append("expressions", extra.copy())
        for prop in parsed:
            if isinstance(prop, _PASSIVE) and any(
                isinstance(e, type(prop)) for e in properties.expressions
            ):
                continue
            properties.append("expressions", prop.copy())
        existing.update(_property_key(p) for p in active)
    if properties.expressions:
        create.set("properties", properties)
    if tail and create.args.get("expression") is not None:
        raise ValueError("engine clauses cannot be added to CREATE TABLE ... AS SELECT")
    return tail
