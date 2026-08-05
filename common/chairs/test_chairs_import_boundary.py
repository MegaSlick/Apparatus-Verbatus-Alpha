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

Literal dynamic `import_module(...)` and `__import__(...)` calls are checked alongside
ordinary imports.
They are executable imports just as much as a top-level statement is; letting a
constant `pipeline...` string evade the AST walker would make the boundary a naming
convention. The one allowed deferred hub import is counted explicitly below.

Meta-invariant #88: no loop here reports success over an empty population.
"""

import ast
from pathlib import Path

import pytest

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


def _is_hub_loader(node: ast.AST) -> bool:
    """A literal `import_module("huggingface_hub")` call, however it is nested."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "import_module"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "huggingface_hub"
    )


def _signature_parts(node: ast.AST) -> list[ast.AST]:
    """The parts of a `def` that run where the `def` sits, not when it is called.

    Argument defaults, decorators and annotations are all evaluated at the moment
    the function is *defined*. `ast.walk` over a `FunctionDef` descends into them
    exactly as it descends into the body, so
    `def fetch(client=import_module("huggingface_hub")):` reads as function-scoped
    while in fact running on import — which is the one thing this file exists to
    forbid.
    """
    parts: list[ast.AST] = list(getattr(node, "decorator_list", []))
    arguments = node.args
    parts += [default for default in arguments.defaults if default is not None]
    parts += [default for default in arguments.kw_defaults if default is not None]
    for argument in arguments.posonlyargs + arguments.args + arguments.kwonlyargs:
        if argument.annotation is not None:
            parts.append(argument.annotation)
    if getattr(node, "returns", None) is not None:
        parts.append(node.returns)
    return parts


def _hub_loader_calls(source: str) -> list[tuple[str, bool]]:
    """`(where, deferred)` per hub loader call. `deferred` means "only when called".

    Deliberately not `ast.walk`: the question is not "does a function enclose this
    call" but "does importing this module run it", and those differ wherever a
    signature is evaluated.
    """
    found: list[tuple[str, bool]] = []

    def scan(node: ast.AST, deferred: bool, where: str) -> None:
        if _is_hub_loader(node):
            found.append((where, deferred))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            name = getattr(node, "name", "<lambda>")
            signature_scope = f"{where} -> signature of {name}"
            for part in _signature_parts(node):
                scan(part, deferred, signature_scope)
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            for statement in body:
                scan(statement, True, f"{where} -> body of {name}")
            return
        for child in ast.iter_child_nodes(node):
            scan(child, deferred, where)

    scan(ast.parse(source), False, "module level")
    return found


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
    #
    # Parsed, not string-matched. A substring test proved only that the call text
    # appears somewhere in the file, so moving the very same call to module level —
    # the one change this test exists to catch, because it makes importing the
    # package require the optional dependency — would have kept it green. Scope is
    # the claim, so scope is what is asserted.
    calls = _hub_loader_calls((CHAIRS / "registry.py").read_text(encoding="utf-8"))
    assert calls, (
        "registry.py no longer reaches huggingface_hub through a literal "
        '`import_module("huggingface_hub")` call; either the seam is gone or it is '
        "spelled in a way this boundary check can no longer see"
    )
    at_import = [where for where, deferred in calls if not deferred]
    assert not at_import, (
        "registry.py reaches huggingface_hub at import time, which makes importing this "
        f"package require the optional dependency: {at_import}"
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


HUB_LOADER = 'import_module("huggingface_hub")'

DEFERRED_SPELLINGS = [
    f"def fetch():\n    client = {HUB_LOADER}\n",
    f"async def fetch():\n    client = {HUB_LOADER}\n",
    f"def outer():\n    def inner():\n        return {HUB_LOADER}\n",
    f"class Fetcher:\n    def build(self):\n        return {HUB_LOADER}\n",
    f"def fetch():\n    loader = lambda: {HUB_LOADER}\n    return loader\n",
]

AT_IMPORT_SPELLINGS = [
    # The plain case the substring check could not see.
    f"client = {HUB_LOADER}\n",
    # Every one of these sits syntactically inside a `def`, and every one of them
    # runs the moment the module is imported.
    f"def fetch(client={HUB_LOADER}):\n    return client\n",
    f"def fetch(*, client={HUB_LOADER}):\n    return client\n",
    f"@{HUB_LOADER}.cache\ndef fetch():\n    return None\n",
    f"def fetch() -> {HUB_LOADER}.Client:\n    return None\n",
    f"def fetch(client: {HUB_LOADER}.Client = None):\n    return client\n",
    f"class Fetcher:\n    client = {HUB_LOADER}\n",
]


@pytest.mark.parametrize("source", DEFERRED_SPELLINGS)
def test_a_hub_loader_that_runs_only_when_called_is_read_as_deferred(source):
    calls = _hub_loader_calls(source)

    assert calls, f"the loader call was not found at all in:\n{source}"
    assert all(deferred for _where, deferred in calls), (
        f"a call that only runs when the function is called was read as import-time:\n{source}"
    )


@pytest.mark.parametrize("source", AT_IMPORT_SPELLINGS)
def test_a_hub_loader_that_runs_on_import_is_caught_however_it_is_spelled(source):
    """A guard must prove its new branch can become red.

    Six of these seven sit inside a `def`, which is what made the first version of
    this check wrong: `ast.walk` descends into defaults, decorators and annotations
    exactly as it descends into the body, so each would have been recorded as
    function-scoped while running on import — the precise failure the check exists
    to prevent.
    """
    calls = _hub_loader_calls(source)

    assert calls, f"the loader call was not found at all in:\n{source}"
    assert any(not deferred for _where, deferred in calls), (
        f"an import-time call was read as deferred:\n{source}"
    )
