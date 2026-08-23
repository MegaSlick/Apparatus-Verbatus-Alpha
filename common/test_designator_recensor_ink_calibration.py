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
            assert isinstance(value, int)
            assert not isinstance(value, bool)
            return value
    raise AssertionError(f"{path} does not declare {name}")


def _parameter_default_name(path: Path, function: str, parameter: str) -> str:
    """Read one named parameter default without executing the stage."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function:
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        pairs = (
            list(zip(positional[-len(node.args.defaults) :], node.args.defaults, strict=True))
            if node.args.defaults
            else []
        )
        pairs.extend(zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True))
        for argument, default in pairs:
            if argument.arg != parameter or default is None:
                continue
            if not isinstance(default, ast.Name):
                raise AssertionError(
                    f"{path} does not declare {function}({parameter}=<named constant>)"
                )
            return default.id
    raise AssertionError(f"{path} does not declare {function}({parameter}=...)")


def test_the_recensor_audit_never_calls_ink_what_the_designator_dismissed():
    """A one-sided retune must fail at the shared boundary, not inside a stage."""
    designator_margin = _literal_constant(
        ROOT / "pipeline" / "2_designator" / "structure.py", "SECONDARY_MARGIN"
    )
    recensor_contrast = _literal_constant(
        ROOT / "common" / "residual_ink.py",
        "MINIMUM_CONTRAST_BELOW_BACKGROUND",
    )
    fallback_reader_margin = _literal_constant(
        ROOT / "pipeline" / "4_perlector" / "reader.py",
        "PAGE_FALLBACK_INK_MARGIN",
    )
    reconcile_margin_name = _parameter_default_name(
        ROOT / "pipeline" / "2_designator" / "conservation.py",
        "reconcile",
        "margin",
    )
    assert reconcile_margin_name == "SECONDARY_MARGIN"
    assert fallback_reader_margin == designator_margin
    assert recensor_contrast >= designator_margin
