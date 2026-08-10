"""Geometry: padding, coordinate-space conversion, and transform digests.

All bounds a stage program stores are in one space only: full-resolution page
pixels. A structure-pass model may still want its own downscaled input, and
that downscaled space is real -- but it is a conversion at the model's edge,
performed once, recorded, and never itself stored as though it were page
geometry. The old pipeline's own audit trail names the opposite -- a
downscaled bounding box used raw against the full-resolution page -- as a
real, historically observed defect class ("narrow left-margin crops"), and
names the fix that held: record both axes' scale factors and refuse an
anisotropic result rather than trust a single ratio silently. `verify_isotropic`
below is that same discipline, kept as a hard refusal rather than a warning.

Two padding roles exist and must never be conflated. *Structural* bounds are
what grouping decided a region's own rectangle is, and an act's identity is
bound to them (`common/contracts/identities.py::act_bindings`) -- recropping
must never move them. *Capture* bounds are what is actually cut and shown to
a witness: the structural rectangle expanded by configured padding, generous
on purpose so a signature extending past the body is not clipped. This module
computes capture bounds from structural bounds; it never mutates the latter.

Every fraction here is an integer count of basis points (1/10000), never a
float: `common/contracts/canonical.py` refuses a float anywhere a payload is
canonicalized, and a padding fraction is exactly the kind of value that would
otherwise cross that boundary silently the first time someone read a percent
sign as a Python float literal.
"""

import tomllib
from pathlib import Path
from typing import Any, Final, TypedDict

from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import ContractError

DEFAULT_PADDING_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "designator_padding.toml"
)

# 1 basis point = 1/10000. Chosen over percent-as-integer (1/100) because a
# percentile-of-shortfall calibration (see `padding_calibration.py`) can land
# on a fractional percent, and basis points hold that precision as an exact
# integer rather than rounding it away at the unit's own boundary.
BP_DENOMINATOR: Final = 10_000

# How far two independently-computed axis scale factors may differ, in basis
# points of the larger, before a rescale is refused as anisotropic rather than
# accepted. Zero would refuse any integer-rounding noise at all; this allows
# exactly the rounding a rational scale over small integer dimensions produces
# and nothing structural.
_DEFAULT_ANISOTROPY_TOLERANCE_BP: Final = 50

_PADDING_FIELDS: Final = ("top_bp", "bottom_bp", "left_bp", "right_bp")

# Every field a padding config's `[padding.provenance]` table must carry, and
# the only fields it may carry -- a closed schema, not a convention, because a
# config that can silently omit provenance is what lets a number carried from
# somewhere else travel forward as though this project had validated it.
# `caveat` is free text for a human reader; every other field is read by
# `load_padding_config` and is expected to answer a specific question rather
# than restate the caveat in other words.
_PROVENANCE_FIELDS: Final = (
    "source",
    "corpus",
    "sample_unit",
    "sample_count",
    "statistic",
    "calibrated_for_this_corpus",
    "caveat",
)


class Bounds(TypedDict):
    x: int
    y: int
    w: int
    h: int


