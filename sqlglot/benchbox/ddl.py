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
from sqlglot.dialects.dialect import Dialect, DialectType


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

    Strip options are applied first, then ``type_map``, ``or_replace`` and ``singlestore``,
    so a primary key removed by ``strip_primary_keys`` no longer counts as one when
    ``singlestore.random_shard_key`` decides on ``SHARD KEY ()``.
    """

    strip_primary_keys: bool = False
    strip_foreign_keys: bool = False
    strip_unique: bool = False
    or_replace: bool = False
    replace_if_not_exists: bool = False
    type_map: Mapping[str, str] = field(default_factory=dict)
    drop_type_parameters: Collection[str] = field(default_factory=frozenset)
    singlestore: SingleStoreKeys | None = None

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
        if isinstance(tree, exp.Create) and str(tree.args.get("kind") or "").upper() == "TABLE":
            kinds: list[type[exp.Expr]] = []
            for name, classes in _CONSTRAINT_KINDS.items():
                if getattr(options, name):
                    kinds.extend(classes)
            if kinds:
                _strip_constraints(tree, tuple(kinds))
            _map_types(tree, options)
            _replace(tree, options)
            if options.singlestore is not None:
                _singlestore(tree, options.singlestore, target, opts)
        statements.append(target.generate(tree, copy=False, **opts))
    return statements
