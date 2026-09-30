"""The door-private modules: nothing outside this stage may import them.

No later stage has an API to re-render a page, because the render module is
door-private; this test makes that claim executable. It follows the shape of
`common/chairs/test_chairs_import_boundary.py`, adapted for a non-package directory
where every import of a sibling module is a bare name (`import pdf_render`) rather
than a dotted path, since `1_exemplar` cannot itself be a Python package name.

The population is `git ls-files --cached --others --exclude-standard`: everything
git tracks, plus everything untracked that is not gitignored. That is the same set
on every checkout, where a filesystem walk would also parse gitignored scratch that
exists on one machine and not another, and it still catches a violation written a
minute ago and not yet staged.

An empty population fails the check rather than passing it, and a `git ls-files`
that cannot run is a failed check rather than an empty one.
"""

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The only files permitted to import each door-private module at all: the door and
# the admission module themselves, plus the direct test suites that exercise them by
# design. Everything else in the repository is "later" or "elsewhere". The two
# allow-lists differ — `admission.py` imports the validators and must never import
# the renderer — so this is a table, not a copied test.
DOOR_PRIVATE_MODULES = {
    "pdf_render": frozenset(
        {
            "pipeline/1_exemplar/door.py",
            "pipeline/1_exemplar/test_pdf_render.py",
            "pipeline/1_exemplar/test_render_config.py",
        }
    ),
    "image_formats": frozenset(
        {
            "pipeline/1_exemplar/admission.py",
            "pipeline/1_exemplar/door.py",
            "pipeline/1_exemplar/pdf_render.py",
            "pipeline/1_exemplar/test_admission.py",
            "pipeline/1_exemplar/test_image_formats.py",
            "pipeline/1_exemplar/test_pdf_render.py",
            "pipeline/1_exemplar/test_door.py",
        }
    ),
}

# Tracked but deliberately not parsed. `cleanroom/` holds presumed-contaminated
# drafts nobody has reviewed yet — `pyproject.toml` excludes it from ruff and pytest
# for the same reason, and parsing one here would be this test reading contaminated
# code. Nothing in it can import anything either, because nothing in it runs.
EXCLUDED_PREFIXES = ("cleanroom/",)


def repository_python_files() -> list[str]:
    """Every `.py` path that belongs to this repository, from its root.

    Fails closed: a `git ls-files` that errors, or a checkout with no Python in it,
    is a check that could not run, and a check that cannot run is a failure rather
    than a pass.
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
            f"door-private boundary was not checked at all: {error}"
        )
    paths = [path for path in result.stdout.split("\0") if path]
    return sorted(path for path in paths if not path.startswith(EXCLUDED_PREFIXES))


def imports_module(path: Path, module: str) -> bool:
    """True when `path` names `module` in a statically knowable import.

    Walked with `ast.walk`, so a deferred import inside a function is caught exactly
    like a top-level one. Literal `import_module(...)` and `__import__(...)` calls
    are included; aliases and nonliteral module names require data-flow analysis and
    remain outside this deliberately small AST check.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == module for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if (node.module or "").split(".")[0] == module:
                return True
        elif isinstance(node, ast.Call) and (target := _literal_dynamic_import(node)) is not None:
            if target.split(".")[0] == module:
                return True
    return False


def importers_of(module: str, files: list[str], root: Path = ROOT) -> set[str]:
    """Every file in `files` that imports `module`, except the module's own file.

    Only the exact own path is left out: a file elsewhere whose name merely ends
    in the module's name is an importer like any other.
    """
    own_file = f"pipeline/1_exemplar/{module}.py"
    return {path for path in files if path != own_file and imports_module(root / path, module)}


def test_a_file_named_like_a_door_private_module_is_still_an_importer(tmp_path):
    lookalike = "pipeline/4_x/rerender_pdf_render.py"
    (tmp_path / lookalike).parent.mkdir(parents=True)
    (tmp_path / lookalike).write_text("import pdf_render\n", encoding="utf-8")
    own = tmp_path / "pipeline/1_exemplar/pdf_render.py"
    own.parent.mkdir(parents=True)
    own.write_text("import pdf_render\n", encoding="utf-8")

    found = importers_of("pdf_render", [lookalike, "pipeline/1_exemplar/pdf_render.py"], tmp_path)

    assert found == {lookalike}


def _literal_dynamic_import(node: ast.Call) -> str | None:
    """The literal module name for the two direct dynamic-import spellings."""
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return None
    if not isinstance(node.args[0].value, str):
        return None
    function = node.func
    import_module = (
        isinstance(function, ast.Name)
        and function.id == "import_module"
        or isinstance(function, ast.Attribute)
        and function.attr == "import_module"
    )
    builtin = isinstance(function, ast.Name) and function.id == "__import__"
    return node.args[0].value if import_module or builtin else None


def test_the_population_is_the_repositorys_own_python():
    """The guard on the guard. A population of three files would let a violation in
    a fourth pass unnoticed, so the count is asserted before anything is parsed."""
    files = repository_python_files()
    assert len(files) >= 60, f"expected the repository's tracked Python files, found {len(files)}"
    assert "pipeline/1_exemplar/pdf_render.py" in files
    missing = [path for path in files if not (ROOT / path).exists()]
    assert not missing, (
        "git names a Python file this checkout does not hold, so the boundary was "
        f"checked against something other than the working tree: {missing}"
    )


@pytest.mark.parametrize("module", sorted(DOOR_PRIVATE_MODULES))
def test_a_door_private_module_is_imported_only_by_the_door_and_its_own_tests(module):
    files = repository_python_files()
    assert f"pipeline/1_exemplar/{module}.py" in files
    importers = importers_of(module, files)
    assert importers, (
        f"no file imports {module} at all — this test would pass vacuously over an empty population"
    )
    allowed = DOOR_PRIVATE_MODULES[module]
    violations = importers - allowed
    assert not violations, (
        f"{module}.py is door-private and must not be imported outside "
        f"{sorted(allowed)}, but found:\n" + "\n".join(sorted(violations))
    )


@pytest.mark.parametrize("module", sorted(DOOR_PRIVATE_MODULES))
def test_the_allowed_importers_all_still_exist_and_still_import_it(module):
    """A stale allowance is a hole nobody notices: if `door.py` stopped importing
    the renderer, this list would keep permitting a file that no longer needs it."""
    for path in sorted(DOOR_PRIVATE_MODULES[module]):
        target = ROOT / path
        assert target.exists(), f"{path} is on the allow-list and does not exist"
        assert imports_module(target, module), (
            f"{path} is allowed to import {module} and no longer does; the allowance "
            "is stale and should be removed rather than left standing"
        )