def _is_plain_int(value: Any) -> bool:
    """An `int` that is not a `bool`.

    `bool` is an `int` subclass, so an unqualified `isinstance` check reads
    `True` as the coordinate `1`. A hand-edited `h = true` in a fixture would
    otherwise cut a one-pixel-tall crop and publish it as an act's evidence
    without a single refusal anywhere.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def validate_bounds(bounds: Any, width: int, height: int, what: str) -> None:
    """Refuse a rectangle that does not belong to its declared pixel space."""
    if not isinstance(bounds, dict) or set(bounds) != {"x", "y", "w", "h"}:
        raise ContractError(f"{what} is not a closed x/y/w/h rectangle")
    if not all(_is_plain_int(bounds[field]) for field in ("x", "y", "w", "h")):
        raise ContractError(f"{what} has a non-integer coordinate")
    x, y, w, h = (bounds[field] for field in ("x", "y", "w", "h"))
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise ContractError(f"{what} {bounds} falls outside its {width}x{height} pixel space")


def _validate_dimensions(width: Any, height: Any, what: str) -> None:
    if not _is_plain_int(width) or not _is_plain_int(height) or width <= 0 or height <= 0:
        raise ContractError(f"{what} {width}x{height} does not have positive integer dimensions")


def load_padding_config(path: str | Path = DEFAULT_PADDING_CONFIG_PATH) -> dict[str, Any]:
    """Read the padding policy, with the digest that binds it to a run.

    Refused loudly rather than defaulted: a padding fraction silently taken as
    zero would cut a crop nobody configured and no provenance would say so.

    A `[padding.provenance]` table is required, not optional. A padding value
    with no declared source is refused here rather than shipped with an implied
    claim nobody checked — "calibrated against gold annotations" is the kind of
    phrase that reads as validation while naming neither the corpus nor the
    statistic. `cut_region` copies this block onto every proposal region, so a
    reviewer sees it on the evidence itself rather than only in a repository
    file they may never open.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
        config = tomllib.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"the padding configuration at {path} could not be read: {error}"
        ) from error
    padding = config.get("padding") if isinstance(config, dict) else None
    if not isinstance(padding, dict):
        raise ContractError("the padding configuration has no [padding] table")
    values = {name: padding.get(name) for name in _PADDING_FIELDS}
    invalid = [
        name for name in _PADDING_FIELDS if not _is_plain_int(values[name]) or values[name] < 0
    ]
    if invalid:
        raise ContractError(
            f"the padding configuration has invalid non-negative integer field(s) {invalid}"
        )
    provenance = _load_padding_provenance(padding.get("provenance"))
    return {"config_sha256": digest_bytes(data), "provenance": provenance, **values}


def _load_padding_provenance(provenance: Any) -> dict[str, Any]:
    """Validate a padding config's declared provenance against its closed schema.

    Every field is required and every field is checked for shape, because a
    provenance block that is present but says nothing (an empty string, a
    zero sample count with no caveat explaining why) would satisfy "the table
    exists" while failing the actual point of requiring one.
    """
    if not isinstance(provenance, dict):
        raise ContractError(
            "the padding configuration has no [padding.provenance] table; a padding "
            "fraction with no declared source may not be shipped as a default"
        )
    unexpected = sorted(set(provenance) - set(_PROVENANCE_FIELDS))
    if unexpected:
        raise ContractError(
            f"the padding configuration's provenance carries unknown field(s) {unexpected}; "
            "provenance is a closed schema so an unread field cannot be trusted"
        )
    missing = sorted(set(_PROVENANCE_FIELDS) - set(provenance))
    if missing:
        raise ContractError(f"the padding configuration's provenance is missing field(s) {missing}")
    for field in ("source", "corpus", "sample_unit", "statistic", "caveat"):
        if not isinstance(provenance[field], str) or not provenance[field].strip():
            raise ContractError(
                f"the padding configuration's provenance field {field!r} is not a non-empty string"
            )
    if not _is_plain_int(provenance["sample_count"]) or provenance["sample_count"] < 0:
        raise ContractError(
            "the padding configuration's provenance sample_count is not a non-negative integer"
        )
    if not isinstance(provenance["calibrated_for_this_corpus"], bool):
        raise ContractError(
            "the padding configuration's provenance calibrated_for_this_corpus is not a boolean"
        )
    return dict(provenance)


