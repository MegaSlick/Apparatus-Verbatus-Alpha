"""Checks the immutable pixels handed from Exemplar to later stages.

The Exemplar page is more than an ordinal in a census.  A sealed page binds the
Door admission artifact and the exact content-addressed image blob that every
later crop must use.  Consumers call this helper before acting on those pixels so
a changed, missing, or substituted blob cannot be quietly re-hashed into new
downstream evidence.

This deliberately knows contracts and the run tree, but not a numbered pipeline
module. Ink Map, Designator, Attestatores, Perlector, Recensor, and Armarium use
the same check: the first five prevent work over altered pixels; the latter
prevents an export after pixels changed between stages.
"""

import json
from typing import Any, Final

from common.contracts.canonical import canonical_bytes, digest_bytes, verify_self_hash
from common.contracts.envelope import validate_envelope, verify_input_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id, page_id, region_id
from common.contracts.stages import (
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    MAX_TRIAGE_SPLIT_PARTS,
    RECENSOR,
    TRIAGE_MODES,
)
from common.imaging import (
    carries_only_image_chunks,
    crop_png,
    dimensions,
    image_shown,
    imaging_library_versions,
    render_triage_derivative,
)
from common.runtree.store import RunTree

# The one name for a triage derivative's kind. The Door writes it, this boundary
# and the Exemplar stage read it, and each of the three used to spell it out
# separately; a producer and its two consumers agreeing by coincidence is what
# `is_triage_derivative_contract` below exists to stop.
SEALED_DERIVATIVE_PAGE_KIND: Final = "sealed-derivative-page-v1"


def verify_sealed_page_pixels(
    tree: RunTree,
    run: dict[str, Any],
    source: dict[str, Any],
    page: dict[str, Any],
) -> None:
    """Verify one sealed Exemplar page and its immutable Door pixel source.

    ``source`` is the matching self-hashed ``run.json`` source-manifest row and
    ``page`` is a validated Exemplar page artifact.  The page must name exactly
    its Door admission plus its Door blob; both referenced bytes are checked
    again, rather than trusting an earlier stage's successful check.
    """
    ordinal = source.get("ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise ContractError("a submitted source has no integer ordinal for its sealed page")
    if page.get("run_id") != tree.run_id or page.get("stage") != EXEMPLAR:
        raise ContractError("a sealed page belongs to a different Exemplar run")
    if page.get("config_digest") != run.get("config_digest"):
        raise ContractError("a sealed page is bound to a different run configuration")
    if page.get("outcome") != "sealed":
        raise ContractError("immutable page-pixel verification was asked of an unsealed page")

    payload = page.get("payload")
    if not isinstance(payload, dict):
        raise ContractError("a sealed Exemplar page has no payload")
    rows = sealed_submission_rows(payload)
    if ordinal not in rows:
        raise ContractError(
            "a sealed Exemplar page does not name this submitted row among its own submission "
            "rows, so it is not the page this source was sealed into"
        )
    _verify_submission_row(rows[ordinal], source)
    # The page's own top-level filename facts describe one of its rows and must
    # agree with it. Two rows carrying identical bytes are one page, so the
    # sealed record cites the whole set; nothing here may quietly disagree with
    # the citation beside it.
    if payload.get("ordinal") not in rows:
        raise ContractError("a sealed Exemplar page's own ordinal is not one of its submitted rows")
    _verify_page_source_facts(payload, rows[payload["ordinal"]], payload["ordinal"])

    source_digest = payload.get("source_sha256")
    if not _is_sha256(source_digest):
        raise ContractError("a sealed Exemplar page has no lowercase pixel sha256")
    rendered = payload.get("rendered_from")
    # `_page_origin` is the stricter of the two derivations that met here: it
    # type-checks every render field rather than only closing the key set. The
    # try/except is the other branch's contribution and is kept, so a malformed
    # origin that survives validation still becomes a named refusal rather than a
    # raw TypeError or RecursionError out of the identity derivation.
    origin = _page_origin(source_digest, rendered)
    try:
        expected_page_id = page_id(origin, {"operation": "whole"})
    except (ContractError, TypeError, ValueError, RecursionError) as error:
        raise ContractError(
            "a sealed Exemplar page's immutable origin is malformed and cannot derive "
            "a page identity"
        ) from error
    if page.get("subject_id") != expected_page_id:
        raise ContractError(
            "a sealed Exemplar page identity does not bind its immutable origin and transform"
        )

    blob_path = tree.blob_path(DOOR, source_digest)
    if payload.get("image_path") != blob_path:
        raise ContractError("a sealed Exemplar page does not name its Door pixel blob")
    admission_paths = {
        tree.artifact_path(DOOR, "admission", artifact_id(DOOR, "admission", f"source-{row}"))
        for row in rows
    }
    admission_path = tree.artifact_path(
        DOOR,
        "admission",
        artifact_id(DOOR, "admission", f"source-{ordinal}"),
    )
    refs = _references_by_path(page.get("inputs"))
    if set(refs) != admission_paths | {blob_path}:
        raise ContractError(
            "a sealed Exemplar page must input exactly the Door admission of every submission "
            "row it names, and its pixel blob"
        )
    blob_ref = refs[blob_path]
    if blob_ref != {"relative_path": blob_path, "sha256": source_digest}:
        raise ContractError("a sealed Exemplar page's pixel input is not content-addressed")

    _read_checked(tree, blob_ref, "the sealed Exemplar pixel blob")

    admission_data = _read_checked(tree, refs[admission_path], "the sealed Door admission")
    try:
        admission = validate_envelope(json.loads(admission_data.decode("utf-8")))
    except (SchemaRefusal, UnicodeDecodeError, ValueError, TypeError) as error:
        raise ContractError("the sealed page's Door admission is not a valid artifact") from error
    _verify_admission(admission, run, source, ordinal, blob_ref, tree, rendered)


def _page_origin(source_digest: str, rendered: Any) -> dict[str, Any]:
    """Build page identity only from a complete, typed render origin."""
    if rendered is None:
        return {"kind": "source", "sha256": source_digest}
    _validate_rendered_origin(rendered)
    return {
        "kind": "container-page",
        "container_sha256": rendered["container_sha256"],
        "container_page_index": rendered["container_page_index"],
        "render_contract": rendered["render_contract"],
    }


def _validate_rendered_origin(rendered: Any) -> None:
    """Refuse a partial render origin before any consumer indexes its fields."""
    if (
        not isinstance(rendered, dict)
        or set(rendered)
        != {"container_format", "container_sha256", "container_page_index", "render_contract"}
        or not isinstance(rendered.get("container_format"), str)
        or not rendered["container_format"]
        or not _is_sha256(rendered.get("container_sha256"))
        or not isinstance(rendered.get("container_page_index"), int)
        or isinstance(rendered["container_page_index"], bool)
        or rendered["container_page_index"] < 0
        or not isinstance(rendered.get("render_contract"), dict)
    ):
        raise ContractError("a sealed Exemplar page has no complete rendered-container origin")


def verify_refused_page_evidence(
    tree: RunTree,
    run: dict[str, Any],
    source: dict[str, Any],
    page: dict[str, Any],
) -> None:
    """Verify a refused Exemplar page still has its exact Door alarm evidence."""
    ordinal = source.get("ordinal")
    expected_subject = f"source-{ordinal}"
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or page.get("run_id") != tree.run_id
        or page.get("stage") != EXEMPLAR
        or page.get("kind") != "page"
        or page.get("outcome") != "refused"
        or page.get("config_digest") != run.get("config_digest")
        or page.get("subject_id") != expected_subject
        or page.get("artifact_id") != artifact_id(EXEMPLAR, "page", expected_subject)
    ):
        raise ContractError("a refused Exemplar page does not belong to this source and run")
    payload = page.get("payload")
    if not isinstance(payload, dict):
        raise ContractError("a refused Exemplar page has no payload")
    _verify_page_source_facts(payload, source, ordinal)
    reason = payload.get("reason")
    if not isinstance(reason, str) or ":" not in reason:
        raise ContractError("a refused Exemplar page carries no closed Door alarm reason")

    admission_path = tree.artifact_path(
        DOOR, "admission", artifact_id(DOOR, "admission", expected_subject)
    )
    refs = _references_by_path(page.get("inputs"))
    if set(refs) != {admission_path}:
        raise ContractError("a refused Exemplar page must input exactly its Door admission")
    admission_data = _read_checked(tree, refs[admission_path], "the refused Door admission")
    try:
        admission = validate_envelope(json.loads(admission_data.decode("utf-8")))
    except (SchemaRefusal, UnicodeDecodeError, ValueError, TypeError) as error:
        raise ContractError("a refused page's Door admission is not a valid artifact") from error
    if (
        admission.get("run_id") != tree.run_id
        or admission.get("stage") != DOOR
        or admission.get("kind") != "admission"
        or admission.get("outcome") != "refused"
        or admission.get("config_digest") != run.get("config_digest")
        or admission.get("subject_id") != expected_subject
        or admission.get("artifact_id") != artifact_id(DOOR, "admission", expected_subject)
        or admission.get("inputs") != []
    ):
        raise ContractError("a refused Exemplar page's Door admission does not match this source")
    admission_payload = admission.get("payload")
    if not isinstance(admission_payload, dict):
        raise ContractError("a refused Exemplar page's Door admission has no payload")
    _verify_page_source_facts(admission_payload, source, ordinal)
    if admission_payload.get("reason") != reason:
        raise ContractError("a refused Exemplar page changed its Door alarm reason")


