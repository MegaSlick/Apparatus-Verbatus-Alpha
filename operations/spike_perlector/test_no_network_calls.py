"""Guard against direct transport imports anywhere in this package.

`__init__.py` says this package "contains no model client, network transport,
pod control, or real register data". A sentence in a docstring is a claim; this
is checked here against ordinary imports. An `ast` import scan over every file in
this directory refuses the named transport modules, and refuses
`subprocess` too, because an import scan that stops at `httpx` is trivially
defeated by shelling out to `curl`.

The scan fails closed if it finds no files to scan: a check that silently
examined nothing would pass forever while proving nothing, which is the failure
mode `pipeline/test_stage_import_boundaries.py::repository_python_files` already
guards against for its own population.

To see it work, add `import requests` to any file here: this test goes red.

Named limit: an import scan cannot prove the absence of dynamic imports or transport
hidden behind an otherwise allowed dependency. The package is also inspected as code;
this test is a regression guard, not a complete capability proof.
"""

import ast
from pathlib import Path

import pytest

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
    # The clients a list of the obvious four leaves out. `urllib3` and `httpcore`
    # are the transports under `requests` and `httpx` and can be imported
    # directly; `websockets` and `grpc` are neither. Naming only the famous
    # names let this guard report a clean package while a dossier left it.
    "urllib3",
    "httpcore",
    "h11",
    "websockets",
    "websocket",
    "grpc",
    "grpclib",
    "asyncio.streams",
    "telnetlib",
    "poplib",
    "imaplib",
    "xmlrpc",
    "ssl",
}

PACKAGE_ROOT = Path(__file__).resolve().parent


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            # `from http import client` records only `http`, which is not in the
            # blocklist — `http.client` is. Recording the qualified target too is
            # what closes that spelling; `import http.client` was always caught.
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _offending_imports(source: str) -> set[str]:
    imported = _imported_module_names(ast.parse(source))
    return {
        name
        for name in imported
        if name in FORBIDDEN_MODULES or name.split(".")[0] in FORBIDDEN_MODULES
    }


@pytest.mark.parametrize(
    "source",
    (
        "import http.client",
        "from http.client import HTTPConnection",
        # The spelling that escaped: `node.module` alone is `http`, and the
        # blocklist names `http.client`, so neither the exact nor the
        # first-segment test matched and the scan reported a clean file.
        "from http import client",
    ),
)
def test_the_transport_scan_catches_every_spelling_of_one_forbidden_import(source):
    assert _offending_imports(source)


def test_the_transport_scan_still_passes_an_ordinary_import():
    assert not _offending_imports("from dataclasses import dataclass\nimport json")


def test_no_file_in_this_package_imports_a_networking_module():
    # Recursive: the package is flat today, so this changes nothing now. A
    # top-level-only scan would go on reporting "no file in this package" while
    # silently not reading a subpackage the day one is added.
    paths = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert len(paths) > 1, (
        "the no-transport scan found nothing to scan, so it proved nothing; "
        f"expected the package's own modules under {PACKAGE_ROOT}"
    )
    offenders: dict[str, set[str]] = {}
    for path in paths:
        # The same helper the spelling tests above exercise, not a second copy
        # of the rule. A duplicated filter meant those tests could go green on a
        # rule this scan never used, while the import they were taught to catch
        # sat in a shipped module.
        hit = _offending_imports(path.read_text(encoding="utf-8"))
        if hit:
            # Relative to the package, not the bare filename: the scan recurses,
            # so `sub/runner.py` and `runner.py` would share one key and the
            # second would silently overwrite the first.
            offenders[str(path.relative_to(PACKAGE_ROOT))] = hit
    assert not offenders, f"a transport import was found where none may exist: {offenders}"
