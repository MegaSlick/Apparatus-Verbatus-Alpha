"""Pins for this project's ink-calibration constants, read as source literals.

The first test is the cross-stage one: the Designator's secondary margin
against the Recensor's contrast constant. The second is a within-file
coupling of the same kind -- a constant one file's number was measured
against, in a file that cannot import it.
"""

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


def test_the_sealed_ink_bound_still_sits_at_the_level_it_was_measured_at():
    """`max_ink_bp = 7000` was placed in a gap measured at `PRIMARY_MARGIN = 20`.

    The coupling is invisible from either side. `config/designator_grouping.toml`
    states a fraction of a page and says nothing about the level it is probed at;
    `structure.PRIMARY_MARGIN` is a module constant with no mention of the sealed
    bound. But `_settle_background_evidence` probes at that constant, and the
    whole-page ink distribution the bound sits in was measured there and nowhere
    else: 127 real pages, a continuum from 281 to 8502 bp with no gap wider than
    505, and 7000 placed in the 6595-7077 one, 405 bp above the highest page it
    admits and 77 bp below the lowest it refuses (HANDOFF.md, "The background
    inference, calibrated on 127 pages"). Move the level and every one of those
    numbers is about a different statistic, while the bound goes on refusing 13
    pages for a reason nobody re-measured. Measured at the *derived* margin
    instead, the same six silent pages the bound exists for fall to 2239-3498 bp
    against a median of 2434 over the other 121 -- interleaved, and no value of
    the bound separates them at all.

    So the constant is pinned here, in the file that already reads
    `SECONDARY_MARGIN` as a source literal, rather than the level being moved
    into `[grouping.background]` as a fifth field. A sealed field would be a
    second home for one number: `_derived_ink_margin` floors at
    `PRIMARY_MARGIN`, `conservation`'s sensitivity argument is stated against it,
    and `structure_pass` compares to it, so the config would carry a value whose
    only correct setting is the constant's -- the drift this project refuses
    everywhere else. A pin costs nothing and fails loudly on the change that
    matters.
    """
    probe_level = _literal_constant(
        ROOT / "pipeline" / "2_designator" / "structure.py", "PRIMARY_MARGIN"
    )
    assert probe_level == 20, (
        "config/designator_grouping.toml's max_ink_bp = 7000 was placed in a gap "
        "measured with the probe at this level; re-measure that bound over the "
        "127-page calibration before changing it"
    )