def verify_exemplar_corpus_seal(
    tree: RunTree,
    run: dict[str, Any],
    manifest: dict[str, Any],
    sources: dict[int, dict[str, Any]],
    records: dict[int, dict[str, Any]],
    entries_by_ordinal: dict[int, dict[str, Any]],
) -> None:
    """Verify the one Exemplar corpus seal against run authority and page outcomes."""
    expected_ordinals = set(sources)
    _refuse_a_merged_page_no_consumer_reads_yet(records)
    if set(records) != expected_ordinals or set(entries_by_ordinal) != expected_ordinals:
        # By ordinal, never by submitted filename. `run_stage` prints every
        # ContractError to stderr, and the data-handling policy's logging rule
        # excludes a declared path from exactly that channel — the same reason
        # `operations/submit/inventory.py` and `pipeline/1_exemplar/door.py` name
        # their refusals by ordinal. Ruling 1 puts the filename in the hashed
        # ledger and in the sealed record, which is where an operator reads it
        # back; a captured terminal stream is not one of those places.
        missing = sorted(expected_ordinals - set(records))
        unexpected = sorted(set(records) - expected_ordinals)
        raise ContractError(
            "the Exemplar page outcomes do not reconcile with run.json; lost submitted "
            f"page ordinal(s) {missing}, unexpected {unexpected}. The run's own source "
            "manifest names each one, and no page may be lost between them"
        )

    seals = [entry for entry in manifest.get("artifacts", []) if entry.get("kind") == "seal"]
    expected_id = artifact_id(EXEMPLAR, "seal", "corpus-seal")
    if len(seals) != 1 or seals[0].get("artifact_id") != expected_id:
        raise ContractError("the Exemplar carries no single derived corpus seal")
    seal = tree.read_artifact(EXEMPLAR, "seal", expected_id)
    if (
        seal.get("run_id") != tree.run_id
        or seal.get("stage") != EXEMPLAR
        or seal.get("kind") != "seal"
        or seal.get("outcome") != "sealed"
        or seal.get("subject_id") != "corpus-seal"
        or seal.get("artifact_id") != expected_id
        or seal.get("config_digest") != run.get("config_digest")
    ):
        raise ContractError("the Exemplar corpus seal does not belong to this run and stage")
    payload = seal.get("payload")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"page_count", "pages", "self_hash"}
        or not verify_self_hash(payload)
    ):
        raise ContractError("the Exemplar corpus seal does not carry a valid self-hashed census")
    if payload["page_count"] != len(sources) or not isinstance(payload["pages"], list):
        raise ContractError("the Exemplar corpus seal count does not reconcile with run.json")

    census: dict[int, dict[str, Any]] = {}
    for row in payload["pages"]:
        ordinal = row.get("ordinal") if isinstance(row, dict) else None
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError("the Exemplar corpus seal carries a page row without an ordinal")
        if ordinal in census:
            raise ContractError(f"the Exemplar corpus seal names ordinal {ordinal} more than once")
        census[ordinal] = row
    if set(census) != expected_ordinals:
        raise ContractError("the Exemplar corpus seal page set does not reconcile with run.json")

    expected_refs = {
        (entry["relative_path"], entry["sha256"]) for entry in entries_by_ordinal.values()
    }
    actual_refs = {
        (reference.get("relative_path"), reference.get("sha256"))
        for reference in seal.get("inputs", [])
        if isinstance(reference, dict)
    }
    if actual_refs != expected_refs or len(seal.get("inputs", [])) != len(expected_refs):
        raise ContractError("the Exemplar corpus seal inputs do not name every page outcome once")

    for ordinal, source in sources.items():
        record = records[ordinal]
        outcome = record.get("outcome")
        if outcome == "refused":
            verify_refused_page_evidence(tree, run, source, record)
        expected = {
            "ordinal": ordinal,
            "declared_path": source.get("relative_path"),
            "declared_sha256": source.get("sha256"),
            "page_id": record.get("subject_id") if outcome == "sealed" else None,
            "outcome": outcome,
            "source_sha256": record.get("payload", {}).get("source_sha256")
            if outcome == "sealed"
            else None,
        }
        for field in ("bytes", "ledger_sha256", "container_page_index"):
            if source.get(field) is not None:
                expected[{"bytes": "declared_bytes"}.get(field, field)] = source[field]
        if census[ordinal] != expected:
            raise ContractError(
                "the Exemplar corpus seal row does not match its page outcome and "
                "submitted filename ledger"
            )


