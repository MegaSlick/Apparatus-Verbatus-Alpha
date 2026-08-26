"""The closed, derived waist of a native witness report.

Native responses remain in their retained raw blob.  These values are the small
set this pipeline derives from them: what image was presented and the page-pixel
rectangles a witness reported seeing.  They deliberately contain no act identity
or preference: correspondence is a consumer lookup, never witness testimony.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES, writing_directory
from common.corpus_register import _refuse_preference
from common.imaging import crop_png

PRESENTATION_KINDS: Final = frozenset({"page", "region", "adapter-crop"})
# `native` and `derived` are reported-ink evidence. `presented` only associates
# a geometry-free response with its input image; treating it as observed ink
# would give complete coverage to a witness that reported no geometry.
BOUNDS_SOURCES: Final = frozenset({"native", "derived", "presented"})
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
    {
        "reason",
        "reported",
        "partition_disagreement",
        # The retained responses this record's own derived geometry was
        # quantized from, and the declared rule that converted them. Plural
        # because a page record's partition may be assembled from more than one
        # retained response; a page witness that answers once retains one.
        "raw_response_refs",
        "adapter_metadata",
    }
)
PAGE_ROLES: Final = frozenset({"primary", "continuation", "mixed"})


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
    presentation_is_witness_view: bool = True,
) -> list[dict[str, Any]]:
    """Validate dense witness order, source-page boxes, and non-overlapping text spans.

    A span addresses this Testimonium's exact retained string in code points,
    never the normalized alignment view. It may be null, but it may not name an
    offset the record cannot answer (GOALS 5).
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
        bounds = _bounds(item["bounds"], "a Testimonium observed box", page_size=page_size)
        # The containment refusal below guards records whose presentation IS the
        # complete view the witness received (page-scoped records, and act
        # records of act-scoped chairs). A page witness's act view restates the
        # chair's page-level geometry in page-pixel space, so its boxes may
        # legitimately exceed this record's one-crop presentation; they remain
        # bounded by the sealed page via `page_size` above, and Unit 10C's
        # coverage derivation is what consumes that page-space geometry.
        presented_bounds = presented["transform"]["bounds"]
        if presentation_is_witness_view and not (
            presented_bounds["x"] <= bounds["x"]
            and presented_bounds["y"] <= bounds["y"]
            and presented_bounds["x"] + presented_bounds["w"] >= bounds["x"] + bounds["w"]
            and presented_bounds["y"] + presented_bounds["h"] >= bounds["y"] + bounds["h"]
        ):
            raise SchemaRefusal(
                "a Testimonium observed box falls outside the exact image presentation. "
                "The record would attribute unseen page pixels to this witness. Correct the "
                "adapter's page-space transform or refuse this response"
            )
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
    ordered_spans = sorted(spans)
    for previous, current in zip(ordered_spans, ordered_spans[1:], strict=False):
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
    # Both Testimonium kinds retain the exact span-addressable text in `payload`.
    validate_observed(
        observed,
        presented=presented,
        page_size=page_size,
        retained_text=payload.get("payload"),
        # An act view of a page witness (`page_witness: True`, never scope="page")
        # restates page-level geometry; every other record presents the witness's
        # own complete view. Consumers reconcile the flag against the sealed
        # page-witness declaration, so it cannot be forged onto an act chair.
        presentation_is_witness_view=(
            payload.get("scope") == "page" or payload.get("page_witness") is not True
        ),
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

    The list is inapplicable to an empty presentation. For a real presentation,
    a proposal is expressible by this record exactly when it lies wholly inside
    the presentation's page-space bounds; changing presentation kind must not
    change that disclosure rule.
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


def validate_page_testimonium_payload(
    payload: Any,
    *,
    testimonium_id: str | None = None,
    read_bytes: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
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
    validated = validate_native_witness_geometry(payload)
    if "partition_disagreement" in payload:
        presented = payload["presented"]
        validate_partition_disagreement(
            payload["partition_disagreement"],
            observed=payload["observed"],
            source_page_id=presented.get("source_page_id") if presented else None,
            testimonium_id=testimonium_id,
        )
    validate_retained_response_refs(payload, read_bytes=read_bytes)
    return validated


def validate_retained_response_refs(
    payload: dict[str, Any], *, read_bytes: Callable[[str], bytes] | None = None
) -> None:
    """Close a page partition's links to its retained native responses.

    A partition may derive from several responses, so references remain plural
    and ordered. The producing stage owns its blob namespace; this shared seam
    closes the reference shape and prevents quantization metadata from appearing
    without the bytes whose geometry it describes.
    """
    refs = payload.get("raw_response_refs")
    if refs is not None:
        if not isinstance(refs, list) or not refs:
            raise SchemaRefusal("a page Testimonium raw_response_refs is not a non-empty list")
        expected_prefix = f"{writing_directory(ATTESTATORES)}/blobs/sha256/"
        for reference in refs:
            if (
                not isinstance(reference, dict)
                or set(reference) != {"relative_path", "sha256"}
                or not isinstance(reference["relative_path"], str)
                or not reference["relative_path"]
                or not isinstance(reference["sha256"], str)
                or len(reference["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in reference["sha256"])
                or reference["relative_path"] != expected_prefix + reference["sha256"]
            ):
                raise SchemaRefusal(
                    "a page Testimonium retained-response reference is not a closed blob reference"
                )
        if len({reference["sha256"] for reference in refs}) != len(refs):
            raise SchemaRefusal("a page Testimonium names one retained response twice")
        if read_bytes is not None:
            for reference in refs:
                try:
                    retained = read_bytes(reference["relative_path"])
                except OSError as error:
                    raise SchemaRefusal(
                        f"page Testimonium retained response {reference['relative_path']} could "
                        f"not be read: {error}"
                    ) from error
                if digest_bytes(retained) != reference["sha256"]:
                    raise SchemaRefusal(
                        f"page Testimonium retained response {reference['relative_path']} "
                        "differs from its digest"
                    )
    metadata = payload.get("adapter_metadata")
    if metadata is not None:
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"geometry_quantization"}
            or not isinstance(metadata["geometry_quantization"], str)
            or not metadata["geometry_quantization"]
        ):
            raise SchemaRefusal("a page Testimonium adapter metadata is not its closed shape")
        if refs is None:
            raise SchemaRefusal(
                "a page Testimonium declares a quantization rule with no retained response to "
                "have applied it to"
            )


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


def reported_geometry_overlaps(observed: list[dict[str, Any]], bounds: dict[str, int]) -> bool:
    """Presentation echoes never count as reported geometric overlap."""
    return any(
        observation.get("bounds_source") in REPORTED_BOUNDS_SOURCES
        and _overlaps(observation["bounds"], bounds)
        for observation in observed
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

    The denominator is every sealed proposal on the presented page, not one
    act's proposals; otherwise a neighboring act's ink becomes a false finding.

    Only reported geometry counts. A `bounds_source: "presented"` box restates
    the input image and must not create a finding about ink no witness reported.
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
    deltas, unobserved_proposals, ambiguous_pairings = _partition_pairing_facts(
        proposals, observations
    )
    return {
        "proposal_boxes": proposals,
        "observed_boxes": observations,
        "unclaimed_observations": unrouted_observations([testimonium], proposal_regions),
        "unobserved_proposals": unobserved_proposals,
        "boundary_deltas": deltas,
        "ambiguous": bool(ambiguous_pairings),
        "ambiguous_pairings": ambiguous_pairings,
        "overlap_rule": dict(UNROUTED_OBSERVATION_OVERLAP),
    }


def _partition_pairing_facts(
    proposals: list[dict[str, int]], observations: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, int]], list[dict[str, Any]]]:
    """Ambiguity is symmetric, so neither side may choose a single pairing."""
    deltas: list[dict[str, Any]] = []
    pairing_keys: list[tuple[int, int, int, int]] = []
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
    ambiguous_pairings = [
        pairing
        for pairing, key in zip(deltas, pairing_keys, strict=True)
        if observation_match_counts[pairing["observed_ordinal"]] > 1
        or proposal_match_counts[key] > 1
    ]
    unobserved_proposals = [
        proposal
        for proposal in proposals
        if (proposal["x"], proposal["y"], proposal["w"], proposal["h"]) not in proposal_match_counts
    ]
    return deltas, unobserved_proposals, ambiguous_pairings


def validate_partition_disagreement(
    value: Any,
    *,
    observed: Any = None,
    source_page_id: str | None = None,
    testimonium_id: str | None = None,
    proposal_boxes: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
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
    if proposal_boxes is not None and value["proposal_boxes"] != sorted(
        proposal_boxes,
        key=lambda box: (box["y"], box["x"], box["h"], box["w"]),
    ):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts the sealed proposals on its page"
        )
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
    if observed is not None:
        expected_observed = [
            {
                "ordinal": observation["ordinal"],
                "bounds": dict(observation["bounds"]),
                "bounds_source": observation["bounds_source"],
            }
            for observation in observed
            if observation.get("bounds_source") in REPORTED_BOUNDS_SOURCES
        ]
        if value["observed_boxes"] != expected_observed:
            raise SchemaRefusal(
                "a page Testimonium partition disagreement contradicts its observed geometry"
            )
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
    expected_deltas, expected_unobserved, expected_ambiguous = _partition_pairing_facts(
        value["proposal_boxes"], value["observed_boxes"]
    )
    if value["unobserved_proposals"] != expected_unobserved:
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts its unobserved proposals"
        )
    if value["boundary_deltas"] != expected_deltas:
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts its boundary deltas"
        )
    if value["ambiguous_pairings"] != expected_ambiguous or value["ambiguous"] != bool(
        expected_ambiguous
    ):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts its ambiguous pairings"
        )
    expected_unclaimed = [
        observation
        for observation in value["observed_boxes"]
        if not any(
            _overlaps(observation["bounds"], proposal) for proposal in value["proposal_boxes"]
        )
    ]
    if len(value["unclaimed_observations"]) != len(expected_unclaimed):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts its unclaimed observations"
        )
    for finding, observation in zip(
        value["unclaimed_observations"], expected_unclaimed, strict=True
    ):
        if (
            not isinstance(finding, dict)
            or set(finding)
            != {
                "kind",
                "testimonium_id",
                "ordinal",
                "source_page_id",
                "bounds",
                "overlap_rule",
            }
            or finding["kind"] != "unrouted-observation"
            or not isinstance(finding["testimonium_id"], str)
            or not finding["testimonium_id"]
            or (testimonium_id is not None and finding["testimonium_id"] != testimonium_id)
            or finding["ordinal"] != observation["ordinal"]
            or (source_page_id is not None and finding["source_page_id"] != source_page_id)
            or finding["bounds"] != observation["bounds"]
            or finding["overlap_rule"] != UNROUTED_OBSERVATION_OVERLAP
        ):
            raise SchemaRefusal(
                "a page Testimonium partition disagreement has a malformed unclaimed observation"
            )
    return value
