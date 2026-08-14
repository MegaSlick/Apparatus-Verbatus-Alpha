"""Cross-stage pin for the two deliberately asymmetric ink calibrations."""

import ast
import importlib.util
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _literal_constant(path: Path, name: str) -> int:
    """Read one declared numeric constant without importing either stage."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        value_node = node.value if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value_node = node.targets[0], node.value
        if isinstance(target, ast.Name) and target.id == name:
            value = ast.literal_eval(value_node)
            assert isinstance(value, int) and not isinstance(value, bool)
            return value
    raise AssertionError(f"{path} does not declare {name}")


def _load_conservation_module():
    """Load the numeric-stage module without making its directory a package."""
    stage = ROOT / "pipeline" / "2_designator"
    structure_path = stage / "structure.py"
    conservation_path = stage / "conservation.py"
    structure_spec = importlib.util.spec_from_file_location(
        "_ink_calibration_structure", structure_path
    )
    conservation_spec = importlib.util.spec_from_file_location(
        "_ink_calibration_conservation", conservation_path
    )
    assert structure_spec is not None and structure_spec.loader is not None
    assert conservation_spec is not None and conservation_spec.loader is not None
    structure = importlib.util.module_from_spec(structure_spec)
    conservation = importlib.util.module_from_spec(conservation_spec)
    previous = sys.modules.get("structure")
    sys.modules["structure"] = structure
    try:
        structure_spec.loader.exec_module(structure)
        conservation_spec.loader.exec_module(conservation)
    finally:
        if previous is None:
            sys.modules.pop("structure", None)
        else:
            sys.modules["structure"] = previous
    return conservation


def test_the_recensor_audit_never_calls_ink_what_the_designator_dismissed():
    """A one-sided retune must fail at the shared boundary, not inside a stage."""
    designator_margin = _literal_constant(
        ROOT / "pipeline" / "2_designator" / "structure.py", "SECONDARY_MARGIN"
    )
    recensor_contrast = _literal_constant(
        ROOT / "pipeline" / "5_recensor" / "residual_ink.py",
        "MINIMUM_CONTRAST_BELOW_BACKGROUND",
    )
    fallback_reader_margin = _literal_constant(
        ROOT / "pipeline" / "4_perlector" / "reader.py",
        "PAGE_FALLBACK_INK_MARGIN",
    )
    conservation = _load_conservation_module()
    reconcile_margin = inspect.signature(conservation.reconcile).parameters["margin"].default
    assert reconcile_margin == designator_margin
    assert fallback_reader_margin == designator_margin
    assert recensor_contrast >= designator_margin
