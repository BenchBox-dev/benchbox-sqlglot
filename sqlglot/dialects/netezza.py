from __future__ import annotations

from sqlglot.dialects.postgres import Postgres
from sqlglot.parsers.netezza import NetezzaParser


class Netezza(Postgres):
    """IBM Netezza Performance Server, whose SQL descends from PostgreSQL.

    Differs from `Postgres` only by reading the SQLite date idioms that Netezza-sourced
    benchmark queries carry (`JULIANDAY`, `DATE('now' [, '-N days'])`) as portable
    date nodes, so every target dialect writes its own spelling.
    """

    Parser = NetezzaParser
