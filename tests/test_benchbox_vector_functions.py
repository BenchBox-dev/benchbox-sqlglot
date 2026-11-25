"""DuckDB's ``ARRAY_DISTANCE`` and ``ARRAY_COSINE_SIMILARITY`` read as expression nodes and written for SingleStore.

SingleStore has ``EUCLIDEAN_DISTANCE`` and ``DOT_PRODUCT`` but no cosine function, so the similarity is
written as ``DOT_PRODUCT(a, b) / NULLIF(SQRT(DOT_PRODUCT(a, a)) * SQRT(DOT_PRODUCT(b, b)), 0)``.
SingleStore has no engine in CI (``downstream/ENGINE-GAPS.md``): its text is asserted, and the same
formula is executed on DuckDB (``ARRAY_INNER_PRODUCT`` is its ``DOT_PRODUCT``) next to the native
function to show the two agree.
"""

import unittest

import sqlglot
from sqlglot import exp, parse_one
from tests import engines
from tests.engines import requires_engine

COSINE = "array_cosine_similarity(v.embedding, q.query_vector)"
DISTANCE = "array_distance(v.embedding, q.query_vector)"
SINGLESTORE_COSINE = (
    "DOT_PRODUCT(v.embedding, q.query_vector) / NULLIF(SQRT(DOT_PRODUCT(v.embedding, v.embedding)) "
    "* SQRT(DOT_PRODUCT(q.query_vector, q.query_vector)), 0)"
)


def translate(sql, dialect, read="duckdb"):
    return sqlglot.transpile(sql, read=read, write=dialect)[0]


class TestParsing(unittest.TestCase):
    def test_nodes(self):
        tree = parse_one(f"SELECT {COSINE} AS s, {DISTANCE} AS d", read="duckdb")
        self.assertIsInstance(tree.find(exp.CosineSimilarity), exp.CosineSimilarity)
        self.assertIsInstance(tree.find(exp.EuclideanDistance), exp.EuclideanDistance)
        self.assertEqual(tree.find(exp.CosineSimilarity).this.sql(), "v.embedding")
        self.assertEqual(tree.find(exp.EuclideanDistance).expression.sql(), "q.query_vector")
        self.assertIsNone(tree.find(exp.Anonymous))

    def test_list_functions_keep_their_nodes(self):
        tree = parse_one("SELECT list_distance(a, b), list_cosine_distance(a, b)", read="duckdb")
        self.assertIsNone(tree.find(exp.EuclideanDistance).args.get("array"))
        self.assertIsInstance(tree.find(exp.CosineDistance), exp.CosineDistance)


class TestDuckDBRoundTrip(unittest.TestCase):
    def test_array_names_survive(self):
        self.assertEqual(
            translate(f"SELECT {COSINE}, {DISTANCE} FROM v", "duckdb"),
            "SELECT ARRAY_COSINE_SIMILARITY(v.embedding, q.query_vector), "
            "ARRAY_DISTANCE(v.embedding, q.query_vector) FROM v",
        )

    def test_list_names_survive(self):
        self.assertEqual(
            translate("SELECT list_distance(a, b), list_cosine_distance(a, b)", "duckdb"),
            "SELECT LIST_DISTANCE(a, b), LIST_COSINE_DISTANCE(a, b)",
        )


