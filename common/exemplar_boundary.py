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

from common.contracts.canonical import digest_bytes
from common.contracts.envelope import validate_envelope, verify_input_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id, page_id
from common.contracts.stages import DOOR, EXEMPLAR
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