def _validate_exemplar_transform(transform: Any) -> None:
    """Keep the recorded Exemplar transform vocabulary closed and executable."""
    if not isinstance(transform, dict) or not isinstance(transform.get("operation"), str):
        raise ContractError("an Exemplar transform has no declared operation")
    operation = transform["operation"]
    if operation == "crop":
        if set(transform) == {"operation", "bounds"}:
            bounds = transform["bounds"]
            if (
                not isinstance(bounds, dict)
                or set(bounds) != {"space", "x", "y", "w", "h"}
                or bounds.get("space") != "part"
                or any(
                    not isinstance(bounds[key], int) or isinstance(bounds[key], bool)
                    for key in ("x", "y", "w", "h")
                )
                or bounds["x"] < 0
                or bounds["y"] < 0
                or bounds["w"] <= 0
                or bounds["h"] <= 0
            ):
                raise ContractError("a derivative crop transform has no complete part-local bounds")
            return
        if set(transform) != {"operation", "source_page_ordinal", "source_page_id", "bounds"}:
            raise ContractError("a crop region carries no complete Exemplar transform")
        bounds = transform["bounds"]
        if (
            not isinstance(transform["source_page_ordinal"], int)
            or isinstance(transform["source_page_ordinal"], bool)
            or not isinstance(transform["source_page_id"], str)
            or not transform["source_page_id"]
            or not isinstance(bounds, dict)
            or set(bounds) != {"x", "y", "w", "h"}
            or any(
                not isinstance(value, int) or isinstance(value, bool) for value in bounds.values()
            )
            or bounds["w"] <= 0
            or bounds["h"] <= 0
        ):
            raise ContractError("a crop region carries an invalid Exemplar transform")
        return
    if operation == "split":
        region = transform.get("region")
        if (
            set(transform) != {"operation", "region"}
            or not isinstance(region, dict)
            or set(region) != {"space", "x", "y", "w", "h"}
            or region.get("space") != "frame"
            or any(
                not isinstance(region[key], int) or isinstance(region[key], bool)
                for key in ("x", "y", "w", "h")
            )
            or region["x"] < 0
            or region["y"] < 0
            or region["w"] <= 0
            or region["h"] <= 0
        ):
            raise ContractError("a split transform has no complete frame-space region")
        return
    if operation == "deskew":
        rotation = transform.get("rotation")
        if (
            set(transform) != {"operation", "rotation"}
            or not isinstance(rotation, dict)
            or set(rotation) != {"rotation_millidegrees", "direction", "origin", "canvas"}
            or not isinstance(rotation["rotation_millidegrees"], int)
            or isinstance(rotation["rotation_millidegrees"], bool)
            or not -180_000 <= rotation["rotation_millidegrees"] <= 180_000
            or rotation.get("direction") != "clockwise"
            or rotation.get("origin") != "crop-centre"
            or rotation.get("canvas") != "expand"
        ):
            raise ContractError("a deskew transform has no complete rotation recipe")
        return
    if operation == "convert":
        if set(transform) != {"operation", "colour_mode"} or transform.get("colour_mode") not in {
            "keep",
            "grayscale",
            "rgb",
            "bitonal",
        }:
            raise ContractError("a convert transform has no declared colour mode")
        return
    raise ContractError("an Exemplar transform names an operation outside the closed vocabulary")


