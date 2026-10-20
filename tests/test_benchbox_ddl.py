import dataclasses
import unittest

from sqlglot import exp, parse_one
from sqlglot.benchbox.ddl import (
    DdlOptions,
    SingleStoreKeys,
    shard_key_clause,
    sort_key_clause,
    transpile_ddl,
)
from tests import engines
from tests.engines import requires_engine

PARENT = "CREATE TABLE parent (id INTEGER NOT NULL, name VARCHAR(25) NOT NULL, PRIMARY KEY (id))"
CHILD = (
    "CREATE TABLE child (\n"
    "  id INTEGER NOT NULL PRIMARY KEY,\n"
    "  parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE,\n"
    "  code VARCHAR(10) UNIQUE,\n"
    "  note CHAR(3),\n"
    "  amount DECIMAL(15,2),\n"
    "  other_id INTEGER CONSTRAINT other_pk PRIMARY KEY,\n"
    "  UNIQUE (code, note),\n"
    "  CONSTRAINT child_pk PRIMARY KEY (id, other_id),\n"
    "  CONSTRAINT child_fk FOREIGN KEY (parent_id) REFERENCES parent (id),\n"
    "  FOREIGN KEY (other_id) REFERENCES parent (id) ON UPDATE NO ACTION,\n"
    "  CHECK (amount > 0)\n"
    ")"
)
ALL_STRIPPED = DdlOptions(strip_primary_keys=True, strip_foreign_keys=True, strip_unique=True)


def ddl(sql, write="duckdb", options=None, read="duckdb", **opts):
    return transpile_ddl(sql, read=read, write=write, options=options, **opts)


class TestDdlOptions(unittest.TestCase):
    def test_options_are_frozen_and_default_off(self):
        options = DdlOptions()
        self.assertEqual(
            [
                options.strip_primary_keys,
                options.strip_foreign_keys,
                options.strip_unique,
                options.or_replace,
                options.replace_if_not_exists,
            ],
            [False] * 5,
        )
        self.assertEqual((dict(options.type_map), set(options.drop_type_parameters)), ({}, set()))
        self.assertIsNone(options.singlestore)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            options.or_replace = True  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            SingleStoreKeys().random_shard_key = False  # type: ignore[misc]

    def test_mappings_are_copied_and_normalized(self):
        source = {"Char": "TEXT"}
        options = DdlOptions(type_map=source, drop_type_parameters=["text"])
        source["Varchar"] = "TEXT"
        self.assertEqual(dict(options.type_map), {"CHAR": "TEXT"})
        self.assertEqual(options.drop_type_parameters, frozenset({"TEXT"}))
        with self.assertRaises(TypeError):
            options.type_map["X"] = "Y"  # type: ignore[index]

        keys = SingleStoreKeys(
            shard_keys={"Orders": "id"},
            sort_keys={"ORDERS": ["id", "ts"]},
            reference_tables=["Lookup"],
        )
        self.assertEqual(dict(keys.shard_keys), {"orders": ("id",)})
        self.assertEqual(dict(keys.sort_keys), {"orders": ("id", "ts")})
        self.assertEqual(keys.reference_tables, frozenset({"lookup"}))


