import unittest

from tests import engines
from tests.engines import requires_engine

WORDS = "character rank order group key value values partition range rows select table column columns index database schema status type default primary unique current_date current_time current_timestamp interval match natural dense_rank row_number percent_rank cume_dist ntile lead lag total item_id".split()


def attempt(engine, sql):
    try:
        return engines.execute(engine, sql)
    except Exception as e:
        return "ERR " + str(e)[:160].replace("\n", " ")


class Probe:
    ENGINE = ""

    def test_probe(self):
        for word in WORDS:
            print("PROBE", self.ENGINE, "alias", word, attempt(self.ENGINE, f"SELECT 1 AS {word}"), flush=True)
        for sql in (
            "SELECT * FROM (SELECT 1 AS x)",
            "SELECT * FROM (SELECT 1 AS x) AS q",
            "SELECT * FROM (VALUES (1)) AS d(c)",
            "SELECT SUBSTRING('abcdef' FROM 2 FOR 3)",
            'SELECT 1 AS "col"',
            "SELECT 1 AS `col`",
            "SELECT * FROM (SELECT 1) t1, (SELECT 2) t2",
            "WITH a AS (SELECT 1 AS item_id), b AS (SELECT 1 AS item_id) SELECT a.item_id FROM a, b WHERE a.item_id = b.item_id ORDER BY item_id",
            "SELECT CAST('2000-01-01' AS DATE) + INTERVAL 30 DAY",
            "SELECT DATE_ADD(CAST('2000-01-01' AS DATE), INTERVAL 30 DAY)",
            "SELECT 1 WHERE 1 IN (SELECT 1 FROM (SELECT 1 AS a) q) ",
        ):
            print("PROBE", self.ENGINE, sql, "=>", attempt(self.ENGINE, sql), flush=True)


@requires_engine("starrocks")
class StarRocks(Probe, unittest.TestCase):
    ENGINE = "starrocks"


@requires_engine("doris")
class Doris(Probe, unittest.TestCase):
    ENGINE = "doris"
