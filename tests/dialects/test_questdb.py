from sqlglot import ErrorLevel, UnsupportedError, exp, parse_one
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
                "postgres": "SELECT a FROM t ORDER BY a NULLS FIRST LIMIT 5 OFFSET 2",
                "sqlite": "SELECT a FROM t ORDER BY a LIMIT 5 OFFSET 2",
            },
        )

    def test_dialect_is_registered(self):
        dialect = Dialect.get_or_raise("questdb")
        self.assertEqual(type(dialect).__name__, "QuestDB")
        self.assertEqual(Dialects.QUESTDB.value, "questdb")
        self.assertIsInstance(dialect, Postgres)
        self.assertIs(Dialect.get_or_raise(Dialects.QUESTDB).__class__, type(dialect))
        self.assertIsInstance(parse_one("x::INT", read="questdb"), exp.Cast)

    def test_comma_joins(self):
        self.validate_all(
            "SELECT l.x, o.y FROM a AS l JOIN b AS o ON l.k = o.k WHERE l.q > 10",
            read={
                "postgres": "SELECT l.x, o.y FROM a l, b o WHERE l.k = o.k AND l.q > 10",
                "netezza": "SELECT l.x, o.y FROM a l, b o WHERE l.k = o.k AND l.q > 10",
            },
        )
        # The join predicate leaves the WHERE; with no filter left there is no WHERE at all.
        self.validate_all(
            "SELECT a.x FROM a JOIN b ON a.id = b.id",
            read={"postgres": "SELECT a.x FROM a, b WHERE a.id = b.id"},
        )
        self.validate_all(
            "SELECT * FROM a CROSS JOIN b",
            read={"postgres": "SELECT * FROM a, b"},
        )
        # A comma join with no predicate between its tables stays a cross join.
        self.validate_all(
            "SELECT * FROM a CROSS JOIN b WHERE a.v > 5",
            read={"postgres": "SELECT * FROM a, b WHERE a.v > 5"},
        )
        self.validate_all(
            "SELECT c.x FROM a AS p JOIN b AS q ON p.k = q.k JOIN c ON q.m = c.m WHERE p.v > 1",
            read={
                "postgres": "SELECT c.x FROM a p, b q, c WHERE p.k = q.k AND q.m = c.m AND p.v > 1",
            },
        )
        # A mix of explicit and comma joins keeps its order.
        self.validate_all(
            "SELECT a.x FROM a JOIN b ON a.bid = b.id JOIN c ON b.cid = c.id WHERE a.v > 5",
            read={
                "postgres": "SELECT a.x FROM a JOIN b ON a.bid = b.id, c WHERE b.cid = c.id AND a.v > 5",
            },
        )
        # Joins inside CTEs, derived tables and subqueries are rewritten too.
        self.validate_all(
            "WITH t AS (SELECT a.x FROM a JOIN b ON a.id = b.id) SELECT x FROM t",
            read={"postgres": "WITH t AS (SELECT a.x FROM a, b WHERE a.id = b.id) SELECT x FROM t"},
        )
        self.validate_all(
            "SELECT s.x FROM (SELECT a.x FROM a JOIN b ON a.id = b.id) AS s",
            read={"postgres": "SELECT s.x FROM (SELECT a.x FROM a, b WHERE a.id = b.id) s"},
        )
        self.validate_all(
            "SELECT t.x FROM t WHERE EXISTS(SELECT * FROM a JOIN b ON a.id = b.id)",
            read={
                "postgres": "SELECT t.x FROM t WHERE EXISTS(SELECT * FROM a, b WHERE a.id = b.id)"
            },
        )
        # Explicit joins and single tables are untouched.
        self.validate_identity("SELECT l.x FROM a AS l JOIN b AS o ON l.id = o.id WHERE l.q > 5")
        self.validate_identity("SELECT * FROM a WHERE q > 10")
        self.validate_identity("SELECT a.x FROM a CROSS JOIN b")
        self.validate_identity("SELECT a.x FROM a LEFT JOIN b ON a.id = b.id")

    def test_comma_join_predicates(self):
        # Columns without a qualifier join the first comma join that can take them.
        self.validate_all(
            "SELECT x FROM a JOIN b ON a_k = b_k",
            read={"postgres": "SELECT x FROM a, b WHERE a_k = b_k"},
        )
        self.validate_all(
            "SELECT x FROM a JOIN b ON a_k = c_k JOIN c ON a_k = b_k",
            read={"postgres": "SELECT x FROM a, b, c WHERE a_k = c_k AND a_k = b_k"},
        )
        self.validate_all(
            "SELECT x FROM a JOIN b ON a.k = b.k JOIN c ON b.m = c_m",
            read={"postgres": "SELECT x FROM a, b, c WHERE b.m = c_m AND a.k = b.k"},
        )
        # A qualified predicate whose other table is not joined yet waits for that table.
        self.validate_all(
            "SELECT x FROM a CROSS JOIN b JOIN c ON a.k = c.k WHERE b.k = c.k",
            read={"postgres": "SELECT x FROM a, b, c WHERE a.k = c.k AND b.k = c.k"},
        )
        self.validate_all(
            "SELECT x FROM a JOIN b ON a.k = b.k JOIN c ON b.m = c.m",
            read={"postgres": "SELECT x FROM a, b, c WHERE b.m = c.m AND a.k = b.k"},
        )
        # Only an equality of two columns can be a join predicate.
        self.validate_all(
            "SELECT * FROM a CROSS JOIN b WHERE a.k < b.k OR a.v = 1 AND a.k + 1 = b.k",
            read={"postgres": "SELECT * FROM a, b WHERE a.k < b.k OR a.v = 1 AND a.k + 1 = b.k"},
        )
        # Joins that are not bare comma joins are left alone.
        self.validate_identity(
            "SELECT x FROM a CROSS JOIN b, LATERAL (SELECT 1) AS l WHERE a.k = 1"
        )
        self.validate_identity("SELECT x FROM a LEFT JOIN b ON a.k = b.k WHERE b.k IS NULL")

    def test_subquery_lowering(self):
        self.validate_all(
            "SELECT 1 FROM t LEFT JOIN (SELECT DISTINCT r.k AS _qx_exists_1_k0 FROM r "
            "WHERE NOT r.k IS NULL) AS _qx_exists_1 ON t.k = _qx_exists_1._qx_exists_1_k0 "
            "WHERE _qx_exists_1._qx_exists_1_k0 IS NULL",
            read={"postgres": "SELECT 1 FROM t WHERE NOT EXISTS(SELECT * FROM r WHERE t.k = r.k)"},
        )
        self.validate_all(
            "SELECT 1 FROM t JOIN (SELECT DISTINCT r.k AS _qx_exists_1_k0 FROM r "
            "WHERE NOT r.k IS NULL AND r.v > 1) AS _qx_exists_1 "
            "ON t.k = _qx_exists_1._qx_exists_1_k0",
            read={
                "postgres": "SELECT 1 FROM t WHERE EXISTS(SELECT * FROM r WHERE t.k = r.k AND r.v > 1)"
            },
        )
        self.validate_all(
            "SELECT 1 FROM t JOIN (SELECT r.k AS _qx_exists_1_k0, MIN(r.w) AS _qx_exists_1_lo, "
            "MAX(r.w) AS _qx_exists_1_hi FROM r WHERE NOT r.k IS NULL GROUP BY r.k) AS _qx_exists_1 "
            "ON t.k = _qx_exists_1._qx_exists_1_k0 WHERE NOT t.w IS NULL "
            "AND NOT _qx_exists_1._qx_exists_1_lo IS NULL "
            "AND (t.w <> _qx_exists_1._qx_exists_1_lo OR t.w <> _qx_exists_1._qx_exists_1_hi)",
            read={
                "postgres": "SELECT 1 FROM t WHERE EXISTS(SELECT * FROM r WHERE t.k = r.k AND t.w <> r.w)"
            },
        )
        self.validate_all(
            "SELECT 1 FROM t JOIN (SELECT DISTINCT _qx_in_1_k0 FROM "
            "(SELECT r.k AS _qx_in_1_k0 FROM r WHERE r.v > 1) AS _qx_in_1_s "
            "WHERE NOT _qx_in_1_k0 IS NULL) AS _qx_in_1 ON t.k = _qx_in_1._qx_in_1_k0",
            read={"postgres": "SELECT 1 FROM t WHERE t.k IN (SELECT r.k FROM r WHERE r.v > 1)"},
        )

    def test_subquery_aliases_do_not_collide(self):
        self.validate_all(
            "SELECT 1 FROM t AS _qx_exists_1 JOIN "
            "(SELECT DISTINCT r.k AS _qx_exists_2_k0 FROM r WHERE NOT r.k IS NULL) "
            "AS _qx_exists_2 ON _qx_exists_1.k = _qx_exists_2._qx_exists_2_k0 JOIN "
            "(SELECT DISTINCT r.k AS _qx_exists_3_k0 FROM r WHERE NOT r.k IS NULL) "
            "AS _qx_exists_3 ON _qx_exists_1.k = _qx_exists_3._qx_exists_3_k0",
            read={
                "postgres": "SELECT 1 FROM t AS _qx_exists_1 WHERE "
                "EXISTS(SELECT 1 FROM r WHERE _qx_exists_1.k = r.k) AND "
                "EXISTS(SELECT 1 FROM r WHERE _qx_exists_1.k = r.k)"
            },
        )

    def test_subqueries_that_are_not_lowered(self):
        for sql in (
            "SELECT 1 FROM t WHERE NOT EXISTS(SELECT * FROM r WHERE t.k = r.k AND t.w <> r.w)",
            "SELECT 1 FROM t WHERE t.v > 1 OR EXISTS(SELECT * FROM r WHERE t.k = r.k)",
            "SELECT 1 FROM t WHERE EXISTS(SELECT * FROM r WHERE t.k > r.k)",
            "SELECT 1 FROM t WHERE EXISTS(SELECT * FROM r)",
            "SELECT 1 FROM t WHERE NOT t.k IN (SELECT r.k FROM r)",
            "SELECT 1 FROM t WHERE t.k IN (SELECT r.k FROM r WHERE r.m = t.m)",
            "SELECT 1 FROM t WHERE t.k IN (SELECT r.k FROM r LIMIT 2)",
            "SELECT 1 FROM t WHERE t.k IN (1, 2)",
        ):
            with self.subTest(sql=sql):
                self.validate_identity(sql)

    def test_interval_arithmetic(self):
        self.validate_all(
            "SELECT DATEADD('d', -90, CAST('1998-12-01' AS DATE))",
            read={
                "postgres": "SELECT CAST('1998-12-01' AS DATE) - INTERVAL '90' DAY",
                "netezza": "SELECT CAST('1998-12-01' AS DATE) - INTERVAL '90 DAY'",
                "duckdb": "SELECT CAST('1998-12-01' AS DATE) - INTERVAL 90 DAY",
            },
        )
        self.validate_all(
            "SELECT DATEADD('d', 30, x), DATEADD('M', 3, x), DATEADD('y', 1, x), DATEADD('y', -1, x)",
            read={
                "postgres": "SELECT x + INTERVAL '30' DAY, x + INTERVAL '3' MONTH, "
                "x + INTERVAL '1' YEAR, x - INTERVAL '1' YEAR",
            },
        )
        self.validate_all(
            "SELECT DATEADD('M', 2, x), DATEADD('y', -3, x), DATEADD('d', -1, x)",
            read={
                "postgres": "SELECT x + INTERVAL '2 months', x - INTERVAL '3 years', "
                "x - INTERVAL '1 day'",
            },
        )
        self.validate_all(
            "SELECT DATEADD('d', 0, x), DATEADD('d', 14, a + b)",
            read={"postgres": "SELECT x - INTERVAL '0' DAY, a + b + INTERVAL '14' DAY"},
        )
        self.validate_all(
            "SELECT 1 FROM t WHERE d < DATEADD('y', 1, CAST('1994-01-01' AS DATE))",
            read={"postgres": "SELECT 1 FROM t WHERE d < DATE '1994-01-01' + INTERVAL '1' YEAR"},
        )
        # Units QuestDB's DATEADD is not generated for, and non-literal or fractional amounts,
        # keep the PostgreSQL spelling.
        self.validate_all(
            "SELECT x + INTERVAL '1 WEEK', x + INTERVAL '2 HOUR', x + n * INTERVAL '1 DAY'",
            read={
                "postgres": "SELECT x + INTERVAL '1' WEEK, x + INTERVAL '2' HOUR, x + n * INTERVAL '1' DAY"
            },
        )
        self.validate_identity("SELECT x + 5, x - y")

    def test_substring(self):
        self.validate_all(
            "SELECT SUBSTRING(c, 1, 2), SUBSTRING(c, a + 1, b - 1)",
            read={
                "postgres": "SELECT SUBSTRING(c FROM 1 FOR 2), SUBSTRING(c FROM a + 1 FOR b - 1)",
                "mysql": "SELECT SUBSTRING(c, 1, 2), SUBSTRING(c, a + 1, b - 1)",
                "duckdb": "SELECT SUBSTRING(c, 1, 2), SUBSTRING(c, a + 1, b - 1)",
            },
        )
        # QuestDB needs a length, so a missing one is the length of the string.
        self.validate_all(
            "SELECT SUBSTRING(c, 3, LENGTH(c)), SUBSTRING(UPPER(c), 2, LENGTH(UPPER(c)))",
            read={
                "postgres": "SELECT SUBSTRING(c FROM 3), SUBSTRING(UPPER(c) FROM 2)",
                "mysql": "SELECT SUBSTRING(c, 3), SUBSTRING(UPPER(c), 2)",
            },
        )
        self.validate_identity("SELECT SUBSTRING(c, 1, 2)")

    def test_cte_column_list(self):
        self.validate_all(
            "WITH r AS (SELECT k AS p, SUM(v) AS q FROM t GROUP BY k) SELECT p FROM r",
            read={
                "postgres": "WITH r (p, q) AS (SELECT k, SUM(v) FROM t GROUP BY k) SELECT p FROM r",
            },
        )
        # Existing aliases are replaced; the names come from the list.
        self.validate_all(
            "WITH r AS (SELECT k AS p, v AS q FROM t) SELECT p FROM r",
            read={"postgres": "WITH r (p, q) AS (SELECT k AS a, v AS b FROM t) SELECT p FROM r"},
        )
        # Only the first branch of a set operation names the columns.
        self.validate_all(
            "WITH r AS (SELECT 1 AS p, 2 AS q UNION ALL SELECT 3, 4) SELECT p FROM r",
            read={
                "postgres": "WITH r (p, q) AS (SELECT 1, 2 UNION ALL SELECT 3, 4) SELECT p FROM r"
            },
        )
        self.validate_all(
            "WITH r AS ((SELECT 1 AS p) UNION ALL (SELECT 2)) SELECT p FROM r",
            read={"postgres": "WITH r (p) AS ((SELECT 1) UNION ALL (SELECT 2)) SELECT p FROM r"},
        )
        self.validate_identity("WITH r AS (SELECT k FROM t) SELECT k FROM r")

    def test_cte_column_list_with_nested_parentheses(self):
        # Parentheses in the CTE query, in the column list's neighbours and in a later CTE.
        self.validate_all(
            "WITH a AS (SELECT (1 + (2 * 3)) AS x), "
            "b AS (SELECT SUM(x * (1 - (x / 2))) AS p, (COUNT(*)) AS q FROM a) "
            "SELECT p FROM b",
            read={
                "postgres": "WITH a AS (SELECT (1 + (2 * 3)) AS x), "
                "b (p, q) AS (SELECT SUM(x * (1 - (x / 2))), (COUNT(*)) FROM a) "
                "SELECT p FROM b",
            },
        )
        # Every CTE of a WITH clause, not just the first, and CTEs inside subqueries.
        self.validate_all(
            "WITH a AS (SELECT 1 AS m), b AS (SELECT m AS n FROM a), c AS (SELECT n AS o FROM b) "
            "SELECT o FROM c",
            read={
                "postgres": "WITH a (m) AS (SELECT 1), b (n) AS (SELECT m FROM a), "
                "c (o) AS (SELECT n FROM b) SELECT o FROM c",
            },
        )
        self.validate_all(
            "SELECT * FROM (WITH r AS (SELECT (1 + 2) AS p) SELECT p FROM r) AS s",
            read={"postgres": "SELECT * FROM (WITH r (p) AS (SELECT (1 + 2)) SELECT p FROM r) s"},
        )

    def test_cte_column_list_that_cannot_be_named(self):
        # A star or a different number of projections gives no name to attach the list to.
        for sql in (
            "WITH r (p) AS (SELECT * FROM t) SELECT p FROM r",
            "WITH r (p, q) AS (SELECT 1) SELECT p FROM r",
            "WITH r (p) AS (VALUES (1)) SELECT p FROM r",
        ):
            with self.subTest(sql=sql):
                expression = parse_one(sql, read="postgres")
                with self.assertRaises(UnsupportedError):
                    expression.sql("questdb", unsupported_level=ErrorLevel.RAISE)
                self.assertEqual(
                    expression.sql("questdb", unsupported_level=ErrorLevel.IGNORE),
                    expression.sql("postgres"),
                )

    def test_rewrites_compose(self):
        self.validate_all(
            "WITH rev AS (SELECT k AS sno, 1.0 AS total FROM ps) "
            "SELECT SUBSTRING(c, 1, 2), DATEADD('d', -90, CAST('1998-12-01' AS DATE)) "
            "FROM c JOIN o ON c.k = o.k",
            read={
                "postgres": "WITH rev (sno, total) AS (SELECT k, 1.0 FROM ps) "
                "SELECT SUBSTRING(c FROM 1 FOR 2), CAST('1998-12-01' AS DATE) - INTERVAL '90' DAY "
                "FROM c, o WHERE c.k = o.k",
            },
        )
        sql = (
            "SELECT l.x FROM a l, b o WHERE l.k = o.k AND o.d > "
            "CAST('1998-12-01' AS DATE) - INTERVAL '90' DAY"
        )
        once = parse_one(sql, read="postgres").sql("questdb")
        self.assertEqual(parse_one(once, read="questdb").sql("questdb"), once)