class TestStripConstraints(unittest.TestCase):
    def test_nothing_changes_by_default(self):
        self.assertEqual(
            ddl(CHILD)[0],
            "CREATE TABLE child (id INT NOT NULL PRIMARY KEY, parent_id INT REFERENCES parent (id) "
            "ON DELETE CASCADE, code TEXT UNIQUE, note TEXT, amount DECIMAL(15, 2), "
            "other_id INT CONSTRAINT other_pk PRIMARY KEY, UNIQUE (code, note), "
            "CONSTRAINT child_pk PRIMARY KEY (id, other_id), "
            "CONSTRAINT child_fk FOREIGN KEY (parent_id) REFERENCES parent (id), "
            "FOREIGN KEY (other_id) REFERENCES parent (id) ON UPDATE NO ACTION, CHECK (amount > 0))",
        )

    def test_each_strip_option_removes_only_its_constraints(self):
        for options, expected in (
            (
                DdlOptions(strip_primary_keys=True),
                "CREATE TABLE child (id INT NOT NULL, parent_id INT REFERENCES parent (id) "
                "ON DELETE CASCADE, code TEXT UNIQUE, note TEXT, amount DECIMAL(15, 2), other_id INT, "
                "UNIQUE (code, note), "
                "CONSTRAINT child_fk FOREIGN KEY (parent_id) REFERENCES parent (id), "
                "FOREIGN KEY (other_id) REFERENCES parent (id) ON UPDATE NO ACTION, CHECK (amount > 0))",
            ),
            (
                DdlOptions(strip_foreign_keys=True),
                "CREATE TABLE child (id INT NOT NULL PRIMARY KEY, parent_id INT, code TEXT UNIQUE, "
                "note TEXT, amount DECIMAL(15, 2), other_id INT CONSTRAINT other_pk PRIMARY KEY, "
                "UNIQUE (code, note), CONSTRAINT child_pk PRIMARY KEY (id, other_id), CHECK (amount > 0))",
            ),
            (
                DdlOptions(strip_unique=True),
                "CREATE TABLE child (id INT NOT NULL PRIMARY KEY, parent_id INT REFERENCES parent (id) "
                "ON DELETE CASCADE, code TEXT, note TEXT, amount DECIMAL(15, 2), "
                "other_id INT CONSTRAINT other_pk PRIMARY KEY, "
                "CONSTRAINT child_pk PRIMARY KEY (id, other_id), "
                "CONSTRAINT child_fk FOREIGN KEY (parent_id) REFERENCES parent (id), "
                "FOREIGN KEY (other_id) REFERENCES parent (id) ON UPDATE NO ACTION, CHECK (amount > 0))",
            ),
            (
                ALL_STRIPPED,
                "CREATE TABLE child (id INT NOT NULL, parent_id INT, code TEXT, note TEXT, "
                "amount DECIMAL(15, 2), other_id INT, CHECK (amount > 0))",
            ),
        ):
            with self.subTest(options=options):
                self.assertEqual(ddl(CHILD, options=options)[0], expected)

    def test_table_level_key_first_leaves_no_stray_comma(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE t (PRIMARY KEY (a), a INT, FOREIGN KEY (a) REFERENCES p (a))",
                options=ALL_STRIPPED,
            ),
            ["CREATE TABLE t (a INT)"],
        )

    def test_table_with_only_constraints_collapses(self):
        self.assertEqual(
            ddl("CREATE TABLE t (a INT PRIMARY KEY)", options=ALL_STRIPPED),
            ["CREATE TABLE t (a INT)"],
        )

    def test_composite_and_expression_keys(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE t (a VARCHAR(5), b INT, PRIMARY KEY (a, b))",
                options=DdlOptions(strip_primary_keys=True),
            ),
            ["CREATE TABLE t (a TEXT, b INT)"],
        )

    def test_other_statements_pass_through(self):
        self.assertEqual(
            ddl(
                "DROP TABLE IF EXISTS t; CREATE INDEX i ON t (a); "
                "CREATE VIEW v AS SELECT 1 AS a; INSERT INTO t VALUES (1)",
                options=ALL_STRIPPED,
            ),
            [
                "DROP TABLE IF EXISTS t",
                "CREATE INDEX i ON t(a)",
                "CREATE VIEW v AS SELECT 1 AS a",
                "INSERT INTO t VALUES (1)",
            ],
        )

    def test_source_text_is_not_modified_and_each_call_is_independent(self):
        options = DdlOptions(strip_primary_keys=True)
        first = ddl(PARENT, options=options)
        self.assertEqual(first, ddl(PARENT, options=options))
        self.assertIn("PRIMARY KEY", ddl(PARENT)[0])

    def test_dialects(self):
        for write in ("postgres", "sqlite", "mysql", "snowflake", "bigquery", "databricks"):
            with self.subTest(write=write):
                (sql,) = ddl(CHILD, write=write, options=ALL_STRIPPED)
                tree = parse_one(sql, read=write)
                self.assertFalse(
                    list(
                        tree.find_all(
                            exp.PrimaryKey,
                            exp.PrimaryKeyColumnConstraint,
                            exp.ForeignKey,
                            exp.Reference,
                            exp.UniqueColumnConstraint,
                        )
                    ),
                    sql,
                )
                self.assertIn("CHECK", sql)


