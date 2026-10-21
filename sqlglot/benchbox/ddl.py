"""DDL translation for the BenchBox fork: ``DdlOptions`` and ``transpile_ddl``.

``transpile_ddl`` parses CREATE TABLE statements, applies the structural changes the
options ask for, and writes them for the target dialect. Every table name, column list
and tuning value arrives through ``DdlOptions``; nothing here names a benchmark.
"""

from __future__ import annotations

import typing as t
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

import sqlglot
from sqlglot import exp
from sqlglot.benchbox.ddl_engines import apply_layout, drop_array_sizes
from sqlglot.benchbox.layout import TableLayout, frozen_layouts
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.dialects.dialect import Dialect, DialectType
from sqlglot.dialects.duckdb import DuckDB
from sqlglot.dialects.postgres import Postgres
from sqlglot.dialects.questdb import QuestDB


def _frozen_columns(
    mapping: Mapping[str, str | Sequence[str]] | None,
) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType(
        {
            table.lower(): (columns,) if isinstance(columns, str) else tuple(columns)
            for table, columns in (mapping or {}).items()
        }
    )


@dataclass(frozen=True)
class SingleStoreKeys:
    """SingleStore key clauses, supplied by the caller for each table.

    Attributes:
        shard_keys: table name to the column or columns of its ``SHARD KEY``. A table whose
            columns are not all defined by the statement gets no mapped key.
        sort_keys: table name to the columns of its ``SORT KEY``; columns the statement
            does not define are dropped, and no clause is written when none remain.
        reference_tables: tables written as ``CREATE REFERENCE TABLE`` and given no
            ``SHARD KEY`` or ``SORT KEY``.
        random_shard_key: write ``SHARD KEY ()`` (random distribution) for a table that has no
            mapped shard key and no primary key.

    Table names are matched case-insensitively and without any schema qualifier.
    """

    shard_keys: Mapping[str, str | Sequence[str]] = field(default_factory=dict)
    sort_keys: Mapping[str, str | Sequence[str]] = field(default_factory=dict)
    reference_tables: Collection[str] = field(default_factory=frozenset)
    random_shard_key: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "shard_keys", _frozen_columns(self.shard_keys))
        object.__setattr__(self, "sort_keys", _frozen_columns(self.sort_keys))
        object.__setattr__(
            self, "reference_tables", frozenset(name.lower() for name in self.reference_tables)
        )


@dataclass(frozen=True)
class DdlOptions:
    """What ``transpile_ddl`` changes besides the dialect spelling. Everything is off by default.

    Attributes:
        strip_primary_keys: remove inline and table-level ``PRIMARY KEY`` constraints,
            named or not.
        strip_foreign_keys: remove ``FOREIGN KEY`` constraints and inline ``REFERENCES``
            clauses, with their referential actions.
        strip_unique: remove inline and table-level ``UNIQUE`` constraints.
        or_replace: write ``CREATE OR REPLACE TABLE`` for a ``CREATE TABLE`` that is not
            temporary and does not say ``IF NOT EXISTS``.
        replace_if_not_exists: with ``or_replace``, also turn ``CREATE TABLE IF NOT EXISTS`` into
            ``CREATE OR REPLACE TABLE``.
        type_map: column data type name (``CHAR``, ``VARCHAR``, ``DECIMAL``...) to the type
            name written instead, with the original parameters kept. The replacement is
            written exactly as given.
        drop_type_parameters: replacement type names written without parameters, so a
            ``VARCHAR(25)`` mapped to ``TEXT`` becomes ``TEXT``.
        singlestore: SingleStore ``SHARD KEY``, ``SORT KEY`` and ``REFERENCE TABLE`` clauses.
        strip_not_null: remove ``NOT NULL`` from columns.
        if_not_exists: write ``CREATE TABLE IF NOT EXISTS`` for a permanent ``CREATE TABLE``
            that does not say it (and is not turned into ``CREATE OR REPLACE``).
        schema: qualify table names that have no schema with this one; a dotted value is read
            as ``catalog.schema`` (BigQuery ``project.dataset``). The tables a ``REFERENCES``
            clause names are qualified, suffixed and re-cased the same way, so they keep
            pointing at the tables this run creates.
        table_suffix: appended to every ``CREATE TABLE`` name.
        table_case: ``upper`` or ``lower`` to write table names in that case, for engines whose
            table names are case-sensitive (BigQuery) when the callers refer to them in one
            case. Applies to the created table and to the tables a ``REFERENCES`` clause names.
        layouts: table name to the ``TableLayout`` written for that table. Names are matched
            case-insensitively, without any schema qualifier.
        default_layout: the ``TableLayout`` for permanent tables that have no entry in
            ``layouts``.

    Strip options are applied first, then ``type_map``, ``or_replace``, ``singlestore`` and the
    table layout, so a primary key removed by ``strip_primary_keys`` no longer counts as one when
    ``singlestore.random_shard_key`` decides on ``SHARD KEY ()``. A layout still sees the
    statement's own primary key (the ClickHouse ``ORDER BY`` fallback, the Doris and StarRocks
    ``primary`` model) because it is read before anything is stripped.
    """

    strip_primary_keys: bool = False
    strip_foreign_keys: bool = False
    strip_unique: bool = False
    or_replace: bool = False
    replace_if_not_exists: bool = False
    type_map: Mapping[str, str] = field(default_factory=dict)
    drop_type_parameters: Collection[str] = field(default_factory=frozenset)
    singlestore: SingleStoreKeys | None = None
    strip_not_null: bool = False
    if_not_exists: bool = False
    schema: str | None = None
    table_suffix: str = ""
    table_case: str | None = None
    layouts: Mapping[str, TableLayout] = field(default_factory=dict)
    default_layout: TableLayout | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "type_map",
            MappingProxyType({key.upper(): value for key, value in self.type_map.items()}),
        )
        object.__setattr__(
            self,
            "drop_type_parameters",
            frozenset(name.upper() for name in self.drop_type_parameters),
        )
        object.__setattr__(self, "layouts", frozen_layouts(self.layouts))
        if self.table_case is not None:
            case = self.table_case.lower()
            if case not in ("upper", "lower"):
                raise ValueError(f"table_case must be 'upper' or 'lower', not {self.table_case!r}")
            object.__setattr__(self, "table_case", case)


