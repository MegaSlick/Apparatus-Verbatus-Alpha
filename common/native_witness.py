"""The closed, derived waist of a native witness report.

Native responses remain in their retained raw blob.  These values are the small
set this pipeline derives from them: what image was presented and the page-pixel
rectangles a witness reported seeing.  They deliberately contain no act identity
or preference: correspondence is a consumer lookup, never witness testimony.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES, writing_directory
from common.corpus_register import _refuse_preference
from common.imaging import crop_png

PRESENTATION_KINDS: Final = frozenset({"page", "region", "adapter-crop"})
# `native`  — geometry the witness itself reported.
# `derived` — geometry this pipeline computed from what the witness reported.
# `presented` — NOT witness geometry at all: a restatement of the box that was
#   presented, recorded for a witness whose response carries no geometry.  It
#   says "this report pertains to this image", never "the witness saw ink here",
#   so no coverage derivation may read it as observed ink.  A whole-page
#   presentation restated this way would otherwise contain every proposal on the
#   page and hand a chair with no geometry at all complete witness coverage --
#   a measurement that measured nothing (GOVERNANCE 10).  Unit 10C owns that
#   derivation and must exclude this value from it.
BOUNDS_SOURCES: Final = frozenset({"native", "derived", "presented"})
# The two values above that are evidence of ink a witness actually reported.
REPORTED_BOUNDS_SOURCES: Final = frozenset({"native", "derived"})
_BOUNDS_FIELDS: Final = frozenset({"x", "y", "w", "h"})
PAGE_TESTIMONIUM_REQUIRED_FIELDS: Final = frozenset(
    {
        "chair",
        "act_key",
        "attempt_ordinal",
        "regions",
        "provenance",
        "format_capabilities",
        "payload",
        "witness_reported",
        "content_health",
        "presented",
        "observed",
        "unpresented_regions",
        "scope",
        "page_ordinal",
        "page_role",
        "unjoined_act_attempts",
    }
)
PAGE_TESTIMONIUM_OPTIONAL_FIELDS: Final = frozenset(
    {"reason", "reported", "partition_disagreement", "native_capture"}
)
PAGE_ROLES: Final = frozenset({"primary", "continuation", "mixed"})

# The serving request is already bounded at 24,000 generated tokens.  Four MiB
# still allows more than 174 UTF-8 response bytes per requested token -- far
# beyond an OCR transcription -- while giving the XML parser and the post-hoc
# repetition scan a hard ceiling when a provider ignores that request bound.
CHURRO_OUTPUT_TOKENS: Final = 24_000
CHURRO_MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
_CHURRO_REPETITION_WINDOW: Final = 24
_CHURRO_REPETITION_MIN_REPEATS: Final = 3


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _refuse_float(value: Any, what: str) -> None:
    if isinstance(value, float):
        raise SchemaRefusal(f"{what} carries a float; derived witness geometry is integer pixels")
    if isinstance(value, dict):
        for key, item in value.items():
            _refuse_float(item, f"{what}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _refuse_float(item, f"{what}[{index}]")


def _bounds(value: Any, what: str, *, page_size: tuple[int, int] | None) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != _BOUNDS_FIELDS:
        raise SchemaRefusal(f"{what} is not the closed {{x, y, w, h}} page-pixel box")
    if any(isinstance(value[key], float) for key in _BOUNDS_FIELDS):
        raise SchemaRefusal(f"{what} carries a float; derived witness geometry is integer pixels")
    if not all(_integer(value[key]) for key in _BOUNDS_FIELDS):
        raise SchemaRefusal(f"{what} has non-integer page-pixel coordinates")
    if value["x"] < 0 or value["y"] < 0 or value["w"] <= 0 or value["h"] <= 0:
        raise SchemaRefusal(f"{what} is not a non-empty non-negative page-pixel box")
    if page_size is not None and (
        value["x"] + value["w"] > page_size[0] or value["y"] + value["h"] > page_size[1]
    ):
        raise SchemaRefusal(f"{what} falls outside the sealed source page")
    return value


def validate_presented(value: Any, *, page_size: tuple[int, int] | None = None) -> dict[str, Any]:
    """Validate the exact image presentation and its page-space recipe."""
    if not isinstance(value, dict):
        raise SchemaRefusal("a Testimonium presented block is not an object")
    kind = value.get("kind")
    required = {
        "kind",
        "source_page_id",
        "source_page_ordinal",
        "image_path",
        "image_sha256",
        "transform",
    }
    if kind == "region":
        required.add("region_ref")
    if set(value) != required:
        raise SchemaRefusal("a Testimonium presented block is not its closed kind-specific schema")
    if not isinstance(kind, str) or kind not in PRESENTATION_KINDS:
        raise SchemaRefusal("a Testimonium presented block has an unknown presentation kind")
    if (
        not isinstance(value["source_page_id"], str)
        or not value["source_page_id"]
        or not _integer(value["source_page_ordinal"])
        or value["source_page_ordinal"] < 1
        or not isinstance(value["image_path"], str)
        or not value["image_path"]
        or not isinstance(value["image_sha256"], str)
        or len(value["image_sha256"]) != 64
    ):
        raise SchemaRefusal("a Testimonium presented block has invalid source or blob identity")
    transform = value["transform"]
    if not isinstance(transform, dict) or set(transform) != {
        "operation",
        "source_page_ordinal",
        "source_page_id",
        "bounds",
    }:
        raise SchemaRefusal("a Testimonium presented block has no complete page transform")
    if (
        not isinstance(transform["operation"], str)
        or transform["source_page_id"] != value["source_page_id"]
        or transform["source_page_ordinal"] != value["source_page_ordinal"]
    ):
        raise SchemaRefusal("a Testimonium presented transform disagrees with its source page")
    _bounds(transform["bounds"], "a Testimonium presented transform", page_size=page_size)
    if kind == "region":
        ref = value["region_ref"]
        if (
            not isinstance(ref, dict)
            or set(ref) != {"region_id"}
            or not isinstance(ref["region_id"], str)
            or not ref["region_id"]
        ):
            raise SchemaRefusal("a region presentation has no closed region_ref")
    _refuse_float(value, "a Testimonium presented block")
    return value


def validate_observed(
    value: Any,
    *,
    presented: dict[str, Any],
    page_size: tuple[int, int] | None = None,
    retained_text: Any = None,
) -> list[dict[str, Any]]:
    """Validate dense witness order, source-page boxes, and non-overlapping text spans.

    A span addresses **this Testimonium's own retained text**, in code points of
    the string exactly as retained -- not of the NFC-normalized, whitespace-
    collapsed view `common/alignment.py` derives, whose offsets are recoverable
    from that view's own `offset_map`. A span was previously checked only for
    shape, so `{"start": 0, "end": 10_000}` over a forty-character reading
    validated and addressed text no record holds: a consumer resolving it either
    crashes or silently truncates, and a witness's report would then be quoted
    from ink nobody retained (GOALS 5). An observation may still carry
    `span: null`; what it may not do is name an offset the record cannot answer.
    """
    if not isinstance(value, list):
        raise SchemaRefusal("a Testimonium observed block is not a list")
    spans: list[tuple[int, int]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {
            "ordinal",
            "bounds",
            "bounds_source",
            "span",
        }:
            raise SchemaRefusal("a Testimonium observed entry is not its closed schema")
        if not _integer(item["ordinal"]) or item["ordinal"] != index:
            raise SchemaRefusal("a Testimonium observed ordinals are not dense, unique, 0-based")
        if (
            not isinstance(item["bounds_source"], str)
            or item["bounds_source"] not in BOUNDS_SOURCES
        ):
            raise SchemaRefusal("a Testimonium observed box has an unknown bounds_source")
        if (
            item["bounds_source"] == "presented"
            and item["bounds"] != presented["transform"]["bounds"]
        ):
            raise SchemaRefusal(
                "a presented-source observed box differs from the presented transform"
            )
        _bounds(item["bounds"], "a Testimonium observed box", page_size=page_size)
        span = item["span"]
        if span is not None:
            if (
                not isinstance(span, dict)
                or set(span) != {"start", "end"}
                or not _integer(span["start"])
                or not _integer(span["end"])
                or span["start"] < 0
                or span["end"] < span["start"]
            ):
                raise SchemaRefusal(
                    "a Testimonium observed span is not a non-negative [start, end) span"
                )
            if not isinstance(retained_text, str):
                raise SchemaRefusal(
                    "a Testimonium observed span addresses text this record does not retain"
                )
            if span["end"] > len(retained_text):
                raise SchemaRefusal(
                    "a Testimonium observed span runs past the end of its own retained text"
                )
            spans.append((span["start"], span["end"]))
    for previous, current in zip(sorted(spans), sorted(spans)[1:], strict=False):
        if current[0] < previous[1]:
            raise SchemaRefusal("a Testimonium observed spans overlap")
    _refuse_float(value, "a Testimonium observed block")
    return value


def validate_native_witness_geometry(
    payload: Any, *, page_size: tuple[int, int] | None = None
) -> dict[str, Any]:
    """Validate the two derived blocks and recursively refuse preference claims.

    An empty pair records the distinct, visible fact that the chair was never
    shown an image.  It is required for held acts, refused pages, and explicitly
    absent chairs: inventing a whole-page presentation for any of those paths
    would claim evidence that did not exist.  Once a presentation exists, the
    ordinary closed geometry contract applies unchanged.
    """
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium payload is not an object")
    _refuse_preference(payload)
    presented = payload.get("presented")
    observed = payload.get("observed")
    if presented == {}:
        if observed != []:
            raise SchemaRefusal("an unpresented Testimonium must carry an empty observed block")
        return payload
    presented = validate_presented(presented, page_size=page_size)
    # The retained text a span may address lives under `payload` on both kinds:
    # `page_testimonium_payload` builds through `testimonium_payload`, so the
    # page-scoped record stores its joined reading in the same field.
    validate_observed(
        observed,
        presented=presented,
        page_size=page_size,
        retained_text=payload.get("payload"),
    )
    return payload


def validate_presented_page_binding(
    presented: dict[str, Any],
    *,
    page_ordinal: int,
    page_image_path: str,
    page_sha256: str,
    page_size: tuple[int, int],
    page_bytes: bytes | None = None,
) -> None:
    """Bind a whole-page presentation to the sealed page it names.

    `presented` says which sealed page it is in `source_page_id` and which blob
    was shown in `image_path`/`image_sha256`, and nothing reconciled the two.
    A record could therefore name page 1 and carry page 2's real, digest-bound
    pixels: self-consistent, readable, and a lie about which ink a witness saw.
    Its observed boxes would then be validated against page 1's dimensions and
    read, by any later coverage derivation, as page 1 geometry (GOALS 5;
    ARCHITECTURE invariant 3).

    A region presentation is re-derived against its own sealed Designator
    record by the callers, which is stronger than this. An adapter-crop uses the
    only recipe this four-field transform can express today: an exact PNG crop
    from the sealed page. Its digest is re-derived here; a future resize or other
    operation must extend the closed recipe rather than ride as an opaque word.
    """
    kind = presented["kind"]
    whole_page = {"x": 0, "y": 0, "w": page_size[0], "h": page_size[1]}
    if presented["source_page_ordinal"] != page_ordinal:
        raise SchemaRefusal(
            "a presentation's source page ordinal disagrees with the sealed page its id names"
        )
    if kind == "page":
        if presented["image_path"] != page_image_path or presented["image_sha256"] != page_sha256:
            raise SchemaRefusal(
                "a page presentation names a blob that is not the sealed page it claims to be"
            )
        if presented["transform"]["bounds"] != whole_page:
            raise SchemaRefusal(
                "a page presentation does not cover its whole sealed page; a partial image is "
                "an adapter-crop, not a page"
            )
        if presented["transform"]["operation"] != "whole":
            raise SchemaRefusal("a page presentation has no executable whole-page transform")
    elif kind == "adapter-crop":
        bounds = presented["transform"]["bounds"]
        if presented["image_sha256"] == page_sha256 and bounds != whole_page:
            raise SchemaRefusal(
                "an adapter-crop presentation carries the whole sealed page's blob under a "
                "sub-page transform"
            )
        if presented["transform"]["operation"] != "crop":
            raise SchemaRefusal(
                "an adapter-crop presentation has no executable sealed-page crop transform"
            )
        if page_bytes is None:
            raise SchemaRefusal(
                "an adapter-crop presentation cannot be re-derived without its sealed page bytes"
            )
        expected_sha256 = digest_bytes(crop_png(page_bytes, bounds))
        if presented["image_sha256"] != expected_sha256:
            raise SchemaRefusal(
                "an adapter-crop presentation blob does not re-derive from its sealed page transform"
            )


def validate_unpresented_regions(payload: Any) -> list[str]:
    """Close the explicit list of bound proposal regions outside one presentation."""
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium payload is not an object")
    unpresented = payload.get("unpresented_regions")
    if (
        not isinstance(unpresented, list)
        or any(not isinstance(region_id, str) or not region_id for region_id in unpresented)
        or len(set(unpresented)) != len(unpresented)
    ):
        raise SchemaRefusal(
            "a Testimonium's unpresented_regions is not a unique list of region ids"
        )
    if payload.get("presented") == {} and unpresented:
        raise SchemaRefusal(
            "a Testimonium with no presentation at all cannot name regions its presentation "
            "does not speak for"
        )
    return unpresented


def unpresented_region_ids(
    presented: dict[str, Any], proposal_regions: list[dict[str, Any]]
) -> list[str]:
    """Re-derive which bound proposal crops fall outside one presented image.

    The empty presentation is the separately recorded state "no image was
    presented", so its list is inapplicable and therefore empty.  For a real
    presentation, a proposal is expressible by this record's page-space geometry
    exactly when it lies wholly inside the presented page-space bounds.  This one
    derivation works for region, page, and adapter-crop presentations and prevents
    an adapter that changes presentation kind from making the writer understate
    the limit before either read-back seam sees it.
    """
    if presented == {}:
        return []
    if not isinstance(presented, dict):
        raise SchemaRefusal("a Testimonium presented block is not an object")
    page_id = presented.get("source_page_id")
    presented_bounds = presented.get("transform", {}).get("bounds")
    if not isinstance(page_id, str) or not isinstance(presented_bounds, dict):
        raise SchemaRefusal("a Testimonium presentation cannot locate its page-space bounds")

    unpresented: list[str] = []
    for region in proposal_regions:
        payload = region.get("payload") if isinstance(region, dict) else None
        transform = payload.get("transform") if isinstance(payload, dict) else None
        bounds = transform.get("bounds") if isinstance(transform, dict) else None
        region_id = payload.get("region_id") if isinstance(payload, dict) else None
        if not isinstance(region_id, str) or not region_id or not isinstance(bounds, dict):
            raise SchemaRefusal("a bound proposal region has no page-space identity to compare")
        contained = (
            transform.get("source_page_id") == page_id
            and presented_bounds["x"] <= bounds["x"]
            and presented_bounds["y"] <= bounds["y"]
            and presented_bounds["x"] + presented_bounds["w"] >= bounds["x"] + bounds["w"]
            and presented_bounds["y"] + presented_bounds["h"] >= bounds["y"] + bounds["h"]
        )
        if not contained:
            unpresented.append(region_id)
    return unpresented


def validate_page_testimonium_payload(payload: Any) -> dict[str, Any]:
    """Close the page-scoped native Testimonium at writer and consumer seams."""
    if not isinstance(payload, dict) or not (
        set(payload) <= PAGE_TESTIMONIUM_REQUIRED_FIELDS | PAGE_TESTIMONIUM_OPTIONAL_FIELDS
    ):
        raise SchemaRefusal("a page Testimonium is not its closed schema")
    if missing := sorted(PAGE_TESTIMONIUM_REQUIRED_FIELDS - set(payload)):
        raise SchemaRefusal(f"a page Testimonium lacks required field(s) {missing}")
    page_role = payload["page_role"]
    if (
        payload["scope"] != "page"
        or not isinstance(payload["page_ordinal"], int)
        or isinstance(payload["page_ordinal"], bool)
        or not isinstance(page_role, str)
        or page_role not in PAGE_ROLES
        or not isinstance(payload["unjoined_act_attempts"], list)
    ):
        raise SchemaRefusal("a page Testimonium has invalid page scope facts")
    validate_unpresented_regions(payload)
    if "partition_disagreement" in payload:
        validate_partition_disagreement(payload["partition_disagreement"])
    if "native_capture" in payload:
        capture = validate_native_capture(payload["native_capture"])
        if capture["adapter"] == "churro.v1":
            parse = capture["parse"]
            parsed_text = parse.get("text")
            if parse["state"] == "parsed":
                if payload["payload"] != parsed_text:
                    raise SchemaRefusal(
                        "a Churro page Testimonium payload differs from its parsed native capture"
                    )
                cut_off = capture["transport_stop_reason"] in _CHURRO_CUTOFF_STOP_REASONS
                expected_health = {
                    "native_type": "string",
                    "encoding": "utf-8-json-native",
                    "recordable": True,
                    "empty": parsed_text == "",
                    "blank": parsed_text.strip() == "",
                    "truncated": cut_off,
                    "characters": len(parsed_text),
                    "truncation_basis": "trusted-response-boundary",
                }
                if payload["content_health"] != expected_health:
                    raise SchemaRefusal(
                        "a Churro page Testimonium health differs from its parsed native capture"
                    )
                interrupted_silence = cut_off and parsed_text == ""
                if interrupted_silence:
                    if "reported" in payload:
                        raise SchemaRefusal(
                            "a cut-off empty Churro page capture claims a reported absence"
                        )
                    if not (isinstance(payload.get("reason"), str) and payload["reason"].strip()):
                        raise SchemaRefusal(
                            "a cut-off empty Churro page capture has no failed-attempt reason"
                        )
                elif payload.get("reported") != parsed_text:
                    raise SchemaRefusal(
                        "a parsed Churro page Testimonium lacks its exact textual projection"
                    )
                elif "reason" in payload:
                    raise SchemaRefusal(
                        "a usable Churro page capture carries a failed-attempt reason"
                    )
            else:
                if payload["payload"] is not None or "reported" in payload:
                    raise SchemaRefusal(
                        "an unparseable Churro page capture claims retained page text"
                    )
                cut_off = capture["transport_stop_reason"] in _CHURRO_CUTOFF_STOP_REASONS
                basis = (
                    "response cut off by the provider "
                    f"({capture['transport_stop_reason']!r}); {parse['reason']}"
                    if cut_off
                    else parse["reason"]
                )
                expected_health = {
                    "native_type": "unrecordable",
                    "encoding": "invalid-or-unrecordable",
                    "recordable": False,
                    "empty": None,
                    "blank": None,
                    "truncated": None,
                    "characters": None,
                    "truncation_basis": basis,
                }
                if payload["content_health"] != expected_health:
                    raise SchemaRefusal(
                        "an unparseable Churro page Testimonium health differs from its capture"
                    )
                reason = payload.get("reason")
                if (
                    not isinstance(reason, str)
                    or not reason.strip()
                    or parse["reason"] not in reason
                ):
                    raise SchemaRefusal(
                        "an unparseable Churro page capture has no reason naming its parser refusal"
                    )
    return validate_native_witness_geometry(payload)


# A declared, deliberately UNMEASURED routing rule.  Unit 10 records only the
# unambiguous zero-overlap case; calibrating a near-overlap threshold would be a
# measurement claim GOVERNANCE 10 does not permit until something has actually
# been measured.
UNROUTED_OBSERVATION_OVERLAP: Final = {"rule": "positive-area", "status": "unmeasured"}


def _overlaps(left: dict[str, int], right: dict[str, int]) -> bool:
    """Whether two page-pixel boxes share positive area, never containment."""
    return min(left["x"] + left["w"], right["x"] + right["w"]) > max(left["x"], right["x"]) and min(
        left["y"] + left["h"], right["y"] + right["h"]
    ) > max(left["y"], right["y"])


def reported_geometry_overlaps(payload: dict[str, Any], bounds: dict[str, int]) -> bool:
    """Whether one Testimonium reports positive-area geometry over ``bounds``."""
    return any(
        observation.get("bounds_source") in REPORTED_BOUNDS_SOURCES
        and _overlaps(observation["bounds"], bounds)
        for observation in payload.get("observed", [])
    )


def unrouted_observations(
    testimonia: list[dict[str, Any]],
    proposal_regions: list[dict[str, Any]],
    *,
    prior_findings: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Named, non-fatal findings for reported ink no sealed proposal accounts for.

    The finding is coverage evidence, not a verdict on text or witness quality.
    It gives the Recensor's bounded fallback-recrop route the only legal next
    step; it never turns an observation into an act or moves an attempt ordinal.

    **The denominator is every sealed proposal on the presented page, not the
    reading act's own proposals.** Scoped to one act, a witness box belonging to
    the act *next to it* on the same page reads as unaccounted ink -- on a real
    page of twelve acts, eleven false findings per box, each one an invitation to
    spend a recovery unit on ink the Designator already marked out. The question
    this rule asks is GOALS 1's: did a witness report ink the partition never
    claimed? That question is page-scoped.

    **Only reported geometry counts.** A `bounds_source: "presented"` box is this
    pipeline restating the image it presented, not the witness reporting ink (see
    `BOUNDS_SOURCES`). Routing one would let a whole-page presentation on a page
    with no proposals at all produce a finding about ink no witness ever claimed
    to see.
    """
    prior_findings = prior_findings or set()
    proposal_boxes = [
        region["payload"]["transform"]
        for region in proposal_regions
        if region.get("payload", {}).get("origin") == "proposal"
    ]
    findings: list[dict[str, Any]] = []
    for testimony in testimonia:
        payload = testimony["payload"]
        presented = payload["presented"]
        if not presented:
            continue
        for observation in payload["observed"]:
            if observation.get("bounds_source") not in REPORTED_BOUNDS_SOURCES:
                continue
            key = (testimony["artifact_id"], observation["ordinal"])
            if key in prior_findings:
                continue
            bounds = observation["bounds"]
            overlaps = any(
                transform["source_page_id"] == presented["source_page_id"]
                and _overlaps(bounds, transform["bounds"])
                for transform in proposal_boxes
            )
            if not overlaps:
                findings.append(
                    {
                        "kind": "unrouted-observation",
                        "testimonium_id": testimony["artifact_id"],
                        "ordinal": observation["ordinal"],
                        "source_page_id": presented["source_page_id"],
                        "bounds": dict(bounds),
                        "overlap_rule": dict(UNROUTED_OBSERVATION_OVERLAP),
                    }
                )
    return findings


