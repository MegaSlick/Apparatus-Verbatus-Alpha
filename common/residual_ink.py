"""Shared residual-ink measurement for the early Ink Map and late Recensor.

Ink is derived from the sealed page independently of stage claims. Coverage is
the Designator's declared ``transform.bounds``, clipped here so a later
geometry refusal is not pre-empted by this measurement. The early Ink Map
passes empty coverage for the pre-proposal denominator; the Recensor passes
every proposal and recovery region. This module invents no act, requests no
recovery and holds no run.

The paper value comes from `common.background.infer_background_evidence`
under the Designator's sealed `[grouping.background]` policy, so the audit and
the stage it audits threshold against the same paper. The
contrast stays this module's own: an audit sharing the Designator's margin
would restate it rather than check it.

The page-spanning component the Designator withholds from grouping
(`pipeline/2_designator/grouping.partition_page_spanning`) held 35 to 87 per
cent of audited ink on 44 real pages, so counting it flagged every page. It is
re-derived here at the page's own derived margin, because the Designator's record
carries only whole-page boxes, and taken out of `total_ink_pixels` and
`outside_ink_pixels`; `page_ink_pixels` and `page_spanning_ink_pixels` keep the
whole-page figure. It is cut at the derived margin rather than at this module's
looser contrast, which merges writing touching it into the component and hides
missed ink;
`test_writing_touching_a_faint_page_spanning_line_is_still_counted_outside_coverage`
pins that.

A page whose background the shared inference refuses raises
`common.background.BackgroundInferenceRefusal`; the caller records it rather
than publishing a zero nobody measured.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, TypedDict

from common.background import (
    BASIS_POINTS,
    DEFAULT_BACKGROUND_CONFIG_PATH,
    BackgroundPolicy,
    _ink_threshold,
    infer_background_evidence,
    round_half_up_bp,
    validate_background_table,
)
from common.calibration import validate_provenance_block
from common.components import label_component_runs, runs_in_row
from common.contracts.canonical import is_plain_int
from common.contracts.errors import ContractError
from common.imaging import Bounds
from common.sealed_config import read_sealed_toml

#: Sealed, not a constant, so it is inside every run's config digest.
MINIMUM_INK_PIXELS_FIELD: Final = "minimum_ink_pixels"

#: A pixel this many levels below the page's own inferred background is ink.
#: Kept at or above the Designator conservation denominator's margin (2), so
#: this audit never calls ink a pixel that accounting dismissed, the one
#: disagreement that could lose ink silently (pinned by
#: `common/test_designator_recensor_ink_calibration.py`). A reasoned default,
#: not measured on real pages. Below a page's derived margin it counts more ink
#: than the Designator's primary scan does; that is why the gates are fractions.
MINIMUM_CONTRAST_BELOW_BACKGROUND = 40

#: Fraction of the page's own ink outside every region that flags it, in basis
#: points; sealed beside the noise floor, integer because artifacts carry no floats.
MINIMUM_FRACTION_OUTSIDE_BP_FIELD: Final = "minimum_fraction_outside_bp"

#: Outside-coverage ink that flags a page on its own, as a fraction of the page's
#: AREA, so it asks the same question of a small page and a large leaf: the
#: fraction gate alone lets several missed words through on a dense page.
#: Resolved to pixels as `substantial_ink_pixels`.
SUBSTANTIAL_INK_AREA_BP_FIELD: Final = "substantial_ink_area_bp"

#: The Ink Map outcome for a page whose paper value the shared inference refused,
#: also read by the Armarium and its export verifier. Not a variant of `mapped`:
#: this page has no measurement.
INK_NOT_MEASURABLE = "ink-not-measurable"

#: The retained runs are the audited ink, less the page-spanning component; a
#: reader of any other schema id is refused rather than reading them as whole-page ink.
INK_RUNS_SCHEMA = "ink-runs.v2"

#: The perimeter strip, a fraction of the page's SHORTER side so every strip has
#: one thickness and `2 * band < min(width, height)` holds by construction; an
#: instrument boundary rather than a calibrated cross-page-act threshold.
EDGE_BAND_BP_FIELD: Final = "edge_band_bp"


class CoverageAuditPolicy(TypedDict):
    """One page's own resolved coverage-audit policy.

    Lengths are resolved to this page's pixels; `page_spanning_area_bp` and
    `gap_tolerance_px` are not lengths and pass through unresolved.
    """

    substantial_ink_pixels: int
    edge_band_px: int
    page_spanning_area_bp: int
    gap_tolerance_px: int
    minimum_ink_pixels: int
    minimum_fraction_outside_bp: int


#: Beside `[grouping.background]` under the one `designator-grouping` seal: the
#: component this audit removes must be the one the Designator withheld, which
#: holds only while both read one `page_spanning_area_bp`.
DEFAULT_COVERAGE_AUDIT_CONFIG_PATH: Final = DEFAULT_BACKGROUND_CONFIG_PATH

COVERAGE_AUDIT_BP_FIELDS: Final = (SUBSTANTIAL_INK_AREA_BP_FIELD, EDGE_BAND_BP_FIELD)

#: A sub-table with its own provenance, so the gates' calibration claim is not
#: over-read onto two values nobody measured.
COVERAGE_NOISE_FLOOR_TABLE: Final = "noise_floor"
COVERAGE_NOISE_FLOOR_FIELDS: Final = (MINIMUM_INK_PIXELS_FIELD, MINIMUM_FRACTION_OUTSIDE_BP_FIELD)


def validate_coverage_audit_table(table: Any, *, where: str = "[coverage_audit]") -> dict[str, int]:
    """The sealed coverage-audit values (two gates and the noise floor), checked and returned.

    Here rather than in the Designator's loader: three stages run under this
    block, and a value one would refuse all three must.
    """
    if not isinstance(table, dict):
        raise ContractError(f"the grouping configuration has no {where} table")
    unexpected = sorted(
        set(table) - set(COVERAGE_AUDIT_BP_FIELDS) - {"provenance", COVERAGE_NOISE_FLOOR_TABLE}
    )
    if unexpected:
        raise ContractError(
            f"the grouping configuration's {where} carries unknown field(s) "
            f"{unexpected}; an unread policy field cannot be applied"
        )
    missing = sorted((set(COVERAGE_AUDIT_BP_FIELDS) | {COVERAGE_NOISE_FLOOR_TABLE}) - set(table))
    if missing:
        raise ContractError(f"the grouping configuration's {where} is missing field(s) {missing}")
    values = {name: table[name] for name in COVERAGE_AUDIT_BP_FIELDS}
    values.update(
        validate_coverage_noise_floor_table(
            table[COVERAGE_NOISE_FLOOR_TABLE], where=f"{where[:-1]}.{COVERAGE_NOISE_FLOOR_TABLE}]"
        )
    )
    if not is_plain_int(values[SUBSTANTIAL_INK_AREA_BP_FIELD]) or not (
        0 < values[SUBSTANTIAL_INK_AREA_BP_FIELD] <= BASIS_POINTS
    ):
        raise ContractError(
            f"the grouping configuration's {where} {SUBSTANTIAL_INK_AREA_BP_FIELD} is not a "
            f"basis-point integer in 1..{BASIS_POINTS}; a gate of zero "
            "flags every page that carries a single unclaimed pixel and says nothing"
        )
    if not is_plain_int(values[EDGE_BAND_BP_FIELD]) or not (
        0 < values[EDGE_BAND_BP_FIELD] < BASIS_POINTS // 2
    ):
        raise ContractError(
            f"the grouping configuration's {where} {EDGE_BAND_BP_FIELD} is not a basis-point "
            f"integer strictly between 0 and {BASIS_POINTS // 2}; a band of zero has no strip "
            "to measure and a band of half the shorter side leaves the page no centre, so "
            "the perimeter measure would be a whole-page measure under another name"
        )
    return values


def validate_coverage_noise_floor_table(
    table: Any, *, where: str = "[coverage_audit.noise_floor]"
) -> dict[str, int]:
    """The two sealed noise-floor values, checked against their bounds and returned."""
    if not isinstance(table, dict):
        raise ContractError(f"the grouping configuration has no {where} table")
    unexpected = sorted(set(table) - set(COVERAGE_NOISE_FLOOR_FIELDS) - {"provenance"})
    if unexpected:
        raise ContractError(
            f"the grouping configuration's {where} carries unknown field(s) "
            f"{unexpected}; an unread policy field cannot be applied"
        )
    missing = sorted(set(COVERAGE_NOISE_FLOOR_FIELDS) - set(table))
    if missing:
        raise ContractError(f"the grouping configuration's {where} is missing field(s) {missing}")
    values = {name: table[name] for name in COVERAGE_NOISE_FLOOR_FIELDS}
    if not is_plain_int(values[MINIMUM_INK_PIXELS_FIELD]) or values[MINIMUM_INK_PIXELS_FIELD] <= 0:
        raise ContractError(
            f"the grouping configuration's {where} {MINIMUM_INK_PIXELS_FIELD} is not a positive "
            "integer; a floor of zero flags every page that carries a single stray pixel"
        )
    if not is_plain_int(values[MINIMUM_FRACTION_OUTSIDE_BP_FIELD]) or not (
        0 < values[MINIMUM_FRACTION_OUTSIDE_BP_FIELD] <= BASIS_POINTS
    ):
        raise ContractError(
            f"the grouping configuration's {where} {MINIMUM_FRACTION_OUTSIDE_BP_FIELD} is not a "
            f"basis-point integer in 1..{BASIS_POINTS}; a fraction of zero fires on every page "
            "that clears the noise floor"
        )
    return values


def load_coverage_audit_config(
    path: str | Path = DEFAULT_COVERAGE_AUDIT_CONFIG_PATH,
) -> dict[str, Any]:
    """The sealed coverage-audit policy and the seal of the file it came from.

    Refused loudly rather than defaulted: a gate silently taken as unlimited
    would change which pages are held with no config line saying so.
    """
    config, digest = read_sealed_toml(path, "coverage-audit configuration")
    grouping = config.get("grouping")
    if not isinstance(grouping, dict):
        raise ContractError("the coverage-audit configuration has no [grouping] table")
    page_area = grouping.get("page_area_bp")
    absolute = grouping.get("absolute")
    if not isinstance(page_area, dict) or not isinstance(absolute, dict):
        raise ContractError(
            "the coverage-audit configuration is missing [grouping.page_area_bp] or "
            "[grouping.absolute]; this audit takes the page-spanning bound and the "
            "stroke-connectivity radius from the same file the Designator withheld under"
        )
    spanning = page_area.get("page_spanning_area_bp")
    gap = absolute.get("gap_tolerance_px")
    if not is_plain_int(spanning) or not 0 < spanning <= BASIS_POINTS:
        raise ContractError(
            "the coverage-audit configuration's [grouping.page_area_bp] page_spanning_area_bp "
            f"is not a basis-point integer in 1..{BASIS_POINTS}"
        )
    if not is_plain_int(gap) or gap < 0:
        raise ContractError(
            "the coverage-audit configuration's [grouping.absolute] gap_tolerance_px is not a "
            "non-negative integer"
        )
    # The page-spanning split is cut at the page's derived margin, so a reader
    # checking a recorded margin needs the sealed fraction it derives from.
    ink_margin_bp = validate_background_table(grouping.get("background"))["ink_margin_bp"]
    audit = config.get("coverage_audit")
    values = validate_coverage_audit_table(audit)
    # Only a shipped file must declare provenance; the table validators also
    # take bare tables built in code.
    validate_provenance_block(audit.get("provenance"), where="[coverage_audit.provenance]")
    validate_provenance_block(
        audit[COVERAGE_NOISE_FLOOR_TABLE].get("provenance"),
        where=f"[coverage_audit.{COVERAGE_NOISE_FLOOR_TABLE}.provenance]",
    )
    return {
        "config_sha256": digest,
        "coverage_audit": values,
        "page_spanning_area_bp": spanning,
        "gap_tolerance_px": gap,
        "ink_margin_bp": ink_margin_bp,
    }


def resolve_coverage_audit_policy(
    config: dict[str, Any], width: int, height: int
) -> CoverageAuditPolicy:
    """One page's own resolved coverage-audit policy.

    `substantial_ink_pixels` is floored at the sealed `minimum_ink_pixels`:
    `coverage_flag` reads the substantial gate before the noise floor, so a
    smaller value would flag a page on a speck. Where the floor binds (pages under
    about 245x245) the two gates coincide; without it a 20x20 page would resolve
    the gate to zero and flag every stray pixel. Both resolutions use
    `common.background.round_half_up_bp`, the one basis-point rounding rule.
    """
    if not is_plain_int(width) or not is_plain_int(height) or width <= 0 or height <= 0:
        raise ContractError(f"page {width}x{height} does not have positive integer dimensions")
    audit = config["coverage_audit"]
    return {
        "substantial_ink_pixels": max(
            audit[MINIMUM_INK_PIXELS_FIELD],
            round_half_up_bp(width * height, audit[SUBSTANTIAL_INK_AREA_BP_FIELD]),
        ),
        # A one-pixel page has no centre but still has an edge.
        "edge_band_px": max(1, round_half_up_bp(min(width, height), audit[EDGE_BAND_BP_FIELD])),
        "page_spanning_area_bp": config["page_spanning_area_bp"],
        "gap_tolerance_px": config["gap_tolerance_px"],
        "minimum_ink_pixels": audit[MINIMUM_INK_PIXELS_FIELD],
        "minimum_fraction_outside_bp": audit[MINIMUM_FRACTION_OUTSIDE_BP_FIELD],
    }


def coverage_flag(
    total_ink_pixels: int,
    outside_ink_pixels: int,
    *,
    substantial_ink_pixels: int,
    minimum_ink_pixels: int,
    minimum_fraction_outside_bp: int,
) -> tuple[float, bool]:
    """The one outside-coverage gate, and the ratio it is read against.

    Either gate flags on its own: the fraction gate catches a miss large relative
    to the page's ink, the substantial gate one large outright. The Armarium's
    export verifier recomputes this over recorded counts, so there is one copy.
    """
    fraction_outside = (outside_ink_pixels / total_ink_pixels) if total_ink_pixels else 0.0
    over_fraction = (
        total_ink_pixels > 0
        and outside_ink_pixels * BASIS_POINTS >= minimum_fraction_outside_bp * total_ink_pixels
    )
    flagged = outside_ink_pixels >= substantial_ink_pixels or (
        outside_ink_pixels >= minimum_ink_pixels and over_fraction
    )
    return fraction_outside, flagged


def _policy_flag(
    total_ink: int, outside_ink: int, coverage_policy: CoverageAuditPolicy
) -> tuple[float, bool]:
    return coverage_flag(
        total_ink,
        outside_ink,
        substantial_ink_pixels=coverage_policy["substantial_ink_pixels"],
        minimum_ink_pixels=coverage_policy["minimum_ink_pixels"],
        minimum_fraction_outside_bp=coverage_policy["minimum_fraction_outside_bp"],
    )


def _edge_band(width: int, height: int, coverage_policy: CoverageAuditPolicy) -> int:
    # At least one pixel: a one-pixel-wide page has no centre but still has an
    # edge, and a zero band would let the Ink Map and the Armarium disagree.
    return min(coverage_policy["edge_band_px"], max(1, width // 2), max(1, height // 2))


def _row_bits(mask: bytearray, y: int, width: int) -> int:
    return int.from_bytes(bytes(mask[y * width : (y + 1) * width]), "big")


def _ink_table(background: int) -> bytes:
    """A 256-entry table mapping every pixel value to 1 (ink) or 0, for `bytes.translate`."""
    # Called for its refusal: a background too dark for this contrast would
    # otherwise give an all-zero table.
    _ink_threshold(background, MINIMUM_CONTRAST_BELOW_BACKGROUND)
    return bytes(
        1 if background - value >= MINIMUM_CONTRAST_BELOW_BACKGROUND else 0 for value in range(256)
    )


def page_background(
    width: int, height: int, rows: list[bytearray], *, background_policy: BackgroundPolicy
) -> dict[str, Any]:
    """This page's paper value and the level this audit thresholds it at.

    The paper value is the shared inference's, the identical call the
    Designator makes on the identical bytes. `ink_margin` and `dark_mode` decide
    nothing here; they are recorded so a reader can tell a disagreement about
    paper from one about sensitivity. Raises `BackgroundInferenceRefusal` when the
    paper cannot be inferred or is too dark to express this contrast.
    """
    evidence = infer_background_evidence(width, height, rows, background_policy=background_policy)
    background = evidence["background"]
    return {
        "background_level": background,
        "background_source": evidence["source"],
        "dark_mode": evidence["dark_mode"],
        "ink_margin": evidence["ink_margin"],
        "contrast_below_background": MINIMUM_CONTRAST_BELOW_BACKGROUND,
        "ink_threshold": _ink_threshold(background, MINIMUM_CONTRAST_BELOW_BACKGROUND),
    }


def page_spanning_components(
    width: int,
    height: int,
    rows: list[bytearray],
    *,
    background_evidence: dict[str, Any],
    coverage_policy: CoverageAuditPolicy,
) -> tuple[list[dict[str, Any]], bytearray]:
    """This page's page-spanning components, and a page-sized 0/1 mask of their pixels.

    The question `pipeline/2_designator/grouping.partition_page_spanning` asks,
    on the same bytes at the same margin, gap tolerance and bound, so the
    component removed here is the one that stage withheld. The mask is needed
    because such a component's bounding box is the whole page.

    One labelling costs about as much as the rest of `residual_ink` (up to
    1.83 s and 207 MB on an 18.4-megapixel page), so `ink_map_page` labels
    once for all of the Ink Map's measures.
    """
    threshold = _ink_threshold(
        background_evidence["background_level"], background_evidence["ink_margin"]
    )
    table = bytes(1 if value <= threshold else 0 for value in range(256))
    runs_by_row: dict[int, list[tuple[int, int]]] = {}
    for y, row in enumerate(rows):
        runs = runs_in_row(row.translate(table))
        if runs:
            runs_by_row[y] = runs
    area = width * height
    found: list[dict[str, Any]] = []
    mask = bytearray(area)
    for component, runs in label_component_runs(
        runs_by_row, gap_tolerance_px=coverage_policy["gap_tolerance_px"]
    ):
        bounds = component["bounds"]
        if (bounds["w"] * bounds["h"] * BASIS_POINTS) // area < coverage_policy[
            "page_spanning_area_bp"
        ]:
            continue
        found.append(dict(component))
        for y, x0, x1 in runs:
            mask[y * width + x0 : y * width + x1] = b"\x01" * (x1 - x0)
    return found, mask


class PageInkBasis(TypedDict):
    """What every measure of one page shares: its paper and its page-spanning ink."""

    background: dict[str, Any]
    spanning_components: list[Bounds]
    spanning_mask: bytearray
    ink_table: bytes


def _page_ink_basis(
    width: int,
    height: int,
    rows: list[bytearray],
    *,
    background_policy: BackgroundPolicy,
    coverage_policy: CoverageAuditPolicy,
) -> PageInkBasis:
    background = page_background(width, height, rows, background_policy=background_policy)
    spanning, spanning_mask = page_spanning_components(
        width, height, rows, background_evidence=background, coverage_policy=coverage_policy
    )
    return {
        "background": background,
        "spanning_components": [component["bounds"] for component in spanning],
        "spanning_mask": spanning_mask,
        "ink_table": _ink_table(background["background_level"]),
    }


def residual_ink(
    width: int,
    height: int,
    rows: list[bytearray],
    covered: list[Bounds],
    *,
    background_policy: BackgroundPolicy,
    coverage_policy: CoverageAuditPolicy,
) -> dict[str, Any]:
    """How much of this page's own ink sits outside every region cut for it.

    `covered` is every proposal and recovery region cut for this page, in page
    pixels as the Designator recorded them. Out-of-page bounds are clipped, not
    refused, so a later stage's own geometry refusal is still reached. Both
    policies must be resolved for this page's own dimensions.

    `total_ink_pixels` and `outside_ink_pixels` exclude the page-spanning
    component; `page_ink_pixels - page_spanning_ink_pixels == total_ink_pixels`.
    """
    basis = _page_ink_basis(
        width, height, rows, background_policy=background_policy, coverage_policy=coverage_policy
    )
    return _residual_counts(width, height, rows, covered, basis, coverage_policy)


def _residual_counts(
    width: int,
    height: int,
    rows: list[bytearray],
    covered: list[Bounds],
    basis: PageInkBasis,
    coverage_policy: CoverageAuditPolicy,
) -> dict[str, Any]:
    covered_mask = bytearray(width * height)
    for bounds in covered:
        x0 = max(0, min(bounds["x"], width))
        y0 = max(0, min(bounds["y"], height))
        x1 = max(x0, min(bounds["x"] + bounds["w"], width))
        y1 = max(y0, min(bounds["y"] + bounds["h"], height))
        span = b"\x01" * (x1 - x0)
        for y in range(y0, y1):
            covered_mask[y * width + x0 : y * width + x1] = span

    # Rows and masks hold only 0 or 1 bytes, so integer `ink & ~covered` is the
    # per-pixel predicate (pinned by
    # `test_the_fast_counts_agree_with_a_straightforward_implementation`).
    ink_table = basis["ink_table"]
    spanning_mask = basis["spanning_mask"]
    page_ink = 0
    total_ink = 0
    outside_ink = 0
    spanning_ink = 0
    for y, row in enumerate(rows):
        ink_row = row.translate(ink_table)
        ink_bits = int.from_bytes(ink_row, "big")
        covered_bits = _row_bits(covered_mask, y, width)
        spanning_bits = _row_bits(spanning_mask, y, width)
        page_ink += ink_row.count(1)
        # The component is found at the Designator's margin, so its mask may hold
        # pixels this audit does not call ink; only this audit's ink is removed.
        spanning_ink += (ink_bits & spanning_bits).bit_count()
        audited_bits = ink_bits & ~spanning_bits
        total_ink += audited_bits.bit_count()
        outside_ink += (audited_bits & ~covered_bits).bit_count()

    fraction_outside, flagged = _policy_flag(total_ink, outside_ink, coverage_policy)
    return {
        "background": basis["background"],
        "page_ink_pixels": page_ink,
        "page_spanning_ink_pixels": spanning_ink,
        "page_spanning_components": list(basis["spanning_components"]),
        "total_ink_pixels": total_ink,
        "outside_ink_pixels": outside_ink,
        "fraction_outside": fraction_outside,
        "flagged": flagged,
        "substantial_ink_pixels": coverage_policy["substantial_ink_pixels"],
    }


def _audited_runs(
    width: int, height: int, rows: list[bytearray], basis: PageInkBasis
) -> dict[str, Any]:
    encoded: list[list[list[int]]] = []
    for y, row in enumerate(rows):
        bits = int.from_bytes(bytes(row.translate(basis["ink_table"])), "big") & ~_row_bits(
            basis["spanning_mask"], y, width
        )
        encoded.append(
            [[start, end - start] for start, end in runs_in_row(bits.to_bytes(width, "big"))]
        )
    return {"schema": INK_RUNS_SCHEMA, "width": width, "height": height, "rows": encoded}


def edge_ink_from_runs(
    evidence: dict[str, Any], covered: list[Bounds], *, coverage_policy: CoverageAuditPolicy
) -> dict[str, Any]:
    """Re-measure the edge finding against later Designator cuts.

    The initial measure precedes all proposals, so it is a candidate finding. A
    later crop may release it only under the same band and gates over these
    retained runs; only the coverage mask may change. `coverage_policy` must be
    resolved for this page's own dimensions.
    """
    width, height, rows = _validated_evidence(evidence)
    band = _edge_band(width, height, coverage_policy)

    def edge_intervals(y: int) -> list[tuple[int, int]]:
        # On a page this narrow the side bands touch, so the row is one interval
        # rather than two that would count a run twice.
        if y < band or y >= height - band or width <= 2 * band:
            return [(0, width)]
        return [(0, band), (width - band, width)]

    total_ink, outside_ink = _ink_outside(width, rows, covered, edge_intervals)
    fraction_outside, flagged = _policy_flag(total_ink, outside_ink, coverage_policy)
    return {
        "total_ink_pixels": total_ink,
        "outside_ink_pixels": outside_ink,
        "fraction_outside": fraction_outside,
        "flagged": flagged,
        "edge_band_pixels": band,
        "substantial_ink_pixels": coverage_policy["substantial_ink_pixels"],
        "named_finding": "unclaimed-edge-ink",
    }


def residual_ink_from_runs(
    evidence: dict[str, Any], covered: list[Bounds], *, coverage_policy: CoverageAuditPolicy
) -> dict[str, Any]:
    """`residual_ink`'s counts and gate over retained ink runs instead of pixels.

    The runs are the Ink Map's audited ink, already without the page-spanning
    component, so `total_ink_pixels` and `outside_ink_pixels` answer the same
    question `residual_ink` answers from the page bytes. `coverage_policy` must be
    resolved for this page's own dimensions.
    """
    width, _height, rows = _validated_evidence(evidence)
    total_ink, outside_ink = _ink_outside(width, rows, covered, lambda _y: [(0, width)])
    fraction_outside, flagged = _policy_flag(total_ink, outside_ink, coverage_policy)
    return {
        "total_ink_pixels": total_ink,
        "outside_ink_pixels": outside_ink,
        "fraction_outside": fraction_outside,
        "flagged": flagged,
        "substantial_ink_pixels": coverage_policy["substantial_ink_pixels"],
    }


def _ink_outside(
    width: int,
    rows: list[Any],
    covered: list[Bounds],
    counted: Callable[[int], list[tuple[int, int]]],
) -> tuple[int, int]:
    """All retained ink, and the ink in each row's `counted` intervals outside `covered`."""
    total_ink = 0
    outside_ink = 0
    for y, row in enumerate(rows):
        runs = _validated_runs(row, width)
        total_ink += sum(end - start for start, end in runs)
        merged_coverage = _row_coverage(covered, y, width)
        for run_start, run_end in runs:
            for interval_start, interval_end in counted(y):
                start, end = max(run_start, interval_start), min(run_end, interval_end)
                if start < end:
                    outside_ink += _uncovered_length(start, end, merged_coverage)
    return total_ink, outside_ink


