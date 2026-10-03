"""The Recensor reviews coverage and never re-reads: it asks for no recovery and runs nothing."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECENSOR_DIRECTORY = ROOT / "pipeline/5_recensor"

# Every non-test source file in the stage, not just `run.py`, and `rglob` so a
# subpackage is scanned too: a guard that scans less than it says reports a
# pass for ground it never covered.
RECENSOR_SOURCES = sorted(
    path for path in RECENSOR_DIRECTORY.rglob("*.py") if not path.name.startswith("test_")
)

INVOCATION_MODULES = frozenset(
    {"subprocess", "os", "importlib", "multiprocessing", "runpy", "pty", "asyncio"}
)


def _modules() -> list[tuple[Path, ast.Module]]:
    """Every source file of this stage, parsed, so nothing hides in a sibling."""
    assert RECENSOR_SOURCES, "the stage has no source files; this guard found nothing to guard"
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in RECENSOR_SOURCES]


def _top_level_imports(trees) -> set[str]:
    """The top-level module name of every `import x` and `from x import y`."""
    imported: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    return imported


def test_the_recensor_cannot_re_invoke_a_reading_stage_at_all():
    """The deeper structural guarantee: this stage has no way to run anything.

    A stage that could invoke the Perlector itself could re-read until it liked
    the answer. A `subprocess` import here would be the first step towards that.
    """
    imported = _top_level_imports(tree for _, tree in _modules())
    # `runpy` re-invokes a stage in this process and `pty` reaches a shell, each
    # importing nothing else on the list. `asyncio` is listed because this scan
    # reads the stage's own imports and never follows a transitive one, so
    # `asyncio.create_subprocess_exec` would otherwise pass. The scan covers
    # direct imports only; `__import__` of a computed name is outside it.
    assert not imported & INVOCATION_MODULES, (
        f"the Recensor imports {sorted(imported & INVOCATION_MODULES)}; it reviews "
        "readings and never invokes the stage that makes them"
    )