def _pad_amount(dimension: int, bp: int) -> int:
    """Round-half-up pixel amount for a basis-point fraction of one dimension.

    Round-half-up, not banker's rounding or truncation, so the amount actually
    applied is deterministic and independent of Python's float rounding rules
    -- this is pure integer arithmetic and never touches a float at all.
    """
    return (dimension * bp + BP_DENOMINATOR // 2) // BP_DENOMINATOR


def apply_padding(
    bounds: Bounds, page_w: int, page_h: int, padding: dict[str, Any]
) -> dict[str, Any]:
    """Expand structural `bounds` into capture bounds, clamped to the page.

    Padding is a fraction of the region's OWN width/height, not the page's --
    a signature below a short entry needs less absolute margin than one below
    a long one, and a page-fraction pad would either starve the short entry or
    bloat the long one. Returns both the final bounds and the exact pixel
    amount actually applied per edge, which is not always the nominal amount:
    clamping at a page edge shaves it, and a caller that only recorded the
    configured fraction would describe a crop bigger than the one it cut.
    """
    _validate_dimensions(page_w, page_h, "page")
    validate_bounds(bounds, page_w, page_h, "structural bounds")
    x, y, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]

    top = _pad_amount(h, padding["top_bp"])
    bottom = _pad_amount(h, padding["bottom_bp"])
    left = _pad_amount(w, padding["left_bp"])
    right = _pad_amount(w, padding["right_bp"])

    padded_x0 = max(0, x - left)
    padded_y0 = max(0, y - top)
    padded_x1 = min(page_w, x + w + right)
    padded_y1 = min(page_h, y + h + bottom)

    return {
        "bounds": {
            "x": padded_x0,
            "y": padded_y0,
            "w": padded_x1 - padded_x0,
            "h": padded_y1 - padded_y0,
        },
        "applied_px": {
            "top": y - padded_y0,
            "bottom": padded_y1 - (y + h),
            "left": x - padded_x0,
            "right": padded_x1 - (x + w),
        },
        "configured_bp": {
            "top": padding["top_bp"],
            "bottom": padding["bottom_bp"],
            "left": padding["left_bp"],
            "right": padding["right_bp"],
        },
    }


