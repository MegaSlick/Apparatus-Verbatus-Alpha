"""No pipeline stage imports another stage's modules.

Each numbered stage, and the orchestrator, may import its own files, `common/`,
the standard library and declared dependencies; stages exchange only the files
their CONTRACT.md declares. The numbered directory names already make
`import 4_perlector` invalid Python, but a bare import of a uniquely named
sibling module, or a dotted `pipeline.` import, could still cross.

The walk reads `import` and `from` statements and literal `import_module(...)`
and `__import__(...)` calls anywhere in a file, deferred imports included. It
does not resolve a computed module name or a `spec_from_file_location` load;
tests load stage modules that way on purpose, through the root conftest's
`load_stage`. Every stage's entry file is `run.py`, so which `run` a bare
`import run` binds depends on `sys.path` at runtime and is not checked here.

The population is `git ls-files`, so ignored local folders such as `workbench/`
do not change what is checked.
"""

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "pipeline"

# The numbered stages, the Coniector's side branch among them, and the
# orchestrator, which is held to the same rule.
STAGE_DIRECTORIES = (
    "1_exemplar",
    "1_ink_map",
    "2_designator",
    "3_attestatores",
    "4_perlector",
    "4b_coniector",
    "5_recensor",
    "6_archetypus",
    "7_armarium",
    "orchestrator",
)


def repository_python_files() -> list[str]:
    """Every `.py` path that belongs to this repository, from its root.

    Fails closed: a `git ls-files` that errors, or a checkout with no Python in
    it, is a check that could not run, and a check that cannot run is a failure
    rather than a pass.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:  # pragma: no cover - environment
        pytest.fail(
            "git ls-files could not enumerate tracked Python files, so the "
            f"stage import boundary was not checked at all: {error}"
        )
    paths = [path for path in result.stdout.split("\0") if path]
    return sorted(paths)


def _stage_of(path: str) -> str | None:
    """Which stage directory a repository-relative path belongs to, or None."""
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != "pipeline":
        return None
    return parts[1] if parts[1] in STAGE_DIRECTORIES else None


def _own_module_names(stage: str) -> set[str]:
    """The bare module names a stage's own `.py` files provide."""
    return {path.stem for path in (PIPELINE / stage).glob("*.py")}


def _imports_in(path: Path) -> list[tuple[str, str]]:
    """`(root_module, full_module)` for every statically knowable import.

    A relative `from . import x` is reported under the name it binds, and
    `from .pkg import x` under `pkg`, so no import node kind is skipped.
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
        elif isinstance(node, ast.ImportFrom):
            dots = "." * node.level
            if node.module:
                found.append((node.module.split(".")[0], f"{dots}{node.module}"))
            else:
                for alias in node.names:
                    found.append((alias.name.split(".")[0], f"{dots}{alias.name}"))
        elif isinstance(node, ast.Call):
            for module in _literal_import_modules(node):
                found.append((module.split(".")[0], module))
    return found


def _literal_import_modules(node: ast.Call) -> list[str]:
    """Every module a literal dynamic-import call names, the `fromlist` included.

    `__import__("pkg.stage", fromlist=("helper",))` binds the parent but *loads*
    `pkg.stage.helper`, so reporting the first argument alone would let the
    helper arrive under a name no guard here ever sees. The submodules the
    fromlist names are therefore reported beside their parent. `import_module`
    has no fromlist; only `__import__` is read this way.
    """
    function = node.func
    is_import_module = (
        isinstance(function, ast.Name)
        and function.id == "import_module"
        or isinstance(function, ast.Attribute)
        and function.attr == "import_module"
    )
    is_builtin_import = (
        isinstance(function, ast.Name)
        and function.id == "__import__"
        or isinstance(function, ast.Attribute)
        and function.attr == "__import__"
    )
    if not (
        (is_import_module or is_builtin_import)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return []
    parent = node.args[0].value
    modules = [parent]
    if is_builtin_import:
        names = _literal_fromlist(node)
        if names is None:
            modules.append(f"{parent}.{UNVERIFIABLE_FROMLIST}" if parent else UNVERIFIABLE_FROMLIST)
        else:
            for name in names:
                modules.append(f"{parent}.{name}" if parent else name)
    return modules


UNVERIFIABLE_FROMLIST = "<unverifiable fromlist>"


def _literal_fromlist(node: ast.Call) -> list[str] | None:
    """The literal string entries of a `__import__` fromlist, positional or keyword.

    Only literal tuples and lists of literal strings are readable. An absent
    fromlist is an empty list — the call loads the parent and nothing else. A
    fromlist that is present but computed, or that carries a computed element,
    is `None`: what it loads is not statically knowable, and the guard fails
    closed on that rather than narrowing the call to its parent. `"*"` names no
    submodule and is dropped.
    """
    fromlist: ast.expr | None = node.args[3] if len(node.args) > 3 else None
    for keyword in node.keywords:
        if keyword.arg == "fromlist":
            fromlist = keyword.value
    if fromlist is None:
        return []
    if not isinstance(fromlist, ast.Tuple | ast.List):
        return None
    names: list[str] = []
    for element in fromlist.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        if element.value != "*":
            names.append(element.value)
    return names


def test_the_population_covers_every_stage_directory():
    """A population missing a stage would let a violation inside it pass."""
    files = repository_python_files()
    covered = {stage for path in files if (stage := _stage_of(path)) is not None}
    assert covered == set(STAGE_DIRECTORIES), (
        f"expected every stage directory represented, found only {sorted(covered)}"
    )


def test_no_stage_imports_another_stages_uniquely_named_module():
    """A stage may not import a bare module name that exists as a `.py` file
    only inside a different stage's directory."""
    files = repository_python_files()
    modules_by_stage = {stage: _own_module_names(stage) for stage in STAGE_DIRECTORIES}

    checked = 0
    violations: list[str] = []
    for path in files:
        stage = _stage_of(path)
        if stage is None:
            continue
        checked += 1
        own = modules_by_stage[stage]
        for root, full in _imports_in(ROOT / path):
            for other_stage, other_modules in modules_by_stage.items():
                if other_stage == stage or root in own:
                    continue
                # Every stage has a run.py, so "run" cannot be attributed.
                if root != "run" and root in other_modules:
                    violations.append(
                        f"{path} imports {full!r}, which is owned by pipeline/{other_stage}/"
                    )

    assert checked >= 10, f"expected pipeline stage files to be checked, found {checked}"
    assert not violations, "a stage crossed another stage's boundary:\n" + "\n".join(
        sorted(violations)
    )


def test_no_stage_imports_pipeline_by_its_dotted_path():
    """No stage imports `pipeline.something` by its dotted path."""
    files = repository_python_files()
    violations = [
        f"{path} imports {full!r}"
        for path in files
        if _stage_of(path) is not None
        for root, full in _imports_in(ROOT / path)
        if root == "pipeline"
    ]
    assert not violations, "a stage imported pipeline/ by its dotted path:\n" + "\n".join(
        violations
    )
