"""Pins for this project's ink-calibration constants, read as source literals.

The first test is the cross-stage one: the Designator's secondary margin
against the Recensor's contrast constant. The second is a within-file
coupling of the same kind -- a constant one file's number was measured
against, in a file that cannot import it.

**The third is the one that stopped these being arithmetic.** Two margins only
order two ink sets when both are taken below the *same* background, and until
2026-09-06 they were not: the Designator inferred a page's paper from its own
two population modes while `common/residual_ink.py` took the raw histogram mode.
On a photographed page those are different values by two hundred grey levels,
the audit's ink set was empty, and the containment the first test pins held over
nothing at all. `test_the_containment_is_not_vacuous_on_a_photographed_page`
runs both thresholds on one photographed-shaped page through the shared
inference and checks the sets themselves.
"""

import ast
from pathlib import Path

from common.background import (
    PRIMARY_MARGIN,
    infer_background_evidence,
    load_background_config,
    resolve_background_policy,
)
from common.residual_ink import (
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    load_coverage_audit_config,
    residual_ink,
    resolve_coverage_audit_policy,
)

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

    It is read from `common/background.py` since 2026-09-06, which is where the
    constant and the probe that uses it both live now: the inference moved out of
    the Designator so that three stages could share it. The file it is compared
    *against* is unchanged, so this is still two source literals in two files.
    """
    probe_level = _literal_constant(ROOT / "common" / "background.py", "PRIMARY_MARGIN")
    assert probe_level == 20, (
        "config/designator_grouping.toml's max_ink_bp = 7000 was placed in a gap "
        "measured with the probe at this level; re-measure that bound over the "
        "127-page calibration before changing it"
    )


def photographed_shaped_page() -> tuple[int, int, list[bytearray]]:
    """A SYNTHETIC page with the shape a photographed register opening has.

    Not a photograph and not claimed to be one: a 200x200 frame of near-black at
    5, a lit interior of paper at 210, and three marks on it. What it reproduces
    from real material is the one property that broke the retired inference --
    **the surround wins the histogram**: 20,400 pixels of frame against the
    interior's 19,000 of paper (its 19,600 pixels less the 600 the three marks
    take), so the page's single most common value is 5, which is what
    `common/residual_ink.py` called paper until 2026-09-06. Its interior
    darkness at the level the surround test measures is 4,043 basis points,
    inside the sealed 5,000 bound and above the 287-3,452 the Designator's 127
    real pages measured, which is stated so the shape is not read as a
    calibration sample. The real pages are in that stage's own survey; nothing
    here measures a corpus.

    The three marks are chosen to separate the three thresholds rather than to
    look like handwriting:

    * **40** -- below all three thresholds. Ink to the primary scan, to the
      audit and to conservation alike: the strokes this page shares.
    * **160** -- below the audit's 170 and above the primary scan's 142. Ink to
      the audit and not to the primary scan, which is the direction the audit's
      whole purpose depends on: it must be able to see a mark the proposing pass
      did not.
    * **180** -- above both, below conservation's 208. Ink to the Designator's
      own reconciliation and to nothing else, which is what makes the
      containment a strict one rather than an equality that happens to hold.
    """
    width = height = 200
    frame = 30
    rows = [bytearray([5] * width) for _ in range(height)]
    for y in range(frame, height - frame):
        for x in range(frame, width - frame):
            rows[y][x] = 210
    for value, top in ((40, 60), (160, 90), (180, 110)):
        for y in range(top, top + (10 if value == 40 else 5)):
            for x in range(50, 80):
                rows[y][x] = value
    return width, height, rows


def _ink_at(rows: list[bytearray], threshold: int) -> set[tuple[int, int]]:
    return {
        (x, y) for y, row in enumerate(rows) for x, value in enumerate(row) if value <= threshold
    }


def test_the_containment_is_not_vacuous_on_a_photographed_page():
    """One background, three margins, and three ink sets that actually nest.

    The first test in this file pins `recensor_contrast >= designator_margin` as
    two source literals. That inequality orders two *thresholds* only if both are
    subtracted from the same background, and on a photographed page they were
    not: the Designator inferred 210 here and the audit inferred 5, whose 40-level
    contrast is below every 8-bit sample. The audit's ink set was empty, its
    `flagged` was `False` by construction, and "the audit never calls ink what
    the Designator dismissed" was true of nothing.

    So this asserts the sets, on the page the failure needed. All three now come
    from `common.background`'s one inference under the shipped sealed policy --
    the file itself, not a hand-built one, so a change to those four values fails
    here as well as in the Designator.
    """
    width, height, rows = photographed_shaped_page()
    policy = resolve_background_policy(load_background_config(), width, height)
    evidence = infer_background_evidence(width, height, rows, background_policy=policy)

    # The failure this unit closed, stated as a fact about this page rather than
    # as history: the value the audit used to call paper cannot express the
    # audit's own contrast at all, so every pixel here read as background.
    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    retired_background = max(range(256), key=lambda value: histogram[value])
    assert retired_background == 5
    assert retired_background - MINIMUM_CONTRAST_BELOW_BACKGROUND < 0

    assert evidence["background"] == 210
    assert evidence["source"] == "inferred-interior-mode"
    assert evidence["ink_margin"] == 68

    secondary_margin = _literal_constant(
        ROOT / "pipeline" / "2_designator" / "structure.py", "SECONDARY_MARGIN"
    )
    background = evidence["background"]
    primary = _ink_at(rows, background - evidence["ink_margin"])
    audited = _ink_at(rows, background - MINIMUM_CONTRAST_BELOW_BACKGROUND)
    reconciled = _ink_at(rows, background - secondary_margin)

    # Non-vacuous: the audit's set is not empty, which is the whole difference.
    assert len(audited) == 20_850
    # The audit sees every stroke the proposing scan saw, and one more.
    assert primary < audited
    assert len(primary) == 20_700
    # And the Designator's own reconciliation still sees everything the audit
    # does, strictly: this is the containment the first test pins, holding over
    # 20,850 pixels rather than over none.
    assert audited < reconciled
    assert len(reconciled) == 21_000

    # The dark strokes are counted as ink by all three, which is the claim
    # "the audit and the Designator count the same strokes" reduces to.
    strokes = {(x, y) for y in range(60, 70) for x in range(50, 80)}
    assert strokes <= primary
    assert strokes <= audited
    assert strokes <= reconciled

    # And the whole thing, through the shipped audit rather than through a
    # threshold restated here: coverage is empty, so every ink pixel is outside
    # it, and the page flags.
    finding = residual_ink(
        width,
        height,
        rows,
        [],
        background_policy=policy,
        coverage_policy=resolve_coverage_audit_policy(load_coverage_audit_config(), width, height),
    )
    assert finding["background"]["background_level"] == background
    assert finding["background"]["contrast_below_background"] == MINIMUM_CONTRAST_BELOW_BACKGROUND
    assert finding["background"]["ink_threshold"] == background - MINIMUM_CONTRAST_BELOW_BACKGROUND
    # `page_ink_pixels` is the audit's whole set and is what the containment
    # above is a statement about. `total_ink_pixels` is the AUDITED pair's
    # denominator -- the same set with this page's page-spanning component taken
    # out -- and on a photographed-shaped page that component is the bezel,
    # which is most of the ink. Both are on the record, which is what keeps the
    # subtraction visible rather than silent.
    assert finding["page_ink_pixels"] == len(audited)
    assert finding["page_spanning_ink_pixels"] == 20_400
    assert finding["page_spanning_components"] == [{"x": 0, "y": 0, "w": width, "h": height}]
    assert finding["total_ink_pixels"] == len(audited) - 20_400 == 450
    assert finding["outside_ink_pixels"] == 450
    assert finding["flagged"] is True


def test_the_probe_level_is_below_the_audits_own_contrast():
    """`PRIMARY_MARGIN` floors every derived margin; the audit's 40 does not.

    Both numbers are offsets below one background now, so their order is a real
    statement about two instruments rather than two unrelated constants. The
    floor is the most permissive threshold any scan in this project applies, and
    the audit's contrast sits below it -- meaning the audit is *stricter* than
    the most permissive scan and looser than the derived margin a photographed
    page produces. That is the band this audit is meant to work in, and nothing
    else in the tree says so.
    """
    assert PRIMARY_MARGIN < MINIMUM_CONTRAST_BELOW_BACKGROUND
