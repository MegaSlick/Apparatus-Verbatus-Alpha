"""The door-private modules: nothing outside this stage may import them.

Spec 03, test 4: "re-render attempts by any later stage have no API to call (the
render module is door-private)." Every docstring in this tree that makes that claim
says so in prose; this is what makes it executable rather than merely asserted — the
same shape `common/chairs/test_chairs_import_boundary.py` uses for its own boundary,
adapted for a non-package directory where every import of a sibling module is a bare
name (`import pdf_render`) rather than a dotted path, since `1_exemplar` cannot
itself be a Python package name.

**The population is `git ls-files`, not a filesystem walk, and that is the whole
repair.** An earlier version of this test walked `ROOT.rglob("*.py")` with a
hand-maintained exclusion list. It named `cleanroom/` and not `workbench/` — and
`workbench/` is gitignored, so it is *absent* inside a container and *present and
full of unparseable drafts* on the machine this project actually runs on. The test
passed in the chamber and died on the host, over a file that has nothing to do with
this boundary. Git's own idea of what belongs to this repository is the only
population that means the same thing in both places.

Specifically `--cached --others --exclude-standard`: everything git tracks, plus
everything untracked that is *not* gitignored. Tracked alone would miss a violation
written a minute ago and not yet staged, which is exactly when someone would want to
hear about it; adding gitignored scratch back would reintroduce the defect above.

Meta-invariant #88: no loop here reports success over an empty population, and a
`git ls-files` that cannot run is a failed check rather than an empty one.
"""

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The only files permitted to import each door-private module at all: the door and
# the admission module themselves, plus the direct test suites that exercise them by
# design. Everything else in the repository is "later" or "elsewhere".
#
# **`image_formats` is here because its docstring made the same claim and nothing
# checked it.** An asserted invariant sitting beside an executable one is the shape
# CLAUDE.md's "a summary is never verification" is about: the claim happened to be
# true, and nothing would have failed if a later stage had imported it tomorrow. The
# two allow-lists are genuinely different — `admission.py` imports the validators and
# must never import the renderer — so this is a table, not a copied test.
DOOR_PRIVATE_MODULES = {
    "pdf_render": frozenset(
        {
            "pipeline/1_exemplar/door.py",
            "pipeline/1_exemplar/test_pdf_render.py",
            "pipeline/1_exemplar/test_door.py",
        }
    ),
    "tiff_render": frozenset(
        {
            "pipeline/1_exemplar/door.py",
            "pipeline/1_exemplar/test_tiff_render.py",
        }
    ),
    "image_formats": frozenset(
        {
            "pipeline/1_exemplar/admission.py",
            "pipeline/1_exemplar/door.py",
            "pipeline/1_exemplar/pdf_render.py",
            "pipeline/1_exemplar/tiff_render.py",
            "pipeline/1_exemplar/test_admission.py",
            "pipeline/1_exemplar/test_image_formats.py",
            "pipeline/1_exemplar/test_pdf_render.py",
            "pipeline/1_exemplar/test_tiff_render.py",
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
    """True when `path` names `module` in any `import`/`from ... import`.

    Walked with `ast.walk`, so a deferred import inside a function is caught exactly
    like a top-level one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == module for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if (node.module or "").split(".")[0] == module:
                return True
    return False


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
    importers = {
        path
        for path in files
        if not path.endswith(f"{module}.py") and imports_module(ROOT / path, module)
    }
    assert importers, (
        f"no file imports {module} at all — this test would pass vacuously over an "
        "empty population, which is exactly the false green meta-invariant #88 refuses"
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


def test_an_unparseable_gitignored_file_does_not_break_this_check():
    """The failing case this test was repaired for, reproduced.

    On the machine this project runs on, `workbench/` is real and holds a
    deliberately unparseable file left by an old audit. The previous version of this
    test walked the filesystem and AST-parsed everything it found; it excluded
    `cleanroom/` and not `workbench/`, so it died there. Inside a container
    `workbench/` is masked to an empty mount, the walk found nothing, and the test
    passed — the same code, green in one place and red in the other.

    So: plant exactly that file, and require both checks above to still hold.
    Gitignored source is not repository source, and nothing here reads it.
    """
    scratch = ROOT / "workbench" / "not-python-at-all.py"
    assert not scratch.exists(), "the planted file already exists; refusing to overwrite it"
    scratch.write_text("this is prose, not python: it does not parse ((\n", encoding="utf-8")
    try:
        assert str(scratch.relative_to(ROOT)) not in repository_python_files()
        test_the_population_is_the_repositorys_own_python()
        for module in sorted(DOOR_PRIVATE_MODULES):
            test_a_door_private_module_is_imported_only_by_the_door_and_its_own_tests(module)
    finally:
        scratch.unlink()
