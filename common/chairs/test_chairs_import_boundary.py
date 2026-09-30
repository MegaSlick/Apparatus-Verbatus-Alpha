"""Import boundary: a static check that `common/` never imports a stage.

`common/` knows nothing about stages: stages import it and it never imports back.
The chair package and its tests fail on any import of `pipeline` or `proof`, and
every other module under `common/` fails on any import of `pipeline`, wherever in
a file the import sits.

The check is static because numbering the stage directories only makes a
statement such as `import 4_perlector` invalid Python; a dynamic import or path
manipulation would still cross. Everything below reads source through `ast` and
executes nothing.

Literal dynamic `import_module(...)` and `__import__(...)` calls are checked alongside
ordinary imports.
They are executable imports just as much as a top-level statement is; letting a
constant `pipeline...` string evade the AST walker would make the boundary a naming
convention.

No loop here reports success over an empty population.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "common"
CHAIRS = COMMON / "chairs"

FORBIDDEN_ROOTS = ("pipeline", "proof")


def _imports_in(path: Path) -> list[tuple[str, str]]:
    """`(root_package, full_module)` for every literal absolute import in a file.

    `ast.walk` descends into function and class bodies as well as the module top
    level, so a deliberately deferred import — this package has several — is
    caught exactly like a top-level one. Constant-string calls through either
    `import_module` spelling and the builtin `__import__` are included too.
    Loader aliases and nonliteral names require data-flow analysis and remain
    outside this intentionally small AST check. Relative imports (`from .errors
    import ...`) are intra-package by construction and are not returned.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            found.append((module.split(".")[0], module))
        elif isinstance(node, ast.Call) and _is_literal_import_module(node):
            argument = node.args[0]
            assert isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            found.append((argument.value.split(".")[0], argument.value))
    return found


def _is_literal_import_module(node: ast.Call) -> bool:
    """Whether a call invokes importlib's loader with a literal module string."""
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return False
    if not isinstance(node.args[0].value, str):
        return False
    function = node.func
    is_import_module = (
        isinstance(function, ast.Name)
        and function.id == "import_module"
        or isinstance(function, ast.Attribute)
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
        and function.attr == "import_module"
    )
    return is_import_module or (isinstance(function, ast.Name) and function.id == "__import__")


def _modules() -> list[Path]:
    """Every module production may import — the tests and their plumbing aside.

    Not "every shipped module": `pyproject.toml` includes `common.*` wholesale, so
    the tests and `conftest.py` are installed alongside these. What keeps the fake
    out of production is its own constructor guard, not this list.
    """
    return sorted(
        path
        for path in CHAIRS.glob("*.py")
        if not path.name.startswith("test_") and path.name != "conftest.py"
    )


def test_the_chair_package_never_imports_a_stage_or_a_fixture():
    modules = _modules()
    assert len(modules) >= 7, f"expected the whole chair package under {CHAIRS}, found {modules}"

    violations = [
        f"{path.relative_to(ROOT)} imports {full!r}"
        for path in modules
        for root, full in _imports_in(path)
        if root in FORBIDDEN_ROOTS
    ]
    assert not violations, "common/chairs/ crossed its boundary:\n" + "\n".join(violations)


def test_the_chair_tests_never_import_a_stage_either():
    """A test that reached into `pipeline/` would cross the very boundary it is
    here to guard — and would quietly make `common/` depend on stage code
    through the one file nobody thinks of as code."""
    tests = sorted(CHAIRS.glob("test_*.py")) + [CHAIRS / "conftest.py"]
    assert len(tests) >= 8, f"expected the chair test suite under {CHAIRS}, found {tests}"

    violations = [
        f"{path.relative_to(ROOT)} imports {full!r}"
        for path in tests
        for root, full in _imports_in(path)
        if root in ("pipeline", "proof")
    ]
    assert not violations, "a chair test crossed the boundary:\n" + "\n".join(violations)


def test_nothing_anywhere_under_common_imports_pipeline():
    """`common/chairs/` is one module inside `common/`; the boundary against
    stage code holds for all of `common/`."""
    files = sorted(COMMON.rglob("*.py"))
    assert len(files) >= 20, f"expected the whole of common/ under {COMMON}, found {len(files)}"

    violations = [
        f"{path.relative_to(ROOT)} imports {full!r}"
        for path in files
        for root, full in _imports_in(path)
        if root == "pipeline"
    ]
    assert not violations, "common/ must never import pipeline/, but found:\n" + "\n".join(
        violations
    )


def test_no_production_module_under_common_imports_operations():
    """Stages and `operations/` both import `common/`, so a shared shape such as
    `common/contracts/serving.py` must sit where neither depends on the other's
    package. Tests may import `operations/` to prove a contract on both sides."""
    files = sorted(
        path
        for path in COMMON.rglob("*.py")
        if not path.name.startswith("test_") and path.name != "conftest.py"
    )
    assert len(files) >= 20, f"expected the whole of common/ under {COMMON}, found {len(files)}"

    violations = [
        f"{path.relative_to(ROOT)} imports {full!r}"
        for path in files
        for root, full in _imports_in(path)
        if root == "operations"
    ]
    assert not violations, "common/ must never import operations/, but found:\n" + "\n".join(
        violations
    )


def test_a_literal_dynamic_stage_import_is_detected(tmp_path):
    """A guard must prove its new dynamic branch can become red."""
    source = tmp_path / "dynamic_import.py"
    source.write_text(
        "from importlib import import_module\nimport_module('pipeline.forbidden_stage')\n"
        "__import__('pipeline.another_forbidden_stage')\n",
        encoding="utf-8",
    )
    assert ("pipeline", "pipeline.forbidden_stage") in _imports_in(source)
    assert ("pipeline", "pipeline.another_forbidden_stage") in _imports_in(source)