def shard_key_clause(
    columns: Sequence[str] = (), dialect: DialectType = "singlestore", **opts: t.Any
) -> str:
    """``SHARD KEY (a, b)``, or ``SHARD KEY ()`` for random distribution when no columns are given."""
    return f"SHARD KEY ({_column_list(columns, dialect, opts)})"


def sort_key_clause(
    columns: Sequence[str], dialect: DialectType = "singlestore", **opts: t.Any
) -> str:
    """``SORT KEY (a, b)``, or an empty string when no columns are given."""
    return f"SORT KEY ({_column_list(columns, dialect, opts)})" if columns else ""


def _column_list(columns: Sequence[str], dialect: DialectType, opts: dict[str, t.Any]) -> str:
    return ", ".join(exp.to_identifier(column).sql(dialect, **opts) for column in columns)


_CONSTRAINT_KINDS: dict[str, tuple[type[exp.Expr], ...]] = {
    "strip_primary_keys": (exp.PrimaryKeyColumnConstraint, exp.PrimaryKey),
    "strip_foreign_keys": (exp.Reference, exp.ForeignKey),
    "strip_unique": (exp.UniqueColumnConstraint,),
}


def _strip_constraints(create: exp.Create, kinds: tuple[type[exp.Expr], ...]) -> None:
    schema = create.this
    if not isinstance(schema, exp.Schema):
        return

    for column in schema.find_all(exp.ColumnDef):
        for constraint in list(column.args.get("constraints") or []):
            if isinstance(constraint.args.get("kind"), kinds):
                constraint.pop()

    for item in list(schema.expressions):
        if isinstance(item, kinds):
            item.pop()
        elif isinstance(item, exp.Constraint):
            members = item.expressions
            if members and all(isinstance(member, kinds) for member in members):
                item.pop()


def _data_type_key(data_type: exp.DataType) -> str:
    if data_type.is_type(exp.DType.USERDEFINED):
        return str(data_type.args.get("kind") or "").upper()
    return data_type.this.name


def _map_types(create: exp.Create, options: DdlOptions) -> None:
    schema = create.this
    if not isinstance(schema, exp.Schema) or not options.type_map:
        return

    for column in schema.find_all(exp.ColumnDef):
        kind = column.args.get("kind")
        if not isinstance(kind, exp.DataType):
            continue
        for data_type in list(kind.find_all(exp.DataType)):
            target = options.type_map.get(_data_type_key(data_type))
            if target is None:
                continue
            parameters = (
                []
                if target.upper() in options.drop_type_parameters
                else [parameter.copy() for parameter in data_type.expressions]
            )
            data_type.replace(exp.DataType(this=target, expressions=parameters))


def _is_permanent_table(create: exp.Create) -> bool:
    return str(create.args.get("kind") or "").upper() == "TABLE" and not any(
        isinstance(p, exp.TemporaryProperty) for p in create.find_all(exp.Property)
    )


