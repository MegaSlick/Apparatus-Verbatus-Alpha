"""Checks the immutable pixels handed from Exemplar to later stages.

A sealed Exemplar page binds the Door admission artifact and the exact
content-addressed image blob every later crop must use. Consumers call this
before acting on those pixels so a changed, missing or substituted blob cannot
be quietly re-hashed into new downstream evidence.

Knows contracts and the run tree, not a numbered pipeline module: Ink Map,
Designator, Attestatores, Perlector, Recensor, and Armarium all use this same
check, the first five to prevent work over altered pixels, Armarium to prevent
an export after pixels changed between stages.
"""

import json
from typing import Any, Callable, Final

from common.contracts.canonical import (
    digest_bytes,
    digest_of,
    is_sha256,
    verify_self_hash,
)
from common.contracts.envelope import read_verified, validate_envelope
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id, page_id, region_id
from common.contracts.stages import DOOR, EXEMPLAR, PERLECTOR
from common.contracts.triage import validate_row
from common.imaging import (
    DETERMINISTIC_ENCODER,
    carries_only_image_chunks,
    crop_png,
    dimensions,
    image_shown,
    imaging_library_versions,
    render_triage_derivative,
    triage_apply_recipe,
    triage_mode_transform,
    triage_operations,
)
from common.replay import run_holds
from common.runtree.store import RunTree

# The one name for a triage derivative's kind. The Door writes it, and this
# boundary and the Exemplar stage read it; a producer and its two consumers
# agreeing by coincidence is what `is_triage_derivative_contract` below exists
# to stop.
SEALED_DERIVATIVE_PAGE_KIND: Final = "sealed-derivative-page-v1"