class TestOrReplace(unittest.TestCase):
    OPTIONS = DdlOptions(or_replace=True)

    def test_create_table_becomes_create_or_replace(self):
        for write in ("snowflake", "bigquery", "databricks", "duckdb"):
            with self.subTest(write=write):
                (sql,) = ddl("CREATE TABLE t (a INT)", write=write, options=self.OPTIONS)
                self.assertTrue(sql.startswith("CREATE OR REPLACE TABLE t ("), sql)

    def test_other_creates_are_left_alone(self):
        for sql, expected in (
            ("CREATE TABLE IF NOT EXISTS t (a INT)", "CREATE TABLE IF NOT EXISTS t (a INT)"),
            ("CREATE OR REPLACE TABLE t (a INT)", "CREATE OR REPLACE TABLE t (a INT)"),
            ("CREATE TEMPORARY TABLE t (a INT)", "CREATE TEMPORARY TABLE t (a INT)"),
            ("CREATE VIEW v AS SELECT 1 AS a", "CREATE VIEW v AS SELECT 1 AS a"),
            ("DROP TABLE t", "DROP TABLE t"),
        ):
            with self.subTest(sql=sql):
                self.assertEqual(ddl(sql, options=self.OPTIONS), [expected])

    def test_replace_if_not_exists(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE IF NOT EXISTS s.t (a INT)",
                options=DdlOptions(or_replace=True, replace_if_not_exists=True),
            ),
            ["CREATE OR REPLACE TABLE s.t (a INT)"],
        )
        self.assertEqual(
            ddl(
                "CREATE TABLE IF NOT EXISTS t (a INT)",
                options=DdlOptions(replace_if_not_exists=True),
            ),
            ["CREATE TABLE IF NOT EXISTS t (a INT)"],
        )

    def test_create_table_as_select_and_qualified_names(self):
        self.assertEqual(
            ddl("CREATE TABLE d.t AS SELECT 1 AS a", options=self.OPTIONS),
            ["CREATE OR REPLACE TABLE d.t AS SELECT 1 AS a"],
        )

    def test_combined_with_stripping(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE t (a INT PRIMARY KEY)",
                write="snowflake",
                options=DdlOptions(or_replace=True, strip_primary_keys=True),
            ),
            ["CREATE OR REPLACE TABLE t (a INT)"],
        )


class TestTypeMap(unittest.TestCase):
    FIREBOLT_LIKE = DdlOptions(
        type_map={"VARCHAR": "TEXT", "CHAR": "TEXT", "DECIMAL": "NUMERIC"},
        drop_type_parameters=["TEXT"],
    )

    def test_parameters_are_kept_or_dropped(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE t (a VARCHAR(25), b CHAR(1), c DECIMAL(15,2), d DECIMAL, e INT, f VARCHAR)",
                options=self.FIREBOLT_LIKE,
                read="postgres",
            ),
            ["CREATE TABLE t (a TEXT, b TEXT, c NUMERIC(15, 2), d NUMERIC, e INT, f TEXT)"],
        )

    def test_rename_keeps_parameters(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE t (a CHAR(10), b CHAR)",
                options=DdlOptions(type_map={"char": "VARCHAR"}),
                read="postgres",
            ),
            ["CREATE TABLE t (a VARCHAR(10), b VARCHAR)"],
        )

    def test_the_replacement_is_written_as_given_in_every_dialect(self):
        for write in ("postgres", "mysql", "singlestore", "snowflake", "duckdb"):
            with self.subTest(write=write):
                (sql,) = ddl(
                    "CREATE TABLE t (g DECIMAL(15,2))",
                    write=write,
                    options=DdlOptions(type_map={"DECIMAL": "NUMERIC"}),
                    read="postgres",
                )
                self.assertEqual(sql, "CREATE TABLE t (g NUMERIC(15, 2))")

    def test_only_column_types_are_mapped(self):
        self.assertEqual(
            ddl(
                "CREATE TABLE t (a INT DEFAULT CAST(1 AS DECIMAL(3,1)), b VARCHAR(3))",
                options=DdlOptions(type_map={"VARCHAR": "TEXT"}),
                read="postgres",
            ),
            ["CREATE TABLE t (a INT DEFAULT CAST(1 AS DECIMAL(3, 1)), b TEXT(3))"],
        )


