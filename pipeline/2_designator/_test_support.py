"""Shared import support for tests that exercise the Designator program in process."""

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def load_designator(module_name: str) -> ModuleType:
    """Load ``run.py`` under a caller-owned name without duplicating import mechanics."""
    path = ROOT / "pipeline" / "2_designator" / "run.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken Python import runtime
        raise RuntimeError(f"could not create an import specification for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