def _validated_evidence(evidence: Any) -> tuple[int, int, list[Any]]:
    if not isinstance(evidence, dict) or evidence.get("schema") != INK_RUNS_SCHEMA:
        raise ValueError("ink-run evidence has the wrong schema")
    if set(evidence) != {"schema", "width", "height", "rows"}:
        raise ValueError("ink-run evidence is not a closed record")
    width, height, rows = evidence.get("width"), evidence.get("height"), evidence.get("rows")
    if (
        not is_plain_int(width)
        or width <= 0
        or not is_plain_int(height)
        or height <= 0
        or not isinstance(rows, list)
        or len(rows) != height
    ):
        raise ValueError("ink-run evidence has invalid dimensions")
    return width, height, rows


def _validated_runs(row: Any, width: int) -> list[tuple[int, int]]:
    if not isinstance(row, list):
        raise ValueError("ink-run evidence has a malformed row")
    runs: list[tuple[int, int]] = []
    previous_end = 0
    for run in row:
        if not isinstance(run, list) or len(run) != 2 or not all(is_plain_int(v) for v in run):
            raise ValueError("ink-run evidence has a malformed run")
        start, length = run
        end = start + length
        if start < previous_end or length <= 0 or end > width:
            raise ValueError("ink-run evidence has unordered or out-of-bounds runs")
        runs.append((start, end))
        previous_end = end
    return runs