class TestSingleStoreKeys(unittest.TestCase):
    KEYS = SingleStoreKeys(
        shard_keys={"fact": "fact_id", "pair": ["a", "b"]},
        sort_keys={"fact": ["fact_id", "seq"], "pair": ["a"], "plain": ["missing"]},
        reference_tables={"lookup"},
    )

    def run_keys(self, sql, keys=None, generator=None, **kwargs):
        return ddl(
            sql,
            write="singlestore",
            options=DdlOptions(singlestore=keys or self.KEYS, **kwargs),
            **(generator or {}),
        )

    def test_shard_and_sort_keys_follow_the_columns(self):
        self.assertEqual(
            self.run_keys("CREATE TABLE fact (fact_id INT, seq INT, v DOUBLE)"),
            [
                "CREATE TABLE fact (fact_id INT, seq INT, v DOUBLE, SHARD KEY (fact_id), SORT KEY (fact_id, seq))"
            ],
        )
        self.assertEqual(
            self.run_keys("CREATE TABLE pair (a INT, b INT)"),
            ["CREATE TABLE pair (a INT, b INT, SHARD KEY (a, b), SORT KEY (a))"],
        )

    def test_table_names_ignore_case_and_schema(self):
        self.assertEqual(
            self.run_keys("CREATE TABLE db.FACT (fact_id INT)"),
            ["CREATE TABLE db.FACT (fact_id INT, SHARD KEY (fact_id), SORT KEY (fact_id))"],
        )

    def test_sort_columns_the_table_lacks_are_dropped(self):
        self.assertEqual(
            self.run_keys("CREATE TABLE fact (fact_id INT)"),
            ["CREATE TABLE fact (fact_id INT, SHARD KEY (fact_id), SORT KEY (fact_id))"],
        )
        self.assertEqual(
            self.run_keys("CREATE TABLE plain (x INT)"),
            ["CREATE TABLE plain (x INT, SHARD KEY ())"],
        )

    def test_random_shard_key_when_there_is_no_key_and_no_primary_key(self):
        self.assertEqual(
            self.run_keys("CREATE TABLE other (x INT)"),
            ["CREATE TABLE other (x INT, SHARD KEY ())"],
        )
        self.assertEqual(
            self.run_keys("CREATE TABLE other (x INT PRIMARY KEY)"),
            ["CREATE TABLE other (x INT PRIMARY KEY)"],
        )
        self.assertEqual(
            self.run_keys("CREATE TABLE other (x INT, PRIMARY KEY (x))"),
            ["CREATE TABLE other (x INT, PRIMARY KEY (x))"],
        )
        self.assertEqual(
            self.run_keys("CREATE TABLE fact (other INT, PRIMARY KEY (other))"),
            ["CREATE TABLE fact (other INT, PRIMARY KEY (other))"],
        )
        self.assertEqual(
            self.run_keys(
                "CREATE TABLE other (x INT)",
                SingleStoreKeys(random_shard_key=False),
            ),
            ["CREATE TABLE other (x INT)"],
        )

    def test_a_stripped_primary_key_no_longer_counts(self):
        self.assertEqual(
            self.run_keys("CREATE TABLE other (x INT PRIMARY KEY)", strip_primary_keys=True),
            ["CREATE TABLE other (x INT, SHARD KEY ())"],
        )

    def test_reference_tables_have_no_keys(self):
        self.assertEqual(
            self.run_keys("CREATE TABLE lookup (k INT, v TEXT)"),
            ["CREATE REFERENCE TABLE lookup (k INT, v TEXT)"],
        )
        self.assertEqual(
            self.run_keys(
                "CREATE TABLE Lookup (k INT, v TEXT, FOREIGN KEY (k) REFERENCES p (k))",
                strip_foreign_keys=True,
            ),
            ["CREATE REFERENCE TABLE Lookup (k INT, v TEXT)"],
        )

    def test_statements_other_than_permanent_tables_are_untouched(self):
        for sql in (
            "CREATE TEMPORARY TABLE fact (fact_id INT)",
            "CREATE TABLE fact AS SELECT 1 AS fact_id",
            "CREATE VIEW fact AS SELECT 1 AS fact_id",
            "DROP TABLE fact",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.run_keys(sql), ddl(sql, write="singlestore"))

    def test_quoting_follows_the_generator_options(self):
        self.assertEqual(
            self.run_keys("CREATE TABLE fact (fact_id INT, seq INT)", generator={"identify": True}),
            [
                "CREATE TABLE `fact` (`fact_id` INT, `seq` INT, SHARD KEY (`fact_id`), SORT KEY (`fact_id`, `seq`))"
            ],
        )

    def test_clause_emitters(self):
        self.assertEqual(shard_key_clause(["a", "b"]), "SHARD KEY (a, b)")
        self.assertEqual(shard_key_clause(), "SHARD KEY ()")
        self.assertEqual(sort_key_clause(["a", "b"]), "SORT KEY (a, b)")
        self.assertEqual(sort_key_clause([]), "")
        self.assertEqual(shard_key_clause(["Odd Name"]), "SHARD KEY (`Odd Name`)")
        self.assertEqual(sort_key_clause(["a"], "postgres", identify=True), 'SORT KEY ("a")')

    def test_keys_are_only_written_for_singlestore_options(self):
        self.assertEqual(
            ddl("CREATE TABLE fact (fact_id INT)", write="duckdb"),
            ["CREATE TABLE fact (fact_id INT)"],
        )