def verify_exemplar_crop_lineage(
    tree: RunTree, run: dict[str, Any], region: dict[str, Any]
) -> dict[str, Any]:
    """Verify one Designator crop against the exact sealed Exemplar page it inputs."""
    if (
        region.get("run_id") != tree.run_id
        or region.get("stage") != DESIGNATOR
        or region.get("kind") != "region"
        or region.get("outcome") != "proposed"
        or region.get("config_digest") != run.get("config_digest")
    ):
        raise ContractError("a crop region does not belong to this run and Designator")
    payload = region.get("payload")
    if not isinstance(payload, dict):
        raise ContractError("a crop region has no payload")
    transform = payload.get("transform")
    _validate_exemplar_transform(transform)
    if set(transform) != {"operation", "source_page_ordinal", "source_page_id", "bounds"}:
        raise ContractError("a crop region carries no complete Exemplar transform")
    # The operation is settled by the two checks above rather than here: the
    # closed vocabulary gives "split", "deskew" and "convert" key sets of their
    # own, so nothing but a validated crop survives the four-key shape check.
    ordinal = transform["source_page_ordinal"]
    source_page_id = transform["source_page_id"]
    bounds = transform["bounds"]
    if payload.get("region_id") != region_id(region.get("subject_id"), transform):
        raise ContractError("a crop region's identities do not bind its recorded transform")
    _verify_act_identity_binding(tree, region, payload)
    sources = [row for row in run.get("source_manifest", []) if row.get("ordinal") == ordinal]
    if len(sources) != 1:
        raise ContractError("a crop region's source ordinal does not name one submitted page")
    source = sources[0]
    page_artifact_id = artifact_id(EXEMPLAR, "page", source_page_id)
    page = tree.read_artifact(EXEMPLAR, "page", page_artifact_id)
    if page.get("subject_id") != source_page_id:
        raise ContractError("a crop region's page id does not name its Exemplar page")
    verify_sealed_page_pixels(tree, run, source, page)

    page_path = page["payload"]["image_path"]
    page_digest = page["payload"]["source_sha256"]
    page_pixels = _read_checked(
        tree,
        {"relative_path": page_path, "sha256": page_digest},
        "the sealed Exemplar page",
    )
    page_width, page_height = dimensions(page_pixels)
    if (
        bounds["x"] < 0
        or bounds["y"] < 0
        or bounds["x"] + bounds["w"] > page_width
        or bounds["y"] + bounds["h"] > page_height
    ):
        raise ContractError("a crop region's transform falls outside its Exemplar page")
    expected_page_ref = {"relative_path": page_path, "sha256": page_digest}
    inputs = region.get("inputs")
    origin = payload.get("origin")
    if origin == "proposal":
        if inputs != [expected_page_ref]:
            raise ContractError(
                "a proposal crop region does not input only the Exemplar page its transform names"
            )
    elif origin == "recovery":
        if not isinstance(inputs, list) or expected_page_ref not in inputs or len(inputs) != 2:
            raise ContractError(
                "a recovery crop region does not input its Exemplar page and one recovery request"
            )
        request_ref = next(reference for reference in inputs if reference != expected_page_ref)
        request = tree.read_artifact_reference(
            request_ref,
            stage=RECENSOR,
            kind="recovery-request",
            subject_id=region["subject_id"],
        )
        request_payload = request.get("payload")
        if (
            request["outcome"] != "recovery-requested"
            or not isinstance(request_payload, dict)
            or request_payload.get("act_key") != payload.get("act_key")
        ):
            raise ContractError(
                "a recovery crop region is not bound to a matching Recensor request"
            )
    else:
        raise ContractError("a crop region has no recognized proposal or recovery origin")
    image_path, image_digest = payload.get("image_path"), payload.get("image_sha256")
    if not isinstance(image_path, str) or not _is_sha256(image_digest):
        raise ContractError("a crop region names no content-addressed crop image")
    crop = _read_checked(
        tree,
        {"relative_path": image_path, "sha256": image_digest},
        "the sealed Designator crop",
    )
    expected_crop = crop_png(page_pixels, bounds)
    if crop != expected_crop:
        _verify_crop_is_the_same_image(crop, expected_crop)
    width, height = dimensions(crop)
    if (width, height) != (bounds["w"], bounds["h"]):
        raise ContractError("a crop region's pixels disagree with its recorded bounds")
    return {
        "region_id": payload.get("region_id"),
        "image_path": image_path,
        "image_sha256": image_digest,
        "verified_dimensions": {"w": width, "h": height},
        "source_page_ordinal": ordinal,
        "source_page_id": source_page_id,
        "transform": dict(transform),
        # Attestatores and Perlector validate this receipt-backed provenance
        # before invoking this helper. Keep it with the verified crop facts so
        # the export can still name the chair that marked the ink out.
        "structure_provenance": payload.get("provenance"),
    }


def _verify_crop_is_the_same_image(stored: bytes, derived: bytes) -> None:
    """Decide what a byte difference between a sealed crop and the re-derived one
    actually is, and refuse only the difference that matters.

    ARCHITECTURE's third invariant is about the *image*: "the exact image shown to
    a model is reproducible from the Exemplar plus the recorded transforms". A
    byte comparison also asserts that the encoder which wrote the crop and the
    encoder running now emit the same stream — true while one build writes both
    sides, and false the moment a pod, a CI matrix, a Python upgrade or a resumed
    run puts a different zlib or a different Pillow on the second side. That is a
    benign environment change, and reporting it as tampered evidence is both a
    false alarm and a lost one: an operator who has been told the pixels do not
    trace stops looking at the pixels.

    So the comparison that decides is on the image, and a crop that shows exactly
    the derived crop passes however it was framed — which is also what lets a run
    tree sealed by an earlier encoder still verify under this one. Two things the
    byte comparison used to say are said explicitly instead: the stored crop must
    decode at all, and it must be the picture and nothing else, because "the
    pixels match" is silent about a text chunk or a block of bytes travelling
    beside them. `image_sha256` is checked before this and still binds the crop's
    bytes to its record; nothing here loosens immutability.
    """
    try:
        stored_image = image_shown(stored)
    except ValueError as error:
        raise ContractError(
            "a crop region's sealed pixels are not a decodable image to compare against "
            "the Exemplar page its transform names"
        ) from error
    # The derived side was produced by `crop_png` a moment ago, so a failure here
    # is this pipeline's own encoder, not the evidence, and the refusal must say
    # so by name rather than escaping as a bare ValueError.
    try:
        derived_image = image_shown(derived)
    except ValueError as error:
        raise ContractError(
            "the crop re-derived from the Exemplar page is not decodable; this is the "
            "pipeline's own encoder failing, not a fault of the sealed evidence"
        ) from error
    if stored_image != derived_image:
        raise ContractError(
            "a crop region's pixels are not the exact crop of the Exemplar page its transform names"
        )
    if not carries_only_image_chunks(stored):
        raise ContractError(
            "a crop region's sealed image shows the right pixels but carries content beyond "
            "the crop itself"
        )