def verify_sealed_page_pixels(
    tree: RunTree,
    run: dict[str, Any],
    source: dict[str, Any],
    page: dict[str, Any],
) -> bytes:
    """Verify one sealed Exemplar page and return its immutable Door pixel bytes.

    ``source`` is the matching self-hashed ``run.json`` source-manifest row and
    ``page`` is a validated Exemplar page artifact.  The page must name exactly
    its Door admission plus its Door blob; both referenced bytes are checked
    again, rather than trusting an earlier stage's successful check.
    """
    ordinal = source.get("ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise ContractError("a submitted source has no integer ordinal for its sealed page")
    if not tree.holds_run_id(page.get("run_id"), EXEMPLAR) or page.get("stage") != EXEMPLAR:
        raise ContractError("a sealed page belongs to a different Exemplar run")
    if page.get("config_digest") != run.get("config_digest"):
        raise ContractError("a sealed page is bound to a different run configuration")
    if page.get("outcome") != "sealed":
        raise ContractError("immutable page-pixel verification was asked of an unsealed page")

    payload = page.get("payload")
    if not isinstance(payload, dict):
        raise ContractError("a sealed Exemplar page has no payload")
    if payload.get("ordinal") != ordinal:
        raise ContractError(
            "a sealed Exemplar page names another submitted ordinal, so it is not the page "
            "this source was sealed into"
        )
    _verify_page_source_facts(payload, source, ordinal)

    source_digest = payload.get("source_sha256")
    if not is_sha256(source_digest):
        raise ContractError("a sealed Exemplar page has no lowercase pixel sha256")
    rendered = payload.get("rendered_from")
    # `_page_origin` type-checks every render field rather than only closing
    # the key set; the try/except below is kept so a malformed origin that
    # survives validation still becomes a named refusal rather than a raw
    # TypeError or RecursionError out of the identity derivation.
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
    admission_path = tree.artifact_path(
        DOOR,
        "admission",
        artifact_id(DOOR, "admission", f"source-{ordinal}"),
    )
    refs = _references_by_path(page.get("inputs"))
    if set(refs) != {admission_path, blob_path}:
        raise ContractError(
            "a sealed Exemplar page must input exactly its Door admission and its pixel blob"
        )
    blob_ref = refs[blob_path]
    if blob_ref != {"relative_path": blob_path, "sha256": source_digest}:
        raise ContractError("a sealed Exemplar page's pixel input is not content-addressed")

    page_bytes = read_verified(
        tree.read_bytes, blob_ref, "the sealed Exemplar pixel blob", ContractError
    )

    admission_data = read_verified(
        tree.read_bytes, refs[admission_path], "the sealed Door admission", ContractError
    )
    try:
        admission = validate_envelope(json.loads(admission_data.decode("utf-8")))
    except (SchemaRefusal, UnicodeDecodeError, ValueError, TypeError) as error:
        raise ContractError("the sealed page's Door admission is not a valid artifact") from error
    # `page_bytes` were read and hashed against `blob_ref`, so its digest stands for them.
    _verify_admission(admission, run, source, ordinal, blob_ref, tree, rendered)
    return page_bytes


def sealed_page_bytes(
    tree: RunTree,
    page: dict[str, Any],
    *,
    what: str = "",
    refusal: type[ContractError] = SchemaRefusal,
) -> bytes:
    """Read a sealed Exemplar page's pixels once, checked against its sealed digest.

    Image work must use these same bytes: a second read reopens the gap a swap
    between check and use would cross. ``what`` names the reader in refusals.
    """
    subject = f"sealed Exemplar page {page.get('subject_id')!r}" + (f" for {what}" if what else "")
    payload = page.get("payload")
    image_path = payload.get("image_path") if isinstance(payload, dict) else None
    if not isinstance(image_path, str) or not image_path:
        raise refusal(f"{subject} has no image path")
    ref = {"relative_path": image_path, "sha256": payload.get("source_sha256")}
    return read_verified(tree.read_bytes, ref, subject, refusal)


def exemplar_crop_transform(page_ordinal: int, page_id: str, bounds: dict) -> dict[str, Any]:
    """The one construction of a crop transform, as `verify_reading_region_lineage` reads it.

    Region identity derives from this shape, so every stage that cuts a crop
    from a sealed page builds it here.
    """
    return {
        "operation": "crop",
        "source_page_ordinal": page_ordinal,
        "source_page_id": page_id,
        "bounds": bounds,
    }


def cut_exemplar_crop(
    retain: Callable[[bytes], dict[str, str]],
    page_bytes: bytes,
    page_ordinal: int,
    page_id: str,
    bounds: dict,
) -> dict[str, Any]:
    """Cut one crop from a sealed page's checked bytes and store it through `retain`.

    Returns the fields that describe it: `transform`, `transform_digest`,
    `image_path` and `image_sha256`. The crop is `crop_png` over the sealed
    pixels, so the stored bytes are reproducible from the page and transform.
    """
    transform = exemplar_crop_transform(page_ordinal, page_id, bounds)
    stored = retain(crop_png(page_bytes, bounds))
    return {
        "transform": transform,
        "transform_digest": digest_of(transform),
        "image_path": stored["relative_path"],
        "image_sha256": stored["sha256"],
    }


def read_sealed_page(
    tree: RunTree,
    page_id: str,
    *,
    what: str = "",
    refusal: type[ContractError] = SchemaRefusal,
) -> tuple[dict[str, Any], bytes]:
    """One Exemplar page artifact and its pixels, read once through `sealed_page_bytes`."""
    page = tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    return page, sealed_page_bytes(tree, page, what=what, refusal=refusal)


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
        or not is_sha256(rendered.get("container_sha256"))
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
        or not tree.holds_run_id(page.get("run_id"), EXEMPLAR)
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
    admission_data = read_verified(
        tree.read_bytes, refs[admission_path], "the refused Door admission", ContractError
    )
    try:
        admission = validate_envelope(json.loads(admission_data.decode("utf-8")))
    except (SchemaRefusal, UnicodeDecodeError, ValueError, TypeError) as error:
        raise ContractError("a refused page's Door admission is not a valid artifact") from error
    if (
        not tree.holds_run_id(admission.get("run_id"), DOOR)
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
    if set(records) != expected_ordinals or set(entries_by_ordinal) != expected_ordinals:
        # By ordinal, never by submitted filename: the data-handling policy
        # excludes a declared path from the stderr channel `run_stage` prints
        # refusals to. The filename lives in the hashed ledger and sealed
        # record instead, where an operator reads it back.
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
        not tree.holds_run_id(seal.get("run_id"), EXEMPLAR)
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


def _sealed_source_page(
    tree: RunTree, run: dict[str, Any], transform: dict[str, Any], what: str
) -> tuple[bytes, dict[str, str]]:
    """The sealed Exemplar page a crop transform names: its pixels and its input reference.

    The page must be the one submitted source at the transform's ordinal, its
    pixels must verify against the Door's admission, and the transform's
    bounds must lie inside it.
    """
    ordinal = transform["source_page_ordinal"]
    source_page_id = transform["source_page_id"]
    bounds = transform["bounds"]
    sources = [row for row in run.get("source_manifest", []) if row.get("ordinal") == ordinal]
    if len(sources) != 1:
        raise ContractError(f"a {what}'s source ordinal does not name one submitted page")
    page = tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", source_page_id))
    if page.get("subject_id") != source_page_id:
        raise ContractError(f"a {what}'s page id does not name its Exemplar page")
    page_pixels = verify_sealed_page_pixels(tree, run, sources[0], page)
    page_ref = {
        "relative_path": page["payload"]["image_path"],
        "sha256": page["payload"]["source_sha256"],
    }
    page_width, page_height = dimensions(page_pixels)
    if (
        bounds["x"] < 0
        or bounds["y"] < 0
        or bounds["x"] + bounds["w"] > page_width
        or bounds["y"] + bounds["h"] > page_height
    ):
        raise ContractError(f"a {what}'s transform falls outside its Exemplar page")
    return page_pixels, page_ref


def _verify_stored_crop(
    page_pixels: bytes, bounds: dict[str, int], payload: dict[str, Any], tree: RunTree, what: str
) -> tuple[int, int]:
    """The crop a payload names is exactly `bounds` cut from the page; its size is returned."""
    image_path, image_digest = payload.get("image_path"), payload.get("image_sha256")
    if not isinstance(image_path, str) or not is_sha256(image_digest):
        raise ContractError("a crop region names no content-addressed crop image")
    crop = read_verified(
        tree.read_bytes,
        {"relative_path": image_path, "sha256": image_digest},
        what,
        ContractError,
    )
    expected_crop = crop_png(page_pixels, bounds)
    if crop != expected_crop:
        _verify_crop_is_the_same_image(crop, expected_crop)
    width, height = dimensions(crop)
    if (width, height) != (bounds["w"], bounds["h"]):
        raise ContractError("a crop region's pixels disagree with its recorded bounds")
    return width, height


def verify_reading_region_lineage(
    tree: RunTree, run: dict[str, Any], region: dict[str, Any]
) -> dict[str, Any]:
    """Verify one Perlector `act-region` crop against the sealed Exemplar page it was cut from.

    The page reading's regions are cut by `cut_exemplar_crop` over the union of
    the cited boxes: the transform must be that crop of the page the record
    names, `region_id` must bind the act and transform, the record must input
    the page and the crop, and the stored crop must be exactly those pixels.
    An unplaced entry has no crop and is refused here; its caller never asks.
    """
    if (
        region.get("run_id") != tree.run_id
        or region.get("stage") != PERLECTOR
        or region.get("kind") != "act-region"
        or region.get("config_digest") != run.get("config_digest")
    ):
        raise ContractError("a reading region does not belong to this run and Perlector")
    payload = region.get("payload")
    if not isinstance(payload, dict):
        raise ContractError("a reading region has no payload")
    transform = payload.get("transform")
    _validate_exemplar_transform(transform)
    if set(transform) != {"operation", "source_page_ordinal", "source_page_id", "bounds"}:
        raise ContractError("a reading region carries no complete Exemplar transform")
    if transform != exemplar_crop_transform(
        payload.get("page_ordinal"), payload.get("page_id"), payload.get("union_box_px")
    ):
        raise ContractError(
            "a reading region's transform is not the crop of its own union box from its own page"
        )
    if payload.get("transform_digest") != digest_of(transform):
        raise ContractError("a reading region's transform digest does not bind its transform")
    if payload.get("region_id") != region_id(region.get("subject_id"), transform):
        raise ContractError("a reading region's identities do not bind its recorded transform")
    page_pixels, page_ref = _sealed_source_page(tree, run, transform, "reading region")
    inputs = region.get("inputs")
    crop_ref = {"relative_path": payload.get("image_path"), "sha256": payload.get("image_sha256")}
    if not isinstance(inputs, list) or page_ref not in inputs or crop_ref not in inputs:
        raise ContractError("a reading region does not input its Exemplar page and its own crop")
    width, height = _verify_stored_crop(
        page_pixels, transform["bounds"], payload, tree, "the sealed reading crop"
    )
    return {
        "region_id": payload["region_id"],
        "image_path": payload["image_path"],
        "image_sha256": payload["image_sha256"],
        "verified_dimensions": {"w": width, "h": height},
        "source_page_ordinal": transform["source_page_ordinal"],
        "source_page_id": transform["source_page_id"],
        "transform": dict(transform),
    }


def _verify_crop_is_the_same_image(stored: bytes, derived: bytes) -> None:
    """Decide what a byte difference between a sealed crop and the re-derived one
    actually is, and refuse only the difference that matters.

    ARCHITECTURE's third invariant is about the *image*, not the bytes: a byte
    comparison also asserts the encoder that wrote the crop and the one
    running now emit the same stream, which a pod, a CI matrix or a resumed
    run can break benignly. So the comparison that decides is on the image; a
    crop showing exactly the derived crop passes however it was framed. The
    stored crop must still decode, and must be the picture and nothing else,
    since "the pixels match" says nothing about a stray chunk beside them.
    `image_sha256` is checked before this and still binds the crop's bytes.
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
        not run_holds(run, admission.get("run_id"), DOOR)
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
    parent_bytes = read_verified(
        tree.read_bytes, parent_ref, "the derivative page's submitted master", ContractError
    )
    verify_triage_derivative(
        rendered["render_contract"],
        parent_bytes,
        parent_digest,
        parent,
        blob_ref["sha256"],
    )


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

    One function, not two, since this decides which validation a page gets (a
    derivative is re-derived against its parent frame, an ordinary render
    against the contract), and the Exemplar stage asks the same question. Two
    copies could drift apart without crashing: a page sealed as an ordinary
    render whose pixels nobody re-derived, or a re-derivation demanded of a
    page with no parent.
    """
    return (
        isinstance(render_contract, dict)
        and isinstance(render_contract.get("derivative_page"), dict)
        and render_contract["derivative_page"].get("kind") == SEALED_DERIVATIVE_PAGE_KIND
    )


# Re-rendering a split page from its master takes seconds for a full-size scan, and
# every stage re-checks a page once per act on it. The render is a pure function of
# the master's bytes, the frame index and the part, so its result is kept per process,
# keyed by the master's digest, and the sealed page is compared by digest. Every
# check around it, and every read and hash of the master and page, still runs.
_MAX_REMEMBERED_DERIVATIONS: Final = 4096
_derivations: dict[tuple[str, int, str], tuple[str, dict[str, Any]]] = {}


def _rederived(
    parent_bytes: bytes, parent_digest: str, page_index: int, part: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """The digest and geometry of ``part`` rendered from a master whose bytes have ``parent_digest``."""
    key = (parent_digest, page_index, digest_of(part))
    remembered = _derivations.get(key)
    if remembered is None:
        try:
            expected_bytes, geometry = render_triage_derivative(
                parent_bytes, page_index=page_index, part=part
            )
        except ValueError as error:
            raise ContractError(
                "the sealed derivative page cannot be re-derived from its master"
            ) from error
        remembered = (digest_bytes(expected_bytes), geometry)
        if len(_derivations) >= _MAX_REMEMBERED_DERIVATIONS:
            del _derivations[next(iter(_derivations))]
        _derivations[key] = remembered
    return remembered


def verify_triage_derivative(
    contract: dict[str, Any],
    parent_bytes: bytes,
    parent_digest: str,
    parent: dict[str, Any],
    sealed_digest: str,
) -> None:
    """A split page is valid only when its closed decision re-derives its bytes.

    ``parent_digest`` and ``sealed_digest`` are the digests the caller verified the
    master's bytes and the sealed page's bytes against when it read them.
    """
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
    try:
        validate_row(row)
    except SchemaRefusal as error:
        raise ContractError(
            f"a sealed derivative page carries an invalid triage manifest row ({error})"
        ) from error
    # The row is valid, so its operation order is a declared one.
    if derivative["apply_recipe"] != dict(triage_apply_recipe(row["split"]["operation_order"])):
        raise ContractError("a sealed derivative page changes its recorded raster apply recipe")
    row_digest = row["manifest_row_sha256"]
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
        or derivative["operation_order"] != split["operation_order"]
    ):
        raise ContractError("a sealed derivative page does not match its triage split part")
    part = split["parts"][part_index]
    if derivative["operations"] != triage_operations(part, split["operation_order"]):
        raise ContractError(
            "a sealed derivative page's transform vocabulary does not match its manifest part"
        )
    if parent_digest != parent["sha256"]:
        raise ContractError("a sealed derivative page's master bytes are not its parent frame")
    expected_digest, geometry = _rederived(
        parent_bytes, parent_digest, parent["source_frame_index"], part
    )
    expected_mode_transform = triage_mode_transform(
        split["operation_order"], geometry["source_mode"], geometry["color_mode"]
    )
    expected_render_record = {
        "source_mode": geometry["source_mode"],
        "source_bands": geometry["source_bands"],
        "mode_transform": expected_mode_transform,
        "output": {"codec": "png", "color_mode": geometry["color_mode"]},
        "container_page_index": parent["source_frame_index"],
        "width": geometry["width"],
        "height": geometry["height"],
        "deterministic_encoder": DETERMINISTIC_ENCODER,
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
    if expected_digest != sealed_digest:
        raise ContractError(
            "a sealed derivative page's pixels are not reproducible from its master and apply "
            f"recipe{_renderer_drift(contract)}"
        )


def _renderer_drift(contract: dict[str, Any]) -> str:
    """Name a library difference when one is the likelier cause of a pixel mismatch.

    The byte comparison is the property that refuses a page; the recorded library
    versions are compared against the running host only to word the refusal. A host
    whose imaging libraries render the part differently (an upgrade, or another
    platform's arithmetic) refuses the page, and since that message alone points an
    operator at forgery, it names the versions that differ.
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
        if not isinstance(path, str) or not is_sha256(digest) or path in refs:
            raise ContractError("a sealed Exemplar page has an invalid input reference")
        refs[path] = {"relative_path": path, "sha256": digest}
    return refs