EXECUTABLE = (
    "CREATE TABLE parent (id INTEGER NOT NULL, name VARCHAR(25) NOT NULL, PRIMARY KEY (id), UNIQUE (name));"
    "CREATE TABLE child ("
    "id INTEGER NOT NULL PRIMARY KEY, parent_id INTEGER REFERENCES parent(id), code VARCHAR(10) UNIQUE, "
    "note CHAR(3), amount DECIMAL(15,2), other_id INTEGER, UNIQUE (code, note), "
    "CONSTRAINT child_fk FOREIGN KEY (other_id) REFERENCES parent (id), CHECK (amount > 0));"
    "CREATE TABLE named (a INTEGER NOT NULL, b INTEGER NOT NULL, CONSTRAINT named_key PRIMARY KEY (a, b))"
)
TABLES = ("parent", "child", "named")


class ExecutedSchema:
    ENGINE = ""
    WRITE = ""
    SETUP: tuple[str, ...] = ()

    def create(self, options):
        statements = ddl(EXECUTABLE, write=self.WRITE, options=options)
        with engines.connect(self.ENGINE) as connection:
            for statement in self.SETUP:
                connection.execute(statement)
            for statement in statements:
                connection.execute(statement)
            return self.describe(connection)

    def describe(self, connection):
        raise NotImplementedError

    def test_stripped_schema_has_the_same_columns_and_no_constraints(self):
        plain = self.create(None)
        stripped = self.create(ALL_STRIPPED)
        self.assertEqual(stripped["columns"], plain["columns"])
        self.assertTrue(plain["constraints"])
        self.assertEqual(stripped["constraints"], [])

    def test_each_option_removes_exactly_its_constraint_kind(self):
        plain = self.create(None)["constraints"]
        for fields, kinds in (
            (("strip_foreign_keys",), {"foreign key"}),
            (("strip_unique",), {"unique"}),
            (("strip_primary_keys", "strip_foreign_keys"), {"primary key", "foreign key"}),
        ):
            with self.subTest(options=fields):
                kept = self.create(DdlOptions(**dict.fromkeys(fields, True)))["constraints"]
                self.assertEqual(kept, [c for c in plain if c[0] not in kinds])
                self.assertLess(len(kept), len(plain))


@requires_engine("duckdb")
class TestStrippedSchemaOnDuckDB(ExecutedSchema, unittest.TestCase):
    ENGINE = "duckdb"
    WRITE = "duckdb"

    def describe(self, connection):
        columns = connection.execute(
            "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
        )
        constraints = connection.execute(
            "SELECT lower(constraint_type), table_name, array_to_string(constraint_column_names, ',') "
            "FROM duckdb_constraints() WHERE constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY', 'UNIQUE') "
            "ORDER BY 1, 2, 3"
        )
        return {"columns": columns, "constraints": constraints}


