"""``TableLayout``: the physical layout of one table, for ``DdlOptions``.

A layout carries the engine-specific table clauses (storage engine, table format, keys,
partitioning, distribution, properties) as plain data. ``transpile_ddl`` writes the clauses
the target dialect understands; the fields a dialect has no use for are ignored, and a layout
with no field a dialect uses is a ``ValueError``. Nothing here names a benchmark or a table.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType


_TIME_PARTITIONS = ("NONE", "HOUR", "DAY", "WEEK", "MONTH", "YEAR")


def _names(value: str | Sequence[str]) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else tuple(value)


@dataclass(frozen=True)
class TableLayout:
    """Engine-specific clauses for one table. Every field is off (empty) by default.

    Column names and expressions are written as given: a plain identifier is quoted the way
    the target dialect quotes it, anything else (``tuple()``, ``bucket(8, id)``,
    ``toYYYYMM(ts)``) is written verbatim. The fields each dialect reads:

    ================== ====================================================================
    ClickHouse         ``engine``, ``partition_by``, ``sort_by`` (``ORDER BY``),
                       ``primary_key``, ``nullable_columns``, ``properties`` (``SETTINGS``)
    Doris, StarRocks   ``key_model``, ``key_columns``, ``partition_by`` or
                       ``partition_clause``, ``distribution``, ``distribute_by``,
                       ``buckets``, ``sort_by`` (StarRocks ``ORDER BY``), ``properties``
    Redshift           ``distribution``, ``distribute_by`` (``DISTKEY``), ``sort_by``
                       (``SORTKEY``), ``sort_auto``; with ``external``: ``stored_as``,
                       ``location``, ``properties`` (``TABLE PROPERTIES``)
    Spark, Databricks  ``format`` (``USING``), ``stored_as``, ``partition_by``, ``cluster_by``,
                       ``cluster_by_auto``, ``distribute_by`` + ``buckets`` (+ ``sort_by``),
                       ``location``, ``properties`` (``TBLPROPERTIES``)
    Hive, Athena       ``format`` or ``stored_as`` (``STORED AS``), ``partition_by``,
    (external)         ``distribute_by`` + ``buckets`` (+ ``sort_by``), ``external``,
                       ``location``, ``field_delimiter``, ``line_delimiter``, ``properties``
    Trino, Presto      ``format``, ``partition_by`` (``partitioned_by``, or ``partitioning``
                       for ``format="iceberg"``), ``sort_by``, ``distribute_by`` +
                       ``buckets`` (``bucketed_by``), ``properties``
    T-SQL (Synapse)    ``distribution`` (``round_robin``, ``replicate`` or ``hash`` with
                       ``distribute_by``), ``properties``
    PostgreSQL         ``format`` (the table access method, ``USING <format>``)
    QuestDB            ``symbol_columns``, ``timestamp_columns``, ``designated_timestamp``,
                       ``time_partition``
    ================== ====================================================================

    Attributes:
        engine: ClickHouse table engine, written as given (``MergeTree()``).
        format: table format or file format: ``delta``, ``iceberg``, ``parquet``, ``orc``...
        stored_as: Hive-style ``STORED AS`` file format. Dialects that take ``STORED AS`` use
            it in preference to ``format``.
        partition_by: partition columns or expressions, in order.
        partition_clause: a complete ``PARTITION BY ...`` clause written for the target
            dialect, for partitioning that is more than a column list (Doris and StarRocks
            ``RANGE`` and ``LIST``). Used instead of ``partition_by``.
        sort_by: sort columns, in order.
        primary_key: ClickHouse ``PRIMARY KEY`` columns, written after ``ORDER BY``.
        cluster_by: clustering columns (Delta liquid clustering).
        cluster_by_auto: write ``CLUSTER BY AUTO``.
        distribution: ``hash``, ``random``, ``even``, ``all``, ``key``, ``auto``,
            ``round_robin`` or ``replicate``. Empty means ``hash`` when ``distribute_by`` is
            given (Redshift: ``key``) and nothing otherwise.
        distribute_by: distribution or bucketing columns, in order.
        buckets: bucket count of a hash distribution or bucketed table.
        sort_auto: Redshift ``SORTKEY AUTO`` when ``sort_by`` is empty.
        key_model: Doris and StarRocks table model: ``duplicate``, ``unique``, ``aggregate``
            or ``primary``.
        key_columns: the columns the key model should use, as a prefix of the table's
            columns. Columns the statement does not define, columns that follow a missing
            one, and columns of a type that cannot be a key end the prefix; with no usable
            prefix the first keyable column is the key. ``primary`` uses ``key_columns``, or
            the statement's own primary key when empty, and falls back to ``duplicate`` on
            the first column unless those columns are the leading columns.
        location: storage location, written as a string literal.
        external: write ``CREATE EXTERNAL TABLE``.
        field_delimiter: ``ROW FORMAT DELIMITED FIELDS TERMINATED BY`` character.
        line_delimiter: ``LINES TERMINATED BY`` character, with ``field_delimiter``.
        properties: table properties (ClickHouse ``SETTINGS``, Doris and StarRocks
            ``PROPERTIES``, Spark and Hive ``TBLPROPERTIES``, Redshift ``TABLE PROPERTIES``,
            Trino ``WITH``), written in the order given. Values are written as string
            literals, except for ClickHouse ``SETTINGS`` and Trino ``WITH`` where a value
            that is a number, ``true`` or ``false`` or starts with ``ARRAY[`` is written
            as it is.
        nullable_columns: ClickHouse: the columns that are ``Nullable``. Every other column
            is written as a plain type, so a column in ``sort_by``, ``partition_by`` or
            ``primary_key`` that is listed here is a ``ValueError``; columns of the
            statement's own primary key are never ``Nullable``. ``None`` keeps SQLGlot's
            ``Nullable`` wrapping for columns without ``NOT NULL`` and writes a plain type
            for the columns with it.
        symbol_columns: QuestDB: string columns written as ``SYMBOL`` (an interned, indexed-friendly
            type for low-cardinality text). A ``SYMBOL`` is nullable, so ``NOT NULL`` is
            dropped from these columns; names the statement does not define are ignored.
        timestamp_columns: QuestDB: ``DATE`` or ``TIMESTAMP`` columns written as ``TIMESTAMP``
            (QuestDB's ``DATE`` is the legacy millisecond type); names the statement does not
            define, or defines with another type, are ignored.
        designated_timestamp: QuestDB: the column written as ``timestamp(col)``, the table's
            designated timestamp. It is written as ``TIMESTAMP`` and must be a ``DATE`` or
            ``TIMESTAMP`` column of the statement (a ``ValueError`` otherwise).
        time_partition: QuestDB partitioning of the designated timestamp: ``NONE``, ``HOUR``,
            ``DAY``, ``WEEK``, ``MONTH`` or ``YEAR``. Anything but ``NONE`` needs
            ``designated_timestamp``.
    """

    engine: str | None = None
    format: str | None = None
    stored_as: str | None = None
    partition_by: Sequence[str] = ()
    partition_clause: str | None = None
    sort_by: Sequence[str] = ()
    primary_key: Sequence[str] = ()
    cluster_by: Sequence[str] = ()
    cluster_by_auto: bool = False
    distribution: str | None = None
    distribute_by: Sequence[str] = ()
    buckets: int | None = None
    sort_auto: bool = False
    key_model: str | None = None
    key_columns: Sequence[str] = ()
    location: str | None = None
    external: bool = False
    field_delimiter: str | None = None
    line_delimiter: str | None = None
    properties: Mapping[str, str] = field(default_factory=dict)
    nullable_columns: Collection[str] | None = None
    symbol_columns: Sequence[str] = ()
    timestamp_columns: Sequence[str] = ()
    designated_timestamp: str | None = None
    time_partition: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "partition_by",
            "sort_by",
            "primary_key",
            "cluster_by",
            "distribute_by",
            "key_columns",
            "symbol_columns",
            "timestamp_columns",
        ):
            object.__setattr__(self, name, _names(getattr(self, name)))
        object.__setattr__(self, "properties", MappingProxyType(dict(self.properties)))
        if self.distribution is not None:
            object.__setattr__(self, "distribution", self.distribution.lower())
        if self.key_model is not None:
            object.__setattr__(self, "key_model", self.key_model.lower())
        if self.time_partition is not None:
            unit = self.time_partition.upper()
            if unit not in _TIME_PARTITIONS:
                raise ValueError(
                    f"time_partition must be one of {', '.join(_TIME_PARTITIONS)}, "
                    f"not {self.time_partition!r}"
                )
            object.__setattr__(self, "time_partition", unit)
        if self.nullable_columns is not None:
            object.__setattr__(
                self, "nullable_columns", frozenset(c.lower() for c in self.nullable_columns)
            )


def frozen_layouts(mapping: Mapping[str, TableLayout] | None) -> Mapping[str, TableLayout]:
    """Copy ``mapping`` with lower-cased table names, read-only."""
    return MappingProxyType({table.lower(): layout for table, layout in (mapping or {}).items()})


__all__ = ["TableLayout", "frozen_layouts"]
