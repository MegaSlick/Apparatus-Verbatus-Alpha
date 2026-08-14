"""Cross-stage pin for the two deliberately asymmetric ink calibrations."""

import ast
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


def test_the_recensor_audit_never_calls_ink_what_the_designator_dismissed():
    """A one-sided retune must fail at the shared boundary, not inside a stage."""
    designator_margin = _literal_constant(
        ROOT / "pipeline" / "2_designator" / "structure.py", "SECONDARY_MARGIN"
    )
    recensor_contrast = _literal_constant(
        ROOT / "pipeline" / "5_recensor" / "residual_ink.py",
        "MINIMUM_CONTRAST_BELOW_BACKGROUND",
    )
    assert recensor_contrast >= designator_margin
