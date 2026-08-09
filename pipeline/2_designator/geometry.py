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
# integer rather than rounding it away at the unit's own boundary. (An earlier
# version of this comment cited "0.52 IoU-adjacent figures" as an example of
# that precision; a window read for this build's second pass found that 0.52
# is a median *union IoU* from the old pipeline's own calibration record — a
# different quantity from a per-edge shortfall percentile entirely, and citing
# it here was a loose citation rather than a wrong one. See
# `config/designator_padding.toml`'s `[padding.provenance]` for what this
# project can actually claim about where its own current numbers came from.)
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
# config that can silently omit provenance is exactly what let the old
# numbers travel forward as though this project had validated them (this
# build's window read found the calibration source was never named as
# specifically as "gold annotations" suggested -- see
# `calibrated_for_this_corpus` below). `caveat` is free text for a human
# reader; every other field is read by `load_padding_config` and is expected
# to answer a specific question rather than restate the caveat in other words.
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


def load_padding_config(path: str | Path = DEFAULT_PADDING_CONFIG_PATH) -> dict[str, Any]:
    """Read the padding policy, with the digest that binds it to a run.

    Refused loudly rather than defaulted: a padding fraction silently taken as
    zero would cut a crop nobody configured and no provenance would say so.

    A `[padding.provenance]` table is required, not optional: this build's
    window read found that "calibrated against gold annotations" (the phrase
    the shipped default used to carry alone) was materially incomplete —
    the annotations in question are a third-party corpus, not this project's
    own, and the exact calibration statistic could not be independently
    verified. Requiring the table is what stops that same incompleteness from
    happening again silently: a future padding value with no declared source
    is refused here rather than shipped with an implied claim nobody checked.
    `cut_region` copies this block into every proposal region's `padding`
    field, so a reviewer can see it on the evidence itself, not only in a
    repository file they may never open.
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
        name
        for name in _PADDING_FIELDS
        if not isinstance(values[name], int) or isinstance(values[name], bool) or values[name] < 0
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
    if (
        not isinstance(provenance["sample_count"], int)
        or isinstance(provenance["sample_count"], bool)
        or provenance["sample_count"] < 0
    ):
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
    x, y, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]
    if page_w <= 0 or page_h <= 0:
        raise ContractError(f"a {page_w}x{page_h} page has no positive area to pad within")
    if w <= 0 or h <= 0:
        raise ContractError(f"padding cannot be applied to non-positive bounds {bounds}")
    if x < 0 or y < 0 or x + w > page_w or y + h > page_h:
        raise ContractError(f"structural bounds {bounds} fall outside a {page_w}x{page_h} page")

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
    so `from_model_space` inverts exactly instead of rounding a second time
    away from the first -- which is what "the exact image shown to a model is
    reproducible from the Exemplar plus the recorded transforms" (ARCHITECTURE
    invariant 3) requires of a transform that includes a rescale.
    """
    if page_w <= 0 or page_h <= 0:
        raise ContractError(f"a {page_w}x{page_h} page has no positive dimensions to rescale")
    if model_w <= 0 or model_h <= 0:
        raise ContractError(f"a {model_w}x{model_h} model-space target is not positive")
    scale = {
        "x": {"numerator": model_w, "denominator": page_w},
        "y": {"numerator": model_h, "denominator": page_h},
    }
    model_bounds = {
        "x": (bounds["x"] * model_w) // page_w,
        "y": (bounds["y"] * model_h) // page_h,
        "w": max(1, (bounds["w"] * model_w) // page_w),
        "h": max(1, (bounds["h"] * model_h) // page_h),
    }
    return {"bounds": model_bounds, "scale": scale, "page_w": page_w, "page_h": page_h}


def from_model_space(
    model_bounds: Bounds, scale: dict[str, Any], page_w: int, page_h: int
) -> Bounds:
    """Invert `to_model_space` using the exact ratio it recorded.

    Refuses a scale that is not a positive integer ratio pair, and refuses a
    result that falls outside the page it claims to belong to -- a rescale
    computed against one page's dimensions and applied to another's is the
    same class of silent corruption a mismatched digest catches for bytes.
    """
    axes = {}
    for axis in ("x", "y"):
        ratio = scale.get(axis)
        if not isinstance(ratio, dict):
            raise ContractError(f"scale has no {axis!r} ratio")
        numerator, denominator = ratio.get("numerator"), ratio.get("denominator")
        if (
            not isinstance(numerator, int)
            or isinstance(numerator, bool)
            or not isinstance(denominator, int)
            or isinstance(denominator, bool)
            or numerator <= 0
            or denominator <= 0
        ):
            raise ContractError(f"scale.{axis} {ratio} is not a positive integer ratio")
        axes[axis] = (numerator, denominator)

    x_num, x_den = axes["x"]
    y_num, y_den = axes["y"]
    bounds: Bounds = {
        "x": (model_bounds["x"] * x_den) // x_num,
        "y": (model_bounds["y"] * y_den) // y_num,
        "w": max(1, (model_bounds["w"] * x_den) // x_num),
        "h": max(1, (model_bounds["h"] * y_den) // y_num),
    }
    if (
        bounds["x"] < 0
        or bounds["y"] < 0
        or bounds["x"] + bounds["w"] > page_w
        or bounds["y"] + bounds["h"] > page_h
    ):
        raise ContractError(
            f"rescaling {model_bounds} by {scale} lands outside a {page_w}x{page_h} page; "
            "the scale does not belong to this page"
        )
    return bounds


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
    for axis, other in (("x", "y"), ("y", "x")):
        ratio = scale.get(axis)
        other_ratio = scale.get(other)
        if not isinstance(ratio, dict) or not isinstance(other_ratio, dict):
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
