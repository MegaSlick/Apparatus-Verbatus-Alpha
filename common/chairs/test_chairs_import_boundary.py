"""Spec 02, test 8 — Import boundary. "The static import-boundary test covers
`common/chairs/`."

`common/README.md`, verbatim: "It knows nothing about stages. Stages import it;
it never imports back. Add an executable import-boundary check when real modules
exist; a placeholder declaration before code would prove nothing."
`common/chairs/` is that real module, so this is that check.

`pipeline/README.md` names why it has to be static: numbering the stage
directories makes `import 4_perlector` invalid Python, "a useful deterrent... but
not a complete boundary: dynamic imports and path manipulation can still cross
it." Everything below reads source through `ast` and executes nothing.

Literal dynamic `import_module(...)` calls are checked alongside ordinary imports.
They are executable imports just as much as a top-level statement is; letting a
constant `pipeline...` string evade the AST walker would make the boundary a naming
convention. The one allowed deferred hub import is counted explicitly below.

Meta-invariant #88: no loop here reports success over an empty population.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "common"
CHAIRS = COMMON / "chairs"

# `huggingface_hub` is on this list for a reason of its own. The whole package
# must import, parse and run offline with the dependency absent, so the one
# module allowed to reach it does so inside a function — and a top-level import
# anywhere would turn every offline test into a dependency check.
FORBIDDEN_ROOTS = ("pipeline", "proof")


def _imports_in(path: Path) -> list[tuple[str, str]]:
    """`(root_package, full_module)` for every literal absolute import in a file.

    `ast.walk` descends into function and class bodies as well as the module top
    level, so a deliberately deferred import — this package has several — is
    caught exactly like a top-level one. Constant-string calls through either
    `import_module` spelling are included too. Relative imports (`from .errors
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
    return (
        isinstance(function, ast.Name)
        and function.id == "import_module"
        or isinstance(function, ast.Attribute)
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
        and function.attr == "import_module"
    )


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
    """The wider rule `common/README.md` states, made executable while a file is
    open that can state it. `common/chairs/` is one module inside `common/`; the
    boundary belongs to all of it."""
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


def test_the_hub_is_reachable_from_exactly_one_module_and_only_inside_a_function():
    """The dependency has one door. Naming it here means a second one — a
    convenience import in `manifests.py`, say — fails this test rather than
    silently making `resolve()` require a network client to be installed."""
    doors = {
        path.name: [full for root, full in _imports_in(path) if root == "huggingface_hub"]
        for path in _modules()
    }
    reaching = {name: found for name, found in doors.items() if found}
    assert reaching == {"registry.py": ["huggingface_hub"]}, (
        "huggingface_hub must be reached only by registry.py's one deferred fetcher door; found "
        + repr(reaching)
    )

    # The one door is still there, and it is `importlib` inside a function body:
    # importing this package therefore cannot pull the dependency in, which is
    # what lets every test above run with it absent.
    assert 'import_module("huggingface_hub")' in (CHAIRS / "registry.py").read_text(
        encoding="utf-8"
    ), "the production fetcher no longer names the one seam this test is about"


def test_a_literal_dynamic_stage_import_is_detected(tmp_path):
    """A guard must prove its new dynamic branch can become red."""
    source = tmp_path / "dynamic_import.py"
    source.write_text(
        "from importlib import import_module\nimport_module('pipeline.forbidden_stage')\n",
        encoding="utf-8",
    )
    assert ("pipeline", "pipeline.forbidden_stage") in _imports_in(source)
