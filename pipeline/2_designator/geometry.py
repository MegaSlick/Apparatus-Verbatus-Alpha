"""Geometry: padding, coordinate-space conversion, and transform digests.

All stored bounds are full-resolution page pixels; a structure-pass model's
own downscaled space is only a conversion at the model's edge, done once and
recorded, never stored as page geometry. `to_model_space`/`from_model_space`
round outward on both axes so a rectangle can only grow across the round trip,
never clip a signature; `verify_isotropic` hard-refuses a rescale whose two
axes disagree beyond rounding noise (a squished image used anyway). None of
the three is called by this walking skeleton today -- they exist,
property-tested, for the model that will need its own input size.

Two padding roles must never be conflated: *structural* bounds are a region's
identity-bound rectangle (never moved by recropping); *capture* bounds are
that rectangle expanded by configured padding for what a witness actually
sees. This module derives capture bounds from structural bounds and never
mutates the latter.

Every fraction here is an integer count of basis points (1/10000), never a
float, because `common/contracts/canonical.py` refuses a float anywhere a
payload is canonicalized.
"""

from pathlib import Path
from typing import Any, Final, TypedDict

from common.background import round_half_up_bp
from common.calibration import calibrated_claim_has_sample_evidence
from common.contracts.canonical import digest_of
from common.contracts.errors import ContractError
from common.sealed_config import read_sealed_toml

DEFAULT_PADDING_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "designator_padding.toml"
)

# 1 basis point = 1/10000, chosen over percent-as-integer (1/100) because a
# percentile-of-shortfall calibration can land on a fractional percent.
BP_DENOMINATOR: Final = 10_000

# Basis points of the larger axis scale factor allowed before a rescale is
# refused as anisotropic; covers integer-rounding noise only, nothing structural.
_DEFAULT_ANISOTROPY_TOLERANCE_BP: Final = 50

_PADDING_FIELDS: Final = ("top_bp", "bottom_bp", "left_bp", "right_bp")

# Closed schema for a padding config's [padding.provenance] table: a config
# that can silently omit provenance lets an unvalidated number pass as this
# project's own. `caveat` is free text; the rest answer a specific question.
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
    """An `int` that is not a `bool` (`bool` is an `int` subclass in Python)."""
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

    Missing or malformed fields are refused loudly rather than defaulted, and a
    `[padding.provenance]` table is required: `cut_region` copies it onto every
    proposal region, so a reviewer sees the padding's source on the evidence
    itself rather than only in a repository file they may never open.
    """
    config, digest = read_sealed_toml(path, "padding configuration", {"padding"})
    padding = config.get("padding")
    if not isinstance(padding, dict):
        raise ContractError("the padding configuration has no [padding] table")
    unexpected = sorted(set(padding) - (set(_PADDING_FIELDS) | {"provenance"}))
    if unexpected:
        raise ContractError(
            f"the padding configuration has unknown field(s) {unexpected}; "
            "an unread policy field cannot be applied"
        )
    values = {name: padding.get(name) for name in _PADDING_FIELDS}
    invalid = [
        name for name in _PADDING_FIELDS if not _is_plain_int(values[name]) or values[name] < 0
    ]
    if invalid:
        raise ContractError(
            f"the padding configuration has invalid non-negative integer field(s) {invalid}"
        )
    provenance = _load_padding_provenance(padding.get("provenance"))
    return {"config_sha256": digest, "provenance": provenance, **values}


def _load_padding_provenance(provenance: Any) -> dict[str, Any]:
    """Validate a padding config's declared provenance against its closed schema.

    Every field is required and checked for shape, so a table that is present
    but says nothing (an empty string, an unexplained zero sample count) still
    fails validation.
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
    if not calibrated_claim_has_sample_evidence(
        provenance["calibrated_for_this_corpus"], provenance["sample_count"]
    ):
        raise ContractError(
            "the padding configuration's provenance says calibrated_for_this_corpus but "
            "sample_count is zero"
        )
    return dict(provenance)


def _pad_amount(dimension: int, bp: int) -> int:
    """Round-half-up pixel amount for a basis-point fraction of one dimension.

    Delegates to `common.background.round_half_up_bp`, shared with the Ink Map
    and the Recensor so the rounding rule isn't duplicated three times;
    `test_geometry.py` pins `BP_DENOMINATOR` equal to that module's `BASIS_POINTS`.
    """
    return round_half_up_bp(dimension, bp)


def apply_padding(
    bounds: Bounds, page_w: int, page_h: int, padding: dict[str, Any]
) -> dict[str, Any]:
    """Expand structural `bounds` into capture bounds, clamped to the page.

    Padding is a fraction of the region's OWN width/height, not the page's, so
    a short entry doesn't get the same absolute margin as a long one. Also
    returns the exact pixel amount applied per edge, which page-edge clamping
    can shave below the nominal configured fraction.
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

    Axis scales are kept as exact integer ratios so `from_model_space` inverts
    against the same arithmetic rather than rounding a second time. Low edges
    floor and far edges ceil so the model-space rectangle always covers the
    source rectangle; width is derived as `far - near`, not scaled on its own,
    so the two roundings don't compound into an undersized box.
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

    Refuses a scale that isn't a positive integer ratio pair recorded for this
    exact page, and refuses a result outside the page. Low edges floor and far
    edges ceil, so a round trip through model space can only grow a rectangle,
    never shave a far edge into a clipped signature.
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
    x1 = min(page_w, -((-far_x) // x_num))
    y1 = min(page_h, -((-far_y) // y_num))
    return {"x": x0, "y": y0, "w": max(1, x1 - x0), "h": max(1, y1 - y0)}


def verify_isotropic(
    scale: dict[str, Any], *, tolerance_bp: int = _DEFAULT_ANISOTROPY_TOLERANCE_BP
) -> None:
    """Refuse a rescale whose two axes disagree beyond rounding noise.

    A model that letterboxes (preserves aspect ratio) while resizing should
    produce equal x/y scale factors; one that silently squished the image
    would not, and a distorted geometry used anyway is refused rather than
    left for a human to notice later.
    """
    if not _is_plain_int(tolerance_bp) or tolerance_bp < 0:
        raise ContractError(
            f"anisotropy tolerance {tolerance_bp!r} is not a non-negative plain integer"
        )
    axes: dict[str, tuple[int, int]] = {}
    for axis in ("x", "y"):
        ratio = scale.get(axis)
        if not isinstance(ratio, dict):
            raise ContractError(f"scale has no {axis!r} ratio to check for anisotropy")
        numerator = ratio.get("numerator")
        denominator = ratio.get("denominator")
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
    # Cross-multiply to compare the two ratios without division.
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

    Distinct from `region_id`, which binds `(act_id, transform)` together; this
    names the transform alone for a provenance field.
    """
    return digest_of(transform)
