from __future__ import annotations

from sqlglot.dialects.postgres import Postgres
from sqlglot.generators.questdb import QuestDBGenerator
from sqlglot.parsers.questdb import QuestDBParser


class QuestDB(Postgres):
    """QuestDB, a time-series database that speaks the PostgreSQL wire protocol."""

    NULL_ORDERING = "nulls_are_small"

    Parser = QuestDBParser

    Generator = QuestDBGenerator