def _replace(create: exp.Create, options: DdlOptions) -> None:
    if not options.or_replace or not _is_permanent_table(create) or create.args.get("replace"):
        return
    if create.args.get("exists"):
        if not options.replace_if_not_exists:
            return
        create.set("exists", False)
    create.set("replace", True)


def _table_name(create: exp.Create) -> str:
    table = create.this.this if isinstance(create.this, exp.Schema) else create.this
    return table.name.lower() if isinstance(table, exp.Table) else ""


def _has_primary_key(create: exp.Create) -> bool:
    return any(
        isinstance(node, (exp.PrimaryKey, exp.PrimaryKeyColumnConstraint))
        for node in create.this.walk()
    )


def _singlestore(create: exp.Create, keys: SingleStoreKeys, dialect: Dialect, opts: dict) -> None:
    schema = create.this
    if not isinstance(schema, exp.Schema) or not _is_permanent_table(create):
        return
    table = _table_name(create)
    if not table:
        return

    if table in keys.reference_tables:
        create.set("kind", "REFERENCE TABLE")
        return

    defined = {column.name.lower(): column.name for column in schema.find_all(exp.ColumnDef)}

    shard = keys.shard_keys.get(table)
    clauses: list[str] = []
    if shard and all(column.lower() in defined for column in shard):
        clauses.append(shard_key_clause(shard, dialect, **opts))
    elif keys.random_shard_key and not _has_primary_key(create):
        clauses.append(shard_key_clause((), dialect, **opts))

    sort = [column for column in keys.sort_keys.get(table, ()) if column.lower() in defined]
    if sort:
        clauses.append(sort_key_clause(sort, dialect, **opts))

    for clause in clauses:
        schema.append("expressions", exp.var(clause))


def _source_primary_key(create: exp.Create) -> list[str]:
    schema = create.this
    if not isinstance(schema, exp.Schema):
        return []
    columns = [
        column.name
        for column in schema.expressions
        if isinstance(column, exp.ColumnDef)
        and any(
            isinstance(c.args.get("kind"), exp.PrimaryKeyColumnConstraint)
            for c in column.args.get("constraints") or []
        )
    ]
    for key in schema.find_all(exp.PrimaryKey):
        if key.parent is schema or isinstance(key.parent, exp.Constraint):
            return [i.name for e in key.expressions for i in e.find_all(exp.Identifier)]
    return columns


def _strip_not_null(create: exp.Create) -> None:
    schema = create.this
    if not isinstance(schema, exp.Schema):
        return
    for column in schema.find_all(exp.ColumnDef):
        for constraint in list(column.args.get("constraints") or []):
            if isinstance(constraint.args.get("kind"), exp.NotNullColumnConstraint):
                constraint.pop()


def _exists(create: exp.Create, options: DdlOptions) -> None:
    if options.if_not_exists and _is_permanent_table(create) and not create.args.get("replace"):
        create.set("exists", True)


def _qualify(table: exp.Table, options: DdlOptions) -> None:
    name = table.name + options.table_suffix
    if options.table_case:
        name = name.upper() if options.table_case == "upper" else name.lower()
    if name != table.name:
        table.set("this", exp.to_identifier(name, quoted=table.this.args.get("quoted") or None))
    if options.schema and not table.args.get("db"):
        *catalog, schema = options.schema.split(".")
        table.set("db", exp.to_identifier(schema))
        if catalog and not table.args.get("catalog"):
            table.set("catalog", exp.to_identifier(".".join(catalog)))


def _name(create: exp.Create, options: DdlOptions) -> None:
    table = create.this.this if isinstance(create.this, exp.Schema) else create.this
    if not isinstance(table, exp.Table):
        return
    _qualify(table, options)
    for reference in create.find_all(exp.Reference):
        target = reference.this.this if isinstance(reference.this, exp.Schema) else reference.this
        if isinstance(target, exp.Table):
            _qualify(target, options)


def _keeps_array_sizes(target: Dialect) -> bool:
    """DuckDB and PostgreSQL take ``FLOAT[3]``; every other engine's array type has no size."""
    return isinstance(target, DuckDB) or (
        isinstance(target, Postgres) and not isinstance(target, QuestDB)
    )