def _row_coverage(covered: list[Bounds], y: int, width: int) -> list[tuple[int, int]]:
    """The covered x-intervals of row `y`, clipped to the page, sorted and merged."""
    intervals = sorted(
        (max(0, min(bounds["x"], width)), max(0, min(bounds["x"] + bounds["w"], width)))
        for bounds in covered
        if bounds["y"] <= y < bounds["y"] + bounds["h"]
    )
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _uncovered_length(start: int, end: int, merged_coverage: list[tuple[int, int]]) -> int:
    uncovered = 0
    cursor = start
    for covered_start, covered_end in merged_coverage:
        if covered_end <= cursor:
            continue
        if covered_start >= end:
            break
        uncovered += max(0, min(covered_start, end) - cursor)
        cursor = max(cursor, covered_end)
        if cursor >= end:
            break
    return uncovered + max(0, end - cursor)


_EDGE_FINDING_FIELDS: Final = frozenset(
    {
        "page_ink_pixels",
        "page_spanning_ink_pixels",
        "page_spanning_components",
        "total_ink_pixels",
        "outside_ink_pixels",
        "fraction_outside_per_million",
        "flagged",
        "substantial_ink_pixels",
        "edge_band_pixels",
        "named_finding",
    }
)
_EDGE_COUNT_FIELDS: Final = (
    "page_ink_pixels",
    "page_spanning_ink_pixels",
    "total_ink_pixels",
    "outside_ink_pixels",
    "fraction_outside_per_million",
    "substantial_ink_pixels",
    "edge_band_pixels",
)
_RUN_DERIVED_EDGE_FIELDS: Final = (
    "total_ink_pixels",
    "outside_ink_pixels",
    "flagged",
    "substantial_ink_pixels",
    "edge_band_pixels",
    "named_finding",
)


