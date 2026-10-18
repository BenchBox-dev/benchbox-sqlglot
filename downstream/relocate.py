from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = "benchbox_sqlglot"
DYNAMIC_PATHS = {
    "dialects/__init__.py": {"sqlglot.dialects."},
    "dialects/dialect.py": {"sqlglot.dialects", "sqlglot.dialects."},
    "optimizer/__init__.py": {"sqlglot.optimizer.optimizer", "sqlglot.optimizer.scope"},
}
RETAINED_STRINGS = {
    "_typing.py": {"sqlglot.exp.Binary", "sqlglot.exp.Expr", "sqlglot.exp.Func"},
    "expressions/core.py": {"sqlglot.meta", "sqlglot.anonymous"},
}
SUITE_EXCLUSIONS = {
    "test_downstream_contract.py": "Checks source manifest and workflow files, not the installed runtime.",
    "test_downstream_distribution.py": "Checks release tooling in the source repository.",
    "test_downstream_refresh.py": "Checks source Git refresh operations.",
    "test_integration_loader.py": "Loads the optional private upstream integration repository.",
    "sqlglot": "Private integration discovery shim.",
    "gen_fixtures.py": "Source fixture generator, not a test module.",
}


def module_name(name: str) -> str:
    return PACKAGE + name[len("sqlglot") :] if name.split(".")[0] == "sqlglot" else name


def import_statement(node: ast.Import) -> str:
    imports = []
    bind_root = False
    for alias in node.names:
        name = module_name(alias.name)
        asname = alias.asname
        if name != alias.name and not asname:
            if alias.name == "sqlglot":
                asname = "sqlglot"
            else:
                bind_root = True
        imports.append(name + (f" as {asname}" if asname else ""))
    result = "import " + ", ".join(imports)
    return result + (f"; import {PACKAGE} as sqlglot" if bind_root else "")


def relocate(source: str, path: str, *, suite: bool = False) -> str:
    tree = ast.parse(source)
    data = source.encode()
    lines = data.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits: list[tuple[int, int, bytes]] = []

    def edit(node: ast.stmt | ast.expr, replacement: str) -> None:
        assert node.end_lineno is not None and node.end_col_offset is not None
        edits.append(
            (
                offsets[node.lineno - 1] + node.col_offset,
                offsets[node.end_lineno - 1] + node.end_col_offset,
                replacement.encode(),
            )
        )

    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    joined_constants = {
        id(child)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for child in node.values
        if isinstance(child, ast.Constant)
    }
    logger_strings = {
        id(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getLogger"
        and node.args
    }
    allowed = DYNAMIC_PATHS.get(path, set())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(module_name(alias.name) != alias.name for alias in node.names):
                edit(node, import_statement(node))
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if module_name(node.module) != node.module:
                segment = ast.get_source_segment(source, node)
                assert segment is not None
                edit(node, segment.replace(node.module, module_name(node.module), 1))
        elif isinstance(node, ast.JoinedStr):
            values = [v.value for v in node.values if isinstance(v, ast.Constant)]
            matches = [v for v in values if isinstance(v, str) and v.startswith("sqlglot.")]
            if matches:
                if not suite and any(v not in allowed for v in matches):
                    raise ValueError(f"Unreviewed dynamic import string in {path}: {matches}")
                segment = ast.get_source_segment(source, node)
                assert segment is not None
                edit(node, segment.replace("sqlglot.", PACKAGE + "."))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if id(node) in joined_constants:
                continue
            if id(node) in docstrings:
                updated = re.sub(
                    r"(?m)(>>>\s+)import sqlglot\b",
                    rf"\1import {PACKAGE} as sqlglot",
                    value,
                )
                updated = re.sub(r"(?m)(>>>\s+from )sqlglot\b", rf"\1{PACKAGE}", updated)
            elif suite:
                updated = value.replace("<class 'sqlglot.", f"<class '{PACKAGE}.")
                if updated.startswith("sqlglot."):
                    updated = module_name(updated)
                if updated.startswith("import sqlglot;"):
                    updated = updated.replace("import sqlglot;", f"import {PACKAGE} as sqlglot;", 1)
            elif value in allowed:
                updated = module_name(value)
            else:
                updated = value
                if value.startswith("sqlglot.") or value == "sqlglot":
                    retained = value in RETAINED_STRINGS.get(path, set())
                    if not retained and id(node) not in logger_strings:
                        raise ValueError(f"Unreviewed namespace string in {path}: {value!r}")
            if updated != value:
                edit(node, repr(updated))
    last_start = len(data)
    for start, end, replacement in sorted(edits, reverse=True):
        if end > last_start:
            raise ValueError(f"Overlapping relocation edits in {path}")
        data = data[:start] + replacement + data[end:]
        last_start = start
    result = data.decode()
    for node in ast.walk(ast.parse(result)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or ""]
        else:
            continue
        if any(n == "sqlglot" or n.startswith("sqlglot.") for n in names):
            raise ValueError(f"Residual upstream import in {path}")
    return result


def stage_suite(root: Path, destination: Path) -> None:
    for path in sorted((root / "tests").rglob("*")):
        relative = path.relative_to(root / "tests")
        if not path.is_file() or any(part in SUITE_EXCLUSIONS for part in relative.parts):
            continue
        if "__pycache__" in relative.parts:
            continue
        if relative.parts[0] != "fixtures" and path.suffix != ".py":
            continue
        target = destination / "tests" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".py":
            target.write_text(relocate(path.read_text(), relative.as_posix(), suite=True))
        else:
            target.write_bytes(path.read_bytes())
