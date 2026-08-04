"""Checks the immutable pixels handed from Exemplar to later stages.

The Exemplar page is more than an ordinal in a census.  A sealed page binds the
Door admission artifact and the exact content-addressed image blob that every
later crop must use.  Consumers call this helper before acting on those pixels so
a changed, missing, or substituted blob cannot be quietly re-hashed into new
downstream evidence.

This deliberately knows contracts and the run tree, but not a numbered pipeline
module.  Both Designator and Armarium use the same check: the first prevents work
over altered pixels; the latter prevents an export after pixels changed between
stages.
"""

import json
from typing import Any

from common.contracts.canonical import digest_bytes, verify_self_hash
from common.contracts.envelope import validate_envelope, verify_input_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id, page_id, region_id
from common.contracts.stages import DESIGNATOR, DOOR, EXEMPLAR
from common.imaging import dimensions
from common.runtree.store import RunTree


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
    _verify_page_source_facts(payload, source, ordinal)

    source_digest = payload.get("source_sha256")
    if not _is_sha256(source_digest):
        raise ContractError("a sealed Exemplar page has no lowercase pixel sha256")
    expected_page_id = page_id(source_digest, ordinal)
    if page.get("subject_id") != expected_page_id:
        raise ContractError("a sealed Exemplar page identity does not bind its pixels and ordinal")

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
            "a sealed Exemplar page must input exactly its Door admission and pixel blob"
        )
    blob_ref = refs[blob_path]
    if blob_ref != {"relative_path": blob_path, "sha256": source_digest}:
        raise ContractError("a sealed Exemplar page's pixel input is not content-addressed")

    blob = _read_checked(tree, blob_ref, "the sealed Exemplar pixel blob")
    if digest_bytes(blob) != source_digest:  # defensive: `_read_checked` already proves this.
        raise ContractError("the sealed Exemplar pixel blob has an unexpected digest")

    admission_data = _read_checked(tree, refs[admission_path], "the sealed Door admission")
    try:
        admission = validate_envelope(json.loads(admission_data.decode("utf-8")))
    except (SchemaRefusal, UnicodeDecodeError, ValueError, TypeError) as error:
        raise ContractError("the sealed page's Door admission is not a valid artifact") from error
    _verify_admission(admission, run, source, ordinal, blob_ref)


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
    if set(records) != expected_ordinals or set(entries_by_ordinal) != expected_ordinals:
        missing = sorted(expected_ordinals - set(records))
        names = [sources[ordinal].get("relative_path") for ordinal in missing]
        raise ContractError(
            "the Exemplar page outcomes do not reconcile with run.json; lost submitted "
            f"page(s) {names}"
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
    if not isinstance(transform, dict) or set(transform) != {
        "operation",
        "source_page_ordinal",
        "source_page_id",
        "bounds",
    }:
        raise ContractError("a crop region carries no complete Exemplar transform")
    ordinal = transform["source_page_ordinal"]
    source_page_id = transform["source_page_id"]
    bounds = transform["bounds"]
    if (
        transform["operation"] != "crop"
        or not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not isinstance(source_page_id, str)
        or not source_page_id
        or not isinstance(bounds, dict)
        or set(bounds) != {"x", "y", "w", "h"}
        or any(not isinstance(value, int) or isinstance(value, bool) for value in bounds.values())
        or bounds["w"] <= 0
        or bounds["h"] <= 0
    ):
        raise ContractError("a crop region carries an invalid Exemplar transform")
    if payload.get("region_id") != region_id(region.get("subject_id"), transform):
        raise ContractError("a crop region's identities do not bind its recorded transform")
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
    if region.get("inputs") != [{"relative_path": page_path, "sha256": page_digest}]:
        raise ContractError("a crop region does not input the Exemplar page its transform names")
    image_path, image_digest = payload.get("image_path"), payload.get("image_sha256")
    if not isinstance(image_path, str) or not _is_sha256(image_digest):
        raise ContractError("a crop region names no content-addressed crop image")
    crop = _read_checked(
        tree,
        {"relative_path": image_path, "sha256": image_digest},
        "the sealed Designator crop",
    )
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
    }


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
    if admission.get("inputs") != [blob_ref]:
        raise ContractError("a sealed Exemplar page's Door admission has the wrong pixel input")


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
