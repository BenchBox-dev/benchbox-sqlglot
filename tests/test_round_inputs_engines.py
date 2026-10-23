"""Execute ``round_inputs_to_decimal`` on DuckDB and PostgreSQL.

Run by the ``in-process`` and ``postgres`` jobs of ``benchbox-engines.yml``. The query shapes are
the FlightData ones: ``ROUND(AVG(delay), 2)`` over ``DOUBLE`` columns. DuckDB, the dialect the
FlightData queries are written in, rounds doubles half away from zero and is the reference.
PostgreSQL has no ``ROUND(double precision, integer)`` and rounds ``ROUND(double precision)`` half
to even, so the rewrite must make it return DuckDB's rows.
"""

import unittest

import sqlglot
from sqlglot.benchbox.rewrites import round_inputs_to_decimal
from tests import engines
from tests.engines import requires_engine

TIES = (
    (1, "0.5"),
    (2, "1.5"),
    (3, "2.5"),
    (4, "-0.5"),
    (5, "-2.5"),
    (6, "0.25"),
    (7, "0.75"),
    (8, "-0.25"),
    (9, "0.125"),
    (10, "0.375"),
)
SOURCE = "FROM (VALUES {}) AS t(id, d)".format(
    ", ".join(f"({i}, CAST({d} AS DOUBLE))" for i, d in TIES)
)
ONE_ARGUMENT = f"SELECT id, ROUND(AVG(d)) AS r0 {SOURCE} GROUP BY id ORDER BY id"
TWO_ARGUMENTS = (
    f"SELECT id, ROUND(AVG(d), 1) AS r1, ROUND(AVG(d), 2) AS r2, ROUND(d, 2) AS direct, "
    f"ROUND(d * 10) AS scaled "
    f"{SOURCE} GROUP BY id, d ORDER BY id"
)
SHARE = (
    "SELECT ROUND(100.0 * SUM(CASE WHEN d > 0 THEN 1 ELSE 0 END) / COUNT(*), 2) AS positive_pct, "
    f"ROUND(AVG(CASE WHEN d > 0 THEN d END), 2) AS avg_positive {SOURCE}"
)
BELOW_TIE = "SELECT ROUND(AVG(d), 2) FROM (VALUES (CAST(0.285 AS DOUBLE))) AS t(d)"
HALF_AWAY_ONE_ARGUMENT = [1.0, 2.0, 3.0, -1.0, -3.0, 0.0, 1.0, -0.0, 0.0, 0.0]


def postgres_sql(sql, rewrite=True):
    tree = sqlglot.parse_one(sql, read="duckdb")
    if rewrite:
        tree = round_inputs_to_decimal(tree)
    return tree.sql("postgres")


def run(engine, sql):
    with engines.connect(engine) as connection:
        return [tuple(float(v) for v in row) for row in connection.execute(sql)]


class TestRoundInputsText(unittest.TestCase):
    def test_postgres_text(self):
        self.assertEqual(
            postgres_sql("SELECT ROUND(AVG(x), 2), ROUND(y) FROM t"),
            "SELECT ROUND(CAST(AVG(x) AS DECIMAL), 2), ROUND(CAST(y AS DECIMAL)) FROM t",
        )

    def test_nested_rounds_and_idempotence(self):
        once = postgres_sql("SELECT ROUND(ROUND(w, 3), 1)")
        self.assertEqual(once, "SELECT ROUND(CAST(ROUND(CAST(w AS DECIMAL), 3) AS DECIMAL), 1)")
        tree = round_inputs_to_decimal(sqlglot.parse_one(once, read="postgres"))
        self.assertEqual(tree.sql("postgres"), once)

    def test_copy(self):
        tree = sqlglot.parse_one("SELECT ROUND(x)")
        round_inputs_to_decimal(tree)
        self.assertEqual(tree.sql(), "SELECT ROUND(x)")
        round_inputs_to_decimal(tree, copy=False)
        self.assertEqual(tree.sql(), "SELECT ROUND(CAST(x AS DECIMAL))")


@requires_engine("duckdb")
class TestRoundReferenceOnDuckDB(unittest.TestCase):
    def test_duckdb_rounds_doubles_half_away_from_zero(self):
        rows = run("duckdb", ONE_ARGUMENT)
        self.assertEqual([r[1] for r in rows], HALF_AWAY_ONE_ARGUMENT)


@requires_engine("duckdb")
@requires_engine("postgres")
class TestRoundInputsOnPostgres(unittest.TestCase):
    def test_rewritten_queries_match_duckdb(self):
        for sql in (ONE_ARGUMENT, TWO_ARGUMENTS, SHARE):
            with self.subTest(sql=sql):
                self.assertEqual(run("postgres", postgres_sql(sql)), run("duckdb", sql))

    def test_generator_already_casts_two_argument_round_of_a_typed_double(self):
        sql = f"SELECT id, ROUND(AVG(d), 2) AS r2 {SOURCE} GROUP BY id ORDER BY id"
        self.assertEqual(run("postgres", postgres_sql(sql, rewrite=False)), run("duckdb", sql))

    def test_two_argument_round_of_an_untyped_double_is_rejected_unrewritten(self):
        with self.assertRaises(Exception) as caught:
            run("postgres", postgres_sql(TWO_ARGUMENTS, rewrite=False))
        self.assertIn("round(double precision, integer)", str(caught.exception))

    def test_one_argument_round_of_a_double_rounds_half_to_even_unrewritten(self):
        rows = run("postgres", postgres_sql(ONE_ARGUMENT, rewrite=False))
        self.assertEqual([r[1] for r in rows][:5], [0.0, 2.0, 2.0, -0.0, -2.0])
        self.assertNotEqual(rows, run("duckdb", ONE_ARGUMENT))

    def test_value_below_a_decimal_tie_follows_the_decimal_conversion(self):
        self.assertEqual(run("duckdb", BELOW_TIE), [(0.28,)])
        self.assertEqual(run("postgres", postgres_sql(BELOW_TIE)), [(0.29,)])


if __name__ == "__main__":
    unittest.main()