@requires_engine("postgres")
class TestStrippedSchemaOnPostgres(ExecutedSchema, unittest.TestCase):
    ENGINE = "postgres"
    WRITE = "postgres"
    SETUP = tuple(f"DROP TABLE IF EXISTS {table}" for table in ("child", "named", "parent"))

    def describe(self, connection):
        columns = connection.execute(
            "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name IN ('parent', 'child', 'named') "
            "ORDER BY table_name, ordinal_position"
        )
        constraints = connection.execute(
            "SELECT lower(constraint_type), table_name, constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema = current_schema() AND table_name IN ('parent', 'child', 'named') "
            "AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY', 'UNIQUE') ORDER BY 1, 2, 3"
        )
        for table in ("child", "named", "parent"):
            connection.execute(f"DROP TABLE {table}")
        return {"columns": columns, "constraints": constraints}


@requires_engine("sqlite")
class TestStrippedSchemaOnSQLite(ExecutedSchema, unittest.TestCase):
    ENGINE = "sqlite"
    WRITE = "sqlite"

    def describe(self, connection):
        columns = []
        constraints = []
        for table in TABLES:
            for _, name, kind, notnull, _, pk in connection.execute(f"PRAGMA table_info({table})"):
                columns.append((table, name, kind, notnull))
                if pk:
                    constraints.append(("primary key", table, name))
            for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
                constraints.append(("foreign key", table, row[3]))
            for row in connection.execute(f"PRAGMA index_list({table})"):
                if row[3] == "u":
                    constraints.append(("unique", table, row[1]))
        return {"columns": columns, "constraints": sorted(constraints)}


@requires_engine("duckdb")
class TestOrReplaceOnDuckDB(unittest.TestCase):
    def test_second_creation_replaces_the_table(self):
        plain = ddl("CREATE TABLE t (a INT)")[0]
        replacing = ddl("CREATE TABLE t (a INT, b INT)", options=DdlOptions(or_replace=True))[0]
        with engines.connect("duckdb") as connection:
            connection.execute(plain)
            connection.execute("INSERT INTO t VALUES (1)")
            with self.assertRaises(Exception) as raised:
                connection.execute(plain)
            self.assertIn("already exists", str(raised.exception))
            connection.execute(replacing)
            connection.execute(replacing)
            self.assertEqual(connection.execute("SELECT count(*) FROM t"), [(0,)])
            self.assertEqual(
                connection.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 't'"
                ),
                [("a",), ("b",)],
            )


class TypeMapOnEngine:
    ENGINE = ""

    def test_created_column_types(self):
        statements = ddl(
            "CREATE TABLE typed (a VARCHAR(25), b CHAR(3), c DECIMAL(15,2))",
            write=self.ENGINE,
            options=TestTypeMap.FIREBOLT_LIKE,
            read="postgres",
        )
        with engines.connect(self.ENGINE) as connection:
            connection.execute(statements[0])
            rows = connection.execute(self.QUERY)
            connection.execute("DROP TABLE typed")
        self.assertEqual(rows, self.EXPECTED)


@requires_engine("postgres")
class TestTypeMapOnPostgres(TypeMapOnEngine, unittest.TestCase):
    ENGINE = "postgres"
    QUERY = (
        "SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_name = 'typed' AND table_schema = current_schema() "
        "ORDER BY ordinal_position"
    )
    EXPECTED = [
        ("a", "text", None, None, None),
        ("b", "text", None, None, None),
        ("c", "numeric", None, 15, 2),
    ]


@requires_engine("duckdb")
class TestTypeMapOnDuckDB(TypeMapOnEngine, unittest.TestCase):
    ENGINE = "duckdb"
    QUERY = (
        "SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_name = 'typed' ORDER BY ordinal_position"
    )
    EXPECTED = [
        ("a", "VARCHAR", None, None, None),
        ("b", "VARCHAR", None, None, None),
        ("c", "DECIMAL(15,2)", None, 15, 2),
    ]