def reconcile_edge_finding_with_runs(
    finding: Any,
    evidence: Any,
    *,
    coverage_policy: CoverageAuditPolicy,
    expected_dimensions: tuple[int, int],
) -> dict[str, Any]:
    """Validate and reconcile the Ink Map's two initial edge measurements.

    ``finding`` is the producer's closed summary and ``evidence`` is its retained
    audited-pixel run set, which must span ``expected_dimensions``, the sealed
    page's own ``(width, height)``: runs sized for another page reconcile with
    themselves. The runs cannot reconstruct page-spanning pixels or
    component masks, so those fields receive closed type, range, and partition
    checks here; every value the retained runs can prove must agree exactly.
    """
    if not isinstance(finding, dict) or set(finding) != _EDGE_FINDING_FIELDS:
        raise ContractError("the ink-map edge finding is not a closed current record")
    try:
        measured = edge_ink_from_runs(evidence, [], coverage_policy=coverage_policy)
    except (KeyError, TypeError, ValueError) as error:
        raise ContractError(f"the retained ink-run evidence is invalid: {error}") from error
    if (evidence["width"], evidence["height"]) != tuple(expected_dimensions):
        raise ContractError(
            f"the retained ink-run evidence is {evidence['width']}x{evidence['height']}, not "
            "the sealed page's {}x{}".format(*expected_dimensions)
        )

    for field in _EDGE_COUNT_FIELDS:
        value = finding[field]
        if not is_plain_int(value):
            raise ContractError(f"the ink-map edge finding {field} is not a plain integer")
        floor = 1 if field in {"substantial_ink_pixels", "edge_band_pixels"} else 0
        if value < floor:
            qualifier = "positive" if floor else "non-negative"
            raise ContractError(f"the ink-map edge finding {field} is not a {qualifier} integer")
    if finding["fraction_outside_per_million"] > 1_000_000:
        raise ContractError("the ink-map edge finding fraction exceeds one million")
    if finding["outside_ink_pixels"] > finding["total_ink_pixels"]:
        raise ContractError("the ink-map edge finding has more outside than total ink")
    if finding["page_spanning_ink_pixels"] > finding["page_ink_pixels"]:
        raise ContractError("the ink-map edge finding has more spanning than page ink")
    if finding["page_ink_pixels"] != (
        finding["page_spanning_ink_pixels"] + finding["total_ink_pixels"]
    ):
        raise ContractError("the ink-map edge finding does not partition page ink")
    width, height = evidence["width"], evidence["height"]
    if finding["page_ink_pixels"] > width * height or finding["total_ink_pixels"] > width * height:
        raise ContractError("the ink-map edge finding exceeds the retained page area")
    if not isinstance(finding["flagged"], bool):
        raise ContractError("the ink-map edge finding flagged value is not a boolean")
    if finding["named_finding"] != "unclaimed-edge-ink":
        raise ContractError("the ink-map edge finding has the wrong named finding")

    components = finding["page_spanning_components"]
    if not isinstance(components, list):
        raise ContractError("the ink-map edge finding page-spanning components are not a list")
    for component in components:
        if not isinstance(component, dict) or set(component) != {"x", "y", "w", "h"}:
            raise ContractError("the ink-map edge finding has malformed component bounds")
        x, y, component_width, component_height = (
            component["x"],
            component["y"],
            component["w"],
            component["h"],
        )
        if not all(is_plain_int(value) for value in (x, y, component_width, component_height)):
            raise ContractError("the ink-map edge finding has non-integer component bounds")
        if (
            x < 0
            or y < 0
            or component_width <= 0
            or component_height <= 0
            or x + component_width > width
            or y + component_height > height
        ):
            raise ContractError("the ink-map edge finding has out-of-page component bounds")
    spanning_pixels = finding["page_spanning_ink_pixels"]
    if not components and spanning_pixels:
        raise ContractError(
            "the ink-map edge finding has page-spanning ink without component bounds"
        )
    if spanning_pixels > sum(component["w"] * component["h"] for component in components):
        raise ContractError(
            "the ink-map edge finding page-spanning ink exceeds its component bounds"
        )

    for field in _RUN_DERIVED_EDGE_FIELDS:
        if finding[field] != measured[field] or type(finding[field]) is not type(measured[field]):
            raise ContractError(
                f"the ink-map edge finding {field} disagrees with its retained runs"
            )
    measured_ratio = int(round(measured["fraction_outside"] * 1_000_000))
    if finding["fraction_outside_per_million"] != measured_ratio:
        raise ContractError(
            "the ink-map edge finding fraction_outside_per_million disagrees with its retained runs"
        )
    return measured