def to_model_space(bounds: Bounds, page_w: int, page_h: int, model_w: int, model_h: int) -> dict:
    """Downscale full-res `bounds` to a model's own input geometry, once.

    The two axis scales are kept as exact integer ratios rather than floats,
    so `from_model_space` inverts against the same arithmetic instead of
    rounding a second time away from the first -- which is what "the exact
    image shown to a model is reproducible from the Exemplar plus the recorded
    transforms" (ARCHITECTURE invariant 3) requires of a transform that
    includes a rescale.

    **Low edges floor, far edges ceil**, the same one-sided rule
    `from_model_space` uses coming back, so the model-space rectangle always
    covers the image of the source rectangle rather than undercutting it. The
    width is derived as `far - near` rather than scaled on its own: scaling a
    width independently of its origin floors twice against the same edge, and
    the two roundings compound into a rectangle that is genuinely short of the
    ink it was supposed to enclose.
    """
    _validate_dimensions(page_w, page_h, "page")
    _validate_dimensions(model_w, model_h, "model-space target")
    validate_bounds(bounds, page_w, page_h, "source bounds")
    scale = {
        "x": {"numerator": model_w, "denominator": page_w},
        "y": {"numerator": model_h, "denominator": page_h},
    }
    x0 = (bounds["x"] * model_w) // page_w
    y0 = (bounds["y"] * model_h) // page_h
    x1 = min(model_w, -((-(bounds["x"] + bounds["w"]) * model_w) // page_w))
    y1 = min(model_h, -((-(bounds["y"] + bounds["h"]) * model_h) // page_h))
    model_bounds = {"x": x0, "y": y0, "w": max(1, x1 - x0), "h": max(1, y1 - y0)}
    return {"bounds": model_bounds, "scale": scale, "page_w": page_w, "page_h": page_h}


def from_model_space(
    model_bounds: Bounds, scale: dict[str, Any], page_w: int, page_h: int
) -> Bounds:
    """Invert `to_model_space` using the exact ratio it recorded, rounding outward.

    Refuses a scale that is not a positive integer ratio pair, and refuses a
    result that falls outside the page it claims to belong to -- a rescale
    computed against one page's dimensions and applied to another's is the
    same class of silent corruption a mismatched digest catches for bytes.

    **Low edges floor, far edges ceil**, so a rectangle that survives a round
    trip through model space can only ever grow, never shrink. Rounding both
    edges the same way loses up to a pixel on each far edge, and the direction
    of that loss is the whole point: a shaved far edge is a clipped signature,
    and GOALS 1 puts a missed act above a poorly read one. (The lane-B build of
    this stage reached the same rule independently in `source_bounds_from_view`
    and named it the same way: it "cannot round a source pixel out of the
    emitted crop".)

    The out-of-page refusal is deliberately tested against the *floored* far
    edge rather than the ceiled one, so a rescale genuinely belonging to a
    different page is still refused while a single pixel of outward rounding at
    the true page edge is merely clamped.
    """
    _validate_dimensions(page_w, page_h, "page")
    if not isinstance(scale, dict):
        raise ContractError("scale is not an x/y ratio object")
    axes = {}
    for axis in ("x", "y"):
        ratio = scale.get(axis)
        if not isinstance(ratio, dict):
            raise ContractError(f"scale has no {axis!r} ratio")
        numerator, denominator = ratio.get("numerator"), ratio.get("denominator")
        if (
            not _is_plain_int(numerator)
            or not _is_plain_int(denominator)
            or numerator <= 0
            or denominator <= 0
        ):
            raise ContractError(f"scale.{axis} {ratio} is not a positive integer ratio")
        axes[axis] = (numerator, denominator)

    x_num, x_den = axes["x"]
    y_num, y_den = axes["y"]
    if x_den != page_w or y_den != page_h:
        raise ContractError(
            f"scale {scale} was recorded for a {x_den}x{y_den} page, not "
            f"the declared {page_w}x{page_h} page"
        )
    validate_bounds(model_bounds, x_num, y_num, "model-space bounds")
    x0 = (model_bounds["x"] * x_den) // x_num
    y0 = (model_bounds["y"] * y_den) // y_num
    far_x = (model_bounds["x"] + model_bounds["w"]) * x_den
    far_y = (model_bounds["y"] + model_bounds["h"]) * y_den
    if x0 < 0 or y0 < 0 or far_x // x_num > page_w or far_y // y_num > page_h:
        raise ContractError(
            f"rescaling {model_bounds} by {scale} lands outside a {page_w}x{page_h} page; "
            "the scale does not belong to this page"
        )
    x1 = min(page_w, -((-far_x) // x_num))
    y1 = min(page_h, -((-far_y) // y_num))
    return {"x": x0, "y": y0, "w": max(1, x1 - x0), "h": max(1, y1 - y0)}


def verify_isotropic(
    scale: dict[str, Any], *, tolerance_bp: int = _DEFAULT_ANISOTROPY_TOLERANCE_BP
) -> None:
    """Refuse a rescale whose two axes disagree beyond rounding noise.

    A structure-pass model that is supposed to letterbox (preserve aspect
    ratio) while resizing should produce equal x and y scale factors; one that
    silently squished the image instead would not. This is the check the old
    pipeline's margin-recovery tooling ran and warned was mandatory -- kept
    here as a hard refusal rather than a value recorded for a human to notice
    later, because a distorted geometry used anyway is exactly the "narrow
    left-margin crops" defect class this module exists to close.
    """
    for axis in ("x", "y"):
        if not isinstance(scale.get(axis), dict):
            raise ContractError(f"scale has no {axis!r} ratio to check for anisotropy")
    x_num, x_den = scale["x"]["numerator"], scale["x"]["denominator"]
    y_num, y_den = scale["y"]["numerator"], scale["y"]["denominator"]
    # Compare x_num/x_den to y_num/y_den without division: cross-multiply, then
    # express the relative difference in basis points of the larger product.
    left = x_num * y_den
    right = y_num * x_den
    difference = abs(left - right)
    denominator = max(left, right)
    if denominator == 0:
        raise ContractError("scale has a zero ratio and cannot be checked for anisotropy")
    difference_bp = (difference * BP_DENOMINATOR) // denominator
    if difference_bp > tolerance_bp:
        raise ContractError(
            f"rescale is anisotropic: x scale {x_num}/{x_den} and y scale {y_num}/{y_den} "
            f"differ by {difference_bp} basis points, above the {tolerance_bp}-basis-point "
            "tolerance; a distorted geometry is refused rather than used"
        )


def transform_digest(transform: dict[str, Any]) -> str:
    """The stable content digest of one transform, independent of act binding.

    `common/contracts/identities.py::region_id` already binds a region's
    identity to `(act_id, transform)` as a whole; this is the same digest
    taken alone, for a provenance field that names "this exact transform" on
    its own rather than through the region identity that also carries the act.
    """
    return digest_of(transform)
