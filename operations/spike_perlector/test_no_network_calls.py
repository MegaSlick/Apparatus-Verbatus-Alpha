"""Guard against direct transport imports anywhere in this package.

`__init__.py` says this package "contains no model client, network transport,
pod control, or real register data". A sentence in a docstring is a claim; this
is checked here against ordinary imports. An `ast` import scan over every file in
this directory refuses the named transport modules, and refuses
`subprocess` too, because an import scan that stops at `httpx` is trivially
defeated by shelling out to `curl`.

Grafted from lane A (`origin/agent/roughin-05-measurement`), which built this
proof and which lane B had no equivalent of. Two changes from lane A's version:
`subprocess` is refused for the reason above, and the scan fails closed if it
finds no files to scan -- a check that silently examined nothing would pass
forever while proving nothing, which is the failure mode
`pipeline/test_stage_import_boundaries.py::repository_python_files` already
guards against for its own population.

To see it work, add `import requests` to any file here: this test goes red.

Named limit: an import scan cannot prove the absence of dynamic imports or transport
hidden behind an otherwise allowed dependency. The package is also inspected as code;
this test is a regression guard, not a complete capability proof.
"""

import ast
from pathlib import Path

FORBIDDEN_MODULES = {
    "requests",
    "httpx",
    "aiohttp",
    "urllib",
    "urllib.request",
    "socket",
    "http.client",
    "ftplib",
    "smtplib",
    # A real weights fetch is spec 04's serving-manager territory. This package
    # resolves no artifact and downloads nothing: every candidate in every test
    # is a `fakes.FakeCandidate` behind the `Candidate` protocol.
    "huggingface_hub",
    # An import scan that only names network libraries is defeated by
    # `subprocess.run(["curl", ...])`. Nothing here needs to start a process.
    "subprocess",
}

PACKAGE_ROOT = Path(__file__).resolve().parent


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_file_in_this_package_imports_a_networking_module():
    paths = sorted(PACKAGE_ROOT.glob("*.py"))
    assert len(paths) > 1, (
        "the no-transport scan found nothing to scan, so it proved nothing; "
        f"expected the package's own modules under {PACKAGE_ROOT}"
    )
    offenders: dict[str, set[str]] = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_module_names(tree)
        hit = {
            name
            for name in imported
            if name in FORBIDDEN_MODULES or name.split(".")[0] in FORBIDDEN_MODULES
        }
        if hit:
            offenders[path.name] = hit
    assert not offenders, f"a transport import was found where none may exist: {offenders}"