class TestSingleStore(unittest.TestCase):
    def test_distance(self):
        self.assertEqual(
            translate(f"SELECT {DISTANCE} AS d", "singlestore"),
            "SELECT EUCLIDEAN_DISTANCE(v.embedding, q.query_vector) AS d",
        )

    def test_cosine_similarity(self):
        self.assertEqual(
            translate(f"SELECT {COSINE} AS similarity", "singlestore"),
            f"SELECT {SINGLESTORE_COSINE} AS similarity",
        )

    def test_a_query(self):
        sql = (
            "SELECT v.id, array_cosine_similarity(v.embedding, q.query_vector) AS similarity "
            "FROM vectors v CROSS JOIN (SELECT query_vector FROM vector_queries WHERE query_id = 1) q "
            "ORDER BY similarity DESC LIMIT 10"
        )
        self.assertEqual(
            translate(sql, "singlestore"),
            f"SELECT v.id, {SINGLESTORE_COSINE} AS similarity FROM vectors AS v CROSS JOIN "
            "(SELECT query_vector FROM vector_queries WHERE query_id = 1) AS q "
            "ORDER BY similarity DESC LIMIT 10",
        )

    def test_the_formula_keeps_its_precedence_inside_an_expression(self):
        self.assertEqual(
            translate(f"SELECT 1 - {COSINE} AS dist", "singlestore"),
            f"SELECT 1 - ({SINGLESTORE_COSINE}) AS dist",
        )
        self.assertEqual(
            translate(f"SELECT 2 * {COSINE} AS x", "singlestore"),
            f"SELECT 2 * ({SINGLESTORE_COSINE}) AS x",
        )
        self.assertEqual(
            translate(f"SELECT -{COSINE} AS x", "singlestore"),
            f"SELECT -({SINGLESTORE_COSINE}) AS x",
        )

    def test_operands_are_any_expression(self):
        self.assertEqual(
            translate("SELECT array_cosine_similarity(COALESCE(a, b), c) AS s", "singlestore"),
            "SELECT DOT_PRODUCT(COALESCE(a, b), c) / NULLIF(SQRT(DOT_PRODUCT(COALESCE(a, b), "
            "COALESCE(a, b))) * SQRT(DOT_PRODUCT(c, c)), 0) AS s",
        )

    def test_source_tree_is_not_modified(self):
        tree = parse_one(f"SELECT 1 - {COSINE}", read="duckdb")
        before = tree.sql("duckdb")
        tree.sql("singlestore")
        self.assertEqual(tree.sql("duckdb"), before)

    def test_singlestore_reads_its_own_functions(self):
        self.assertEqual(
            translate("SELECT EUCLIDEAN_DISTANCE(a, b)", "singlestore", "singlestore"),
            "SELECT EUCLIDEAN_DISTANCE(a, b)",
        )


@requires_engine("duckdb")
class TestEquivalentOnDuckDB(unittest.TestCase):
    """The SingleStore formula returns the native cosine similarity and Euclidean distance."""

    def setUp(self):
        super().setUp()
        self.connection = engines.connect("duckdb")
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "CREATE TABLE vectors (id INT, embedding FLOAT[3], query_vector FLOAT[3])"
        )
        self.connection.execute(
            "INSERT INTO vectors VALUES (1, [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]), "
            "(2, [0.5, 0.5, 0.0], [1.0, 1.0, 0.0]), (3, [-1.0, 2.0, 3.0], [1.0, 1.0, 0.0]), "
            "(4, [0.0, 0.0, 0.0], [1.0, 1.0, 0.0])"
        )

    def singlestore_on_duckdb(self, sql):
        # DuckDB's ARRAY_INNER_PRODUCT is SingleStore's DOT_PRODUCT
        return self.connection.execute(
            translate(sql, "singlestore").replace("DOT_PRODUCT(", "ARRAY_INNER_PRODUCT(")
        )

    def test_cosine_formula_matches_the_function(self):
        sql = f"SELECT id, {COSINE.replace('v.', '').replace('q.', '')} FROM vectors WHERE id < 4 ORDER BY id"
        native = self.connection.execute(sql)
        rows = self.singlestore_on_duckdb(sql)
        self.assertEqual([r[0] for r in rows], [1, 2, 3])
        for (_, expected), (_, actual) in zip(native, rows):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_a_zero_vector_gives_null_not_an_error(self):
        rows = self.singlestore_on_duckdb(
            "SELECT array_cosine_similarity(embedding, query_vector) FROM vectors WHERE id = 4"
        )
        self.assertEqual(rows, [(None,)])

    def test_distance_is_the_function(self):
        (row,) = self.connection.execute(
            "SELECT array_distance(embedding, query_vector) FROM vectors WHERE id = 3"
        )
        self.assertAlmostEqual(row[0], (4 + 1 + 9) ** 0.5, places=5)


if __name__ == "__main__":
    unittest.main()