def _questdb_types(create: exp.Create) -> None:
    """QuestDB column types take no length: ``VARCHAR(25)`` and ``CHAR(25)`` are ``VARCHAR``.

    A ``CHAR`` without a length stays QuestDB's single-character type; ``TIMESTAMP(6)`` loses its precision.
    """
    schema = create.this
    if not isinstance(schema, exp.Schema):
        return
    for column in schema.find_all(exp.ColumnDef):
        kind = column.args.get("kind")
        if not isinstance(kind, exp.DataType):
            continue
        for data_type in kind.find_all(exp.DataType):
            if not data_type.expressions:
                continue
            if data_type.is_type(exp.DType.CHAR, exp.DType.NCHAR, exp.DType.BPCHAR):
                data_type.set("this", exp.DType.VARCHAR)
                data_type.set("expressions", [])
            elif data_type.is_type(exp.DType.VARCHAR, exp.DType.NVARCHAR, exp.DType.TIMESTAMP):
                data_type.set("expressions", [])


_NOT_ENFORCED = "NOT ENFORCED"


def _not_enforced(node: exp.Expr) -> None:
    options = node.args.get("options") or []
    if not any(str(getattr(option, "name", option)).upper() == _NOT_ENFORCED for option in options):
        node.set("options", [*options, exp.var(_NOT_ENFORCED)])


def _bigquery_constraints(create: exp.Create) -> None:
    """BigQuery takes ``PRIMARY KEY`` and ``FOREIGN KEY`` only as table-level, ``NOT ENFORCED`` clauses.

    An inline ``PRIMARY KEY`` or ``REFERENCES`` becomes a table-level constraint, and every key
    constraint is marked ``NOT ENFORCED``, the only mode BigQuery supports.
    """
    schema = create.this
    if not isinstance(schema, exp.Schema):
        return

    primary: list[exp.Expr] = []
    foreign: list[exp.ForeignKey] = []
    for column in schema.find_all(exp.ColumnDef):
        for constraint in list(column.args.get("constraints") or []):
            kind = constraint.args.get("kind")
            if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                primary.append(column.this.copy())
                constraint.pop()
            elif isinstance(kind, exp.Reference):
                foreign.append(
                    exp.ForeignKey(expressions=[column.this.copy()], reference=kind.copy())
                )
                constraint.pop()

    has_table_key = any(
        isinstance(node, exp.PrimaryKey) for node in schema.find_all(exp.PrimaryKey)
    )
    if primary and not has_table_key:
        schema.append("expressions", exp.PrimaryKey(expressions=primary))
    for item in foreign:
        schema.append("expressions", item)

    for key in schema.find_all(exp.PrimaryKey):
        _not_enforced(key)
    for reference in schema.find_all(exp.Reference):
        _not_enforced(reference)


def transpile_ddl(
    sql: str,
    read: DialectType = None,
    write: DialectType = None,
    options: DdlOptions | None = None,
    **opts: t.Any,
) -> list[str]:
    """Translate DDL statements, applying ``options``, and return one string per statement.

    Statements other than ``CREATE TABLE`` are translated unchanged by the options.

    Args:
        sql: the DDL text, one or more statements.
        read: the dialect of ``sql``.
        write: the dialect to write.
        options: the structural changes; defaults to none.
        **opts: generator options passed to ``Expression.sql``, such as ``identify`` or ``pretty``.

    Returns:
        The translated statements.
    """
    options = options or DdlOptions()
    target = Dialect.get_or_raise(write)
    statements: list[str] = []
    for tree in sqlglot.parse(sql, read=read):
        if tree is None:
            continue
        tail: list[str] = []
        if isinstance(tree, exp.Create) and str(tree.args.get("kind") or "").upper() == "TABLE":
            table = _table_name(tree)
            source_primary_key = _source_primary_key(tree)
            kinds: list[type[exp.Expr]] = []
            for name, classes in _CONSTRAINT_KINDS.items():
                if getattr(options, name):
                    kinds.extend(classes)
            if kinds:
                _strip_constraints(tree, tuple(kinds))
            if options.strip_not_null:
                _strip_not_null(tree)
            _map_types(tree, options)
            if isinstance(target, QuestDB):
                _questdb_types(tree)
            _replace(tree, options)
            _exists(tree, options)
            if options.singlestore is not None:
                _singlestore(tree, options.singlestore, target, opts)
            layout = options.layouts.get(table) or options.default_layout
            if layout is not None and _is_permanent_table(tree):
                tail = apply_layout(tree, layout, target, opts, source_primary_key)
            if not _keeps_array_sizes(target) and isinstance(tree.this, exp.Schema):
                drop_array_sizes(list(tree.this.find_all(exp.ColumnDef)))
            if isinstance(target, BigQuery):
                _bigquery_constraints(tree)
            _name(tree, options)
        text = target.generate(tree, copy=False, **opts)
        if tail:
            text = f"{text}{chr(10) if opts.get('pretty') else ' '}" + " ".join(tail)
        statements.append(text)
    return statements
