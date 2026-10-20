from sqlglot import exp, parse_one
from sqlglot.dialects.dialect import Dialect, Dialects
from sqlglot.dialects.postgres import Postgres
from tests.dialects.test_dialect import Validator


class TestQuestDB(Validator):
    dialect = "questdb"

    def test_questdb(self):
        self.validate_identity("SELECT CAST(x AS INT) FROM t")
        self.validate_identity("SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b")
        self.validate_identity("SELECT d_date + 5, d_date - 30 FROM t")

        self.validate_all(
            "SELECT CAST(x AS TEXT) || 'a'",
            read={
                "questdb": "SELECT x::TEXT || 'a'",
                "postgres": "SELECT x::TEXT || 'a'",
            },
            write={
                "questdb": "SELECT CAST(x AS TEXT) || 'a'",
                "postgres": "SELECT CAST(x AS TEXT) || 'a'",
                "duckdb": "SELECT CAST(x AS TEXT) || 'a'",
                "spark": "SELECT CAST(x AS STRING) || 'a'",
            },
        )
        self.validate_all(
            "SELECT a FROM t ORDER BY a LIMIT 5 OFFSET 2",
            write={
                "questdb": "SELECT a FROM t ORDER BY a LIMIT 5 OFFSET 2",
                "postgres": "SELECT a FROM t ORDER BY a LIMIT 5 OFFSET 2",
                "sqlite": "SELECT a FROM t ORDER BY a NULLS LAST LIMIT 5 OFFSET 2",
            },
        )

    def test_dialect_is_registered(self):
        dialect = Dialect.get_or_raise("questdb")
        self.assertEqual(type(dialect).__name__, "QuestDB")
        self.assertEqual(Dialects.QUESTDB.value, "questdb")
        self.assertIsInstance(dialect, Postgres)
        self.assertIs(Dialect.get_or_raise(Dialects.QUESTDB).__class__, type(dialect))
        self.assertIsInstance(parse_one("x::INT", read="questdb"), exp.Cast)