def _verify_act_identity_binding(
    tree: RunTree, region: dict[str, Any], payload: dict[str, Any]
) -> None:
    """Refuse a region whose claimed act does not match the Designator's own seal.

    Everything above proves the region's TRANSFORM traces to a genuine sealed
    Exemplar page, and that `region_id` is self-consistent with whatever act_id
    the region already claims as `subject_id` — but nothing before this line ever
    recomputed that act_id or checked it against the one place act identity is
    recorded once and never rewritten. A region genuinely cut from a real sealed
    page, self-consistent under a *relabelled* subject_id, would pass every check
    above it: pixels that really are act B's crop, filed under act A's identity,
    with every cryptographic reference still green. That is exactly the class of
    fault a recent round closed at the page level (a crop carrying the wrong
    page's identity); this closes its act-level analogue.

    The proposal seal is the downstream expected-act authority (`common/stage.py`'s
    `expected_acts`) and is emitted once, never rewritten, so a region's `act_key`
    must name exactly one seal entry, and that entry's own `act_id` — not the
    region's self-reported one — is what `subject_id` must equal. Proposal
    evidence is checked here too, so this function does not depend on a caller
    first running the broader proposal-seal reconciliation.
    """
    subject_id = region.get("subject_id")
    act_key = payload.get("act_key")
    if not isinstance(act_key, str) or not act_key:
        raise ContractError("a crop region names no act_key to verify its identity against")
    seal = tree.read_artifact(
        DESIGNATOR, "proposal-seal", artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    )
    matches = [
        entry for entry in seal["payload"]["expected_acts"] if entry.get("act_key") == act_key
    ]
    if len(matches) != 1:
        raise ContractError(
            f"a crop region names act_key {act_key!r}, which the proposal seal does not "
            "name exactly once"
        )
    if matches[0].get("act_id") != subject_id:
        raise ContractError(
            "a crop region's subject_id does not match the proposal seal's act identity "
            "for the act_key it claims"
        )
    if payload.get("origin") == "proposal":
        artifact = region.get("artifact_id")
        if not isinstance(artifact, str) or not artifact:
            raise ContractError("a proposal crop region has no artifact id to bind to its seal")
        relative_path = tree.artifact_path(DESIGNATOR, "region", artifact)
        reference = {
            "relative_path": relative_path,
            "sha256": digest_bytes(tree.read_bytes(relative_path)),
        }
        if reference not in matches[0].get("evidence", []):
            raise ContractError(
                "a proposal crop region does not name this proposal crop in the act's sealed evidence"
            )


def _refuse_a_merged_page_no_consumer_reads_yet(records: dict[int, dict[str, Any]]) -> None:
    """Name the one shape the Exemplar can seal and nothing behind it can read.

    Byte-identical sources submitted twice seal as one page citing both rows —
    the right answer, and the Exemplar's. Every stage behind it, though, keys
    its work by submitted ordinal and would process that page once per row,
    minting each act twice against one `page_id`. Until consumers process merged
    pages once per identity, this is refused by name here rather than surfacing
    downstream as "lost submitted page ordinal(s)" — which would be a lie about
    a page that was sealed, cited, and never lost at all.

    **The trap is now closed at the Door**, which refuses such a submission
    whole before its Exemplar ever runs
    (`pipeline/1_exemplar/door.py::require_no_duplicate_sources`), so an
    operator learns from the stage that read the filenames rather than from a
    green Exemplar followed by a fatal consumer. This stays as the second line
    of defence: it guards the sealed shape itself rather than one route into it,
    and a merged page record handed to a consumer directly — by a future
    producer, a repaired tree, or a caller that never passed a door — is refused
    here on its own merits.
    """
    for ordinal, record in records.items():
        if record.get("outcome") != "sealed":
            continue
        payload = record.get("payload")
        rows = sealed_submission_rows(payload) if isinstance(payload, dict) else {}
        if len(rows) > 1:
            raise ContractError(
                f"the Exemplar sealed submitted ordinal(s) {sorted(rows)} into the single page "
                f"{record.get('subject_id')} because they carry identical bytes; that is one "
                "page and one act set, but every stage behind the Exemplar still works one "
                "page per submitted row and would mint each act on it twice. The run is "
                f"refused here rather than read twice (reached via ordinal {ordinal})"
            )


