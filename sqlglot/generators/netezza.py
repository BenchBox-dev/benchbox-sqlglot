from __future__ import annotations

from sqlglot.generators.postgres import PostgresGenerator


class NetezzaGenerator(PostgresGenerator):
    EXACT_MARKED_DIVISION = False
