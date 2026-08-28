"""Every numbered stage after the Exemplar communicates only through common/ and
the files its own HANDOFF.md declares. `pipeline/README.md`'s own rule: "the
repository rule is that stages communicate only through the files declared in
their HANDOFF.md. Boundary tests must accompany the first implementation of each
stage." Only `pipeline/1_exemplar/test_import_boundaries.py` existed, and it
guards a narrower thing (which of 1_exemplar's own sibling modules are
door-private) rather than "does any stage reach into a different stage's
directory at all" -- so pipeline/2_designator through pipeline/7_armarium, and
the orchestrator that invokes all of them, had no boundary test of their own.

Static, for the same reason `common/chairs/test_chairs_import_boundary.py` is:
the numbered directories already make `import 4_perlector` invalid Python,
"a useful deterrent... but not a complete boundary: dynamic imports and path
manipulation can still cross it." This walks `ast.Import`/`ast.ImportFrom`
nodes and literal `import_module("...")` / `__import__("...")` calls, so it sees a deferred import
inside a function exactly like a top-level one. It does NOT decide a nonliteral
dynamic module name, an aliased loader, or an `importlib.util.spec_from_file_location(...)` call.
Every stage's own test
suite already uses exactly that mechanism, under a synthetic module name, to
load a sibling stage's `run.py` for cross-stage boundary testing (e.g.
`pipeline/4_perlector/test_region_boundary.py` loading Attestatores) -- a
deliberate, visible, single-purpose load, not the accidental bare `import`
this test exists to catch.

**What this does not catch.** Every stage's entry file is named `run.py`, so
"does a bare `import run` reach this stage's own file or some other stage's" is
a question sys.path order answers at runtime, not something an AST walk over
one file can determine. This test still catches every OTHER cross-stage name
collision (a stage importing another stage's uniquely-named sibling module, the
same class of gap `pipeline/1_exemplar/test_import_boundaries.py` closed for
`pdf_render`/`image_formats`), and it is worth having even with that one gap
named rather than silently assumed shut.

The population is `git ls-files`, not a filesystem walk, for the reason
`pipeline/1_exemplar/test_import_boundaries.py` already gives: `workbench/` is
gitignored and absent in a container, present and full of unparseable drafts on
the machine this project actually runs on, and git's own idea of what belongs to
this repository is the only population that means the same thing in both places.
"""

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "pipeline"

# Every directory pipeline/README.md's rule binds: the seven numbered stages,
# plus the orchestrator, which is stage-neutral but held to the identical
# "imports only common/ and its own files" rule.
STAGE_DIRECTORIES = (
    "1_exemplar",
    "2_designator",
    "3_attestatores",
    "4_perlector",
    "5_recensor",
    "6_archetypus",
    "7_armarium",
    "orchestrator",
)

EXCLUDED_PREFIXES = ("cleanroom/",)


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
    return sorted(path for path in paths if not path.startswith(EXCLUDED_PREFIXES))


def _stage_of(path: str) -> str | None:
    """Which stage directory a repository-relative path belongs to, or None."""
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != "pipeline":
        return None
    return parts[1] if parts[1] in STAGE_DIRECTORIES else None


def _own_module_names(stage: str) -> set[str]:
    """Bare module names a file may legitimately import from its own stage
    directory — every `.py` file's stem, siblings included (e.g. 1_exemplar's
    `admission`, `door`, `image_formats`, `pdf_render`, `render_config`,
    `synthetic_sources`), so a same-stage import is never mistaken for a
    boundary crossing."""
    return {path.stem for path in (PIPELINE / stage).glob("*.py")}