def partition_disagreement(
    testimonium: dict[str, Any], proposal_regions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record page/chair partition facts without selecting any pairing.

    All positive-area pairings are retained.  Where one observation intersects
    multiple proposals, every competing pairing remains in the record; neither
    witness nor proposal wins a correspondence decision here.
    """
    payload = testimonium["payload"]
    presented = payload["presented"]
    page_id = presented.get("source_page_id") if isinstance(presented, dict) else None
    proposals = sorted(
        [
            dict(region["payload"]["transform"]["bounds"])
            for region in proposal_regions
            if region.get("payload", {}).get("origin") == "proposal"
            and region["payload"]["transform"].get("source_page_id") == page_id
        ],
        key=lambda box: (box["y"], box["x"], box["h"], box["w"]),
    )
    observations = [
        {
            "ordinal": observation["ordinal"],
            "bounds": dict(observation["bounds"]),
            "bounds_source": observation["bounds_source"],
        }
        for observation in payload.get("observed", [])
        if observation.get("bounds_source") in REPORTED_BOUNDS_SOURCES
    ]
    deltas: list[dict[str, Any]] = []
    pairing_keys: list[tuple[int, int, int, int]] = []
    observed_proposals: set[tuple[int, int, int, int]] = set()
    # A tie is a tie from either side: one observation spanning several
    # proposals, or several observations each claiming the same proposal.
    # Counting matches per proposal as well as per observation is what makes
    # `ambiguous` answer "two observations tie" the way the geometry actually
    # ties, not only the direction the loop happens to walk first.
    observation_match_counts: dict[int, int] = {}
    proposal_match_counts: dict[tuple[int, int, int, int], int] = {}
    for observation in observations:
        matches = [proposal for proposal in proposals if _overlaps(observation["bounds"], proposal)]
        observation_match_counts[observation["ordinal"]] = len(matches)
        for proposal in matches:
            key = (proposal["x"], proposal["y"], proposal["w"], proposal["h"])
            proposal_match_counts[key] = proposal_match_counts.get(key, 0) + 1
            pairing = {
                "proposal_box": dict(proposal),
                "observed_ordinal": observation["ordinal"],
                "observed_box": dict(observation["bounds"]),
                "edge_offsets": {
                    "left": observation["bounds"]["x"] - proposal["x"],
                    "top": observation["bounds"]["y"] - proposal["y"],
                    "right": observation["bounds"]["x"]
                    + observation["bounds"]["w"]
                    - proposal["x"]
                    - proposal["w"],
                    "bottom": observation["bounds"]["y"]
                    + observation["bounds"]["h"]
                    - proposal["y"]
                    - proposal["h"],
                },
            }
            deltas.append(pairing)
            pairing_keys.append(key)
            observed_proposals.add(key)
    ambiguous_pairings = [
        pairing
        for pairing, key in zip(deltas, pairing_keys, strict=True)
        if observation_match_counts[pairing["observed_ordinal"]] > 1
        or proposal_match_counts[key] > 1
    ]
    return {
        "proposal_boxes": proposals,
        "observed_boxes": observations,
        "unclaimed_observations": unrouted_observations([testimonium], proposal_regions),
        "unobserved_proposals": [
            proposal
            for proposal in proposals
            if (proposal["x"], proposal["y"], proposal["w"], proposal["h"])
            not in observed_proposals
        ],
        "boundary_deltas": deltas,
        "ambiguous": bool(ambiguous_pairings),
        "ambiguous_pairings": ambiguous_pairings,
        "overlap_rule": dict(UNROUTED_OBSERVATION_OVERLAP),
    }


_NATIVE_CAPTURE_FIELDS: Final = frozenset(
    {
        "schema",
        "adapter",
        "view",
        "raw_response_ref",
        "transport_stop_reason",
        "stop_reason",
        "findings",
        "parse",
    }
)
_NATIVE_CAPTURE_PARSE_STATES: Final = frozenset({"not-requested", "pending", "parsed", "failed"})
_CHURRO_CAPTURE_FINDING_KINDS: Final = frozenset(
    {"post-hoc-repetition", "post-hoc-repetition-uninspected"}
)
_CHURRO_CUTOFF_STOP_REASONS: Final = frozenset({"length", "max_new_tokens"})
_CHURRO_STOP_REASONS: Final = frozenset({"eos", "stop"}) | _CHURRO_CUTOFF_STOP_REASONS


def validate_churro_xml(raw: bytes) -> str:
    """Validate one bounded native Churro response as a plain output element."""
    if not isinstance(raw, (bytes, bytearray)):
        raise SchemaRefusal("Churro response is not raw bytes")
    if len(raw) > CHURRO_MAX_RESPONSE_BYTES:
        raise SchemaRefusal(
            "Churro response exceeds the retained parsing limit of "
            f"{CHURRO_MAX_RESPONSE_BYTES} bytes (received {len(raw)})"
        )
    if b"<!DOCTYPE" in bytes(raw).upper():
        raise SchemaRefusal("Churro response carries a DOCTYPE; a plain <output> element cannot")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, UnicodeDecodeError) as error:
        raise SchemaRefusal(f"Churro response is not parseable XML: {error}") from error
    if root.tag != "output" or set(root.attrib) or list(root):
        raise SchemaRefusal("Churro response must be a plain <output> XML element")
    return root.text or ""


def detect_churro_repetition(raw: bytes) -> dict[str, Any] | None:
    """Report a repeated tail after capture; this function has no generation input."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "kind": "post-hoc-repetition-uninspected",
            "reason": "response is not UTF-8 text",
        }
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < _CHURRO_REPETITION_WINDOW * _CHURRO_REPETITION_MIN_REPEATS:
        return None
    for width in range(
        _CHURRO_REPETITION_WINDOW,
        min(256, len(normalized) // _CHURRO_REPETITION_MIN_REPEATS) + 1,
    ):
        unit = normalized[-width:]
        repeats = 1
        while (repeats + 1) * width <= len(normalized) and (
            normalized[-(repeats + 1) * width : -repeats * width] == unit
        ):
            repeats += 1
        if repeats >= _CHURRO_REPETITION_MIN_REPEATS:
            return {"kind": "post-hoc-repetition", "unit_characters": width, "repeats": repeats}
    return None


def derive_churro_capture(
    raw: bytes,
    transport_stop_reason: str,
    *,
    parser: str | None,
    xml_parser=validate_churro_xml,
    repetition_detector=detect_churro_repetition,
) -> dict[str, Any]:
    """Derive the exact mutable-free facts a Churro capture may publish.

    Oversized bytes have already crossed the response boundary, so they remain
    evidence in the caller's blob store.  They are not handed to either parser
    or detector; both unperformed operations are named in the retained facts.
    """
    if len(raw) > CHURRO_MAX_RESPONSE_BYTES:
        reason = (
            "Churro response exceeds the retained parsing limit of "
            f"{CHURRO_MAX_RESPONSE_BYTES} bytes (received {len(raw)})"
        )
        parse = (
            {"state": "not-requested", "parser": None}
            if parser is None
            else {"state": "failed", "parser": parser, "reason": reason}
        )
        return {
            "parse": parse,
            "findings": [
                {
                    "kind": "post-hoc-repetition-uninspected",
                    "reason": reason,
                    "inspected": "raw-response",
                }
            ],
            "stop_reason": (
                "partial-parse-failed" if parser is not None else transport_stop_reason
            ),
        }

    parse: dict[str, Any] = {"state": "not-requested", "parser": None}
    stop_reason = transport_stop_reason
    if parser == "xml":
        try:
            parse = {"state": "parsed", "parser": "xml", "text": xml_parser(raw)}
        except SchemaRefusal as error:
            parse = {"state": "failed", "parser": "xml", "reason": str(error)}
            stop_reason = "partial-parse-failed"
    parsed_text = parse.get("text")
    inspected, basis = (
        (parsed_text.encode("utf-8"), "parsed-text")
        if isinstance(parsed_text, str)
        else (raw, "raw-response")
    )
    findings: list[dict[str, Any]] = []
    if finding := repetition_detector(inspected):
        findings.append({**finding, "inspected": basis})
        if finding["kind"] == "post-hoc-repetition" and parse["state"] != "failed":
            stop_reason = "partial-post-hoc-repetition-detected"
    return {"parse": parse, "findings": findings, "stop_reason": stop_reason}


def verify_native_capture_bytes(value: Any, raw: bytes) -> dict[str, Any]:
    """Verify one capture's derived record against its authoritative raw blob."""
    capture = validate_native_capture(value)
    if not isinstance(raw, bytes):
        raise SchemaRefusal("a page Testimonium raw response is not bytes")
    actual_digest = digest_bytes(raw)
    if actual_digest != capture["raw_response_ref"]["sha256"]:
        raise SchemaRefusal(
            "a page Testimonium raw response has digest "
            f"{actual_digest}, not its native capture digest "
            f"{capture['raw_response_ref']['sha256']}"
        )
    if capture["adapter"] != "churro.v1":
        return capture
    derived = derive_churro_capture(
        raw,
        capture["transport_stop_reason"],
        parser=capture["parse"]["parser"],
    )
    for field in ("parse", "findings", "stop_reason"):
        if capture[field] != derived[field]:
            raise SchemaRefusal(
                f"a Churro native capture's {field} differs from its retained raw response"
            )
    return capture


def verify_native_capture_blob(tree: Any, value: Any) -> dict[str, Any]:
    """Read, digest-check, and derive from the same raw bytes without a check/use gap."""
    capture = validate_native_capture(value)
    reference = capture["raw_response_ref"]
    try:
        raw = tree.read_bytes(reference["relative_path"])
    except OSError as error:
        raise SchemaRefusal(
            f"a page Testimonium raw response could not be read: {error}"
        ) from error
    return verify_native_capture_bytes(capture, raw)


def validate_native_capture(value: Any) -> dict[str, Any]:
    """Close the derived model view; raw response bytes remain in its referenced blob."""
    if not isinstance(value, dict) or set(value) != _NATIVE_CAPTURE_FIELDS:
        raise SchemaRefusal(
            "a page Testimonium native capture is not its retained model-view schema"
        )
    for field in ("schema", "adapter", "transport_stop_reason", "stop_reason"):
        if not isinstance(value[field], str) or not value[field]:
            raise SchemaRefusal(f"a page Testimonium native capture has a blank {field}")
    if value["schema"] != "attestatores-model-view.v1":
        raise SchemaRefusal(
            "a page Testimonium native capture has an unknown retained model-view schema"
        )
    if not isinstance(value["view"], dict):
        raise SchemaRefusal("a page Testimonium native capture view is not an object")
    reference = value["raw_response_ref"]
    if not isinstance(reference, dict) or set(reference) != {"relative_path", "sha256"}:
        raise SchemaRefusal("a page Testimonium native capture has no raw-response reference")
    if not all(isinstance(reference[key], str) and reference[key] for key in reference):
        raise SchemaRefusal(
            "a page Testimonium native capture has an invalid raw-response reference"
        )
    digest = reference["sha256"]
    expected_path = f"{writing_directory(ATTESTATORES)}/blobs/sha256/{digest}"
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or reference["relative_path"] != expected_path
    ):
        raise SchemaRefusal(
            "a page Testimonium native capture raw-response reference is not its "
            "content-addressed Attestatores blob path and digest"
        )
    findings = value["findings"]
    if not isinstance(findings, list) or not all(
        isinstance(finding, dict) and isinstance(finding.get("kind"), str) and finding["kind"]
        for finding in findings
    ):
        raise SchemaRefusal("a page Testimonium native capture has a malformed findings list")
    parse = value["parse"]
    state = parse.get("state") if isinstance(parse, dict) else None
    if not isinstance(parse, dict) or state not in _NATIVE_CAPTURE_PARSE_STATES:
        raise SchemaRefusal("a page Testimonium native capture has a malformed parse record")
    parser = parse.get("parser")
    if state == "not-requested":
        expected, parser_valid = {"state", "parser"}, parser is None
    elif state == "pending":
        expected, parser_valid = {"state", "parser"}, isinstance(parser, str) and bool(parser)
    else:
        expected = {"state", "parser", "text" if state == "parsed" else "reason"}
        parser_valid = isinstance(parser, str) and bool(parser)
    if set(parse) != expected or not parser_valid:
        raise SchemaRefusal("a page Testimonium native capture parse record has the wrong shape")
    if state == "parsed" and not isinstance(parse["text"], str):
        raise SchemaRefusal("a page Testimonium native capture claims parsed with no text")
    if state == "failed" and not (isinstance(parse["reason"], str) and parse["reason"]):
        raise SchemaRefusal(
            "a page Testimonium native capture claims a parse failure with no reason"
        )
    if value["adapter"] == "churro.v1":
        if value["transport_stop_reason"] not in _CHURRO_STOP_REASONS:
            raise SchemaRefusal(
                "a Churro page capture has an unknown transport stop reason "
                f"{value['transport_stop_reason']!r}"
            )
        view = value["view"]
        if set(view) != {"prompt", "generation"}:
            raise SchemaRefusal(
                "a Churro page capture does not retain exactly its prompt and generation view"
            )
        prompt, generation = view["prompt"], view["generation"]
        if (
            not isinstance(prompt, dict)
            or set(prompt) != {"system", "user"}
            or any(not isinstance(prompt[field], str) or not prompt[field] for field in prompt)
        ):
            raise SchemaRefusal("a Churro page capture has no closed nonblank prompt view")
        if (
            not isinstance(generation, dict)
            or set(generation) != {"max_new_tokens"}
            or not isinstance(generation["max_new_tokens"], int)
            or isinstance(generation["max_new_tokens"], bool)
            or generation["max_new_tokens"] != CHURRO_OUTPUT_TOKENS
        ):
            raise SchemaRefusal(
                f"a Churro page capture does not retain its {CHURRO_OUTPUT_TOKENS}-token bound"
            )
        if state not in {"parsed", "failed"} or parser != "xml":
            raise SchemaRefusal("a retained Churro page capture has no terminal XML parse record")
        if len(findings) > 1:
            raise SchemaRefusal("a Churro page capture carries more than one repetition finding")
        for finding in findings:
            kind = finding["kind"]
            if kind not in _CHURRO_CAPTURE_FINDING_KINDS:
                raise SchemaRefusal(f"a Churro page capture has unknown finding kind {kind!r}")
            if kind == "post-hoc-repetition":
                if set(finding) != {"kind", "unit_characters", "repeats", "inspected"} or any(
                    not isinstance(finding[field], int)
                    or isinstance(finding[field], bool)
                    or finding[field] <= 0
                    for field in ("unit_characters", "repeats")
                ):
                    raise SchemaRefusal(
                        "a Churro page capture has a malformed post-hoc repetition finding"
                    )
            elif set(finding) != {"kind", "reason", "inspected"} or not (
                isinstance(finding["reason"], str) and finding["reason"]
            ):
                raise SchemaRefusal(
                    "a Churro page capture has a malformed uninspected-repetition finding"
                )
            if finding["inspected"] not in {"parsed-text", "raw-response"}:
                raise SchemaRefusal(
                    "a Churro page capture repetition finding does not name the inspected view"
                )
        repeated = any(finding["kind"] == "post-hoc-repetition" for finding in findings)
        expected_stop = value["transport_stop_reason"]
        if state == "failed":
            expected_stop = "partial-parse-failed"
        elif repeated:
            expected_stop = "partial-post-hoc-repetition-detected"
        if value["stop_reason"] != expected_stop:
            raise SchemaRefusal(
                "a Churro page capture stop reason disagrees with its parse and findings"
            )
    return value