def _edge_counts(
    width: int,
    height: int,
    rows: list[bytearray],
    basis: PageInkBasis,
    coverage_policy: CoverageAuditPolicy,
) -> dict[str, Any]:
    band = _edge_band(width, height, coverage_policy)
    covered = (
        []
        if 2 * band >= width or 2 * band >= height
        else [{"x": band, "y": band, "w": width - 2 * band, "h": height - 2 * band}]
    )
    finding = _residual_counts(width, height, rows, covered, basis, coverage_policy)
    return {**finding, "edge_band_pixels": band, "named_finding": "unclaimed-edge-ink"}


def ink_map_page(
    width: int,
    height: int,
    rows: list[bytearray],
    *,
    background_policy: BackgroundPolicy,
    coverage_policy: CoverageAuditPolicy,
) -> dict[str, Any]:
    """Everything the Ink Map publishes for one page, from one inference and one labelling.

    `background` is the page's paper evidence, `edge` the perimeter finding
    (whose `total_ink_pixels` is the page's whole audited ink, the
    pre-proposal denominator) and `edge_findings` the retained audited runs.
    Raises `common.background.BackgroundInferenceRefusal` when the paper cannot
    be inferred or is too dark for this audit's contrast.
    """
    basis = _page_ink_basis(
        width, height, rows, background_policy=background_policy, coverage_policy=coverage_policy
    )
    edge = _edge_counts(width, height, rows, basis, coverage_policy)
    del edge["background"]
    return {
        "background": basis["background"],
        "edge": edge,
        "edge_findings": _audited_runs(width, height, rows, basis),
    }