def _imports_in(path: Path) -> list[tuple[str, str]]:
    """`(root_module, full_module)` for every statically knowable import.

    A relative `from . import x` is reported under the name it binds, and
    `from .pkg import x` under `pkg`. No pipeline directory is a package — there
    is no `__init__.py` anywhere under `pipeline/` — so a relative import there
    is a runtime error rather than a boundary crossing, and this reports none
    today. It is here so the guard fails closed rather than resting on that: a
    node kind silently skipped is how a guard grows a gap it never announced.
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
        for name in _literal_fromlist(node):
            modules.append(f"{parent}.{name}" if parent else name)
    return modules


def _literal_fromlist(node: ast.Call) -> list[str]:
    """The literal string entries of a `__import__` fromlist, positional or keyword.

    Only literal tuples and lists of literal strings are readable; a computed
    fromlist is not statically knowable and is reported as nothing rather than
    guessed at. `"*"` names no submodule and is dropped.
    """
    fromlist: ast.expr | None = node.args[3] if len(node.args) > 3 else None
    for keyword in node.keywords:
        if keyword.arg == "fromlist":
            fromlist = keyword.value
    if not isinstance(fromlist, ast.Tuple | ast.List):
        return []
    return [
        element.value
        for element in fromlist.elts
        if isinstance(element, ast.Constant)
        and isinstance(element.value, str)
        and element.value != "*"
    ]


def test_the_population_covers_every_stage_directory():
    """The guard on the guard: a population missing a stage would let a
    violation inside it pass unnoticed."""
    files = repository_python_files()
    covered = {stage for path in files if (stage := _stage_of(path)) is not None}
    assert covered == set(STAGE_DIRECTORIES), (
        f"expected every stage directory represented, found only {sorted(covered)}"
    )


def test_no_stage_imports_another_stages_uniquely_named_module():
    """A stage may import its own sibling files and anything in `common/`,
    stdlib, or a declared third-party dependency. It may not import a bare
    module name that only exists as a `.py` file inside a *different* stage's
    directory — the cross-stage collision `pipeline/1_exemplar/`'s door-private
    check already guards for two of its own siblings, extended here to every
    stage and every uniquely-named module rather than a hand-picked pair."""
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
                # "run" collides by construction (every stage has one) and is
                # documented above as this check's one known gap; every other
                # name is unique to the stage that defines it.
                if root != "run" and root in other_modules:
                    violations.append(
                        f"{path} imports {full!r}, which is owned by pipeline/{other_stage}/"
                    )

    assert checked >= 10, f"expected pipeline stage files to be checked, found {checked}"
    assert not violations, "a stage crossed another stage's boundary:\n" + "\n".join(
        sorted(violations)
    )


def test_no_stage_imports_pipeline_by_its_dotted_path():
    """`import 4_perlector` is already invalid Python (the deterrent
    pipeline/README.md names), but nothing stopped `import pipeline.something`
    or `from pipeline.something import x` outright — checked directly rather
    than assumed impossible from the numbering alone."""
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


def test_production_stage_code_never_imports_the_reseal_forgery_helper():
    """`reseal_chain` exists only to make test forgeries internally coherent.

    Matched on the last dotted component rather than the root, so a qualified
    `import_module("pipeline.6_archetypus.reseal_chain")` is caught here by name
    as well as by the dotted-path test above — the two guards describe different
    wrongs and the qualified form is both of them.
    """
    violations = [
        f"{path} imports {full!r}"
        for path in repository_python_files()
        if _stage_of(path) is not None
        and not Path(path).name.startswith("test_")
        and Path(path).name != "reseal_chain.py"
        for root, full in _imports_in(ROOT / path)
        if root == "reseal_chain" or full.split(".")[-1] == "reseal_chain"
    ]
    assert not violations, "production stage code imported a test forgery helper:\n" + "\n".join(
        violations
    )


def test_literal_dynamic_pipeline_imports_are_visible_to_the_guard(tmp_path):
    source = tmp_path / "dynamic_import.py"
    source.write_text(
        "import importlib\nfrom importlib import import_module\n"
        "importlib.import_module('pipeline.7_armarium.run')\n"
        "import_module('pipeline.2_designator.run')\n"
        "__import__('pipeline.4_perlector.run')\n",
        encoding="utf-8",
    )
    assert _imports_in(source) == [
        ("importlib", "importlib"),
        ("importlib", "importlib"),
        ("pipeline", "pipeline.7_armarium.run"),
        ("pipeline", "pipeline.2_designator.run"),
        ("pipeline", "pipeline.4_perlector.run"),
    ]


def test_the_reseal_guard_sees_every_form_the_helper_could_arrive_under(tmp_path):
    """Bare, qualified, relative, and by fromlist — the guard reports all of them.

    No pipeline directory is a package, so the relative forms cannot execute
    there today. They are checked anyway: an AST node kind the walker silently
    skips is a gap the guard would never announce, and the qualified form used to
    be reported only under the root `pipeline`.

    The fromlist routes are the ones that ran while being reported as something
    else. `__import__` given a fromlist loads the named child, and a stage
    directory is a namespace package, so `__import__("6_archetypus",
    fromlist=("reseal_chain",))` executed the helper while the guard saw only the
    directory — a name owned by no stage, matching no other check here either.
    """
    source = tmp_path / "reseal_routes.py"
    source.write_text(
        "import reseal_chain\n"
        "from importlib import import_module\n"
        "import_module('pipeline.6_archetypus.reseal_chain')\n"
        "from . import reseal_chain as forge\n"
        "from .reseal_chain import reseal\n"
        "__import__('pipeline.6_archetypus', fromlist=('reseal_chain',))\n"
        "__import__('6_archetypus', fromlist=['reseal_chain'])\n"
        "import builtins\n"
        "builtins.__import__('5_recensor', fromlist=('reseal_chain',))\n",
        encoding="utf-8",
    )

    # A set, because `ast.walk` is breadth-first: the `import_module` call is a
    # node deeper than the import statements and is reached after them. What is
    # asserted is that every route is seen, not the order they are seen in.
    names = {full for _root, full in _imports_in(source)}
    assert {full for full in names if full.split(".")[-1] == "reseal_chain"} == {
        "reseal_chain",
        "pipeline.6_archetypus.reseal_chain",
        ".reseal_chain",
        "6_archetypus.reseal_chain",
        "5_recensor.reseal_chain",
    }