def validate_partition_disagreement(value: Any) -> dict[str, Any]:
    """Close the retained facts without converting them into a verdict."""
    required = {
        "proposal_boxes",
        "observed_boxes",
        "unclaimed_observations",
        "unobserved_proposals",
        "boundary_deltas",
        "ambiguous",
        "ambiguous_pairings",
        "overlap_rule",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise SchemaRefusal("a page Testimonium partition_disagreement is not its closed schema")
    for field in ("proposal_boxes", "unobserved_proposals"):
        if not isinstance(value[field], list):
            raise SchemaRefusal(
                "a page Testimonium partition disagreement has malformed proposal boxes"
            )
        for box in value[field]:
            _bounds(box, "a page Testimonium partition proposal box", page_size=None)
    if not isinstance(value["observed_boxes"], list):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement has malformed observed boxes"
        )
    for observation in value["observed_boxes"]:
        if not isinstance(observation, dict) or set(observation) != {
            "ordinal",
            "bounds",
            "bounds_source",
        }:
            raise SchemaRefusal("a page Testimonium partition observed box is malformed")
        if (
            not _integer(observation["ordinal"])
            or observation["bounds_source"] not in REPORTED_BOUNDS_SOURCES
        ):
            raise SchemaRefusal(
                "a page Testimonium partition observed box is not reported geometry"
            )
        _bounds(observation["bounds"], "a page Testimonium partition observed box", page_size=None)
    if value["overlap_rule"] != UNROUTED_OBSERVATION_OVERLAP:
        raise SchemaRefusal(
            "a page Testimonium partition disagreement changes its declared overlap rule"
        )
    if not isinstance(value["ambiguous"], bool):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement ambiguous flag is not boolean"
        )
    for field in ("unclaimed_observations", "boundary_deltas", "ambiguous_pairings"):
        if not isinstance(value[field], list):
            raise SchemaRefusal(
                "a page Testimonium partition disagreement has malformed retained facts"
            )
    if value["ambiguous"] != bool(value["ambiguous_pairings"]):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement omits its ambiguous pairings"
        )
    return value