def sealed_submission_rows(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Every submission row one sealed page names, by ordinal.

    Ordinarily one. Byte-identical sources submitted under two filenames derive
    one `page_id` — identity binds the bytes, not the manifest row — so the
    Exemplar seals one page artifact citing both rows rather than publishing the
    same identity twice. The rows are the page's account of which submissions it
    discharges, and every consumer reads them through here.
    """
    rows = payload.get("submission_rows")
    if not isinstance(rows, list) or not rows:
        raise ContractError("a sealed Exemplar page cites no submitted row")
    by_ordinal: dict[int, dict[str, Any]] = {}
    for row in rows:
        ordinal = row.get("ordinal") if isinstance(row, dict) else None
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError("a sealed Exemplar page cites a submitted row with no ordinal")
        if ordinal in by_ordinal:
            raise ContractError(
                f"a sealed Exemplar page cites submitted ordinal {ordinal} twice; a row "
                "counted twice is a page count that no longer reconciles"
            )
        by_ordinal[ordinal] = row
    if list(by_ordinal) != sorted(by_ordinal):
        raise ContractError("a sealed Exemplar page cites its submitted rows out of order")
    return by_ordinal


def _verify_submission_row(row: dict[str, Any], source: dict[str, Any]) -> None:
    """One cited submission row against the run authority's manifest row."""
    for field in ("relative_path", "sha256", "bytes", "ledger_sha256", "container_page_index"):
        if source.get(field) != row.get(field):
            raise ContractError(
                "a sealed Exemplar page cites a submitted row that no longer matches its "
                "submitted filename ledger entry"
            )


def _verify_page_source_facts(
    payload: dict[str, Any], source: dict[str, Any], ordinal: int
) -> None:
    expected = {
        "ordinal": ordinal,
        "declared_path": source.get("relative_path"),
        "declared_sha256": source.get("sha256"),
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise ContractError(
            "a sealed Exemplar page no longer matches its submitted filename ledger entry"
        )
    for source_field, payload_field in (
        ("bytes", "declared_bytes"),
        ("ledger_sha256", "ledger_sha256"),
        ("container_page_index", "container_page_index"),
    ):
        source_value = source.get(source_field)
        if source_value is None:
            if payload_field in payload:
                raise ContractError(
                    "a sealed Exemplar page carries a filename-ledger fact absent from run.json"
                )
        elif payload.get(payload_field) != source_value:
            raise ContractError(
                "a sealed Exemplar page no longer matches its submitted filename ledger entry"
            )


def _verify_admission(
    admission: dict[str, Any],
    run: dict[str, Any],
    source: dict[str, Any],
    ordinal: int,
    blob_ref: dict[str, str],
    tree: RunTree,
    page_rendered: Any,
) -> None:
    if (
        admission.get("run_id") != run.get("run_id")
        or admission.get("stage") != DOOR
        or admission.get("kind") != "admission"
        or admission.get("outcome") != "admitted"
        or admission.get("config_digest") != run.get("config_digest")
        or admission.get("subject_id") != f"source-{ordinal}"
    ):
        raise ContractError("a sealed Exemplar page's Door admission does not match this source")
    payload = admission.get("payload")
    if not isinstance(payload, dict):
        raise ContractError("a sealed Exemplar page's Door admission has no payload")
    expected = {
        "ordinal": ordinal,
        "declared_path": source.get("relative_path"),
        "declared_sha256": source.get("sha256"),
        "sha256": blob_ref["sha256"],
        "stored_at": blob_ref["relative_path"],
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise ContractError("a sealed Exemplar page's Door admission disagrees with its pixel blob")
    for source_field, payload_field in (
        ("bytes", "declared_bytes"),
        ("ledger_sha256", "ledger_sha256"),
    ):
        source_value = source.get(source_field)
        if source_value is not None and payload.get(payload_field) != source_value:
            raise ContractError(
                "a sealed Exemplar page's Door admission disagrees with the filename ledger"
            )
    rendered = _verify_rendered_source_link(page_rendered, payload.get("rendered_from"), source)
    render_contract = rendered.get("render_contract") if isinstance(rendered, dict) else None
    if not is_triage_derivative_contract(render_contract):
        if admission.get("inputs") != [blob_ref]:
            raise ContractError("a sealed Exemplar page's Door admission has the wrong pixel input")
        return
    parent = payload.get("parent_frame")
    if not isinstance(parent, dict) or set(parent) != {"sha256", "stored_at", "source_frame_index"}:
        raise ContractError("a sealed derivative page has no complete parent-frame back-link")
    parent_digest = parent["sha256"]
    parent_path = parent["stored_at"]
    if (
        # `.get`, because a submitted-source row that carries no digest at all is
        # the boundary's business to refuse, not to raise KeyError over: this
        # function's callers convert ContractError into a refusal and nothing else.
        parent_digest != source.get("sha256")
        or parent_path != tree.blob_path(DOOR, parent_digest)
        or not isinstance(parent["source_frame_index"], int)
        or isinstance(parent["source_frame_index"], bool)
        or parent["source_frame_index"] < 0
    ):
        raise ContractError(
            "a sealed derivative page's parent frame disagrees with its submitted master"
        )
    parent_ref = {"relative_path": parent_path, "sha256": parent_digest}
    expected_inputs = {
        (blob_ref["relative_path"], blob_ref["sha256"]),
        (parent_ref["relative_path"], parent_ref["sha256"]),
    }
    if {
        (reference.get("relative_path"), reference.get("sha256"))
        for reference in admission.get("inputs", [])
        if isinstance(reference, dict)
    } != expected_inputs or len(admission.get("inputs", [])) != len(expected_inputs):
        raise ContractError("a sealed derivative page does not input exactly its pixels and master")
    parent_bytes = _read_checked(tree, parent_ref, "the derivative page's submitted master")
    sealed_bytes = _read_checked(tree, blob_ref, "the sealed derivative page")
    verify_triage_derivative(rendered["render_contract"], parent_bytes, parent, sealed_bytes)


def _verify_rendered_source_link(
    page_rendered: Any, admission_rendered: Any, source: dict[str, Any]
) -> dict[str, Any] | None:
    """Bind a page's claimed container origin back to its Door admission and source row."""
    if admission_rendered != page_rendered:
        raise ContractError("a sealed Exemplar page changed its Door admission's render origin")
    if admission_rendered is None:
        if source.get("container_page_index") is not None:
            raise ContractError("a fanned source page carries no rendered-container origin")
        return None
    _validate_rendered_origin(admission_rendered)
    if admission_rendered["container_sha256"] != source.get("sha256") or admission_rendered[
        "container_page_index"
    ] != source.get("container_page_index"):
        raise ContractError(
            "a sealed Exemplar page's rendered-container origin does not bind its submitted source"
        )
    return admission_rendered


def is_triage_derivative_contract(render_contract: Any) -> bool:
    """Whether a render contract describes a sealed triage derivative.

    One function rather than two. This decides which validation a page gets — a
    derivative is checked against its parent frame and re-derived, an ordinary
    render against the render contract — and the Exemplar stage asked the same
    question with its own copy. Two copies is one kind vocabulary too many: teach
    one of them a `sealed-derivative-page-v2` and the other keeps saying no, and
    the disagreement is not a crash. It is a page sealed as an ordinary render
    whose pixels nobody re-derived, or a re-derivation demanded of a page that has
    no parent. Both callers pass the render contract, which is the smaller of the
    two shapes they had between them.
    """
    return (
        isinstance(render_contract, dict)
        and isinstance(render_contract.get("derivative_page"), dict)
        and render_contract["derivative_page"].get("kind") == SEALED_DERIVATIVE_PAGE_KIND
    )


def _validate_embedded_triage_row(row: Any) -> None:
    """Validate the provenance fields the common boundary must not take on trust.

    Geometry is checked executable against every recorded operation and the master
    itself. The common boundary cannot import the numbered triage pipeline, so it
    must also close mode, actor, override, confidence, cluster identity, and every
    row/split/part field set here.
    """
    required = {
        "corpus_id",
        "source_frame_sha256",
        "frame",
        "split",
        "re_shoot_cluster_id",
        "confidence",
        "mode",
        "actor",
        "human_override",
        "manifest_row_sha256",
    }
    if not isinstance(row, dict) or set(row) != required:
        raise ContractError("a sealed derivative page carries no complete triage manifest row")
    if not isinstance(row["corpus_id"], str) or not row["corpus_id"].strip():
        raise ContractError("a sealed derivative page's triage row has no corpus identity")
    if row["mode"] not in TRIAGE_MODES:
        raise ContractError("a sealed derivative page's triage row has no declared mode")
    if (
        not isinstance(row["confidence"], int)
        or isinstance(row["confidence"], bool)
        or row["confidence"] not in range(5)
        or not isinstance(row["human_override"], bool)
    ):
        raise ContractError(
            "a sealed derivative page's triage row has invalid confidence or override provenance"
        )
    cluster_id = row["re_shoot_cluster_id"]
    if cluster_id is not None and (not isinstance(cluster_id, str) or not cluster_id.strip()):
        raise ContractError("a sealed derivative page's triage row has an invalid cluster identity")
    actor = row["actor"]
    if (
        not isinstance(actor, dict)
        or set(actor) != {"kind", "identity", "revision"}
        or actor.get("kind") not in {"human", "model", "scantailor"}
        or not isinstance(actor.get("identity"), str)
        or not actor["identity"].strip()
        or (actor["kind"] == "human" and actor.get("revision") is not None)
        or (
            actor["kind"] != "human"
            and (not isinstance(actor.get("revision"), str) or not actor["revision"].strip())
        )
    ):
        raise ContractError("a sealed derivative page's triage row has no resolved actor")
    split = row["split"]
    if (
        not isinstance(split, dict)
        or set(split) != {"operation_order", "parts"}
        or split.get("operation_order") != "region-crop-rotate"
        or not isinstance(split.get("parts"), list)
        or not split["parts"]
        or any(
            not isinstance(part, dict)
            or set(part) != {"region", "crop_box", "rotation", "colour_mode"}
            for part in split["parts"]
        )
    ):
        raise ContractError("a sealed derivative page's triage row has no closed split record")
    if len(split["parts"]) > MAX_TRIAGE_SPLIT_PARTS:
        # Bounded here as well as in the pre-door contract, and before the pairwise
        # overlap loop below rather than after it: this boundary exists precisely
        # because the row reaching it is not taken on trust, and the loop it guards
        # is quadratic in the number of parts.
        raise ContractError(
            f"a sealed derivative page's triage row exceeds the "
            f"{MAX_TRIAGE_SPLIT_PARTS}-part split limit"
        )
    frame = row["frame"]
    if (
        not isinstance(frame, dict)
        or set(frame) != {"width", "height"}
        or any(
            not isinstance(frame[field], int) or isinstance(frame[field], bool) or frame[field] <= 0
            for field in ("width", "height")
        )
    ):
        raise ContractError("a sealed derivative page's triage row has no closed frame geometry")
    regions = []
    for part in split["parts"]:
        operations = (
            {"operation": "split", "region": part["region"]},
            {"operation": "crop", "bounds": part["crop_box"]},
            {"operation": "deskew", "rotation": part["rotation"]},
            {"operation": "convert", "colour_mode": part["colour_mode"]},
        )
        for operation in operations:
            _validate_exemplar_transform(operation)
        region = part["region"]
        crop_box = part["crop_box"]
        if (
            region["x"] + region["w"] > frame["width"]
            or region["y"] + region["h"] > frame["height"]
            or crop_box["x"] + crop_box["w"] > region["w"]
            or crop_box["y"] + crop_box["h"] > region["h"]
        ):
            raise ContractError("a sealed derivative page's triage row has out-of-frame geometry")
        regions.append(region)
    for index, region in enumerate(regions):
        for other in regions[index + 1 :]:
            disjoint = (
                region["x"] + region["w"] <= other["x"]
                or other["x"] + other["w"] <= region["x"]
                or region["y"] + region["h"] <= other["y"]
                or other["y"] + other["h"] <= region["y"]
            )
            if not disjoint:
                raise ContractError("a sealed derivative page's triage row has overlapping parts")
    if sum(region["w"] * region["h"] for region in regions) != frame["width"] * frame["height"]:
        raise ContractError("a sealed derivative page's triage row does not partition its frame")


def verify_triage_derivative(
    contract: dict[str, Any],
    parent_bytes: bytes,
    parent: dict[str, Any],
    sealed_bytes: bytes,
) -> None:
    """A split page is valid only when its closed decision re-derives its bytes."""
    contract_fields = {
        "renderer",
        "renderer_version",
        "pillow_heif_version",
        "libheif_version",
        "source_mode",
        "source_bands",
        "mode_transform",
        "output",
        "container_page_index",
        "width",
        "height",
        "deterministic_encoder",
        "derivative_page",
    }
    if not isinstance(contract, dict) or set(contract) != contract_fields:
        raise ContractError("a sealed derivative page has no complete renderer record")
    derivative = contract.get("derivative_page")
    required = {
        "kind",
        "parent_frame_sha256",
        "parent_frame_page_index",
        "triage_manifest_row",
        "triage_backlink",
        "operation_order",
        "apply_recipe",
        "operations",
    }
    if not isinstance(derivative, dict) or set(derivative) != required:
        raise ContractError("a sealed derivative page has no complete apply recipe")
    if derivative["apply_recipe"] != {
        "schema": "triage-raster-apply-v1",
        "rotation_resample": "Pillow.Resampling.BICUBIC",
        "rotation_fill": "Pillow-default-zero",
        "rotation_expand": True,
        "colour_conversion": "Pillow.Image.convert-direct-or-via-RGB",
        "encoder": "common.imaging.encode_image_deterministic-v1",
    }:
        raise ContractError("a sealed derivative page changes its recorded raster apply recipe")
    if contract.get("renderer") != "Pillow" or any(
        not isinstance(contract.get(field), str) or not contract[field]
        for field in ("renderer_version", "pillow_heif_version", "libheif_version")
    ):
        raise ContractError("a sealed derivative page carries no complete renderer version record")
    row = derivative["triage_manifest_row"]
    backlink = derivative["triage_backlink"]
    if not isinstance(row, dict) or not isinstance(backlink, dict):
        raise ContractError(
            "a sealed derivative page does not carry its manifest row and back-link"
        )
    _validate_embedded_triage_row(row)
    row_digest = row.get("manifest_row_sha256")
    if not _is_sha256(row_digest):
        raise ContractError("a sealed derivative page's manifest row has no sha256")
    if (
        digest_bytes(
            canonical_bytes(
                {key: value for key, value in row.items() if key != "manifest_row_sha256"}
            )
        )
        != row_digest
    ):
        raise ContractError("a sealed derivative page's manifest row digest does not bind its row")
    expected_backlink = {
        "corpus_id": row["corpus_id"],
        "source_frame_sha256": row["source_frame_sha256"],
        "triage_manifest_row_sha256": row_digest,
        "triage_part_index": backlink.get("triage_part_index"),
    }
    if backlink != expected_backlink or row["source_frame_sha256"] != parent["sha256"]:
        raise ContractError(
            "a sealed derivative page's manifest back-link does not name its master"
        )
    part_index = backlink["triage_part_index"]
    split = row["split"]
    if (
        not isinstance(part_index, int)
        or isinstance(part_index, bool)
        or not 0 <= part_index < len(split["parts"])
        or derivative["parent_frame_sha256"] != parent["sha256"]
        or derivative["parent_frame_page_index"] != parent["source_frame_index"]
        or derivative["operation_order"] != "region-crop-rotate"
    ):
        raise ContractError("a sealed derivative page changes Unit 5's closed split semantics")
    part = split["parts"][part_index]
    expected_operations = [
        {"operation": "split", "region": part.get("region")},
        {"operation": "crop", "bounds": part.get("crop_box")},
        {"operation": "deskew", "rotation": part.get("rotation")},
        {"operation": "convert", "colour_mode": part.get("colour_mode")},
    ]
    if derivative["operations"] != expected_operations:
        raise ContractError(
            "a sealed derivative page's transform vocabulary does not match its manifest part"
        )
    try:
        expected_bytes, geometry = render_triage_derivative(
            parent_bytes, page_index=parent["source_frame_index"], part=part
        )
    except ValueError as error:
        raise ContractError(
            "the sealed derivative page cannot be re-derived from its master"
        ) from error
    expected_mode_transform = (
        "triage-region-crop-rotate-convert"
        if geometry["source_mode"] == geometry["color_mode"]
        else f"triage-region-crop-rotate-convert-to-{geometry['color_mode'].lower()}"
    )
    expected_render_record = {
        "source_mode": geometry["source_mode"],
        "source_bands": geometry["source_bands"],
        "mode_transform": expected_mode_transform,
        "output": {"codec": "png", "color_mode": geometry["color_mode"]},
        "container_page_index": parent["source_frame_index"],
        "width": geometry["width"],
        "height": geometry["height"],
        "deterministic_encoder": "common.imaging.encode_image_deterministic-v1",
    }
    if any(contract.get(field) != value for field, value in expected_render_record.items()):
        raise ContractError(
            "a sealed derivative page's renderer record does not describe its re-derived pixels"
        )
    # The row validator proves coverage only against the row's declared frame;
    # equality with the decoded master is what prevents undeclared source pixels.
    frame = row["frame"]
    if (geometry["source_width"], geometry["source_height"]) != (
        frame["width"],
        frame["height"],
    ):
        raise ContractError(
            "a sealed derivative page's manifest row declares a frame that is not the size "
            "of the master it was cut from, so the row's parts do not account for that master"
        )
    if expected_bytes != sealed_bytes:
        raise ContractError(
            "a sealed derivative page's pixels are not reproducible from its master and apply "
            f"recipe{_renderer_drift(contract)}"
        )


def _renderer_drift(contract: dict[str, Any]) -> str:
    """Name a library upgrade when one is the likelier cause of a pixel mismatch.

    The apply recipe is verified as a *record*, not against the running host: a
    run sealed under an older Pillow stays verifiable, which refusing on version
    drift would destroy for every archived run on the next routine upgrade. The
    byte comparison above is the real property. But its message on its own points
    an operator at forgery, and an upgraded decoder is the ordinary explanation —
    so when the versions differ, say which ones.
    """
    fields = ("renderer_version", "pillow_heif_version", "libheif_version")
    try:
        running = imaging_library_versions()
        recorded = {field: contract[field] for field in fields}
    except (KeyError, TypeError, OSError, ImportError):
        return ""
    drifted = {
        name: (recorded[name], running[name])
        for name in fields
        if recorded[name] != running.get(name)
    }
    if not drifted:
        return ""
    named = ", ".join(f"{name} {was!r} -> {now!r}" for name, (was, now) in sorted(drifted.items()))
    return (
        f"; the page was sealed under different imaging libraries than this host runs ({named}), "
        "which is the ordinary cause and is recorded, not enforced"
    )


def _references_by_path(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, list):
        raise ContractError("a sealed Exemplar page has no input references")
    refs: dict[str, dict[str, str]] = {}
    for ref in value:
        if not isinstance(ref, dict):
            raise ContractError("a sealed Exemplar page has an invalid input reference")
        path, digest = ref.get("relative_path"), ref.get("sha256")
        if not isinstance(path, str) or not _is_sha256(digest) or path in refs:
            raise ContractError("a sealed Exemplar page has an invalid input reference")
        refs[path] = {"relative_path": path, "sha256": digest}
    return refs


def _read_checked(tree: RunTree, ref: dict[str, str], label: str) -> bytes:
    try:
        data = tree.read_bytes(ref["relative_path"])
    except OSError as error:
        raise ContractError(f"{label} could not be read") from error
    try:
        verify_input_bytes(ref, data)
    except SchemaRefusal as error:
        raise ContractError(f"{label} no longer matches its sealed input digest") from error
    return data


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
